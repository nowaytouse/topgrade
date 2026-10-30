#!/usr/bin/env python3
"""Reconcile Cursor-managed built-in Skills into the shared ~/.agents/skills root.

Mirrors only a fixed allowlist of 26 skills with locally present Cursor native
counterparts. Preserves original directories by renaming them to a backup,
keeps local configuration where present, and makes the existing Topgrade coverage
check verify exact file parity. Does NOT modify ~/.cursor/skills-cursor.

Usage: python3 topgrade_cursor_native_reconcile.py --apply
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

HOME = Path.home()
NAMES = (
    'automate', 'autopilot', 'canvas', 'create-hook', 'create-rule',
    'create-skill', 'create-subagent', 'deploy-with-vercel', 'goal',
    'loop', 'migrate-to-skills', 'new-repo', 'onboard', 'origin',
    'rename-chat', 'review', 'review-bugbot', 'review-security',
    'sdk', 'share', 'shell', 'split-to-prs', 'statusline',
    'update-cli-config', 'update-cursor-settings', 'visualize',
)
NAMES_SET = frozenset(NAMES)
BUILTIN = HOME / '.cursor' / 'skills-cursor'
SHARED = HOME / '.agents' / 'skills'
STATE = HOME / '.local' / 'state' / 'topgrade-cursor-skills'
ORIGINAL_GATE = HOME / '.local' / 'bin' / 'topgrade-skills-integrity.py'
SELF_INSTALLED = HOME / '.local' / 'bin' / 'topgrade-cursor-native-reconcile.py'
FRAGMENT = Path(os.environ.get('XDG_CONFIG_HOME') or HOME / '.config') / 'topgrade.d' / '90-skills-coverage.toml'
GATE_NAME = 'Topgrade Skills Coverage Gate'
# The exact SKILL.md blob observed in the earlier user-provided fingerprints.
OLD_BABYSIT_BLOB = '626915d181d87db48def05ceec5fbbdc873dd445'
CONFIG_EXACT = frozenset({
    '.env', '.env.local', '.env.production', '.env.development',
    'settings.json', 'settings.local.json', 'config.json', 'config.toml',
    'config.yaml', 'config.yml', 'secrets.json', 'credentials.json',
})


def git_blob(data: bytes) -> str:
    return hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()


def safe_tree(folder: Path) -> dict[str, str]:
    """Reject symlinks; hash the exact regular files only."""
    if folder.is_symlink() or not folder.is_dir():
        raise RuntimeError(f'Expected a real directory: {folder}')
    mapping = {}
    for p in folder.rglob('*'):
        if p.is_symlink():
            raise RuntimeError(f'Refusing symlink inside skill: {p}')
        if p.is_file():
            rel = p.relative_to(folder).as_posix()
            mapping[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        elif not p.is_dir():
            raise RuntimeError(f'Unsupported file type: {p}')
    if 'SKILL.md' not in mapping:
        raise RuntimeError(f'No SKILL.md in {folder}')
    return mapping


def name_matches(folder: Path, name: str) -> bool:
    data = (folder / 'SKILL.md').read_text(encoding='utf-8')
    m = re.match(r'\A---\r?\n(.*?)\r?\n---', data, re.S)
    if not m:
        return False
    names = re.findall(r'^name:\s*[\'\"]?([\w-]+)', m.group(1), re.M)
    return len(names) == 1 and names[0] == name


def configs_in(folder: Path):
    for p in folder.rglob('*'):
        if p.is_symlink():
            raise RuntimeError(f'Cannot preserve symlink config safely: {p}')
        if p.is_file() and p.name in CONFIG_EXACT:
            yield p


def equivalent(native: Path, shared: Path) -> bool:
    """All published native files identical. Extra user config allowed."""
    try:
        a = safe_tree(native)
        b = safe_tree(shared)
        return all(b.get(name) == sha or Path(name).name in CONFIG_EXACT for name, sha in a.items()) and all(
            rel in a or Path(rel).name in CONFIG_EXACT for rel in b
        )
    except (OSError, RuntimeError):
        return False


def make_report_dir() -> Path:
    p = STATE / dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    p.mkdir(parents=True, exist_ok=False, mode=0o700)
    p.chmod(0o700)
    (p / 'backups').mkdir(mode=0o700)
    return p


def mirror_one(name: str, run_dir: Path) -> str:
    native = BUILTIN / name
    target = SHARED / name
    if not name_matches(native, name):
        raise RuntimeError(f'Native source is missing/renamed/invalid: {native}')
    native_digest_before = safe_tree(native)
    if not target.is_dir() or target.is_symlink():
        raise RuntimeError(f'Current shared install is missing or symlinked: {target}')
    if not name_matches(target, name):
        raise RuntimeError(f'Shared skill name mismatch: {target}')
    local_digest_before = safe_tree(target)
    if equivalent(native, target):
        return 'CURRENT'
    stage = Path(tempfile.mkdtemp(prefix='.cursor-native-stage-', dir=SHARED))
    # mkdtemp creates a directory; remove before copytree.
    stage.rmdir()
    backup = run_dir / 'backups' / name
    moved = False
    try:
        shutil.copytree(native, stage, symlinks=True)
        # Preserve only local user configuration, never overwrite executable assets
        # or SKILL.md with old copies.
        for cfg in configs_in(target):
            rel = cfg.relative_to(target)
            dst = stage / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cfg, dst)
        if safe_tree(native) != native_digest_before:
            raise RuntimeError(f'Native source changed while staging: {native}')
        if not equivalent(native, stage):
            raise RuntimeError(f'Staging verification failed for {name}')
        if safe_tree(target) != local_digest_before:
            raise RuntimeError(f'Shared source changed during staging: {target}')
        # Atomic rename on a single filesystem; archive the original unchanged.
        os.replace(target, backup)
        moved = True
        try:
            os.replace(stage, target)
        except BaseException:
            os.replace(backup, target)
            moved = False
            raise
        if not equivalent(native, target):
            # Restore previously installed skill, keeping failed new tree as a
            # diagnostic backup rather than removing user data.
            failed = run_dir / 'backups' / (name + '.failed-new')
            os.replace(target, failed)
            os.replace(backup, target)
            moved = False
            raise RuntimeError(f'Post-install verification failed: {name}')
        return 'UPDATED'
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def archive_legacy_babysit(run_dir: Path) -> str:
    """Optional archival only if it is exactly the previously observed old copy."""
    old = SHARED / 'babysit'
    if not old.exists():
        return 'ALREADY_ABSENT'
    if old.is_symlink() or not old.is_dir():
        raise RuntimeError('babysit path is not an ordinary directory')
    f = safe_tree(old)
    if list(f) != ['SKILL.md'] or git_blob((old / 'SKILL.md').read_bytes()) != OLD_BABYSIT_BLOB:
        raise RuntimeError('babysit changed since prior audit; preserving as unresolved')
    if (BUILTIN / 'babysit' / 'SKILL.md').is_file():
        raise RuntimeError('Cursor now provides babysit: refusing to archive native counterpart')
    os.replace(old, run_dir / 'backups' / 'babysit')
    return 'ARCHIVED_OBSOLETE_COPY'


def load_gate():
    if not ORIGINAL_GATE.is_file() or ORIGINAL_GATE.is_symlink():
        raise RuntimeError(f'Original coverage gate not found: {ORIGINAL_GATE}')
    spec = importlib.util.spec_from_file_location('installed_topgrade_skills_integrity', ORIGINAL_GATE)
    if not spec or not spec.loader:
        raise RuntimeError('Cannot load original gate')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, 'classify') or not hasattr(mod, 'audit'):
        raise RuntimeError('Installed gate interface changed: cannot verify coverage')
    return mod


def enhanced_audit():
    mod = load_gate()
    original_classify = mod.classify
    def classify(rows, lock, skip_reasons):
        statuses = original_classify(rows, lock, skip_reasons)
        for item in statuses:
            name = item['skill']
            if name not in NAMES_SET or item['status'] != 'NO_VERIFIED_UPDATER':
                continue
            target = SHARED / name
            if Path(item['path']) != target:
                continue
            native = BUILTIN / name
            if equivalent(native, target) and name_matches(native, name):
                item['status'] = 'CURSOR_NATIVE_MIRROR_VERIFIED'
                item['cursor_source'] = str(native)
        return statuses
    mod.classify = classify
    return mod.audit()


def sync_and_audit(archive_babysit: bool) -> int:
    run_dir = make_report_dir()
    counts = {}
    results = []
    failure = False
    for name in NAMES:
        try:
            status = mirror_one(name, run_dir)
        except Exception as exc:
            status = 'FAILED'
            failure = True
            results.append({'name': name, 'status': status, 'error': str(exc)})
            print(f'FAILED  {name}: {exc}')
        else:
            results.append({'name': name, 'status': status})
            print(f'{status:7} {name}')
        counts[status] = counts.get(status, 0) + 1
    if archive_babysit:
        try:
            status = archive_legacy_babysit(run_dir)
            results.append({'name': 'babysit', 'status': status})
            print(f'{status:23} babysit')
        except Exception as exc:
            failure = True
            results.append({'name': 'babysit', 'status': 'FAILED', 'error': str(exc)})
            print(f'FAILED babysit: {exc}')
    (run_dir / 'sync-report.json').write_text(json.dumps({
        'results': results, 'summary': counts, 'archive_babysit': archive_babysit,
        'backup': str(run_dir/'backups'),
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Sync report: {run_dir / "sync-report.json"}')
    print(f'Original backups: {run_dir / "backups"}')
    print('Running independent coverage audit (GH and npx remain enabled):')
    try:
        rc = enhanced_audit()
    except Exception as exc:
        print(f'Coverage audit failed: {exc}', file=sys.stderr)
        return 2
    return 2 if failure or rc else 0


def atomic_replace(path: Path, data: bytes):
    fd, temp = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def setup(archive_babysit: bool):
    if not FRAGMENT.is_file() or FRAGMENT.is_symlink():
        raise RuntimeError(f'Expected original additive Topgrade fragment: {FRAGMENT}')
    if not ORIGINAL_GATE.is_file():
        raise RuntimeError(f'Expected installed coverage gate: {ORIGINAL_GATE}')
    data = FRAGMENT.read_text(encoding='utf-8')
    cfg = tomllib.loads(data)
    command = cfg.get('commands', {}).get(GATE_NAME)
    if not isinstance(command, str) or str(ORIGINAL_GATE) not in command or '--audit' not in command:
        raise RuntimeError('Original Coverage Gate command was customized; refusing overwrite')
    if SELF_INSTALLED.exists() or SELF_INSTALLED.is_symlink():
        if SELF_INSTALLED.is_symlink() or SELF_INSTALLED.read_bytes() != Path(__file__).read_bytes():
            raise RuntimeError(f'Another script already exists at {SELF_INSTALLED}')
    else:
        SELF_INSTALLED.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(SELF_INSTALLED, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
        with os.fdopen(fd, 'wb') as f:
            f.write(Path(__file__).read_bytes())
    archive_arg = ' --archive-obsolete-babysit' if archive_babysit else ''
    new_command = f'{shlex.quote(sys.executable)} {shlex.quote(str(SELF_INSTALLED))} --sync-audit{archive_arg}'
    prefix = data[:]
    old_encoded = json.dumps(command, ensure_ascii=False)
    new_encoded = json.dumps(new_command, ensure_ascii=False)
    if old_encoded not in data:
        # tomllib semantics are valid but lexical TOML could use different quotes
        raise RuntimeError('Topgrade fragment is nonstandard; left unchanged')
    if data.count(old_encoded) != 1:
        raise RuntimeError('Ambiguous config command; left unchanged')
    replacement = data.replace(old_encoded, new_encoded, 1)
    tomllib.loads(replacement)
    run_dir = make_report_dir()
    backup = run_dir / '90-skills-coverage.toml.original'
    shutil.copy2(FRAGMENT, backup)
    try:
        atomic_replace(FRAGMENT, replacement.encode())
        test = subprocess.run(['topgrade', '--dry-run'], capture_output=True, text=True, timeout=120)
        stdout = test.stdout + '\n' + test.stderr
        if test.returncode or GATE_NAME not in stdout:
            raise RuntimeError('Topgrade dry-run did not retain coverage command')
        if not all(x in stdout for x in ('GitHub CLI Skills', 'Skills')):
            raise RuntimeError('Topgrade dry-run did not retain both normal update steps')
    except BaseException:
        atomic_replace(FRAGMENT, backup.read_bytes())
        raise
    print(f'INSTALLED {SELF_INSTALLED}')
    print(f'TOPGRADE FRAGMENT UPDATED {FRAGMENT}')
    print(f'CONFIG BACKUP {backup}')
    print('VERIFIED topgrade dry-run retains original GH / npx updaters and coverage gate')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--apply', action='store_true', help='install Topgrade integration and reconcile now')
    mode.add_argument('--sync-audit', action='store_true', help='refresh copies and run coverage audit')
    mode.add_argument('--audit', action='store_true', help='read-only enhanced audit')
    ap.add_argument('--archive-obsolete-babysit', action='store_true', help='archive original babysit only if unchanged and no longer present in Cursor')
    args = ap.parse_args()
    if args.audit:
        return enhanced_audit()
    if args.apply:
        try:
            setup(args.archive_obsolete_babysit)
        except Exception as exc:
            print(f'SETUP FAILED: {exc}', file=sys.stderr)
            return 2
        return sync_and_audit(args.archive_obsolete_babysit)
    return sync_and_audit(args.archive_obsolete_babysit)


if __name__ == '__main__':
    sys.exit(main())
