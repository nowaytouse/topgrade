import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "bin/topgrade-helm-verified"
loader = importlib.machinery.SourceFileLoader("helm_verified", str(PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)


class HelmVerifiedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.index = self.cache / "bitnami-index.yaml"
        self.index.write_bytes(b"apiVersion: v1\nentries: {}\n")
        self.config = self.root / "repositories.yaml"
        self.config.write_text("repositories: []")
        self.body = self.index.read_bytes()
        self.curl_rc = 0
        self.final_url = "https://repo.broadcom.com/bitnami-files/index.yaml"
        self.native_rc = 0
        self.native_text = "Update Complete.\n"
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, args):
        self.calls.append(args)
        if args[0] == "curl":
            if self.curl_rc == 0:
                Path(args[args.index("--output") + 1]).write_bytes(self.body)
            return SimpleNamespace(returncode=self.curl_rc, stdout="200\n" + self.final_url, stderr="")
        if args[1:3] == ["repo", "list"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"name": "bitnami", "url": "https://charts.bitnami.com/bitnami"}]), stderr="")
        if args[1:3] == ["env", "HELM_REPOSITORY_CACHE"]:
            return SimpleNamespace(returncode=0, stdout=str(self.cache), stderr="")
        if args[1:3] == ["env", "HELM_REPOSITORY_CONFIG"]:
            return SimpleNamespace(returncode=0, stdout=str(self.config), stderr="")
        return SimpleNamespace(returncode=self.native_rc, stdout=self.native_text, stderr="")

    def run_main(self):
        with patch.object(mod, "invoke", self.invoke), patch.object(mod.shutil, "which", lambda name: name), patch.object(mod.sys, "argv", [str(PATH)]):
            return mod.main()

    def native_calls(self):
        return [args for args in self.calls if args[1:3] == ["repo", "update"]]

    def test_authoritative_identical_body_verifies_without_cache_write(self):
        before = self.index.stat().st_mtime_ns
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.native_calls(), [])
        self.assertEqual(self.index.stat().st_mtime_ns, before)
        self.assertEqual(self.index.read_bytes(), self.body)

    def test_changed_body_delegates_and_preserves_native_failure(self):
        old = self.index.read_bytes()
        self.body += b"generated: later\n"
        self.native_rc = 7
        self.assertEqual(self.run_main(), 7)
        self.assertEqual(self.native_calls(), [["helm", "repo", "update", "bitnami"]])
        self.assertEqual(self.index.read_bytes(), old)

    def test_transfer_failure_delegates(self):
        self.curl_rc = 18
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.native_calls()), 1)

    def test_zero_exit_with_native_error_is_failure(self):
        self.body += b"changed"
        self.native_text = 'Unable to get an update from the "bitnami" chart repository\n'
        self.assertEqual(self.run_main(), 1)

    def test_untrusted_redirect_fails_closed(self):
        self.final_url = "https://example.invalid/index.yaml"
        with self.assertRaises(ValueError):
            self.run_main()
        self.assertFalse(self.native_calls())

    def test_linked_cache_fails_closed(self):
        other = self.root / "other"
        self.index.rename(other)
        self.index.symlink_to(other)
        with self.assertRaises(OSError):
            self.run_main()
        self.assertFalse(self.native_calls())

    def test_missing_cache_uses_native(self):
        self.index.unlink()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.native_calls()), 1)


if __name__ == "__main__":
    unittest.main()
