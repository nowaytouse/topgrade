#!/usr/bin/env python3.15
"""Exercise Topgrade release switching in a synthetic home directory."""

import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

helper = Path(__file__).resolve().parents[1] / "bin/topgrade-rustup-headsync"
old_sha = "1" * 40
new_sha = "2" * 40


class Fixture:
    def __init__(self, root):
        self.root = root
        self.home = root / "home"
        self.base = self.home / ".local/share/topgrade-rustup-head"
        self.releases = self.base / "releases"
        self.link = self.home / ".local/bin/topgrade"
        self.previous = self.base / "previous-target"
        self.bin = root / "bin"
        for directory in (self.releases, self.link.parent, self.bin):
            directory.mkdir(parents=True)
        git = self.bin / "git"
        git.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$HOME/git-calls"\n[ "${TEST_GIT_FAIL:-0}" = 0 ] || exit 1\nprintf "%s\\trefs/heads/main\\n" "${TEST_GIT_SHA:-' + new_sha + '}"\n')
        git.chmod(0o755)
        mv = self.bin / "mv"
        mv.write_text('''#!/bin/sh
if [ "${TEST_FAIL_SWITCH:-0}" = 1 ] && [ "$1" = "-f" ] &&
   case "$2" in "$HOME/.local/bin/.topgrade-headsync."*/topgrade) true;; *) false;; esac &&
   [ "$3" = "$HOME/.local/bin/topgrade" ]; then
    exit 71
fi
exec /bin/mv "$@"
''')
        mv.chmod(0o755)
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.home),
                    "LC_ALL": "C"}

    def release(self, sha, kind="good"):
        target = self.releases / sha / "bin/topgrade"
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind in ("good", "link-version-fails"):
            target.write_text('''#!/bin/sh
if [ "$0" = "$HOME/.local/bin/topgrade" ] &&
   [ "${TEST_LINK_VERSION_FAIL:-0}" = 1 ] && [ "$1" = "--version" ]; then
    exit 9
fi
case "$1" in --version) echo topgrade-fixture;; --help) exit 0;; *) exit 2;; esac
''')
        else:
            target.write_text("#!/bin/sh\nexit 9\n")
        target.chmod(0o755)
        return target

    def activate(self, target):
        self.link.symlink_to(target)

    def run(self, mode, **extra):
        return subprocess.run(["/bin/bash", str(helper), mode], env=self.env | extra,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)


def normal(f):
    old, new = f.release(old_sha), f.release(new_sha)
    f.activate(old)
    for _ in range(2):
        result = f.run("update")
        assert result.returncode == 0, result.stdout
        assert f.link.readlink() == new
    assert f.previous.read_text().strip() == str(old)
    for expected in (old, new):
        result = f.run("rollback")
        assert result.returncode == 0, result.stdout
        assert f.link.readlink() == expected


def broken_active(f):
    old, current = f.release(old_sha), f.releases / new_sha / "bin/topgrade"
    f.activate(current)
    f.previous.write_text(str(old) + "\n")
    result = f.run("rollback")
    assert result.returncode == 0, result.stdout
    assert f.link.readlink() == old


def broken_active_fallback(f):
    f.activate(f.releases / new_sha / "bin/topgrade")
    result = f.run("rollback")
    assert result.returncode == 0, result.stdout
    assert not f.link.is_symlink()


def broken_previous(f):
    current, previous = f.release(new_sha), f.release(old_sha, "broken")
    f.activate(current)
    f.previous.write_text(str(previous) + "\n")
    result = f.run("rollback")
    assert result.returncode != 0, result.stdout
    assert f.link.readlink() == current and f.previous.read_text().strip() == str(previous)


def failed_switch_restores_previous_record(f):
    active = f.release(old_sha)
    prior_previous = f.release("3" * 40)
    f.release(new_sha)
    f.activate(active)
    f.previous.write_text(str(prior_previous) + "\n")
    result = f.run("update", TEST_FAIL_SWITCH="1")
    assert result.returncode != 0, result.stdout
    assert f.link.readlink() == active
    assert f.previous.read_text() == str(prior_previous) + "\n"
    assert not list(f.base.glob(".previous-target.snapshot.*"))


def failed_link_version_restores_missing_previous(f):
    active = f.release(old_sha)
    f.release(new_sha)
    f.activate(active)
    result = f.run("update", TEST_LINK_VERSION_FAIL="1")
    assert result.returncode != 0 and "verification failed" in result.stdout, result.stdout
    assert f.link.readlink() == active
    assert not f.previous.exists()
    assert not list(f.base.glob(".previous-target.snapshot.*"))


def invalid_active_revision_is_rejected(f):
    active = f.releases / "../outside" / "bin/topgrade"
    f.activate(active)
    result = f.run("update")
    assert result.returncode != 0 and "refusing to replace unmanaged" in result.stdout, result.stdout
    assert f.link.readlink() == active
    assert not (f.home / "git-calls").exists()


