#!/usr/bin/env python3.15
"""Install only this audited macOS profile; default action is a read-only plan."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent
BIN = frozenset("""brew-headsync-upgrade topgrade-rustup-headsync topgrade-mas-verified
topgrade-gcloud-verified update-python-sqlite-trunk update-sqlite-trunk update-tourbox-beta
update-preview-extras update-preview-apps update-project-deps cli-versions
npm-bundled-security-guard.py topgrade-cursor-native-reconcile.py design-skill-kit-sync.py
topgrade-skills-verified.py topgrade-skills-integrity.py normalize-gh-skill-lock.py
topgrade-select-xcode-beta proxy-auto-run update-gradle-nightly update-xcode-beta
topgrade-helm-verified topgrade-gh-skills-verified""".split())
LIBEXEC = frozenset(("rolling-deps/edge-extras.py", "rolling-deps/update-gradle-nightly.real.py",
                     "preview-apps/update-preview-apps.py"))
CONFIG = frozenset(("topgrade.toml", "topgrade.d/90-brew-headsync.toml",
                    "topgrade.d/90-skills-coverage.toml", "topgrade.d/90-xcode-beta-select.toml",
                    "topgrade.d/95-skills-unattended.toml"))
REFERENCE = frozenset(("before.json", "leaf-candidate-after.json", "validation-report.json",
                       "leaf-candidate-report.json"))

def digest(data):
    return hashlib.sha256(data).hexdigest()

def safe_path(root, relative):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in ("..", ".", "") for p in parts):
        raise RuntimeError(f"Unsafe relative path: {relative}")
    target = root.joinpath(*parts)
    for path in (target, *target.parents):
        if path.is_symlink():
            raise RuntimeError(f"Refusing symlink: {path}")
        if path == root:
            break
    return target

def file_bytes(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"Expected a regular file: {path}")
    return path.read_bytes() if path.exists() else None

def mapped_destination(source):
    prefix, _, suffix = source.partition("/")
    if prefix == "bin" and suffix in BIN:
        return ".local/bin/" + suffix
    if prefix == "libexec" and suffix in LIBEXEC:
        return ".local/libexec/" + suffix
    if prefix == "config" and suffix in CONFIG:
        return ".config/" + suffix
    if source.startswith("share/npm-security/"):
        tail = source.removeprefix("share/npm-security/")
        if tail in REFERENCE or tail.startswith(("patches/undici/", "patches/ip-address/",
                                                "node_modules/undici/", "node_modules/ip-address/")):
            return ".local/share/topgrade-macos-profile/npm-security/" + tail
    raise RuntimeError(f"Source outside fixed profile allowlist: {source}")

def assets():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    if manifest.get("version") != 1:
        raise RuntimeError("Unsupported profile manifest")
    result, destinations = [], set()
    for entry in manifest["files"]:
        source = safe_path(ROOT, entry["source"])
        destination = mapped_destination(entry["source"])
        if destination != entry["destination"] or destination in destinations:
            raise RuntimeError("Duplicate or inconsistent destination")
        destinations.add(destination)
        content = file_bytes(source)
        if content is None or digest(content) != entry["sha256"]:
            raise RuntimeError(f"Source checksum mismatch: {entry['source']}")
        if entry["mode"] not in (0o600, 0o644, 0o700, 0o755):
            raise RuntimeError("Unsupported profile file mode")
        result.append(dict(entry, content=content))
    if not result:
        raise RuntimeError("Empty profile manifest")
    return sorted(result, key=lambda e: ({"share":0, "libexec":1, "bin":2, "config":3}[e["source"].split("/")[0]], e["source"]))

def installed(home):
    path = safe_path(home, ".local/state/topgrade-macos-profile/installed.json")
    raw = file_bytes(path)
    if raw is None:
        return None
    value = json.loads(raw)
    if value.get("version") != 1 or not isinstance(value.get("files"), dict):
        raise RuntimeError("Unrecognized installed profile record")
    return value

def plan(home):
    state = safe_path(home, ".local/state/topgrade-macos-profile")
    if state.exists():
        pending(state)
    previous = installed(home)
    owned = previous["files"] if previous else {}
    changes, unchanged = [], []
    for entry in assets():
        target = safe_path(home, entry["destination"])
        old = file_bytes(target)
        old_sha = digest(old) if old is not None else None
        if old_sha == entry["sha256"] and stat.S_IMODE(target.stat().st_mode) == entry["mode"]:
            unchanged.append(entry["destination"])
            continue
        expected = owned.get(entry["destination"], entry.get("adoptSha256"))
        if old_sha is not None and old_sha != expected:
            raise RuntimeError(f"Local drift: {entry['destination']}; preserve and reconcile it before applying")
        changes.append(dict(entry, before=old, beforeSha256=old_sha,
                            beforeMode=stat.S_IMODE(target.stat().st_mode) if old is not None else None,
                            beforeMtimeNs=target.stat().st_mtime_ns if old is not None else None))
    return changes, unchanged

def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def atomic_write(path, data, mode=0o600, mtime_ns=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".topgrade-profile.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        if mtime_ns is not None:
            os.utime(name, ns=(mtime_ns, mtime_ns))
        os.replace(name, path)
        fsync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)

def write_json(path, value):
    atomic_write(path, (json.dumps(value, indent=2) + "\n").encode())

@contextmanager
def locked(home):
    state = safe_path(home, ".local/state/topgrade-macos-profile")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = safe_path(state, "lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("Profile lock is not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield state
    finally:
        os.close(fd)

def pending(state):
    directory = safe_path(state, "transactions")
    if directory.exists():
        for path in directory.glob("*/journal.json"):
            if path.is_symlink() or path.parent.is_symlink():
                raise RuntimeError("Symlinked transaction journal")
            journal = json.loads(path.read_text())
            if journal["status"] not in ("complete", "rolled-back"):
                raise RuntimeError(f"Incomplete transaction {path.parent.name}; use --rollback {path.parent.name}")

def rollback(home, transaction):
    if len(transaction) != 32 or any(c not in "0123456789abcdef" for c in transaction):
        raise RuntimeError("Invalid transaction ID")
    state = safe_path(home, ".local/state/topgrade-macos-profile")
    folder = safe_path(state, "transactions/" + transaction)
    journal_path = safe_path(folder, "journal.json")
    journal = json.loads(journal_path.read_text())
    if journal["status"] == "rolled-back":
        return {"status":"already-rolled-back", "transaction":transaction}
    current_record = installed(home)
    current_id = current_record.get("transaction") if current_record else None
    allowed_ids = {transaction, journal["previous"].get("transaction") if journal["previous"] else None}
    if current_id not in allowed_ids:
        raise RuntimeError("A newer profile is active; refusing older rollback")
    for row in journal["files"]:
        if mapped_destination(row["source"]) != row["destination"]:
            raise RuntimeError("Invalid rollback destination")
        target = safe_path(home, row["destination"])
        current = file_bytes(target)
        current_sha = digest(current) if current is not None else None
        if current_sha not in (row["sha256"], row["beforeSha256"]):
            raise RuntimeError(f"Rollback would overwrite local drift: {row['destination']}")
        if row["beforeSha256"] is not None:
            backup = safe_path(folder, "backup/" + row["backup"])
            backup_bytes = file_bytes(backup)
            if backup_bytes is None or digest(backup_bytes) != row["beforeSha256"]:
                raise RuntimeError("Backup checksum mismatch")
    for row in reversed(journal["files"]):
        target = safe_path(home, row["destination"])
        current = file_bytes(target)
        current_sha = digest(current) if current is not None else None
        if current_sha == row["beforeSha256"] and (current is None or (
            stat.S_IMODE(target.stat().st_mode) == row["beforeMode"] and
            target.stat().st_mtime_ns == row["beforeMtimeNs"])):
            continue
        if row["beforeSha256"] is None:
            target.unlink()
            fsync_dir(target.parent)
        else:
            atomic_write(target, file_bytes(safe_path(folder, "backup/" + row["backup"])),
                         row["beforeMode"], row["beforeMtimeNs"])
    record = safe_path(state, "installed.json")
    if journal["previous"] is None:
        record.unlink(missing_ok=True)
        fsync_dir(state)
    else:
        write_json(record, journal["previous"])
    journal["status"] = "rolled-back"
    write_json(journal_path, journal)
    return {"status":"rolled-back", "transaction":transaction}

def apply(home):
    with locked(home) as state:
        changes, unchanged = plan(home)
        desired = {row["destination"]:row["sha256"] for row in assets()}
        current = installed(home)
        if not changes and current and current["files"] == desired:
            return {"status":"already-current", "changed":0, "unchanged":len(unchanged)}
        transaction = uuid.uuid4().hex
        folder = safe_path(state, "transactions/" + transaction)
        folder.mkdir(parents=True, mode=0o700)
        backup_dir = folder / "backup"
        backup_dir.mkdir(mode=0o700)
        rows = []
        for index, row in enumerate(changes):
            row = dict(row)
            before = row.pop("before")
            row.pop("content")
            row["backup"] = str(index)
            if before is not None:
                atomic_write(backup_dir / row["backup"], before)
            rows.append(row)
        journal = {"version":1, "status":"prepared", "previous":installed(home), "files":rows}
        journal_path = folder / "journal.json"
        write_json(journal_path, journal)
        fsync_dir(folder)
        fsync_dir(folder.parent)
        fsync_dir(state)
        try:
            for row in changes:
                target = safe_path(home, row["destination"])
                current = file_bytes(target)
                if (digest(current) if current is not None else None) != row["beforeSha256"]:
                    raise RuntimeError(f"Changed during deployment: {row['destination']}")
                atomic_write(target, row["content"], row["mode"])
            if any(file_bytes(safe_path(home, row["destination"])) != row["content"] for row in changes):
                raise RuntimeError("Post-install verification failed")
            write_json(safe_path(state, "installed.json"), {"version":1, "transaction":transaction,
                       "files":{row["destination"]:row["sha256"] for row in assets()}})
            journal["status"] = "complete"
            write_json(journal_path, journal)
        except BaseException:
            try:
                rollback(home, transaction)
            except Exception as recovery:
                raise RuntimeError(f"Deployment incomplete; preserve transaction {transaction}; recovery failed: {recovery}") from recovery
            raise
        return {"status":"installed", "changed":len(changes), "unchanged":len(unchanged),
                "transaction":transaction, "rollback":"--rollback " + transaction}

def main(argv=None):
    if sys.version_info[:2] < (3, 15):
        print("ERROR: Python 3.15 or newer is required; run this installer with python3.15.", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--verify", action="store_true")
    action.add_argument("--rollback", metavar="TRANSACTION")
    parser.add_argument("--home", type=Path, default=Path.home(), help="Explicit alternate home for isolated validation")
    args = parser.parse_args(argv)
    home = args.home.resolve()
    if not home.is_dir() or args.home.is_symlink() or home == Path("/"):
        parser.error("Expected an existing non-symlink home directory")
    try:
        if args.rollback:
            with locked(home):
                result = rollback(home, args.rollback)
        elif args.apply:
            result = apply(home)
        else:
            changes, unchanged = plan(home)
            result = {"status":"verified" if args.verify and not changes else "plan",
                      "changed":len(changes), "unchanged":len(unchanged),
                      "firstChanges":[row["destination"] for row in changes[:30]],
                      "moreChanges":max(0, len(changes)-30)}
            if args.verify and changes:
                raise RuntimeError(f"Profile is not fully installed: {len(changes)} file(s)")
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print("ERROR: " + str(error))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
