"""Synthetic HOME tests; never run production updaters or touch the real profile."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

PROFILE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mac_profile_install", PROFILE/"install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/"profile"
        self.home = Path(self.temp.name)/"home"
        self.root.mkdir()
        self.home.mkdir()
        self.patch = mock.patch.object(installer, "ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.make_manifest()

    def make_manifest(self, names=("cli-versions",), before=b"original"):
        rows = []
        for name in names:
            path = self.root/"bin"/name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(("new "+name).encode())
            rows.append({"source":"bin/"+name, "destination":".local/bin/"+name,
                         "sha256":installer.digest(path.read_bytes()), "mode":0o755,
                         "adoptSha256":installer.digest(before)})
        (self.root/"manifest.json").write_text(json.dumps({"version":1, "files":rows}))

    def original(self, name="cli-versions", value=b"original"):
        path = self.home/".local/bin"/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        path.chmod(0o640)
        return path

    def test_plan_has_no_writes(self):
        changes, unchanged = installer.plan(self.home)
        self.assertEqual((len(changes),len(unchanged)),(1,0))
        self.assertEqual(list(self.home.iterdir()),[])

    def test_apply_verify_idempotence_and_exact_rollback(self):
        path = self.original()
        before_time = path.stat().st_mtime_ns
        result = installer.apply(self.home)
        self.assertEqual(path.read_bytes(),b"new cli-versions")
        self.assertEqual(installer.plan(self.home)[0],[])
        installed_time = path.stat().st_mtime_ns
        self.assertEqual(installer.apply(self.home)["status"],"already-current")
        self.assertEqual(path.stat().st_mtime_ns,installed_time)
        installer.rollback(self.home,result["transaction"])
        self.assertEqual(path.read_bytes(),b"original")
        self.assertEqual(path.stat().st_mode & 0o777,0o640)
        self.assertEqual(path.stat().st_mtime_ns,before_time)
        self.assertIsNone(installer.installed(self.home))

    def test_new_files_rollback_preserves_unrelated(self):
        unrelated = self.original("unrelated",b"valuable")
        result = installer.apply(self.home)
        installer.rollback(self.home,result["transaction"])
        self.assertFalse((self.home/".local/bin/cli-versions").exists())
        self.assertEqual(unrelated.read_bytes(),b"valuable")

    def test_drift_and_symlink_fail_before_replacement(self):
        path = self.original(value=b"user edited")
        with self.assertRaisesRegex(RuntimeError,"Local drift"):
            installer.plan(self.home)
        self.assertEqual(path.read_bytes(),b"user edited")
        path.unlink()
        outside = Path(self.temp.name)/"outside"
        outside.write_bytes(b"do not touch")
        path.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError,"symlink"):
            installer.plan(self.home)
        self.assertEqual(outside.read_bytes(),b"do not touch")

    def test_modified_source_or_path_escape_is_rejected(self):
        (self.root/"bin/cli-versions").write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError,"checksum"):
            installer.plan(self.home)
        for relative in ("../outside","/absolute"):
            with self.subTest(relative=relative), self.assertRaises(RuntimeError):
                installer.safe_path(self.home,relative)

    def test_mid_apply_error_rolls_back_all_files(self):
        self.make_manifest(("cli-versions","proxy-auto-run"))
        a=self.original()
        b=self.original("proxy-auto-run")
        atomic=installer.atomic_write
        failed=False
        def fail_once(path,*args,**kwargs):
            nonlocal failed
            if path==b and not failed:
                failed=True
                raise OSError("simulated disk failure")
            return atomic(path,*args,**kwargs)
        with mock.patch.object(installer,"atomic_write",side_effect=fail_once):
            with self.assertRaisesRegex(OSError,"simulated"):
                installer.apply(self.home)
        self.assertEqual((a.read_bytes(),b.read_bytes()),(b"original",b"original"))
        journals=list((self.home/".local/state/topgrade-macos-profile/transactions").glob("*/journal.json"))
        self.assertEqual(json.loads(journals[0].read_text())["status"],"rolled-back")

    def test_pending_transaction_blocks_and_explicit_recovery_works(self):
        self.original()
        result=installer.apply(self.home)
        state=self.home/".local/state/topgrade-macos-profile"
        journal=state/"transactions"/result["transaction"]/"journal.json"
        value=json.loads(journal.read_text())
        value["status"]="prepared"
        journal.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError,"Incomplete transaction"):
            installer.plan(self.home)
        with self.assertRaisesRegex(RuntimeError,"Incomplete transaction"):
            installer.apply(self.home)
        installer.rollback(self.home,result["transaction"])
        self.assertEqual((self.home/".local/bin/cli-versions").read_bytes(),b"original")

    def test_rollback_refuses_newer_user_change(self):
        self.original()
        result=installer.apply(self.home)
        path=self.home/".local/bin/cli-versions"
        path.write_bytes(b"new user edit")
        with self.assertRaisesRegex(RuntimeError,"local drift"):
            installer.rollback(self.home,result["transaction"])
        self.assertEqual(path.read_bytes(),b"new user edit")

    def test_mode_only_change_restores_original_metadata(self):
        self.original(value=b"new cli-versions")
        manifest=json.loads((self.root/"manifest.json").read_text())
        manifest["files"][0]["adoptSha256"]=installer.digest(b"new cli-versions")
        (self.root/"manifest.json").write_text(json.dumps(manifest))
        result=installer.apply(self.home)
        installer.rollback(self.home,result["transaction"])
        self.assertEqual((self.home/".local/bin/cli-versions").stat().st_mode & 0o777,0o640)

class NpmReferenceTests(unittest.TestCase):
    def test_exact_bundled_patch_trees_and_tamper_rejection(self):
        path=PROFILE/"bin/npm-bundled-security-guard.py"
        module=ast.parse(path.read_text())
        functions=[node for node in module.body if isinstance(node,ast.FunctionDef)
                   and node.name in ("manifest","verify_patch_leaves")]
        namespace={"hashlib":hashlib}
        exec(compile(ast.Module(body=functions,type_ignores=[]),str(path),"exec"),namespace)
        verify=namespace["verify_patch_leaves"]
        data=PROFILE/"share/npm-security"
        after=json.loads((data/"leaf-candidate-after.json").read_text())
        verify(data/"node_modules",after)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in ("undici","ip-address"):
                (root/name).mkdir()
                (root/name/"a").write_bytes(b"expected")
            reference={f"node_modules/{name}/a":{"sha256":installer.digest(b"expected")}
                       for name in ("undici","ip-address")}
            verify(root,reference)
            (root/"undici/a").write_bytes(b"corrupt")
            with self.assertRaises(RuntimeError):
                verify(root,reference)

    def test_vendor_data_is_pruned_by_real_dependency_discovery(self):
        import os
        path=PROFILE/"bin/update-project-deps"
        module=ast.parse(path.read_text())
        definitions=[node for node in module.body if (
            isinstance(node,ast.FunctionDef) and node.name in ("walk_files","topmost_dirs","dirs_with")
        ) or (
            isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=="SKIP_DIRS" for t in node.targets)
        )]
        namespace={"os":os,"Path":Path}
        exec(compile(ast.Module(body=definitions,type_ignores=[]),str(path),"exec"),namespace)
        vendor=PROFILE/"share/npm-security/node_modules"
        expected={vendor/name/"package.json" for name in ("undici","ip-address")}
        self.assertTrue(all(p.is_file() for p in expected))
        self.assertFalse((PROFILE/"share/npm-security/patches").exists())
        walked=set(namespace["walk_files"](PROFILE))
        self.assertTrue(expected.isdisjoint(walked))
        projects=namespace["dirs_with"](PROFILE,{"package.json"})
        self.assertFalse(any(vendor==p or vendor in p.parents for p in projects))
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/"package.json").write_text("{}")
            nested=root/"node_modules/undici"
            nested.mkdir(parents=True)
            (nested/"package.json").write_text("{}")
            self.assertEqual(namespace["dirs_with"](root,{"package.json"}),[root.resolve()])

    def test_historical_vendor_paths_remain_valid_for_rollback(self):
        for prefix in ("patches","node_modules"):
            relative=f"share/npm-security/{prefix}/undici/package.json"
            self.assertEqual(installer.mapped_destination(relative),
                f".local/share/topgrade-macos-profile/npm-security/{prefix}/undici/package.json")

if __name__=="__main__":
    unittest.main()
