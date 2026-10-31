#!/usr/bin/env python3.15
"""Synchronize installed plugin87 skills with a complete, commit-pinned kit.

Ordinary failures are rolled back, but abrupt process termination between the
current-link switch and all skill writes is not crash-atomic. Backups remain
available for manual restoration after such an interruption.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


SOURCE = "plugin87/ux-ui-agent-skills"
SOURCE_URL = "https://github.com/plugin87/ux-ui-agent-skills.git"
NPM = "/opt/homebrew/bin/npm"
SKILL_PATH_RE = re.compile(r"\.claude/skills/([a-z0-9]+(?:-[a-z0-9]+)*)/SKILL\.md\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
PREFIX = "${CLAUDE_SKILL_DIR}/../../../"
CORE_PATHS = (
    "tokens",
    ".claude/rules",
    ".claude/rules/accessibility.md",
    ".claude/rules/brand-and-operations.md",
    ".claude/rules/components.md",
    ".claude/rules/frameworks.md",
    ".claude/rules/review-and-research.md",
    ".claude/rules/tokens-and-color.md",
    ".claude/rules/typography-and-spacing.md",
    "workflows/redesign-audit.md",
    "frameworks",
    "taste",
    "taste/design-taste.md",
    "components",
    "scripts/gate.mjs",
    "scripts/accuracy_report.mjs",
)


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def lockfile(self) -> Path:
        return self.home / ".agents/.skill-lock.json"

    @property
    def skills(self) -> Path:
        return self.home / ".agents/skills"

    @property
    def root(self) -> Path:
        return self.home / ".local/share/design-skill-kit"

    @property
    def source(self) -> Path:
        return self.home / ".local/state/design-skill-kit/source"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed ({p.returncode}): {p.stderr.strip()}")
    return p.stdout.strip()


def read_lock(paths: Paths) -> dict[str, dict[str, str]]:
    try:
        lock = json.loads(paths.lockfile.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"cannot read skill lock {paths.lockfile}: {e}") from e
    if not isinstance(lock, dict) or not isinstance(lock.get("skills"), dict):
        raise RuntimeError("skill lock has invalid schema")
    selected: dict[str, dict[str, str]] = {}
    for name, item in lock["skills"].items():
        if not isinstance(item, dict) or item.get("source") != SOURCE:
            continue
        if item.get("sourceType") != "github":
            raise RuntimeError(f"{name}: unexpected sourceType")
        m = SKILL_PATH_RE.fullmatch(str(item.get("skillPath", "")))
        if not m or m.group(1) != name or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise RuntimeError(f"{name}: unsafe or mismatched skillPath")
        tree_hash = item.get("skillFolderHash")
        if not isinstance(tree_hash, str) or not SHA_RE.fullmatch(tree_hash):
            raise RuntimeError(f"{name}: invalid skillFolderHash")
        selected[name] = {"path": item["skillPath"], "tree": tree_hash}
    if not selected:
        raise RuntimeError(f"skill lock contains no {SOURCE} skills")
    return selected


def git_tree_hash(repo: Path, commit: str, rel: str) -> str:
    return run_git(repo, "rev-parse", f"{commit}:{rel}")


def git_file(repo: Path, commit: str, rel: str) -> bytes:
    p = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{rel}"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError(f"missing source file {rel} at {commit}: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def validate_lock_against_tree(repo: Path, commit: str, skills: dict[str, dict[str, str]]) -> dict[str, bytes]:
    originals: dict[str, bytes] = {}
    for name, item in skills.items():
        actual = git_tree_hash(repo, commit, f".claude/skills/{name}")
        if actual != item["tree"]:
            raise RuntimeError(f"{name}: lock tree hash {item['tree']} != source {actual}")
        originals[name] = git_file(repo, commit, item["path"])
    return originals


def validate_global_files(paths: Paths, skills: dict[str, dict[str, str]], originals: dict[str, bytes], receipt_dir: Path) -> dict[str, tuple[bytes, bytes | None]]:
    current: dict[str, tuple[bytes, bytes | None]] = {}
    for name in skills:
        folder = paths.skills / name
        target = folder / "SKILL.md"
        if folder.is_symlink() or not folder.is_dir() or target.is_symlink() or not target.is_file():
            raise RuntimeError(f"{name}: installed skill path missing, linked, or not a regular file")
        data = target.read_bytes()
        receipt_path = receipt_dir / f"{name}.json"
        if receipt_path.is_symlink() or (os.path.lexists(receipt_path) and not receipt_path.is_file()):
            raise RuntimeError(f"{name}: receipt path is linked or not a regular file")
        receipt_bytes = receipt_path.read_bytes() if receipt_path.is_file() else None
        known = data == originals[name]
        if receipt_bytes is not None:
            try:
                receipt = json.loads(receipt_bytes)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"{name}: invalid sync receipt: {e}") from e
            known = known or (
                isinstance(receipt, dict)
                and receipt.get("name") == name
                and receipt.get("renderedSha256") == sha256(data)
                and isinstance(receipt.get("sourceTreeHash"), str)
                and SHA_RE.fullmatch(receipt["sourceTreeHash"])
            )
        if not known:
            raise RuntimeError(f"{name}: local SKILL.md differs from source and this helper's prior rendering; preserved")
        current[name] = (data, receipt_bytes)
    return current


def ensure_core(root: Path) -> None:
    missing = [p for p in CORE_PATHS if not (root / p).exists()]
    if missing:
        raise RuntimeError(f"kit snapshot lacks required resources: {', '.join(missing)}")


def validate_release(root: Path, commit: str) -> dict:
    ensure_core(root)
    marker = root / ".design-skill-kit.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"invalid release manifest at {marker}: {e}") from e
    if not isinstance(data, dict) or set(data) != {"source", "commit", "skillTreeHashes", "trackedFiles"} or data.get("commit") != commit or data.get("source") != SOURCE:
        raise RuntimeError(f"release manifest does not identify {SOURCE}@{commit}")
    skill_hashes = data.get("skillTreeHashes")
    tracked = data.get("trackedFiles")
    if not isinstance(skill_hashes, dict) or not skill_hashes or not isinstance(tracked, dict) or not tracked:
        raise RuntimeError("release manifest has empty or invalid skill/file hash maps")
    for name, digest in skill_hashes.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or not isinstance(digest, str) or not SHA_RE.fullmatch(digest):
            raise RuntimeError(f"release manifest has invalid skill hash entry: {name!r}")
    for rel, digest in tracked.items():
        if not isinstance(rel, str):
            raise RuntimeError(f"release manifest has non-string file path: {rel!r}")
        path = PurePosixPath(rel)
        if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in rel or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise RuntimeError(f"release manifest has unsafe or invalid file entry: {rel!r}")
        target = root.joinpath(*path.parts)
        components = [root.joinpath(*path.parts[:i]) for i in range(1, len(path.parts))]
        if root.is_symlink() or any(parent.is_symlink() for parent in components):
            raise RuntimeError(f"release contains symlinked path component: {rel}")
        if target.is_symlink() or not target.is_file() or sha256_file(target) != digest:
            raise RuntimeError(f"release file missing, non-regular, or changed: {rel}")
    validate_dependencies(root)
    return data


def locked_dependencies(root: Path) -> dict[str, str]:
    try:
        manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
        required = lock["packages"][""].get("devDependencies", {})
        packages = lock["packages"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
        raise RuntimeError(f"kit package-lock has invalid or missing dependency metadata: {e}") from e
    manifest_required = manifest.get("devDependencies", {})
    if not isinstance(manifest_required, dict) or manifest_required != required:
        raise RuntimeError("package.json devDependencies do not match package-lock root metadata")
    result = {}
    for name in required:
        package = packages.get(f"node_modules/{name}")
        if not isinstance(package, dict) or not isinstance(package.get("version"), str):
            raise RuntimeError(f"package-lock has no exact version for direct dependency {name}")
        result[name] = package["version"]
    return result


def validate_dependencies(root: Path) -> None:
    required = locked_dependencies(root)
    missing_or_wrong = []
    for name, version in required.items():
        try:
            installed = json.loads((root / "node_modules" / name / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            missing_or_wrong.append(f"{name} (missing)")
            continue
        if installed.get("version") != version:
            missing_or_wrong.append(f"{name} (expected {version}, found {installed.get('version')})")
    if missing_or_wrong:
        raise RuntimeError("release dependencies are incomplete: " + ", ".join(missing_or_wrong))


def install_dependencies(root: Path) -> None:
    p = subprocess.run([NPM, "ci", "--include=dev", "--ignore-scripts"], cwd=root,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode:
        raise RuntimeError(f"npm ci failed ({p.returncode}); stdout:\n{p.stdout}\nstderr:\n{p.stderr}")
    if p.stdout:
        print(p.stdout, end="" if p.stdout.endswith("\n") else "\n")
    if p.stderr:
        print(p.stderr, file=sys.stderr, end="" if p.stderr.endswith("\n") else "\n")
    validate_dependencies(root)


def export_release(repo: Path, commit: str, root: Path, tree_hashes: dict[str, str]) -> Path:
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / commit
    if release.is_symlink():
        raise RuntimeError(f"refusing symlink release path: {release}")
    if release.exists():
        manifest = validate_release(release, commit)
        if manifest.get("skillTreeHashes") != tree_hashes:
            raise RuntimeError(f"existing release manifest has unexpected skill hashes: {release}")
        return release
    staging = Path(tempfile.mkdtemp(prefix=f".{commit}.staging-", dir=releases))
    archive_path = staging.parent / f".{commit}.{os.getpid()}.tar"
    try:
        with archive_path.open("wb") as out:
            p = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", "--prefix=ux-ui-agent-skills/", commit], stdout=out, stderr=subprocess.PIPE)
        if p.returncode:
            raise RuntimeError(f"git archive failed: {p.stderr.decode(errors='replace').strip()}")
        seen: set[str] = set()
        tracked_files: dict[str, str] = {}
        with tarfile.open(archive_path, "r:") as tar:
            for member in tar:
                raw = member.name
                rel = PurePosixPath(raw)
                if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                    raise RuntimeError(f"unsafe archive member: {raw!r}")
                if rel.parts[0] != "ux-ui-agent-skills":
                    raise RuntimeError(f"unexpected archive prefix: {raw!r}")
                sub = PurePosixPath(*rel.parts[1:])
                if not sub.parts:
                    continue
                name = sub.as_posix()
                if name in seen:
                    raise RuntimeError(f"duplicate archive member: {name}")
                seen.add(name)
                dest = staging.joinpath(*sub.parts)
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise RuntimeError(f"archive contains non-regular entry: {raw!r}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise RuntimeError(f"cannot read archive member: {raw!r}")
                with source, dest.open("xb") as out:
                    shutil.copyfileobj(source, out)
                dest.chmod(0o755 if member.mode & 0o111 else 0o644)
                tracked_files[name] = sha256_file(dest)
        ensure_core(staging)
        install_dependencies(staging)
        manifest = {"source": SOURCE, "commit": commit, "skillTreeHashes": tree_hashes, "trackedFiles": tracked_files}
        (staging / ".design-skill-kit.json").write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
        validate_release(staging, commit)
        os.rename(staging, release)
        return release
    finally:
        with contextlib.suppress(FileNotFoundError):
            archive_path.unlink()
        if staging.exists():
            shutil.rmtree(staging)


def source_commit(source: Path) -> str:
    cloned = False
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.run(["git", "clone", "--no-checkout", SOURCE_URL, str(source)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode:
            raise RuntimeError(f"cannot clone {SOURCE_URL}: {p.stderr.decode(errors='replace').strip()}")
        cloned = True
    if run_git(source, "remote", "get-url", "origin") != SOURCE_URL:
        raise RuntimeError(f"source cache origin is not the fixed official URL: {source}")
    if cloned:
        return run_git(source, "rev-parse", "refs/remotes/origin/main^{commit}")
    p = subprocess.run(["git", "-C", str(source), "fetch", "origin", "main"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError(f"cannot fetch official main: {p.stderr.decode(errors='replace').strip()}")
    return run_git(source, "rev-parse", "FETCH_HEAD^{commit}")


def read_current(root: Path) -> str | None:
    cur = root / "current"
    if not os.path.lexists(cur):
        return None
    if not cur.is_symlink():
        raise RuntimeError(f"refusing to replace non-symlink current path: {cur}")
    target = os.readlink(cur)
    m = re.fullmatch(r"releases/([0-9a-f]{40})", target)
    if not m:
        raise RuntimeError(f"current symlink has unexpected target: {target}")
    validate_release(root / target, m.group(1))
    return target


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        temp.chmod(mode)
        os.replace(temp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def atomic_current(root: Path, target: str | None) -> None:
    cur = root / "current"
    if target is None:
        with contextlib.suppress(FileNotFoundError):
            cur.unlink()
        return
    temp = root / f".current.{os.getpid()}.{time.time_ns()}"
    os.symlink(target, temp)
    try:
        os.replace(temp, cur)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def acquire_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    f = (root / ".sync.lock").open("a+b")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        f.close()
        raise RuntimeError("another design-skill-kit sync is already running") from e
    return f


def render_all(skills: dict[str, dict[str, str]], originals: dict[str, bytes], current: dict[str, tuple[bytes, bytes | None]], root: Path, commit: str):
    rendered: dict[str, bytes] = {}
    receipts: dict[str, bytes] = {}
    prefix = PREFIX.encode()
    replacement = (str(root / "current") + "/").encode()
    for name, item in skills.items():
        original = originals[name]
        # Re-render the source bytes, never transform an older rendering.
        new = original.replace(prefix, replacement)
        if PREFIX.encode() in new:
            raise RuntimeError(f"{name}: unresolved kit-relative path marker")
        rendered[name] = new
        receipts[name] = (json.dumps({
            "version": 1, "source": SOURCE, "name": name, "commit": commit,
            "sourceTreeHash": item["tree"], "sourceSha256": sha256(original),
            "renderedSha256": sha256(new),
        }, sort_keys=True) + "\n").encode()
    return rendered, receipts


def check(paths: Paths) -> None:
    skills = read_lock(paths)
    current_target = read_current(paths.root)
    if current_target is None:
        raise RuntimeError("complete kit current release is not installed")
    release = paths.root / current_target
    manifest = validate_release(release, current_target.split("/")[-1])
    for name, item in skills.items():
        if manifest.get("skillTreeHashes", {}).get(name) != item["tree"]:
            raise RuntimeError(f"{name}: current kit does not match the skill lock; run sync")
    receipt_dir = paths.root / "receipts"
    originals = {name: (release / item["path"]).read_bytes() for name, item in skills.items()}
    current = validate_global_files(paths, skills, originals, receipt_dir)
    expected, _ = render_all(skills, originals, current, paths.root, current_target.split("/")[-1])
    for name, data in expected.items():
        if current[name][0] != data:
            raise RuntimeError(f"{name}: global SKILL.md was restored or differs from the synchronized rendering; run sync")
        try:
            receipt = json.loads(current[name][1] or b"null")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{name}: invalid sync receipt: {e}") from e
        if not isinstance(receipt, dict) or receipt.get("renderedSha256") != sha256(data) or receipt.get("sourceTreeHash") != skills[name]["tree"]:
            raise RuntimeError(f"{name}: receipt does not match synchronized skill; run sync")
    print(f"OK: {len(skills)} skills resolve against kit {current_target.split('/')[-1]}")


def sync(paths: Paths) -> None:
    lock_handle = acquire_lock(paths.root)
    with lock_handle:
        lock_before = paths.lockfile.read_bytes()
        skills = read_lock(paths)
        old_current = read_current(paths.root)
        commit = source_commit(paths.source)
        tree_hashes = {name: git_tree_hash(paths.source, commit, f".claude/skills/{name}") for name in skills}
        originals = validate_lock_against_tree(paths.source, commit, skills)
        receipt_dir = paths.root / "receipts"
        previous = validate_global_files(paths, skills, originals, receipt_dir)
        export_release(paths.source, commit, paths.root, tree_hashes)
        rendered, receipts = render_all(skills, originals, previous, paths.root, commit)
        target_current = f"releases/{commit}"

        # npm staging can take time; preserve any concurrent npx/user edits and newer lock.
        if paths.lockfile.read_bytes() != lock_before or read_current(paths.root) != old_current:
            raise RuntimeError("lock or current release changed during staging; no global skill files were written")
        for name in skills:
            skill_path = paths.skills / name / "SKILL.md"
            receipt_path = receipt_dir / f"{name}.json"
            if skill_path.is_symlink() or not skill_path.is_file():
                raise RuntimeError(f"{name}: global skill path changed during staging; no global skill files were written")
            if receipt_path.is_symlink() or (os.path.lexists(receipt_path) and not receipt_path.is_file()):
                raise RuntimeError(f"{name}: receipt path changed during staging; no global skill files were written")
            receipt_now = receipt_path.read_bytes() if receipt_path.is_file() else None
            if skill_path.read_bytes() != previous[name][0] or receipt_now != previous[name][1]:
                raise RuntimeError(f"{name}: global skill or receipt changed during staging; no global skill files were written")

        if old_current == target_current and all(
            previous[name][0] == rendered[name] and previous[name][1] == receipts[name]
            for name in skills
        ):
            check(paths)
            print(f"Already current: {SOURCE}@{commit}; no skill files rewritten")
            return

        timestamp = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"
        backup_root = paths.root / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_dir = Path(tempfile.mkdtemp(prefix=timestamp + "-", dir=backup_root))
        for name in skills:
            atomic_write(backup_dir / f"{name}.SKILL.md", previous[name][0])
            if previous[name][1] is not None:
                atomic_write(backup_dir / f"{name}.receipt.json", previous[name][1])
        if old_current is not None:
            atomic_write(backup_dir / "current-target.txt", (old_current + "\n").encode())

        changed: list[str] = []
        try:
            # shortcut: abrupt termination may leave a partial switch; restore retained backups manually.
            if old_current != target_current:
                atomic_current(paths.root, target_current)
            for name in skills:
                write_skill = previous[name][0] != rendered[name]
                write_receipt = previous[name][1] != receipts[name]
                if write_skill or write_receipt:
                    changed.append(name)
                if write_skill:
                    atomic_write(paths.skills / name / "SKILL.md", rendered[name])
                if write_receipt:
                    atomic_write(receipt_dir / f"{name}.json", receipts[name])
            check(paths)
        except Exception as original_error:
            restore_errors = []
            for name in reversed(changed):
                try:
                    atomic_write(paths.skills / name / "SKILL.md", previous[name][0])
                    old_receipt = previous[name][1]
                    receipt_path = receipt_dir / f"{name}.json"
                    if old_receipt is None:
                        with contextlib.suppress(FileNotFoundError):
                            receipt_path.unlink()
                    else:
                        atomic_write(receipt_path, old_receipt)
                except Exception as e:
                    restore_errors.append(f"{name}: {e}")
            try:
                atomic_current(paths.root, old_current)
            except Exception as e:
                restore_errors.append(f"current: {e}")
            detail = f"; rollback incomplete: {'; '.join(restore_errors)}" if restore_errors else "; prior state restored"
            raise RuntimeError(f"sync failed: {original_error}{detail}; backup retained at {backup_dir}") from original_error
        print(f"Synced {len(skills)} skills from {SOURCE}@{commit}; backup: {backup_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--sync", action="store_true", help="fetch main and synchronize; this is the default")
    group.add_argument("--check", action="store_true", help="read-only integrity check; no fetch or writes")
    args = parser.parse_args(argv)
    paths = Paths(Path.home())
    try:
        if args.check:
            check(paths)
        else:
            sync(paths)
        return 0
    except Exception as e:
        print(f"design-skill-kit: ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
