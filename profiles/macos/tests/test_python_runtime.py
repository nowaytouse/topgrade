"""Runtime selection and minimum-version contracts; no real update commands run."""
import ast
import contextlib
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

PROFILE=Path(__file__).resolve().parents[1]

def load_source(name,path):
    loader=importlib.machinery.SourceFileLoader(name,str(path))
    spec=importlib.util.spec_from_loader(loader.name,loader)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    loader.exec_module(module)
    return module

class PythonRuntimeTests(unittest.TestCase):
    def python_sources(self):
        manifest=json.loads((PROFILE/"manifest.json").read_text())
        operational=[PROFILE/row["source"] for row in manifest["files"]
                     if row["source"].startswith(("bin/","libexec/"))
                     and "python" in (PROFILE/row["source"]).read_text().splitlines()[0]]
        return operational+[PROFILE/"install.py"]+list((PROFILE/"tests").glob("*.py"))

    def test_owned_python_entrypoints_select_versioned_interpreter(self):
        for path in self.python_sources():
            header=path.read_text().splitlines()[0]
            if header.startswith("#!"):
                with self.subTest(path=path.relative_to(PROFILE)):
                    self.assertRegex(header,r"python3\.15(?:$|\s)")

    def test_shell_launchers_cannot_fall_back_to_unversioned_python(self):
        manifest=json.loads((PROFILE/"manifest.json").read_text())
        for row in manifest["files"]:
            if not row["source"].startswith("bin/") or row["source"]=="bin/cli-versions":
                continue
            path=PROFILE/row["source"]
            text=path.read_text()
            if "python" not in text.splitlines()[0]:
                with self.subTest(path=row["source"]):
                    self.assertNotRegex(text,r"\bpython3\s")

    def test_runtime_scripts_have_no_third_party_python_imports(self):
        for path in self.python_sources():
            imports=set()
            for node in ast.walk(ast.parse(path.read_text(),filename=str(path))):
                if isinstance(node,ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node,ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
            with self.subTest(path=path.relative_to(PROFILE)):
                self.assertFalse(imports-sys.stdlib_module_names)

    def test_fork_metadata_requires_python_315_or_newer(self):
        project=tomllib.loads((PROFILE.parents[1]/"pyproject.toml").read_text())["project"]
        match=re.fullmatch(r">=(\d+)\.(\d+)(?:\.\d+)?",project["requires-python"])
        self.assertIsNotNone(match)
        self.assertGreaterEqual(tuple(map(int,match.groups())),(3,15))

    def test_old_interpreter_fails_before_destination_or_payload_access(self):
        installer=load_source("profile_runtime_installer",PROFILE/"install.py")
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)/"not-created"
            message=io.StringIO()
            with mock.patch.object(installer.sys,"version_info",(3,14,99)), \
                 mock.patch.object(installer,"apply") as apply, \
                 mock.patch.object(installer,"plan") as plan, \
                 mock.patch.object(installer,"rollback") as rollback, \
                 contextlib.redirect_stderr(message):
                self.assertEqual(installer.main(["--apply","--home",str(home)]),1)
                apply.assert_not_called()
                plan.assert_not_called()
                rollback.assert_not_called()
            self.assertIn("Python 3.15",message.getvalue())
            self.assertFalse(home.exists())

    def test_owned_helper_children_inherit_running_interpreter(self):
        rolling=load_source("profile_runtime_rolling",PROFILE/"bin/update-project-deps")
        with tempfile.TemporaryDirectory() as folder:
            repo=Path(folder)
            for name,content in (("build.zig.zon",".{}"),("build.gradle",""),("Dockerfile","FROM scratch\n")):
                (repo/name).write_text(content)
            surfaces={surface.name:surface for surface in rolling.detect_surfaces(repo,edge=False)}
            for name in ("Zig packages","Gradle","Docker images"):
                with self.subTest(surface=name):
                    command=surfaces[name].commands[0]
                    self.assertEqual(command[:2],(sys.executable,rolling.EDGE_EXTRAS))

if __name__=="__main__":
    unittest.main()
