#!/usr/bin/env python3.15
"""Restore two verified npm security patches or audit an unfamiliar upstream tree."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request

os.umask(0o077)
base = Path.home()/".local/state/topgrade-macos-profile/npm-security"
probe = Path.home()/".local/share/topgrade-macos-profile/npm-security"
base.mkdir(parents=True, exist_ok=True, mode=0o700)
live = Path("/opt/homebrew/lib/node_modules/npm")
candidate = probe/"node_modules"
before = json.loads((probe/"before.json").read_text())
after = json.loads((probe/"leaf-candidate-after.json").read_text())

def manifest(root):
    result = {}
    for path in root.rglob("*"):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise RuntimeError(f"Escaping package link: {relative}")
            result[relative] = {"link": os.readlink(path)}
        elif path.is_file():
            result[relative] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return result

# Fresh npm installs omit these exact, previously recorded Python bytecode caches.
cache_paths = {key for key in before if key.startswith("node_modules/node-gyp/gyp/pylib/")
               and "/__pycache__/" in key and key.endswith(".cpython-315.pyc")}
assert all(before[key] == after[key] for key in cache_paths)

def matches_reference(actual, expected):
    observed = {key: value for key, value in actual.items()
                if not (key in cache_paths and value == before[key])}
    reference = {key: value for key, value in expected.items() if key not in cache_paths}
    return observed == reference

def audit_unknown_tree():
    names = ("undici", "ip-address")
    versions = {}
    for name in ("npm", *names):
        root = live if name == "npm" else live/"node_modules"/name
        path = root/"package.json"
        if root.is_symlink() or path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing or symlinked package metadata: {name}")
        metadata = json.loads(path.read_text())
        version = metadata.get("version") if isinstance(metadata, dict) else None
        if metadata.get("name") != name or not isinstance(version, str) or not re.fullmatch(
                r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version):
            raise ValueError(f"Invalid installed package identity: {name}")
        versions[name] = version

    endpoint = "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk"
    request = urllib.request.Request(endpoint,
        data=json.dumps({name: [versions[name]] for name in names}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.geturl() != endpoint:
            raise ValueError("Unexpected advisory registry redirect")
        body = response.read(1048577)
    if len(body) > 1048576:
        raise ValueError("Advisory registry response exceeds 1 MiB")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate advisory registry JSON key")
            result[key] = value
        return result
    advisories = json.loads(body, object_pairs_hook=unique_object)
    if not isinstance(advisories, dict) or set(advisories) - set(names):
        raise ValueError("Unexpected advisory registry package keys")
    records, ids = [], set()
    for name, entries in advisories.items():
        if not isinstance(entries, list):
            raise ValueError("Advisory registry entries must be arrays")
        for entry in entries:
            if not isinstance(entry, dict) or not {"id", "url", "title", "severity", "vulnerable_versions"} <= set(entry):
                raise ValueError("Malformed advisory registry record")
            identifier, url, affected = entry["id"], entry["url"], entry["vulnerable_versions"]
            if (type(identifier) is not int or identifier <= 0 or identifier in ids
                    or not isinstance(url, str) or not re.fullmatch(r"https://github\.com/advisories/GHSA-[a-z0-9-]+", url)
                    or not isinstance(entry["title"], str) or not entry["title"].strip()
                    or entry["severity"] not in ("low", "moderate", "high", "critical")
                    or not isinstance(affected, str) or not affected or len(affected) > 1024):
                raise ValueError("Invalid advisory registry fields")
            ids.add(identifier)
            records.append({"name": name, "version": versions[name], "range": affected, "id": identifier, "url": url})
    script = """const fs=require('node:fs'),s=require(process.argv[1]);