def symlinked_release_component_is_rejected(f):
    outside = f.root / "outside-release"
    binary = outside / "bin/topgrade"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    release = f.releases / old_sha
    release.symlink_to(outside)
    target = release / "bin/topgrade"
    f.activate(target)
    result = f.run("update")
    assert result.returncode != 0 and "refusing to replace unmanaged" in result.stdout, result.stdout
    assert f.link.readlink() == target


def invalid_previous_revision_is_rejected(f):
    active = f.release(new_sha)
    f.activate(active)
    invalid = f.releases / "../outside" / "bin/topgrade"
    f.previous.write_text(str(invalid) + "\n")
    result = f.run("rollback")
    assert result.returncode != 0 and "invalid rollback target" in result.stdout, result.stdout
    assert f.link.readlink() == active
    assert f.previous.read_text() == str(invalid) + "\n"


def cached_symlink_release_is_rejected(f, component, mode):
    active = f.release(old_sha)
    f.activate(active)
    external = f.root / "external-release/bin/topgrade"
    external.parent.mkdir(parents=True)
    external.write_text('#!/bin/sh\nprintf executed >> "$HOME/external-executed"\nexit 0\n')
    external.chmod(0o755)

    release = f.releases / new_sha
    if component == "release":
        release.symlink_to(external.parent.parent)
    elif component == "bin":
        release.mkdir()
        (release / "bin").symlink_to(external.parent)
    else:
        (release / "bin").mkdir(parents=True)
        (release / "bin/topgrade").symlink_to(external)

    result = f.run(mode)
    assert result.returncode != 0 and "refusing unmanaged cached Topgrade release" in result.stdout, result.stdout
    assert f.link.readlink() == active
    assert not (f.home / "external-executed").exists()


def git_failure(f):
    old = f.release(old_sha)
    f.activate(old)
    result = f.run("update", TEST_GIT_FAIL="1")
    assert result.returncode != 0, result.stdout
    assert f.link.readlink() == old


def lock_contention(f):
    old = f.release(old_sha)
    f.activate(old)
    lock = f.base / "lock"
    with lock.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = f.run("rollback")
        assert result.returncode != 0 and "lock" in result.stdout.lower(), result.stdout
        assert f.link.readlink() == old


def fork_dry_run(f):
    before = set(f.home.rglob("*"))
    result = f.run("--dry-run")
    assert result.returncode == 0 and "would build" in result.stdout, result.stdout
    assert (f.home / "git-calls").read_text().splitlines() == [
        "ls-remote", "--exit-code", "https://github.com/nowaytouse/topgrade.git", "refs/heads/main"]
    assert set(f.home.rglob("*")) - before == {f.home / "git-calls"}
    assert not f.link.exists() and not f.previous.exists()


def invalid_remote_sha(f):
    active = f.release(old_sha)
    f.activate(active)
    result = f.run("update", TEST_GIT_SHA="not-a-full-sha")
    assert result.returncode != 0 and "invalid Topgrade main revision" in result.stdout, result.stdout
    assert f.link.readlink() == active and not f.previous.exists()


def dry_run_current_noop(f):
    current = f.release(new_sha)
    f.activate(current)
    result = f.run("--dry-run")
    assert result.returncode == 0 and "already on main" in result.stdout, result.stdout
    assert f.link.readlink() == current and not f.previous.exists()
    assert not (f.base / "lock").exists()


CASES = [
    ("repeat update and rollback", normal),
    ("rollback from missing current release", broken_active),
    ("fallback from missing first release", broken_active_fallback),
    ("refuse broken previous release", broken_previous),
    ("switch failure restores previous record", failed_switch_restores_previous_record),
    ("post-switch version failure restores absent record", failed_link_version_restores_missing_previous),
    ("reject non-SHA and traversal active target", invalid_active_revision_is_rejected),
    ("reject symlinked release component", symlinked_release_component_is_rejected),
    ("reject traversal rollback target", invalid_previous_revision_is_rejected),
    ("cached release symlink is rejected before update execution",
     lambda f: cached_symlink_release_is_rejected(f, "release", "update")),
    ("cached bin symlink is rejected before dry-run execution",
     lambda f: cached_symlink_release_is_rejected(f, "bin", "--dry-run")),
    ("cached executable symlink is rejected before update execution",
     lambda f: cached_symlink_release_is_rejected(f, "executable", "update")),
    ("remote resolution failure preserves active release", git_failure),
    ("concurrent mutation fails closed", lock_contention),
    ("fork remote and dry-run isolation", fork_dry_run),
    ("invalid remote revision preserves release", invalid_remote_sha),
    ("dry-run current release is a no-op", dry_run_current_noop),
]


@unittest.skipUnless(sys.platform == "darwin", "macOS lockf updater")
class SelfUpdateTests(unittest.TestCase):
    def test_regressions(self):
        for name, callback in CASES:
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix="topgrade-rollback-regression-") as directory:
                callback(Fixture(Path(directory)))


if __name__ == "__main__":
    unittest.main()
