#!/usr/bin/env python3
"""Topgrade Agent Skills coverage gate.

No skill is installed, deleted, rewritten, or assigned a guessed GitHub origin.
Runs *after* Topgrade's existing gh and npx steps and turns skipped/unmanaged
skills into an explicit FAILED result. --setup adds an additive commands fragment.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

HOME = Path.home()
NAME = 'Topgrade Skills Coverage Gate'
INSTALL_NAME = 'topgrade-skills-integrity.py'
FRAGMENT_NAME = '90-skills-coverage.toml'
GH_LIST = ['gh', 'skill', 'list', '--json', 'skillName,sourceURL,scope,version,pinned,path']
MISSING = re.compile(r'^!\s+(.*?)\s+has no GitHub metadata\.', re.M)
SKIPPED = re.compile(r'^!\s+Skipping\s+([^:]+):\s*(.+)$', re.M)


def call(argv, timeout=600):
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, check=False)


def load_npx_lock():
    xdg = os.environ.get('XDG_STATE_HOME')
    path = (Path(xdg) / 'skills/.skill-lock.json') if xdg else HOME / '.agents/.skill-lock.json'
    if not path.is_file() or path.is_symlink():
        return {}, f'npx lock not found (or symlink): {path}'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('version', 0) < 3 or not isinstance(data.get('skills'), dict):
            raise ValueError('invalid schema')
        return data['skills'], None
    except (OSError, ValueError) as exc:
        return {}, f'unreadable npx lock: {path}: {exc}'


def owner_from_npx(row, entry):
    """An npx lock name alone is insufficient: inspect registered source fields."""
    if not isinstance(entry, dict):
        return False
    if entry.get('sourceType') not in {'github', 'git', 'gitlab', 'well-known'}:
        return False
    if not isinstance(entry.get('source'), str) or not entry['source']:
        return False
    if entry.get('sourceType') == 'well-known':
        if not entry.get('wellKnownDigest'):
            return False
    elif not entry.get('skillPath') or not entry.get('skillFolderHash'):
        return False
    path = Path(row.get('path', ''))
    if not path.is_absolute():
        return False
    # The lock records global ownership, not one directory per host. Only
    # classify a *present* SKILL.md under a conventional global skill tree.
    return path.is_relative_to(HOME) and (path / 'SKILL.md').is_file()


def same_repo_origin(gh_source, npx_entry):
    """Avoid treating a different publisher as a fallback for skipped gh updates."""
    if not gh_source or not isinstance(npx_entry, dict):
        return False
    parsed = urlparse(gh_source)
    gh_repo = parsed.path.strip('/').removesuffix('.git').lower()
    npx_repo = str(npx_entry.get('source', '')).strip('/').removesuffix('.git').lower()
    return parsed.hostname in ('github.com', 'www.github.com') and gh_repo == npx_repo


def classify(rows, lock, skip_reasons):
    statuses = []
    for row in rows:
        name = row.get('skillName', '')
        source = row.get('sourceURL') or ''
        entry = lock.get(name)
        npx = owner_from_npx(row, entry)
        if name in skip_reasons:
            status = ('NPX_OVERLAP_SOURCE_UNDISCOVERABLE' if npx and same_repo_origin(source, entry)
                      else 'GH_SOURCE_UNDISCOVERABLE')
        elif row.get('pinned'):
            status = 'PINNED_NOT_LATEST'
        elif source:
            status = 'GH_TRACKED'
        elif npx:
            status = 'NPX_TRACKED'
        else:
            status = 'NO_VERIFIED_UPDATER'
        statuses.append({
            'skill': name, 'path': row.get('path'), 'status': status,
            'gh_source': source or None,
            'npx_source': entry.get('source') if npx else None,
            'reason': skip_reasons.get(name),
        })
    return statuses


def audit():
    if not shutil.which('gh'):
        print('FAIL: gh CLI is unavailable', file=sys.stderr)
        return 2
    listing = call(GH_LIST, timeout=90)
    if listing.returncode != 0:
        print('FAIL: gh skill list: ' + (listing.stderr or listing.stdout)[-900:], file=sys.stderr)
        return 2
    try:
        rows = json.loads(listing.stdout)
        if not isinstance(rows, list):
            raise ValueError('not a list')
    except (ValueError, TypeError) as exc:
        print(f'FAIL: cannot parse gh skill list: {exc}', file=sys.stderr)
        return 2
    lock, lock_err = load_npx_lock()
    # Read-only check: gh does not change local skills with --dry-run.
    check = call(['gh', 'skill', 'update', '--all', '--dry-run'], timeout=600)
    output = check.stdout + '\n' + check.stderr
    skip_reasons = dict(SKIPPED.findall(output))
    statuses = classify(rows, lock, skip_reasons)
    missing_names = MISSING.findall(output)
    n_missing_warnings = len(missing_names)
    counted = Counter(s['status'] for s in statuses)
    unresolved = [s for s in statuses if s['status'] in {
        'NO_VERIFIED_UPDATER', 'GH_SOURCE_UNDISCOVERABLE', 'PINNED_NOT_LATEST',
    }]
    # npx-owned entries only count as checked if Topgrade actually ran its
    # normal npx step. Outside Topgrade this is an ownership audit, not a
    # fresh proof of latest-version state.
    errors = []
    if check.returncode:
        errors.append(f'gh skill update --dry-run exited {check.returncode}')
    if lock_err:
        errors.append(lock_err)
    if not rows:
        errors.append('gh skill list returned 0 installed skills')
    observed_untracked = Counter(s['skill'] for s in statuses if not s['gh_source'])
    unknown_warnings = Counter(missing_names) - observed_untracked
    if unknown_warnings:
        errors.append(f'gh warnings not correlated with untracked rows: {dict(unknown_warnings)}')

    report_dir = HOME / '.local/state/topgrade-skills-integrity' / dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    report_dir.mkdir(parents=True, exist_ok=False)
    (report_dir/'gh-dry-run.log').write_text(output, encoding='utf-8')
    report = {
        'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'installed_entries': len(rows),
        'gh_missing_source_warnings': n_missing_warnings,
        'gh_skipped_source_warnings': len(SKIPPED.findall(output)),
        'gh_exit_code': check.returncode,
        'statuses': dict(counted),
        'unresolved': unresolved,
        'all_entries': statuses,
        'errors': errors,
        'note': 'npx ownership is based on source lock; run through Topgrade to couple with real npx update',
    }
    (report_dir/'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')

    print('=== Topgrade Skills: updater coverage, NOT just exit status ===')
    print(f'Installed entries: {len(rows)}  |  gh missing-metadata warnings: {n_missing_warnings}')
    for k,v in sorted(counted.items()):
        print(f'  {k}: {v}')
    print(f'Full report: {report_dir / "report.json"}')
    for s in unresolved:
        print(f'UNRESOLVED {s["status"]}: {s["skill"]}  [{s["path"]}]')
    for err in errors:
        print(f'ERROR: {err}')
    if unresolved or errors:
        print('FAILED: At least one skill has no verified update manager or its GH source is not discoverable.')
        return 2
    print('PASS: Each installed entry has an identified update owner; the separate updater steps must still succeed.')
    return 0


def atomic_create(path: Path, payload: bytes, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if path.is_file() and not path.is_symlink() and path.read_bytes() == payload:
            return False
        raise RuntimeError(f'Refusing to overwrite existing file: {path}')
    fd, p = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(p, mode)
        # Exclusive create prevents replacing an unknown race winner.
        os.link(p, path)
    finally:
        Path(p).unlink(missing_ok=True)
    return True


def setup():
    if not shutil.which('topgrade'):
        print('FAIL: topgrade executable not in PATH; no changes applied')
        return 2
    target = HOME / '.local/bin' / INSTALL_NAME
    xdg_cfg = Path(os.environ.get('XDG_CONFIG_HOME') or HOME / '.config')
    fragment = xdg_cfg/'topgrade.d'/FRAGMENT_NAME
    script_payload = Path(__file__).read_bytes()
    command = f'{shlex.quote(sys.executable)} {shlex.quote(str(target))} --audit'
    cfg = f'[commands]\n"{NAME}" = {json.dumps(command)}\n'
    tomllib.loads(cfg)
    created_script = created_fragment = False
    try:
        created_script = atomic_create(target, script_payload, 0o755)
        created_fragment = atomic_create(fragment, cfg.encode())
        test = call(['topgrade','--dry-run'], timeout=120)
        if test.returncode or NAME not in (test.stdout + test.stderr):
            raise RuntimeError('topgrade dry-run did not recognize the coverage command: '
                               +(test.stderr or test.stdout)[-1600:])
        for required in ('GitHub CLI Skills', 'Skills'):
            if not re.search(r'(?m)^' + re.escape(required) + r': OK\s*$', (test.stdout + test.stderr)):
                raise RuntimeError(f'topgrade dry-run is missing existing updater summary {required!r}')
    except Exception as exc:
        if created_fragment and fragment.read_bytes() == cfg.encode():
            fragment.unlink()
        if created_script and target.read_bytes() == script_payload:
            target.unlink()
        print(f'SETUP FAILED / ROLLED BACK: {exc}', file=sys.stderr)
        return 2
    print(f'INSTALLED: {target}')
    print(f'ADD-ONLY CONFIG: {fragment}')
    print('VERIFIED: topgrade dry-run includes GitHub CLI Skills, Skills, and Skills Coverage Gate.')
    print('No existing updater was disabled. No Skill was rewritten.')
    print('Run: topgrade -y')
    return 0


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    opts=ap.add_mutually_exclusive_group(required=True)
    opts.add_argument('--setup',action='store_true',help='install additive Topgrade coverage gate')
    opts.add_argument('--audit',action='store_true',help='read-only cross-manager coverage verification')
    args=ap.parse_args()
    return setup() if args.setup else audit()


if __name__=='__main__':
    sys.exit(main())