const a=JSON.parse(fs.readFileSync(0,'utf8'));
for(const v of Object.values(a.versions))if(!s.valid(v))throw Error('Invalid package version');
for(const r of a.records)if(!s.validRange(r.range))throw Error('Invalid advisory range');
console.log(JSON.stringify(a.records.filter(r=>s.satisfies(r.version,r.range))));"""
    matches = json.loads(subprocess.check_output(
        ["/opt/homebrew/bin/node", "-e", script, str(live/"node_modules/semver")],
        input=json.dumps({"versions": versions, "records": records}), text=True, timeout=10))
    if matches:
        findings = ", ".join(f"{r['name']} {r['version']} {r['url']}" for r in matches)
        raise ValueError("Known vulnerable dependencies in an unrecognized npm tree: " + findings)
    return versions

if live.is_symlink() or live.resolve() != live:
    raise SystemExit("Refusing a symlinked or relocated live npm root")
current = manifest(live)
if matches_reference(current, after):
    print("Already current: both verified npm security patch leaves")
    raise SystemExit(0)
if not matches_reference(current, before):
    try:
        versions = audit_unknown_tree()
        if manifest(live) != current:
            raise ValueError("Live npm changed during the advisory query")
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"FAIL: unfamiliar npm dependency audit: {error}") from error
    print(f"PASS npm {versions['npm']}: installed undici {versions['undici']} and "
          f"ip-address {versions['ip-address']} have no known matching advisories; no packages modified")
    raise SystemExit(0)
validation = json.loads((probe/"validation-report.json").read_text())
assert validation["allChecksPassed"] and validation["candidateUnchangedAfterTests"]
assert validation["officialRegistryAdvisories"] == {}
def verify_patch_leaves(root, reference):
    for name in ("undici", "ip-address"):
        prefix = f"node_modules/{name}/"
        expected = {key[len(prefix):]: value for key, value in reference.items() if key.startswith(prefix)}
        if not expected or manifest(root/name) != expected:
            raise RuntimeError("Candidate patch leaf changed after validation")

verify_patch_leaves(candidate, after)

# renamex_np(RENAME_SWAP) exchanges complete directories atomically on macOS.
libc = ctypes.CDLL(None, use_errno=True)
rename = libc.renamex_np
rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
rename.restype = ctypes.c_int

def exchange(source, target):
    if rename(os.fsencode(source), os.fsencode(target), 2) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))

staging = Path(tempfile.mkdtemp(prefix="npm-patch-switch.", dir=base))
assert staging.stat().st_dev == live.stat().st_dev, "Atomic exchange requires the same filesystem"
pairs = []
for name in ("undici", "ip-address"):
    source, target = candidate/name, live/"node_modules"/name
    assert source.is_dir() and target.is_dir() and not source.is_symlink() and not target.is_symlink()
    prepared = staging/name
    shutil.copytree(source, prepared)
    assert manifest(prepared) == manifest(source)
    pairs.append((prepared, target))

switched = []
receipt_temp = None
try:
    assert manifest(live) == current, "Live npm changed during staging"
    # shortcut: compatible leaves switch separately, verify both after interruption.
    for prepared, target in pairs:
        exchange(prepared, target)
        switched.append((prepared, target))
    assert matches_reference(manifest(live), after), "Promoted npm differs from the tested candidate"
    output = subprocess.check_output(
        ["/opt/homebrew/bin/node", str(live/"bin/npm-cli.js"), "--version"], text=True).strip()
    assert output == "12.2.1", output
    receipt = {"npmVersion": output, "backupDirectory": str(staging),
               "source": "https://registry.npmjs.org",
               "packages": json.loads((probe/"leaf-candidate-report.json").read_text())["packages"],
               "beforeManifest": str(probe/"before.json"),
               "afterManifest": str(probe/"leaf-candidate-after.json"),
               "validationReport": str(probe/"validation-report.json")}
    fd, receipt_name = tempfile.mkstemp(prefix=".npm-security-patch-receipt.", dir=base)
    receipt_temp = Path(receipt_name)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(receipt, indent=2)+"\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(receipt_temp, base/"npm-security-patch-receipt.json")
except BaseException:
    failures = []
    for prepared, target in reversed(switched):
        try:
            exchange(prepared, target)
        except OSError as error:
            failures.append(str(error))
    if failures:
        raise RuntimeError("npm rollback failed: " + "; ".join(failures))
    assert manifest(live) == current, "npm rollback verification failed"
    if receipt_temp is not None:
        receipt_temp.unlink(missing_ok=True)
    raise
print("PASS promoted only official undici 6.29.1 and ip-address 10.7.5 patch leaves")
print("Original complete package directories:", staging)
