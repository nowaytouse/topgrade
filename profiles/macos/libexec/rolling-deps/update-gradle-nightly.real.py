#!/usr/bin/env python3

from pathlib import Path
import hashlib
import json
import os
import shutil
import tempfile
import time
import sys
import re
import fcntl
import subprocess
import urllib.request
import urllib.error
import zipfile

HOME = Path.home()

ROOT = (
    HOME
    / ".local"
    / "opt"
    / "gradle-nightly"
)

LINK = (
    HOME
    / ".local"
    / "bin"
    / "gradle-nightly"
)

API = (
    "https://services.gradle.org/"
    "versions/nightly"
)


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "rolling-deps-gradle-nightly/1"})
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                raise SystemExit(f"Gradle Nightly API: HTTP {exc.code} (not retryable)") from None
            problem = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            problem = f"{type(exc).__name__}: {exc}"
        print(f"Gradle: Nightly API attempt {attempt}/3 failed: {problem}", file=sys.stderr, flush=True)
        if attempt == 3:
            raise SystemExit(f"Gradle Nightly API: RETRYABLE after 3 attempts ({problem})")
        time.sleep(attempt * 2)

def download(url, target, expected):
    """Download and verify a complete Gradle distribution."""
    if not re.fullmatch(r"https://services\.gradle\.org/distributions-snapshots/gradle-\d+(?:\.\d+){1,2}-\d{14}\+\d{4}-bin\.zip", url):
        raise ValueError("Gradle resume requires an official immutable distribution URL")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Gradle download requires a valid SHA-256")
    received, declared, etag = 0, None, None
    digest = hashlib.sha256()
    for attempt in range(1, 4):
        print(f"Gradle: download attempt {attempt}/3", flush=True)
        offset = received if (etag and declared and 0 < received < declared
                              and target.is_file() and target.stat().st_size == received) else 0
        headers = {"User-Agent": "rolling-deps-gradle-nightly/3", "Accept-Encoding": "identity"}
        if offset:
            headers.update({"Range": f"bytes={offset}-", "If-Range": etag})
            print(f"Gradle: resuming verified representation at byte {offset}", flush=True)
        req = urllib.request.Request(url, headers=headers)
        final_url = url
        last_progress = 0
        last_progress_at = time.monotonic()

        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                final_url = response.geturl()
                allowed = (url, url.replace("https://services.gradle.org/", "https://downloads.gradle.org/", 1))
                if final_url not in allowed:
                    raise ValueError("Gradle distribution redirected outside its official immutable URL")
                print(f"Gradle: HTTP {response.status}; URL: {final_url}", flush=True)
                content_length = response.headers.get("Content-Length")
                length = int(content_length) if content_length is not None else None
                current_etag = response.headers.get("ETag")
                encoding = response.headers.get("Content-Encoding", "identity").lower()
                if encoding != "identity":
                    raise ValueError(f"Unexpected content encoding: {encoding}")
                if response.status == 206:
                    part = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if (not offset or not part or current_etag != etag
                            or tuple(map(int, part.groups())) != (offset, declared - 1, declared)
                            or length != declared - offset):
                        raise ValueError("Gradle resumed response has inconsistent range, ETag or total")
                elif response.status == 200:
                    if offset:
                        print("Gradle: server returned 200; restarting distribution from byte zero", flush=True)
                    received, declared, digest = 0, length, hashlib.sha256()
                    etag = current_etag if current_etag and re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', current_etag) else None
                    offset = 0
                else:
                    raise ValueError(f"Unexpected Gradle HTTP status: {response.status}")
                if declared is not None and not 0 < declared <= 1024 ** 3:
                    raise ValueError("Invalid Gradle total size or 1 GiB safety limit exceeded")
                with target.open("ab" if offset else "wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)

                        if not chunk:
                            break

                        received += len(chunk)

                        if received > 1024 ** 3 or (declared is not None and received > declared):
                            raise ValueError(
                                "Download exceeds declared total or 1 GiB safety limit"
                            )

                        output.write(chunk)
                        digest.update(chunk)

                        now = time.monotonic()
                        if (
                            received - last_progress >= 8 * 1024 * 1024
                            or now - last_progress_at >= 5
                        ):
                            total = (
                                f"{declared / 1048576:.1f} MiB"
                                if declared is not None
                                else "unknown"
                            )
                            print(
                                f"Gradle download: "
                                f"{received / 1048576:.1f} MiB / {total}",
                                flush=True,
                            )
                            last_progress = received
                            last_progress_at = now

            if declared is not None and received != declared:
                raise EOFError(
                    f"Incomplete download: "
                    f"received={received}, expected={declared}"
                )

            with target.open("rb") as verified:
                actual = hashlib.file_digest(verified, "sha256").hexdigest()

            if actual != expected:
                raise ValueError(
                    f"SHA-256 mismatch: "
                    f"expected={expected}, actual={actual}"
                )

            print(
                f"Gradle download verified: "
                f"{received} bytes, SHA-256 OK"
            )
            return

        except Exception as exc:
            print(
                f"Gradle download attempt {attempt}/3 failed\n"
                f"  URL: {final_url}\n"
                f"  Received: {received} bytes\n"
                f"  Content-Length: {declared}\n"
                f"  Partial SHA-256: {digest.hexdigest()}\n"
                f"  Error: {exc}",
                file=sys.stderr,
            )

            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            if isinstance(exc, (ValueError, urllib.error.HTTPError)):
                target.unlink(missing_ok=True)
                received, declared, etag = 0, None, None
                digest = hashlib.sha256()

            if attempt == 3:
                target.unlink(missing_ok=True)
                raise SystemExit(
                    "Gradle download failed verification "
                    "after 3 attempts. Existing installation preserved."
                ) from exc

            time.sleep(attempt * 2)


print("Gradle: querying Nightly API...", flush=True)
info = fetch_json(API)

version = info["version"]
url = info["downloadUrl"]
expected = info["checksum"].lower()

if not re.fullmatch(r"\d+(?:\.\d+){1,2}-\d{14}\+\d{4}", version):
    raise SystemExit(f"Unexpected Gradle version: {version}")

if not re.fullmatch(r"[0-9a-f]{64}", expected):
    raise SystemExit("Invalid Gradle SHA-256 from API")

official_url = (
    "https://services.gradle.org/distributions-snapshots/"
    f"gradle-{version}-bin.zip"
)

if url != official_url:
    raise SystemExit(
        f"Unexpected Gradle download URL: {url}"
    )

ROOT.mkdir(
    parents=True,
    exist_ok=True,
)

lock_file = (ROOT / ".update.lock").open("a+")

try:
    fcntl.flock(
        lock_file,
        fcntl.LOCK_EX | fcntl.LOCK_NB,
    )
except BlockingIOError:
    raise SystemExit(
        "Another Gradle nightly update is already running"
    )

print("Gradle: checking Java environment...", flush=True)
brew = shutil.which("brew")

java_home = None

if brew:
    result = __import__("subprocess").run(
        [
            brew,
            "--prefix",
            "openjdk",
        ],
        stdout=__import__("subprocess").PIPE,
        stderr=__import__("subprocess").DEVNULL,
        text=True,
    )

    if result.returncode == 0:
        candidate = (
            Path(result.stdout.strip())
            / "libexec"
            / "openjdk.jdk"
            / "Contents"
            / "Home"
        )

        if (
            candidate
            / "bin"
            / "java"
        ).exists():
            java_home = candidate

if java_home is None:
    raise SystemExit(
        "Homebrew OpenJDK was not found. "
        "Install it with: brew install openjdk"
    )



def gradle_healthy(directory):
    binary_path = directory / "bin" / "gradle"

    if not (
        binary_path.is_file()
        and (directory / "lib").is_dir()
    ):
        return False

    try:
        binary_path.chmod(binary_path.stat().st_mode | 0o111)

        env = os.environ.copy()
        env["JAVA_HOME"] = str(java_home)
        env["PATH"] = (
            str(java_home / "bin")
            + os.pathsep
            + env.get("PATH", "")
        )

        result = subprocess.run(
            [str(binary_path), "--version"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=90,
        )

        return (
            result.returncode == 0
            and "Gradle" in result.stdout
        )

    except (OSError, subprocess.TimeoutExpired):
        return False


target = ROOT / version
binary = target / "bin" / "gradle"

if target.is_symlink() or (
    target.exists() and not target.is_dir()
):
    raise SystemExit(
        f"Unsafe Gradle installation path: {target}"
    )

healthy = gradle_healthy(target) if target.is_dir() else False
repair_required = target.is_dir() and not healthy
quarantined = None

if repair_required:
    print(
        f"Gradle nightly: damaged installation detected; "
        f"automatic repair: {version}"
    )

if not healthy:

    print(
        f"Gradle nightly: installing {version}"
    )

    with tempfile.TemporaryDirectory(
        prefix=".gradle-nightly-",
        dir=ROOT,
    ) as td:

        td = Path(td)

        archive = td / "gradle.zip"

        download(
            url,
            archive,
            expected,
        )

        print("Gradle: SHA-256 OK; verifying ZIP...", flush=True)
        extracted = td / "extracted"

        extracted.mkdir()

        with zipfile.ZipFile(
            archive
        ) as zf:
            bad_member = zf.testzip()

            if bad_member is not None:
                raise SystemExit(
                    f"Gradle ZIP CRC failure: {bad_member}"
                )

            zf.extractall(
                extracted
            )

        candidates = [
            p
            for p in extracted.iterdir()
            if p.is_dir()
        ]

        if len(candidates) != 1:
            raise SystemExit(
                "Unexpected Gradle archive layout"
            )

        candidate = candidates[0]

        if not (
            (candidate / "bin" / "gradle").is_file()
            and (candidate / "lib").is_dir()
        ):
            raise SystemExit(
                "Gradle archive is missing required files"
            )

        print("Gradle: verifying extracted runtime...", flush=True)
        if not gradle_healthy(candidate):
            raise SystemExit(
                "Gradle staged verification failed. "
                "Existing installation preserved."
            )

        if repair_required:
            quarantined = ROOT / (
                f".quarantine-{version}-"
                f"{os.getpid()}-{time.time_ns()}"
            )
            os.replace(target, quarantined)

        try:
            os.replace(candidate, target)
        except BaseException:
            if quarantined is not None and not target.exists():
                os.replace(quarantined, target)
            raise

else:
    print(
        f"Gradle nightly: current {version}"
    )


binary.chmod(
    binary.stat().st_mode
    | 0o111
)

# Verify the final installed path before activating it.
print("Gradle: verifying installed runtime...", flush=True)
if not gradle_healthy(target):
    if not healthy and target.is_dir():
        shutil.rmtree(target)

        if quarantined is not None:
            os.replace(quarantined, target)

    raise SystemExit(
        "Gradle runtime verification failed. "
        "Previous launcher preserved."
    )

# Do not make gradle-nightly a raw symlink.
#
# The standalone Gradle distribution does not inherit
# Homebrew's Java handling, so give it a deterministic
# JAVA_HOME launcher.

launcher = f"""#!/bin/sh
set -eu

if [ -z "${{JAVA_HOME:-}}" ]; then
    export JAVA_HOME="{java_home}"
fi

export PATH="$JAVA_HOME/bin:$PATH"

exec "{binary}" "$@"
"""

fd, tmp_name = tempfile.mkstemp(
    prefix=f".{LINK.name}.",
    dir=LINK.parent,
)
os.close(fd)
tmp_link = Path(tmp_name)

tmp_link.write_text(
    launcher
)

tmp_link.chmod(
    0o755
)

os.replace(
    tmp_link,
    LINK,
)

if quarantined is not None and quarantined.is_dir():
    try:
        shutil.rmtree(quarantined)
    except OSError as exc:
        print(
            f"Warning: cleanup of damaged installation failed: {exc}",
            file=sys.stderr,
        )


# Keep the current and one previous Nightly.
# Never delete unrelated directories.
installed_versions = sorted(
    (
        p for p in ROOT.iterdir()
        if p.is_dir()
        and not p.is_symlink()
        and re.fullmatch(
            r"\d+(?:\.\d+){1,2}-\d{14}\+\d{4}",
            p.name,
        )
    ),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)

for old in installed_versions[2:]:
    if old == target:
        continue

    try:
        shutil.rmtree(old)
    except OSError as exc:
        print(
            f"Warning: could not clean up {old}: {exc}",
            file=sys.stderr,
        )

print(
    f"Gradle nightly ready: {version}"
)
