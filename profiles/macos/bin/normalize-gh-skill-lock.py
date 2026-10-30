#!/usr/bin/env python3
"""Normalize gh's namespaced lock keys to NPX names without dropping installed state.

GH 2.103.3 writes InstallName to the shared Vercel v3 lock even when an update
keeps a pre-existing flat directory. GH itself discovers installed state from
SKILL.md metadata. Restrict this compatibility adapter to mattpocock/skills,
where both a verified flat NPX record and a gh alias already exist.
"""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

SOURCE = "mattpocock/skills"
FIELDS = ("source", "sourceType", "sourceUrl", "skillPath")

def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError(f"Invalid SKILL.md frontmatter: {path}")
    header = text.split("\n---\n", 1)[0][4:]
    values = {}
    for field in ("name", "github-path", "github-repo", "github-tree-sha"):
        pattern = r"^" + ("" if field == "name" else r"[ \t]+") + re.escape(field) + r":\s*([^\n]+)$"
        found = re.findall(pattern, header, re.M)
        if len(found) != 1:
            raise ValueError(f"Missing or duplicate {field}: {path}")
        values[field] = found[0].strip().strip("\"'")
    return values

def plan(data, rows, home):
    if not isinstance(data, dict) or data.get("version") != 3 or not isinstance(data.get("skills"), dict):
        raise ValueError("Unsupported shared skill lock schema")
    result = json.loads(json.dumps(data))
    skills = result["skills"]
    changes = []
    roots = [home / p for p in (".agents/skills", ".codex/skills", ".claude/skills", ".cursor/skills")]
    for alias, record in list(skills.items()):
        if "/" not in alias or not isinstance(record, dict) or record.get("source") != SOURCE:
            continue
        relative = Path(alias)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe gh alias: {alias}")
        if any(os.path.lexists(root / relative) for root in roots):
            raise ValueError(f"Namespaced installation is present; ownership review required: {alias}")
        matches = [(name, value) for name, value in skills.items() if "/" not in name and isinstance(value, dict)
                   and all(value.get(field) == record.get(field) for field in FIELDS)]
        if len(matches) != 1:
            raise ValueError(f"No unique canonical lock record for gh alias: {alias}")
        name, canonical = matches[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError(f"Unsafe canonical name: {name}")
        expected = roots[0] / name
        gh_rows = [row for row in rows if row.get("skillName") == alias]
        if len(gh_rows) != 1 or gh_rows[0].get("path") != str(expected) or gh_rows[0].get("sourceURL") != "https://github.com/" + SOURCE:
            raise ValueError(f"gh installed-state mismatch: {alias}")
        if gh_rows[0].get("pinned") or any(record.get(f) or canonical.get(f) for f in ("ref", "pinnedRef")):
            raise ValueError(f"Pinned/ref-specific alias needs ownership review: {alias}")
        file = expected / "SKILL.md"
        if expected.is_symlink() or not file.is_file() or file.is_symlink():
            raise ValueError(f"Canonical skill is not a regular local installation: {file}")
        meta = frontmatter(file)
        sha = record.get("skillFolderHash", "")
        if (meta["name"] != name or meta["github-path"] + "/SKILL.md" != record.get("skillPath")
                or meta["github-repo"] != "https://github.com/" + SOURCE
                or not re.fullmatch(r"[0-9a-f]{40}", sha) or meta["github-tree-sha"] != sha
                or record.get("sourceType") != "github"):
            raise ValueError(f"Installed metadata does not verify gh alias: {alias}")
        # Installed metadata decides the actual hash; a newer NPX timestamp
        # stays newer even if GH recreated its alias from older metadata.
        original_canonical = dict(canonical)
        def timestamp(entry):
            value = entry.get("updatedAt")
            if not isinstance(value, str):
                raise ValueError(f"Invalid updatedAt: {alias}")
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError(f"Missing updatedAt timezone: {alias}")
            return parsed
        canonical["skillFolderHash"] = sha
        if timestamp(record) > timestamp(canonical):
            canonical["updatedAt"] = record["updatedAt"]
        del skills[alias]
        changes.append({"ghAlias": alias, "canonicalName": name, "installedPath": str(expected),
                        "installedSkillSHA256": hashlib.sha256(file.read_bytes()).hexdigest(),
                        "originalAlias": record, "originalCanonical": original_canonical,
                        "normalizedRecord": canonical})
    return result, changes

def write_all(file, payload):
    view = memoryview(payload)
    while view:
        count = file.write(view)
        if not count:
            raise OSError("Short write to shared skill lock")
        view = view[count:]
    file.truncate()
    os.fsync(file.fileno())

def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def archive_bytes(path, payload):
    with path.open("xb", buffering=0) as backup:
        write_all(backup, payload)

def normalize(home=None, rows=None, archive_root=None):
    home = Path.home() if home is None else Path(home)
    if os.environ.get("XDG_STATE_HOME"):
        raise ValueError("XDG_STATE_HOME changes NPX lock ownership; review before normalization")
    path = home / ".agents/.skill-lock.json"
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+b", buffering=0) as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Skill lock is not a regular file")
        original = file.read()
        if rows is None:
            check = subprocess.run(["/opt/homebrew/bin/gh", "skill", "list", "--json", "skillName,sourceURL,pinned,path"],
                                   capture_output=True, text=True, check=True, timeout=90)
            rows = json.loads(check.stdout)
        if not isinstance(rows, list):
            raise ValueError("Invalid gh installed-state response")
        candidate, changes = plan(json.loads(original), rows, home)
        if not changes:
            print("Skill lock interoperability: already canonical; no changes")
            return None
        root = home / ".local/state/topgrade-skill-lock" if archive_root is None else Path(archive_root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        sync_directory(root.parent)
        archive = Path(tempfile.mkdtemp(prefix="gh-npx-", dir=root))
        archive_bytes(archive / "original.skill-lock.json", original)
        archive_bytes(archive / "normalization.json", (json.dumps({"checkedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "source": "gh 2.103.3 InstallName / NPX frontmatter-name compatibility", "changes": changes}, indent=2) + "\n").encode())
        sync_directory(archive)
        sync_directory(root)
        for change in changes:
            if hashlib.sha256((Path(change["installedPath"]) / "SKILL.md").read_bytes()).hexdigest() != change["installedSkillSHA256"]:
                raise ValueError("Installed skill changed during normalization")
        current = path.stat()
        file.seek(0)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or file.read() != original:
            raise ValueError("Skill lock changed before normalization")
        payload = (json.dumps(candidate, indent=2, ensure_ascii=False) + "\n").encode()
        try:
            file.seek(0)
            write_all(file, payload)
        except BaseException:
            file.seek(0)
            write_all(file, original)
            raise
        print(f"Skill lock interoperability: normalized {len(changes)} gh aliases; original retained at {archive}")
        return archive

if __name__ == "__main__":
    try:
        normalize()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"FAIL: Skill lock interoperability check: {exc}")
