"""Repository policy tests use temporary projects and mock all update execution."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

PROFILE=Path(__file__).resolve().parents[1]
loader=importlib.machinery.SourceFileLoader("profile_rolling_policy",str(PROFILE/"bin/update-project-deps"))
spec=importlib.util.spec_from_loader(loader.name,loader)
rolling=importlib.util.module_from_spec(spec)
sys.modules[loader.name]=rolling
loader.exec_module(rolling)

class RepositoryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.repo=self.root/"project"
        self.logs=self.root/"logs"
        self.repo.mkdir()
        self.logs.mkdir()
        self.policy=self.repo/".topgrade-dependencies.json"
        (self.repo/"Cargo.toml").write_text('[package]\nname="example"\nversion="0.1.0"\n')

    def run_commands(self,edge=True):
        with mock.patch.object(rolling.shutil,"which",side_effect=lambda name:
                "/tools/cargo-upgrade" if name=="cargo-upgrade" else None), \
             mock.patch.object(rolling,"unsupported",return_value=[]), \
             mock.patch.object(rolling,"git_status",return_value=""), \
             mock.patch.object(rolling,"backup_dependencies"), \
             mock.patch.object(rolling,"execute") as execute:
            rolling.process_repo(self.repo,edge,self.logs,False)
            self.assertEqual(execute.call_count,1)
            return execute.call_args.args[1].commands

    def assert_rejected_before_execution(self,edge=True):
        before={p.name:p.read_bytes() for p in self.logs.iterdir()}
        with mock.patch.object(rolling,"detect_surfaces") as detect, \
             mock.patch.object(rolling,"execute") as execute:
            with self.assertRaises(RuntimeError):
                rolling.process_repo(self.repo,edge,self.logs,False)
            detect.assert_not_called()
            execute.assert_not_called()
        self.assertEqual({p.name:p.read_bytes() for p in self.logs.iterdir()},before)

    def test_unmarked_repo_keeps_existing_edge_commands(self):
        self.assertTrue(rolling.repository_edge(self.repo,True))
        self.assertEqual(self.run_commands(),(
            ("cargo","upgrade","--incompatible","allow","--pinned","allow","--recursive","true"),
            ("cargo","update")))

    def test_opt_in_keeps_repo_and_uses_existing_compatible_path(self):
        self.policy.write_text('{"allow_incompatible": false}\n')
        self.assertFalse(rolling.repository_edge(self.repo,True))
        self.assertEqual(self.run_commands(),(("cargo","update"),))
        self.assertIn(self.policy,rolling.relevant_files(self.repo))
        self.assertIn(".topgrade-dependencies.json",rolling.snapshot(self.repo))

    def test_cli_compatible_cannot_be_escalated(self):
        self.assertFalse(rolling.repository_edge(self.repo,False))
        self.assertEqual(self.run_commands(False),(("cargo","update"),))
        self.policy.write_text('{"allow_incompatible": false}')
        self.assertFalse(rolling.repository_edge(self.repo,False))
        self.assertEqual(self.run_commands(False),(("cargo","update"),))
        self.policy.write_text('{"allow_incompatible": true}')
        self.assert_rejected_before_execution(False)

    def test_invalid_schema_and_json_fail_closed(self):
        cases=(b'{}',b'{"allow_incompatible":true}',b'{"allow_incompatible":0}',
               b'{"allow_incompatible":null}',b'{"allow_incompatible":false,"extra":0}',
               b'{"allow_incompatible":true,"allow_incompatible":false}',
               b'[["allow_incompatible",false]]',b'not JSON',b'\xff',
               b'{"allow_incompatible":false}'+b' '*1024)
        for content in cases:
            with self.subTest(content=content[:70]):
                self.policy.write_bytes(content)
                self.assert_rejected_before_execution()

    def test_symlink_is_rejected_even_when_target_is_valid(self):
        target=self.root/"outside-policy"
        target.write_text('{"allow_incompatible":false}')
        self.policy.symlink_to(target)
        self.assert_rejected_before_execution()
        self.assertEqual(target.read_text(),'{"allow_incompatible":false}')

    def test_fifo_and_directory_are_rejected_without_reading(self):
        os.mkfifo(self.policy)
        self.assert_rejected_before_execution()
        self.policy.unlink()
        self.policy.mkdir()
        self.assert_rejected_before_execution()

if __name__=="__main__":
    unittest.main()
