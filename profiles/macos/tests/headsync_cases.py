#!/usr/bin/env python3.15
"""Exercise a HEAD-sync script using only synthetic formulae and fake commands."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

if len(sys.argv) != 2:
    raise SystemExit("usage: verify-headsync-recovery.py HEADSYNC_SCRIPT")

script = Path(sys.argv[1]).resolve(strict=True)
sha = "0123456789abcdef0123456789abcdef01234567"
ggml = '''class Ggml
  head "https://github.com/ggml-org/ggml.git", branch: "master"
  def install
    system "cmake", "-S", ".", "-B", "build", *args, *std_cmake_args
  end
end
'''
llama = '''class LlamaCpp
  head "https://github.com/ggml-org/llama.cpp.git", branch: "master"
end
'''

brew = r'''#!/usr/bin/python3
import os, pathlib, sys
args=sys.argv[1:]
root=pathlib.Path(os.environ["TEST_ROOT"])
with (root/"events").open("a") as f: f.write("brew " + " ".join(args) + "\n")
if args == ["--repo", "homebrew/core"]:
 print(root/"core")
elif args == ["update"]:
 if os.environ.get("HOMEBREW_NO_AUTO_UPDATE") != "1": print("auto-update guard missing",file=sys.stderr); sys.exit(30)
elif args == ["list", "--formula", "--pinned"]:
 if os.environ.get("TEST_FAIL_LIST") == "pinned": print("simulated pinned list error",file=sys.stderr); sys.exit(36)
 print(os.environ.get("TEST_PINNED", "llvm"))
elif args == ["list", "--formula", "--full-name"]:
 if os.environ.get("TEST_FAIL_LIST") == "installed": print("simulated installed list error",file=sys.stderr); sys.exit(37)
 print(os.environ.get("TEST_INSTALLED", "llvm\ntopgrade\nz3\natuin"))
elif args[:1] == ["upgrade"]:
 if os.environ.get("HOMEBREW_NO_INSTALL_CLEANUP") != "1":
  print("missing cleanup protection", file=sys.stderr); sys.exit(31)
 if os.environ.get("HOMEBREW_NO_AUTO_UPDATE") != "1": print("auto-update guard missing",file=sys.stderr); sys.exit(30)
 if "topgrade" in args: print("topgrade must not reach brew upgrade", file=sys.stderr); sys.exit(32)
 target = args[-1] if args[-1] in ("ggml", "llama.cpp") else "bulk"
 for f in (root/"core/Formula/g/ggml.rb",root/"core/Formula/l/llama.cpp.rb"):
  if os.environ["TEST_SHA"] not in f.read_text(): print("formula revision mismatch",file=sys.stderr); sys.exit(33)
 if os.environ.get("TEST_FAIL") == target: print("injected upgrade failure",file=sys.stderr); sys.exit(34)
elif args[:1] == ["outdated"]:
 eligible={name.rsplit("/",1)[-1]:name for name in args[1:] if not name.startswith("--")}
 outdated=set(os.environ.get("TEST_OUTDATED", "topgrade").splitlines())
 found=[name for short,name in eligible.items() if short in outdated]
 mode=os.environ.get("TEST_OUTDATED_MODE", "normal")
 if mode == "malformed": print("{bad json"); sys.exit(1)
 if mode == "one-empty": print('{"formulae": [], "casks": []}'); sys.exit(1)
 if mode == "operational": print("simulated outdated transport failure",file=sys.stderr); sys.exit(2)
 if "--json=v2" in args:
  entries=[{"name":name.rsplit("/",1)[-1],"pinned":mode == "newly-pinned"} for name in found]
  if mode == "unselected": entries=[{"name":"not-a-candidate","pinned":False}]
  if mode == "duplicate" and entries: entries.append(dict(entries[0]))
  print(__import__("json").dumps({"formulae":entries,"casks":[]}))
 else:
  print("\n".join(found))
 if found: sys.exit(1)
elif args[:1] == ["uses"]:
 print("topgrade\natuin")
elif args == ["linkage", "--test", "rust"]:
 if os.environ.get("TEST_RUST_HEALTH", "broken") == "broken":
  print("Broken dependencies: synthetic LLVM"); sys.exit(1)
elif args == ["--prefix", "rust"]:
 print(root/"rust-prefix")
elif args == ["list", "--versions", "ggml"]:
 print("ggml HEAD-"+os.environ["TEST_SHA"][:7]+(" HEAD-deadbee" if os.environ.get("TEST_EXTRA_HEAD") else ""))
elif args == ["list", "--versions", "llama.cpp"]:
 print("llama.cpp HEAD-"+os.environ["TEST_SHA"][:7]+(" HEAD-deadbee" if os.environ.get("TEST_EXTRA_HEAD") else ""))
elif len(args) == 2 and args[0] == "--prefix" and args[1] in ("ggml", "llama.cpp"):
 print(root/"opt"/args[1])
elif len(args) == 2 and args[0] == "--cellar" and args[1] in ("ggml", "llama.cpp"):
 print(root/"Cellar"/args[1])
elif args == ["missing"]:
 pass
else:
 print("unexpected brew call: " + repr(args),file=sys.stderr); sys.exit(35)
'''
git = r'''#!/usr/bin/python3
import os,pathlib,sys
args=sys.argv[1:]
root=pathlib.Path(os.environ["TEST_ROOT"])
with (root/"events").open("a") as f: f.write("git " + " ".join(args) + "\n")
if len(args)>=4 and args[0] == "-C" and args[2] == "status":
 state=os.environ.get("TEST_GIT_STATUS", "clean")
 if state == "fail": print("simulated git error",file=sys.stderr); sys.exit(41)
 if state == "dirty": print(" M Formula/g/ggml.rb")
elif args[:1] == ["ls-remote"]:
 with (root/"events").open("a") as f:
  f.write("gitconfig count=" + os.environ.get("GIT_CONFIG_COUNT", "") + " key0=" + os.environ.get("GIT_CONFIG_KEY_0", "") + " value0=" + os.environ.get("GIT_CONFIG_VALUE_0", "") + " key1=" + os.environ.get("GIT_CONFIG_KEY_1", "") + " value1=" + os.environ.get("GIT_CONFIG_VALUE_1", "") + "\n")
 print(os.environ["TEST_SHA"]+"\trefs/heads/master")
else:
 print("unexpected git call: "+repr(args),file=sys.stderr); sys.exit(42)
'''
helper = r'''#!/bin/sh
printf '%s\n' 'helper update' >> "$TEST_ROOT/events"
if [ "${TEST_FAIL:-}" = helper ]; then exit 43; fi
'''


class Fixture:
    def __init__(self, root):
        self.root = root
        self.core = root / "core"
        self.state = root / "state/brew-headsync"
        self.home = root / "home"
        self.bin = root / "bin"
        for directory in (self.core / "Formula/g", self.core / "Formula/l", self.state,
                          self.home / ".local/bin", self.bin, root / "opt", root / "rust-prefix/bin",
                          root / "Cellar/ggml" / ("HEAD-" + sha[:7]) / "include",
                          root / "Cellar/llama.cpp" / ("HEAD-" + sha[:7]) / "bin"):
            directory.mkdir(parents=True, exist_ok=True)
        self.original = {self.core / "Formula/g/ggml.rb": ggml,
                         self.core / "Formula/l/llama.cpp.rb": llama}
        for path, content in self.original.items():
            path.write_text(content)
        for name in ("ggml", "llama.cpp"):
            (root / "opt" / name).symlink_to(root / "Cellar" / name / ("HEAD-" + sha[:7]))
            (root / "opt" / name / "INSTALL_RECEIPT.json").write_text(
                json.dumps({"source": {"spec": "head", "scm_revision": sha}})
            )
        (root / "opt/ggml/include/ggml-backend.h").write_text("ggml_backend_sched_set_copy_callback\n")
        (root / "opt/llama.cpp/bin/llama-cli").write_text("#!/bin/sh\necho llama-synthetic\n")
        (root / "opt/llama.cpp/bin/llama-cli").chmod(0o755)
        (root / "rust-prefix/bin/rustc").write_text("#!/bin/sh\necho rustc-fixture\n")
        (root / "rust-prefix/bin/rustc").chmod(0o755)
        for path, content in ((self.bin / "brew", brew), (self.bin / "git", git),
                              (self.bin / "nm", "#!/bin/sh\necho ggml_backend_sched_set_copy_callback\n"),
                              (self.bin / "llama-cli", "#!/bin/sh\necho llama-synthetic\n"),
                              (self.home / ".local/bin/topgrade-rustup-headsync", helper)):
            if content.startswith("#!/usr/bin/python3"):
                content = content.replace("#!/usr/bin/python3", f"#!{sys.executable}", 1)
            path.write_text(content)
            path.chmod(0o755)
        (self.bin / "python3").symlink_to(sys.executable)
        (self.bin / "python3.15").symlink_to(sys.executable)
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.home),
                    "XDG_STATE_HOME": str(root / "state"), "TEST_ROOT": str(root), "TEST_SHA": sha,
                    "TMPDIR": str(root), "LC_ALL": "C",
                    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.pager",
                    "GIT_CONFIG_VALUE_0": "fixture-pager",
                    "TEST_PINNED": "llvm",
                    "TEST_INSTALLED": "llvm\ntopgrade\nz3\natuin"}

    def run(self, **overrides):
        env = self.env | overrides
        return subprocess.run(["/bin/zsh", str(script)], env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)

    def events(self):
        path = self.root / "events"
        return path.read_text().splitlines() if path.exists() else []

    def assert_restored(self):
        for path, content in self.original.items():
            assert path.read_text() == content, path
        assert not (self.state / "active").exists()

    def journal(self, foreign=False, incomplete=False):
        journal = self.state / "active"
        for part in ("original", "patched"):
            (journal / part).mkdir(parents=True)
        for path, content in self.original.items():
            (journal / "original" / path.name).write_text(content)
            (journal / "patched" / path.name).write_text("patched " + path.name + "\n")
        first, second = self.original
        first.write_text("patched " + first.name + "\n")
        if foreign:
            second.write_text("foreign edit\n")
        if incomplete:
            (journal / "patched" / second.name).unlink()


def case(name, callback):
    with tempfile.TemporaryDirectory(prefix="headsync-regression-") as directory:
        fixture = Fixture(Path(directory))
        try:
            callback(fixture)
        except Exception as error:
            print(f"FAIL {name}: {error}")
            return False
    print(f"PASS {name}")
    return True


def clean_guard(f, status):
    result = f.run(TEST_GIT_STATUS=status)
    assert result.returncode != 0, result.stdout
    expected = "homebrew/core is dirty" if status == "dirty" else "cannot inspect homebrew/core status"
    assert expected in result.stdout, result.stdout
    assert "brew update" not in f.events(), f.events()
    f.assert_restored()


def recover(f):
    f.journal()
    result = f.run(TEST_FAIL="helper")
    assert result.returncode != 0, result.stdout
    f.assert_restored()
    assert "brew update" not in f.events(), f.events()


def refusal(f, kind):
    f.journal(foreign=kind == "foreign", incomplete=kind == "incomplete")
    before = {p: p.read_text() for p in f.original}
    result = f.run()
    assert result.returncode != 0, result.stdout
    assert (f.state / "active").is_dir()
    assert all(p.read_text() == contents for p, contents in before.items())
    assert "helper update" not in f.events() and "brew update" not in f.events()


def upgrade_failure(f, name):
    kwargs = {"TEST_FAIL": name}
    if name == "bulk":
        kwargs["TEST_OUTDATED"] = "topgrade\nz3"
    result = f.run(**kwargs)
    assert result.returncode != 0 and "injected upgrade failure" in result.stdout, result.stdout
    f.assert_restored()
    result = f.run()
    assert result.returncode == 0, result.stdout
    f.assert_restored()


def idempotent(f):
    for _ in range(2):
        result = f.run(TEST_OUTDATED="topgrade\nz3")
        assert result.returncode == 0, result.stdout
        assert "HEAD sync transaction: OK" in result.stdout
        f.assert_restored()
    upgrades = [line for line in f.events() if line.startswith("brew upgrade")]
    assert len(upgrades) == 6 and all("topgrade" not in line for line in upgrades), upgrades


def rust_guard(f):
    result = f.run(TEST_OUTDATED="topgrade\natuin")
    assert result.returncode != 0 and "depends on Homebrew Rust" in result.stdout, (result.stdout, f.events())
    f.assert_restored()
    assert not any(line.startswith("brew upgrade") and "atuin" in line for line in f.events())


def rust_healthy(f):
    result = f.run(TEST_OUTDATED="topgrade\natuin", TEST_RUST_HEALTH="healthy")
    assert result.returncode == 0, result.stdout
    assert any(line.startswith("brew upgrade") and line.endswith("atuin") for line in f.events())
    f.assert_restored()


def stale_head_is_not_active(f):
    result = f.run(TEST_EXTRA_HEAD="1")
    assert result.returncode == 0, result.stdout
    f.assert_restored()


def wrong_active_head(f):
    old = f.root / "Cellar/llama.cpp/HEAD-deadbee"
    old.mkdir()
    link = f.root / "opt/llama.cpp"
    link.unlink()
    link.symlink_to(old)
    result = f.run()
    assert result.returncode != 0 and "llama.cpp did not land" in result.stdout, result.stdout
    f.assert_restored()


def dangling_pending(f):
    pending = f.core / "Formula/g/ggml.rb.headsync-pending"
    pending.symlink_to(f.root / "outside-missing")
    result = f.run()
    assert result.returncode != 0 and "unexpected pending file" in result.stdout, result.stdout
    assert pending.is_symlink() and not (f.root / "outside-missing").exists()
    f.assert_restored()


def different_full_revision(f):
    receipt = f.root / "opt/llama.cpp/INSTALL_RECEIPT.json"
    receipt.write_text(json.dumps({"source": {"spec": "head", "scm_revision": sha[:7] + "f" * 33}}))
    result = f.run()
    assert result.returncode != 0 and "full revision" in result.stdout, result.stdout
    f.assert_restored()


def pin_scan_exclusions(f):
    result = f.run(TEST_OUTDATED="topgrade\nz3")
    assert result.returncode == 0, result.stdout
    scan = next(line for line in f.events() if line.startswith("brew outdated "))
    assert "llvm" not in scan and "topgrade" not in scan, scan
    assert "z3" in scan, scan
    f.assert_restored()


def unrelated_pin_excluded(f):
    result = f.run(TEST_OUTDATED="z3\natuin",
                   TEST_PINNED="llvm\nz3",
                   TEST_RUST_HEALTH="healthy")
    assert result.returncode == 0, result.stdout
    scan = next(line for line in f.events() if line.startswith("brew outdated "))
    assert "z3" not in scan and "atuin" in scan, scan
    bulk = [line for line in f.events() if line == "brew upgrade --formula --fetch-HEAD atuin"]
    assert bulk, f.events()
    f.assert_restored()


def outdated_exit_one_nonempty_is_parsed(f):
    result = f.run(TEST_OUTDATED="atuin", TEST_RUST_HEALTH="healthy")
    assert result.returncode == 0, result.stdout
    scan = next(line for line in f.events() if line.startswith("brew outdated "))
    assert "--json=v2" in scan, scan
    assert "brew upgrade --formula --fetch-HEAD atuin" in f.events(), f.events()
    f.assert_restored()


def outdated_failure_propagates(f, mode):
    result = f.run(TEST_OUTDATED="atuin", TEST_OUTDATED_MODE=mode,
                   TEST_RUST_HEALTH="healthy")
    assert result.returncode != 0, result.stdout
    assert "brew uses --installed --include-build --formula rust" not in f.events(), f.events()
    assert "brew upgrade --formula --fetch-HEAD atuin" not in f.events(), f.events()
    f.assert_restored()


def no_candidates_skip_scan(f):
    result = f.run(TEST_INSTALLED="llvm\ntopgrade")
    assert result.returncode == 0, result.stdout
    assert "No eligible installed formulae; skipping outdated scan" in result.stdout
    assert not any(line.startswith("brew outdated ") for line in f.events()), f.events()
    assert not any(line.startswith("brew upgrade ") and line.endswith("bulk") for line in f.events()), f.events()
    f.assert_restored()


def inherited_git_config_preserved(f):
    result = f.run(TEST_OUTDATED="", TEST_RUST_HEALTH="healthy")
    assert result.returncode == 0, result.stdout
    expected = ("gitconfig count=2 key0=core.pager value0=fixture-pager "
                "key1=url.https://github.com/exiftool/exiftool.git.insteadOf "
                "value1=https://git.code.sf.net/p/exiftool/code.git")
    assert expected in f.events(), f.events()
    f.assert_restored()


def malformed_git_config_count_recovers_first(f):
    f.journal()
    result = f.run(GIT_CONFIG_COUNT="not-a-number")
    assert result.returncode != 0 and "malformed inherited GIT_CONFIG_COUNT" in result.stdout, result.stdout
    f.assert_restored()
    assert "brew update" not in f.events() and "helper update" not in f.events(), f.events()


def missing_llvm_pin_stops_before_updates(f):
    result = f.run(TEST_PINNED="")
    assert result.returncode != 0 and "Homebrew llvm must remain pinned" in result.stdout, result.stdout
    assert "brew update" not in f.events() and "helper update" not in f.events(), f.events()
    f.assert_restored()


def list_failure_propagates(f, which):
    result = f.run(TEST_FAIL_LIST=which)
    expected = "unable to enumerate pinned" if which == "pinned" else "unable to enumerate installed"
    assert result.returncode != 0 and expected in result.stdout, result.stdout
    assert "brew update" not in f.events() and "helper update" not in f.events(), f.events()
    f.assert_restored()


def updated_list_failure(f):
    list_failure_propagates(f, "installed")


def pinned_list_failure(f):
    list_failure_propagates(f, "pinned")


cases = [
    ("dirty core stops before brew update", lambda f: clean_guard(f, "dirty")),
    ("failed git status stops before brew update", lambda f: clean_guard(f, "fail")),
    ("recover partially applied journal", recover),
    ("preserve unexpected external edit", lambda f: refusal(f, "foreign")),
    ("preserve incomplete journal", lambda f: refusal(f, "incomplete")),
    ("ggml failure recovers and retries", lambda f: upgrade_failure(f, "ggml")),
    ("llama failure recovers and retries", lambda f: upgrade_failure(f, "llama.cpp")),
    ("bulk failure recovers and retries", lambda f: upgrade_failure(f, "bulk")),
    ("repeat successful update", idempotent),
    ("other Homebrew Rust dependent stops", rust_guard),
    ("healthy Homebrew Rust permits its dependent", rust_healthy),
    ("old HEAD keg does not change active result", stale_head_is_not_active),
    ("mismatched active HEAD is rejected", wrong_active_head),
    ("dangling pending file is preserved before journal", dangling_pending),
    ("different full revision with same short SHA is rejected", different_full_revision),
    ("LLVM and Topgrade excluded from HEAD scan", pin_scan_exclusions),
    ("unrelated pinned formula excluded exactly", unrelated_pin_excluded),
    ("outdated exit 1 with nonempty JSON succeeds", outdated_exit_one_nonempty_is_parsed),
    ("malformed outdated JSON fails closed", lambda f: outdated_failure_propagates(f, "malformed")),
    ("outdated exit 1 with empty JSON fails closed", lambda f: outdated_failure_propagates(f, "one-empty")),
    ("outdated operational error propagates", lambda f: outdated_failure_propagates(f, "operational")),
    ("newly pinned outdated formula fails closed", lambda f: outdated_failure_propagates(f, "newly-pinned")),
    ("unselected outdated formula fails closed", lambda f: outdated_failure_propagates(f, "unselected")),
    ("duplicate outdated formula fails closed", lambda f: outdated_failure_propagates(f, "duplicate")),
    ("empty candidate set skips outdated and bulk upgrade", no_candidates_skip_scan),
    ("inherited Git config retained with exact ExifTool rewrite", inherited_git_config_preserved),
    ("malformed Git config fails after journal recovery", malformed_git_config_count_recovers_first),
    ("missing LLVM pin stops before updates", missing_llvm_pin_stops_before_updates),
    ("installed formula enumeration error propagates", updated_list_failure),
    ("pinned formula enumeration error propagates", pinned_list_failure),
]
results = [case(name, callback) for name, callback in cases]
raise SystemExit(0 if all(results) else 1)
