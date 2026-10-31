#!/usr/bin/env python3.15
"""macOS Preview Apps updater: Krita Next and manual release diagnostics.

Only official release sources. No sudo, Gatekeeper bypass or silent PKG installs.
For KDE Nightly without a Developer ID, automatic installation requires an explicit
one-time --allow-unsigned-krita opt-in; HTTPS origin is NOT a publisher signature.
"""
from __future__ import annotations

import argparse
import html as html_utils
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.parsers.expat import ExpatError
import platform

HOME = Path.home()
STATE_DIR = HOME / "Library/Application Support/PreviewAppsUpdater"
STATE_FILE = STATE_DIR / "state.json"
CACHE_DIR = HOME / "Library/Caches/PreviewAppsUpdater"
STATUS = {}
VERSION = "7.1.0"
KRITA_APP = Path("/Applications/krita.app")
TOURBOX_APP = Path("/Applications/TourBox Console.app")
BETTERDISPLAY_APP = Path("/Applications/BetterDisplay.app")
BETTERDISPLAY_FEED = "https://betterdisplay.pro/betterdisplay/sparkle/appcast.xml"
BETTERDISPLAY_ID = "pro.betterdisplay.BetterDisplay"
SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
KRITA_URL = "https://cdn.kde.org/ci-builds/graphics/krita/master/macos-universal/"
TOURBOX_URLS = [
    "https://www.tourboxtech.com/jp/downloads/macos/",
    "https://www.tourboxtech.com/kr/downloads/macos",
    "https://tourboxtech.com/en/downloads/macos/",
    "https://www.tourboxtech.com/it/downloads/macos",
]
USER_AGENT = "PreviewAppsUpdater/1.0 (+macOS; KDE & TourBox public downloads)"
KRITA_IMAGE_RE = re.compile(r"^krita-([A-Za-z0-9][A-Za-z0-9.+_-]*)-([0-9a-f]{7,40})\.dmg$", re.I)
TOURBOX_VERSION_RE = re.compile(r"\bv?([0-9]+(?:\.[0-9]+){2})\s+Beta\s+([0-9]+(?:\.[0-9]+)*)\b", re.I)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


class StripHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text: list[str] = []

    def handle_data(self, data):
        self.text.append(data)


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=25) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} for {url}")
        return response.read(3_000_000).decode("utf-8", "replace")


def run(*args: str, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(args, text=True, capture_output=True, check=check)


class AppMetadataError(RuntimeError):
    """Bundle Info.plist cannot be independently parsed; not a network outage."""


_NATIVE_PLIST_REPORTS: set[str] = set()


def bundle_plist(path: Path) -> dict:
    """Read bundle metadata without silently accepting malformed metadata.

    Python's XML parser can reject a vendor-generated plist that the native
    macOS CFPropertyList implementation can nevertheless read.  Never edit the
    mounted DMG or installed application to make a plist parse successfully.
    """
    info = path / "Contents/Info.plist"
    if not info.is_file():
        raise AppMetadataError(f"missing Info.plist: {info}")
    try:
        value = plistlib.loads(info.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("plist root is not a dictionary")
        return value
    except (ValueError, TypeError, OSError, ET.ParseError, ExpatError, plistlib.InvalidFileException) as error:
        python_error = f"{type(error).__name__}: {error}"

    diagnostics = []
    if sys.platform == "darwin":
        native = subprocess.run(
            ["/usr/bin/plutil", "-convert", "binary1", "-o", "-", str(info)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if native.returncode == 0:
            try:
                result = plistlib.loads(native.stdout)
                if not isinstance(result, dict):
                    raise ValueError("native-converted plist is not a dictionary")
                if str(info) not in _NATIVE_PLIST_REPORTS:
                    print(f"Krita metadata: Python XML parser rejected {info}; macOS plutil recovered it")
                    _NATIVE_PLIST_REPORTS.add(str(info))
                return result
            except (ValueError, TypeError, ET.ParseError, ExpatError, plistlib.InvalidFileException) as exc:
                diagnostics.append(f"native conversion produced invalid plist: {exc}")
        else:
            diagnostics.append("plutil: " + native.stderr.decode("utf-8", "replace").strip()[:450])

        # PlistBuddy uses a different native code path and may handle some
        # vendor-generated property lists that cannot be converted by plutil.
        keys = ("CFBundleIdentifier", "CFBundleExecutable", "CFBundleShortVersionString", "CFBundleVersion")
        recovered = {}
        for key in keys:
            native = subprocess.run(
                ["/usr/libexec/PlistBuddy", "-c", f"Print :{key}", str(info)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            if native.returncode == 0:
                recovered[key] = native.stdout.decode("utf-8", "replace").strip()
        if all(recovered.get(key) for key in keys):
            if str(info) not in _NATIVE_PLIST_REPORTS:
                print(f"Krita metadata: Python XML parser rejected {info}; macOS PlistBuddy recovered it")
                _NATIVE_PLIST_REPORTS.add(str(info))
            return recovered
        diagnostics.append("PlistBuddy: required identifier/executable unavailable")

    detail = "; ".join(filter(None, diagnostics))
    raise AppMetadataError(
        f"cannot parse {info}: Python {python_error}"
        + (f"; macOS {detail}" if detail else "")
        + "; original app/DMG kept unchanged"
    )


def app_info(path: Path) -> dict:
    info = path / "Contents/Info.plist"
    if not info.is_file():
        return {}
    p = bundle_plist(path)
    return {
        "version": str(p.get("CFBundleShortVersionString", "?")),
        "build": str(p.get("CFBundleVersion", "?")),
        "bundle_id": str(p.get("CFBundleIdentifier", "?")),
    }


def state_read() -> dict:
    if not STATE_FILE.exists():
        return {}
    with STATE_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError("state.json is not an object")
    return data


def state_write(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def numeric_base_version(value: str) -> tuple[int, ...]:
    return tuple(int(v) for v in re.findall(r"\d+", value.split("-", 1)[0]))


def status(name: str, label: str, detail: str) -> None:
    STATUS[name] = label
    print(f"{name}: {label} — {detail}", flush=True)


def app_is_running(app: Path) -> bool:
    # Inspect executable paths; avoids AppleScript accessibility prompts.
    p = run("/bin/ps", "-awwxo", "comm=", check=False)
    if p.returncode:
        raise RuntimeError("cannot check running applications; refusing to install")
    target = str(app).casefold()
    for line in p.stdout.splitlines():
        command = line.strip().casefold()
        if command.startswith(target + "/"):
            return True
        if app == KRITA_APP and command.endswith("/krita"):
            return True
        if app == TOURBOX_APP and command.endswith("/tourbox console"):
            return True
    return False


def krita_releases() -> list[dict]:
    html = fetch_text(KRITA_URL)
    p = Links()
    p.feed(html)
    found = {}
    for href in p.hrefs:
        name = Path(urllib.parse.urlsplit(href).path).name
        m = KRITA_IMAGE_RE.fullmatch(name)
        if not m:
            continue
        url = urllib.parse.urljoin(KRITA_URL, href)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "cdn.kde.org":
            continue
        found[name] = {"name": name, "version": m.group(1), "sha": m.group(2), "url": url}
        # Apache/KDE directory listing displays build time next to each artifact.
        mt = re.search(re.escape(name) + r"</a>\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", html, re.I)
        if mt:
            found[name]["modified_utc"] = mt.group(1)
    if not found:
        raise RuntimeError("KDE Nightly DMG listing is empty or changed structure")
    if len(found) == 1:
        return list(found.values())
    # If multiple artifacts are listed, use HTTP modification times rather than
    # guessing which Git hash is newer.
    for info in found.values():
        req = urllib.request.Request(info["url"], headers={"User-Agent": USER_AGENT}, method="HEAD")
        with urllib.request.urlopen(req, timeout=25) as response:
            info["modified"] = response.headers.get("Last-Modified", "")
    for info in found.values():
        try:
            info["sort_date"] = parsedate_to_datetime(info["modified"]).timestamp()
        except (ValueError, TypeError, OverflowError):
            info["sort_date"] = 0.0
    return sorted(found.values(), key=lambda x: x["sort_date"], reverse=True)


def tourbox_candidate_links(page_url: str, raw_html: str, base: str, beta: str) -> list[str]:
    """Return only unambiguous installer hyperlinks associated with the beta.

    Links hidden behind opaque JS POST callbacks are deliberately NOT guessed.
    An unrelated stable installer on the same page is never selected.
    """
    # HTML often contains escaped URLs in JSON script data.
    decoded = html_utils.unescape(raw_html).replace("\\/", "/").replace("\\u0026", "&")
    # Some regional templates wrap the base and Beta words in separate tags.
    gap = r"(?:\s|<[^>]{1,300}>)*"
    version = re.search(r"\bv?" + re.escape(base) + gap + r"Beta" + gap + re.escape(beta) + r"\b", decoded, re.I)
    if not version:
        return []
    stable = re.search(r"\bv?\d+\.\d+\.\d+(?:\s+Beta\s+\d+(?:\.\d+)*)?\s*(?:\||<)", decoded[version.end():], re.I)
    end = version.end() + stable.start() if stable else min(len(decoded), version.end() + 80_000)
    # Detect the next unrelated release section as a hard boundary.
    section = decoded[version.start():end]
    found = []
    for candidate in re.findall(r"(?:https?://[^\s\"'<>]+|(?:href|data-url|download-url)\s*=\s*[\"'][^\"']+[\"'])", section, flags=re.I):
        candidate = re.sub(r"^(?:href|data-url|download-url)\s*=\s*[\"']", "", candidate, flags=re.I).strip("\"'\\,; ")
        candidate = urllib.parse.urljoin(page_url, candidate)
        u = urllib.parse.urlsplit(candidate)
        if (u.scheme != "https" or
                (u.hostname or "").lower() not in {"cdn.tourboxtech.com", "r.tourboxtech.com", "www.tourboxtech.com", "tourboxtech.com"} or
                not u.path.lower().endswith((".pkg", ".dmg"))):
            continue
        if candidate not in found:
            found.append(candidate)
    # No guessing between multiple artifacts. In particular a generic
    # 'Setup.pkg' could be the stable build.
    if len(found) != 1:
        return []
    # Require beta identity in download URL OR an explicit real hyperlink
    # present in the matched beta section. No generic JS string alone.
    url = found[0]
    p = Links()
    p.feed(section)
    href_targets = {urllib.parse.urljoin(page_url, html_utils.unescape(x)) for x in p.hrefs}
    if url not in href_targets:
        name = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lower()
        beta_marker = re.sub(r"[^a-z0-9]", "", f"beta{beta}".lower())
        simplified = re.sub(r"[^a-z0-9]", "", name)
        if beta_marker not in simplified:
            return []
    return [url]


def tourbox_pkg_team(pkg: Path) -> str | None:
    result = run("/usr/sbin/pkgutil", "--check-signature", str(pkg), check=False)
    if result.returncode != 0:
        return None
    text = result.stdout + "\n" + result.stderr
    # Apple pkgutil output identifies the signing certificate by team in ()
    matches = re.findall(r"Developer ID Installer:.*?\(([A-Z0-9]{10})\)", text)
    return matches[-1] if matches else None


def executable_digest(app: Path) -> str | None:
    info_file = app / "Contents/Info.plist"
    if not info_file.is_file():
        return None
    name = bundle_plist(app).get("CFBundleExecutable")
    if not isinstance(name, str) or "/" in name or name in (".", ".."):
        return None
    path = app / "Contents/MacOS" / name
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_official(url: str, dest: Path, hosts: set[str], *, max_bytes: int) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in hosts:
        raise RuntimeError("untrusted download URL")
    tmp = dest.with_suffix(dest.suffix + ".part")
    total = 0
    sha = hashlib.sha256()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=90) as response:
            final = urllib.parse.urlsplit(response.url)
            if response.status != 200 or final.scheme != "https" or final.hostname not in hosts:
                raise RuntimeError("download redirected outside approved host or returned an error")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise RuntimeError("download too large")
            with tmp.open("wb") as fp:
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > max_bytes:
                        raise RuntimeError("download exceeded size limit")
                    fp.write(block)
                    sha.update(block)
        if total < 1_000_000:
            raise RuntimeError("download unexpectedly small")
        os.replace(tmp, dest)
        return sha.hexdigest()
    finally:
        tmp.unlink(missing_ok=True)


def tourbox_releases() -> list[dict]:
    results = []
    errors = []
    for url in TOURBOX_URLS:
        try:
            raw_html = fetch_text(url)
            parser = StripHTML()
            parser.feed(raw_html)
            text = " ".join(parser.text)
            m = TOURBOX_VERSION_RE.search(text)
            if m:
                results.append({"base": m.group(1), "beta": m.group(2), "url": url,
                                "installers": tourbox_candidate_links(url, raw_html, m.group(1), m.group(2))})
            else:
                errors.append(f"{url}: no Beta version found")
        except Exception as e:
            errors.append(f"{url}: {e}")
    if not results:
        raise RuntimeError("all official TourBox download pages unavailable: " + "; ".join(errors))
    def version_key(x):
        return tuple(int(v) for v in x["base"].split(".")) + tuple(int(v) for v in x["beta"].split("."))
    return sorted(results, key=version_key, reverse=True)


def identity(path: Path) -> str | None:
    r = run("/usr/bin/codesign", "-dv", "--verbose=4", str(path), check=False)
    report = r.stdout + "\n" + r.stderr
    m = re.search(r"^TeamIdentifier=([A-Z0-9]+)$", report, re.M)
    return m.group(1) if r.returncode == 0 and m else None


def bundle_fingerprint(app: Path) -> tuple[str | None, str | None, str | None]:
    """Comparable build identity; not a proof of publisher authenticity."""
    info = app / "Contents/Info.plist"
    return (executable_digest(app), file_sha256(info) if info.is_file() else None, code_hash(app))


def usable_krita_candidate(candidate: Path, local: Path, *, allow_unsigned: bool,
                           permit_signing_transition: bool = False) -> tuple[bool, str | None, str]:
    """Admit signed same-Team releases, or explicit KDE-CDN unsigned trust.

    Gatekeeper assessment remains mandatory for Developer-ID signed artifacts.
    No quarantine clearing, spctl exceptions or ad-hoc signing is attempted.
    """
    if app_info(candidate).get("bundle_id") != "org.krita":
        return False, None, "unexpected Krita bundle identifier"
    incoming = identity(candidate)
    existing = identity(local)
    if existing and incoming and existing == incoming:
        if run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(candidate), check=False).returncode:
            return False, incoming, "invalid Developer ID signature"
        if run("/usr/sbin/spctl", "--assess", "--type", "execute", str(candidate), check=False).returncode:
            return False, incoming, "Gatekeeper rejected signed Krita Nightly"
        return True, incoming, "developer-id"
    if existing or incoming:
        if not (permit_signing_transition and allow_unsigned and existing and not incoming):
            return False, incoming, "publisher identity changed; explicit bootstrap required for signed-to-unsigned transition"
        # Only a user-invoked bootstrap may turn a signed stable app into an
        # unsigned official KDE Nightly. Subsequent Topgrade never switches.
        return True, None, "kde-unsigned-explicit-transition"
    if not allow_unsigned:
        return False, None, "KDE Nightly lacks matching Developer Team ID; use --allow-unsigned-krita explicitly"
    return True, None, "kde-unsigned-explicit-opt-in"


def code_hash(app: Path) -> str | None:
    r = run("/usr/bin/codesign", "-dv", "--verbose=4", str(app), check=False)
    if r.returncode:
        return None
    m = re.search(r"^CDHash=([0-9a-fA-F]+)$", r.stdout + "\n" + r.stderr, re.M)
    return m.group(1).lower() if m else None


def download_dmg(url: str, dest: Path) -> str:
    return download_official(url, dest,
                             {"cdn.kde.org", "origin.cdn.kde.org"},
                             max_bytes=1_500_000_000)


def mounted_app(mount: Path) -> Path:
    """Find Krita inside the *mounted DMG*, never inside host Applications.

    DMGs commonly contain an ``Applications`` symlink to /Applications.
    Path.glob("*/*.app") will walk that link and could mistake the host's
    installed Krita for a file provided by KDE.  This is a security boundary:
    do not inspect or install app bundles reached through symbolic links.
    """
    root = mount.resolve(strict=True)
    candidates = list(mount.glob("*.app"))
    for folder in mount.iterdir():
        # Finder's drag-to-Applications shortcut points outside the disk image.
        if folder.is_symlink() or not folder.is_dir():
            continue
        candidates.extend(folder.glob("*.app"))

    matches = []
    parse_failures = []
    seen = set()
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            real = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not real.is_relative_to(root) or real in seen:
            continue
        seen.add(real)
        try:
            if app_info(candidate).get("bundle_id") == "org.krita":
                matches.append(candidate)
        except AppMetadataError as exc:
            parse_failures.append(str(exc))

    if len(matches) != 1:
        detail = "; ".join(parse_failures)
        raise AppMetadataError(
            f"expected exactly one org.krita app within DMG filesystem; got {len(matches)}"
            + (f"; invalid bundle metadata: {detail}" if detail else "")
        )
    return matches[0]


def apply_krita(release: dict, state: dict, *, allow_unsigned: bool = False,
                permit_signing_transition: bool = False) -> str:
    if not KRITA_APP.is_dir():
        status("Krita Next", "DEFERRED", "Krita installation path missing")
        return "deferred"
    if app_is_running(KRITA_APP):
        status("Krita Next", "DEFERRED", "Krita is running; close it before installing")
        return "deferred"
    if not os.access(KRITA_APP.parent, os.W_OK):
        status("Krita Next", "DEFERRED", "no write permission to /Applications; no sudo used")
        return "deferred"
    original = app_info(KRITA_APP)
    if original.get("bundle_id") != "org.krita":
        raise RuntimeError("installed Krita has unexpected bundle identifier")
    old_team = identity(KRITA_APP)
    with tempfile.TemporaryDirectory(prefix="preview-krita-") as td:
        root = Path(td)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        dmg = CACHE_DIR / release["name"]
        digest = file_sha256(dmg) if dmg.is_file() else download_dmg(release["url"], dmg)
        verify = run("/usr/bin/hdiutil", "verify", str(dmg), check=False)
        if verify.returncode != 0:
            raise RuntimeError("hdiutil verify failed: " + verify.stderr[-500:])
        mount = root / "mounted"
        mount.mkdir()
        attached = False
        try:
            p = run("/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount), str(dmg), check=False)
            if p.returncode != 0:
                raise RuntimeError("hdiutil attach failed: " + p.stderr[-500:])
            attached = True
            candidate = mounted_app(mount)
            admitted, new_team, trust_mode = usable_krita_candidate(
                candidate, KRITA_APP, allow_unsigned=allow_unsigned,
                permit_signing_transition=permit_signing_transition)
            if not admitted:
                status("Krita Next", "DEFERRED", trust_mode)
                return "deferred"
            nonce = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            staged = KRITA_APP.with_name(".krita.preview-stage-" + nonce + ".app")
            backup = KRITA_APP.with_name(".krita.preview-backup-" + nonce + ".app")
            if staged.exists() or backup.exists():
                raise RuntimeError("staging or backup path already exists")
            try:
                run("/usr/bin/ditto", str(candidate), str(staged))
                if app_info(staged).get("bundle_id") != "org.krita":
                    raise RuntimeError("staged app identity mismatch")
                if trust_mode == "developer-id":
                    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(staged))
                if bundle_fingerprint(staged) != bundle_fingerprint(candidate):
                    raise RuntimeError("staged Krita does not match the verified official DMG")
                if app_is_running(KRITA_APP):
                    status("Krita Next", "DEFERRED", "Krita started during download; keeping current app")
                    return "deferred"
                KRITA_APP.rename(backup)
                try:
                    staged.rename(KRITA_APP)
                except Exception:
                    backup.rename(KRITA_APP)
                    raise
            finally:
                if staged.exists():
                    shutil.rmtree(staged)
            state["krita"] = {
                "sha": release["sha"],
                "version": app_info(KRITA_APP).get("version"),
                "inode": KRITA_APP.stat().st_ino,
                "executable_sha256": executable_digest(KRITA_APP),
                "team": new_team,
                "trust_mode": trust_mode,
                "allow_unsigned": bool(allow_unsigned),
                "info_plist_sha256": bundle_fingerprint(KRITA_APP)[1],
                "source_url": release["url"],
                "sha256": digest,
                "modified_utc": release.get("modified_utc"),
                "installed_utc": datetime.now(timezone.utc).isoformat(),
            }
            try:
                state_write(state)
            except Exception:
                # If state cannot be committed, roll the application back.
                rollback = KRITA_APP.with_name(".krita.preview-failed-" + nonce + ".app")
                KRITA_APP.rename(rollback)
                backup.rename(KRITA_APP)
                shutil.rmtree(rollback)
                raise
            shutil.rmtree(backup)
            status("Krita Next", "UPDATED", f"{release['sha']} installed; previous application removed")
            return "updated"
        finally:
            if attached:
                run("/usr/bin/hdiutil", "detach", str(mount), check=False)



def _krita_relation_from_base(local_sha, remote_sha, base_sha):
    if not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", base_sha):
        raise RuntimeError("invalid Git merge-base response")
    if base_sha.lower().startswith(local_sha.lower()):
        return "newer"
    if base_sha.lower().startswith(remote_sha.lower()):
        return "older"
    return "diverged"


def _krita_relation_official(local_sha, remote_sha):
    params = urllib.parse.urlencode([
        ("refs[]", local_sha), ("refs[]", remote_sha)
    ])
    url = (
        "https://invent.kde.org/api/v4/projects/"
        "graphics%2Fkrita/repository/merge_base?" + params
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        if (response.status != 200 or
                urllib.parse.urlsplit(response.geturl()).hostname != "invent.kde.org"):
            raise RuntimeError("unexpected KDE API response")
        raw = response.read(32769)
        if len(raw) > 32768:
            raise RuntimeError("KDE response too large")
    data = json.loads(raw)
    return _krita_relation_from_base(local_sha, remote_sha, data.get("id"))


def _krita_relation_git(local_sha, remote_sha):
    """Independent Git ancestry check, with no persistent clone."""
    binary = shutil.which("git")
    if not binary:
        raise RuntimeError("Git executable unavailable")

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    def git(*argv):
        try:
            return subprocess.run(
                [binary, *argv],
                capture_output=True, text=True,
                timeout=90, env=env, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Git transport failed: {exc}") from exc

    with tempfile.TemporaryDirectory(prefix="krita-git-check-") as td:
        repo = Path(td) / "krita"
        initial = git(
            "clone", "--quiet", "--no-checkout", "--single-branch",
            "--branch", "master", "--filter=blob:none", "--depth=128",
            "https://invent.kde.org/graphics/krita.git", str(repo)
        )
        if initial.returncode:
            raise RuntimeError(
                "KDE Git clone failed: " + initial.stderr.strip()[-400:]
            )

        for deepen in (0, 384, 1536):
            if deepen:
                step = git(
                    "-C", str(repo), "fetch", "--quiet", "--no-tags",
                    f"--deepen={deepen}", "origin", "master"
                )
                if step.returncode:
                    raise RuntimeError(
                        "Git history fetch failed: " + step.stderr.strip()[-400:]
                    )

            available = all(
                git("-C", str(repo), "cat-file", "-e",
                    sha + "^{commit}").returncode == 0
                for sha in (local_sha, remote_sha)
            )
            if not available:
                continue

            def ancestor(first, second):
                result = git(
                    "-C", str(repo), "merge-base", "--is-ancestor",
                    first, second
                )
                if result.returncode not in (0, 1):
                    raise RuntimeError("Git ancestry comparison failed")
                return result.returncode == 0

            if ancestor(local_sha, remote_sha):
                return "newer"
            if ancestor(remote_sha, local_sha):
                return "older"
            return "diverged"

    raise RuntimeError("commits not found within bounded KDE Git history")


def krita_commit_relation(local_sha: str, remote_sha: str) -> str:
    for sha in (local_sha, remote_sha):
        if not isinstance(sha, str) or not re.fullmatch(
            r"[0-9a-fA-F]{7,40}", sha
        ):
            raise ValueError("invalid Krita Git SHA")

    if local_sha.lower() == remote_sha.lower():
        return "same"

    try:
        return _krita_relation_official(local_sha, remote_sha)
    except (urllib.error.URLError, TimeoutError, OSError,
            ValueError, RuntimeError, KeyError, TypeError) as api_error:
        try:
            return _krita_relation_git(local_sha, remote_sha)
        except RuntimeError as git_error:
            raise RuntimeError(
                f"KDE API unavailable: {api_error}; "
                f"Git verification unavailable: {git_error}"
            ) from git_error



def check_krita(args, state: dict) -> bool:
    print("\n▶ Krita Next / Qt6")
    release = krita_releases()[0]
    installed = app_info(KRITA_APP)
    if installed.get("bundle_id") != "org.krita":
        status("Krita Next", "UNVERIFIED", "Krita missing or unexpected app identifier")
        return False
    print(f"Installed: {installed.get('version')} (no Git SHA in Info.plist)")
    print(f"Official:  {release['name']} — {release['url']}")
    recorded = state.get("krita", {})
    unsigned_approved = bool(args.allow_unsigned_krita or recorded.get("allow_unsigned"))
    trusted = (recorded.get("inode") == KRITA_APP.stat().st_ino
               and recorded.get("version") == installed.get("version")
               and recorded.get("executable_sha256") is not None
               and recorded.get("executable_sha256") == executable_digest(KRITA_APP)
               and recorded.get("team") == identity(KRITA_APP)
               and (recorded.get("info_plist_sha256") in (None, bundle_fingerprint(KRITA_APP)[1]))
               and isinstance(recorded.get("sha"), str))
    bootstrap = args.bootstrap_krita
    if args.initialize and not trusted:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        image = CACHE_DIR / release["name"]
        if not image.is_file():
            download_dmg(release["url"], image)
        if run("/usr/bin/hdiutil", "verify", str(image), check=False).returncode:
            image.unlink(missing_ok=True)
            raise RuntimeError("Krita official image failed verification; corrupt cache removed")
        args.verify_krita = True
    if args.ack_krita:
        status("Krita Next", "UNVERIFIED", "--ack-krita no longer trusts a version string; use --bootstrap-krita or --verify-krita")
        return False
    if args.verify_krita:
        image = CACHE_DIR / release["name"]
        if not image.is_file():
            status("Krita Next", "UNVERIFIED", "download official DMG first with --download-krita")
            return False
        signed = run("/usr/bin/hdiutil", "verify", str(image), check=False)
        if signed.returncode != 0:
            raise RuntimeError("cached official Krita DMG is damaged")
        with tempfile.TemporaryDirectory(prefix="preview-krita-compare-") as td:
            mount = Path(td) / "mounted"
            mount.mkdir()
            attached = False
            try:
                result = run("/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount), str(image), check=False)
                if result.returncode != 0:
                    raise RuntimeError("cannot mount cached Krita DMG")
                attached = True
                candidate = mounted_app(mount)
                admitted, team, trust_mode = usable_krita_candidate(
                    candidate, KRITA_APP, allow_unsigned=unsigned_approved)
                if not admitted:
                    status("Krita Next", "DEFERRED", trust_mode)
                    return False
                # Match both executable bytes and Info.plist. This is build
                # equality, not cryptographic publisher attribution for unsigned KDE.
                if (not bundle_fingerprint(candidate)[0] or
                        bundle_fingerprint(candidate) != bundle_fingerprint(KRITA_APP) or
                        installed["version"] != app_info(candidate).get("version")):
                    status("Krita Next", "UNVERIFIED", "installed build differs from official DMG; explicit --bootstrap-krita required")
                    return False
                state["krita"] = {
                    "sha": release["sha"], "version": installed["version"],
                    "inode": KRITA_APP.stat().st_ino,
                    "executable_sha256": executable_digest(KRITA_APP),
                    "team": team, "trust_mode": trust_mode,
                    "allow_unsigned": unsigned_approved,
                    "info_plist_sha256": bundle_fingerprint(KRITA_APP)[1],
                    "source_url": release["url"],
                    "modified_utc": release.get("modified_utc"),
                    "verified_by": "same-codesign-cdhash",
                    "installed_utc": datetime.now(timezone.utc).isoformat(),
                }
                state_write(state)
                status("Krita Next", "CURRENT", f"local CDHash matches official {release['sha']}")
                return False
            finally:
                if attached:
                    run("/usr/bin/hdiutil", "detach", str(mount), check=False)
    if args.download_krita:
        cache = CACHE_DIR
        cache.mkdir(parents=True, exist_ok=True)
        image = cache / release["name"]
        if not image.exists():
            digest = download_dmg(release["url"], image)
        else:
            digest = file_sha256(image)
        verified = run("/usr/bin/hdiutil", "verify", str(image), check=False)
        if verified.returncode != 0:
            image.unlink(missing_ok=True)
            raise RuntimeError("Krita downloaded DMG failed hdiutil integrity verification")
        status("Krita Next", "DOWNLOADED", f"{image} (SHA256 {digest}); use --verify-krita to compare installed build")
        return False
    if not trusted and not bootstrap:
        status("Krita Next", "UNVERIFIED", "unknown Git SHA; use --initialize to compare signed build, or explicit --bootstrap-krita")
        return False
    if trusted and recorded["sha"].lower() == release["sha"].lower():
        status("Krita Next", "CURRENT", f"Git {release['sha']}")
        return False
    if trusted and args.topgrade and not bootstrap:
        try:
            relation = krita_commit_relation(recorded["sha"], release["sha"])
        except (ValueError, RuntimeError, KeyError, TimeoutError,
                urllib.error.URLError) as exc:
            status("Krita Next", "DEFERRED",
                   f"cannot verify Git ancestry: {exc}")
            return False

        if relation in {"older", "same"}:
            status("Krita Next", "DEFERRED_NOT_NEWER",
                   f"official Git {release['sha']} is not newer than installed Git {recorded['sha']}")
            return False

        if relation != "newer":
            status("Krita Next", "DEFERRED",
                   "Git history diverged; refusing unattended replacement")
            return False
    if not bootstrap and recorded.get("version") and numeric_base_version(recorded["version"]) > numeric_base_version(release["version"]):
        status("Krita Next", "DEFERRED_NOT_NEWER", "official build version is older than installed version")
        return False
    if not (args.apply or args.topgrade or bootstrap):
        status("Krita Next", "AVAILABLE", f"new build {release['sha']}; run --apply to install")
        return False
    if bootstrap:
        print("BOOTSTRAP: local build SHA unknown; user explicitly authorized switching to latest KDE listing")
    result = apply_krita(release, state, allow_unsigned=unsigned_approved,
                         permit_signing_transition=bool(bootstrap and args.allow_unsigned_krita))
    return result == "updated"


def expected_tourbox_from_pkg(pkg: Path, expected_base: str, team: str) -> dict | None:
    """Read signed app from an expanded installer; only for trusted PKG.

    Some vendor PKGs install via scripts rather than a normal Payload: those
    cannot be proven and require human confirmation instead.
    """
    with tempfile.TemporaryDirectory(prefix="tourbox-pkg-inspect-") as td:
        extracted = Path(td) / "expanded"
        p = run("/usr/sbin/pkgutil", "--expand-full", str(pkg), str(extracted), check=False)
        if p.returncode:
            return None
        apps = []
        for app in extracted.rglob("TourBox Console.app"):
            if app.is_dir() and app_info(app).get("bundle_id") == "com.tourbox.ui.launch":
                apps.append(app)
        if len(apps) != 1:
            return None
        app = apps[0]
        if (app_info(app).get("version") != expected_base or identity(app) != team or
                run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app), check=False).returncode):
            return None
        cdhash = code_hash(app)
        digest = executable_digest(app)
        if not cdhash or not digest:
            return None
        return {"cdhash": cdhash, "executable_sha256": digest}


def tourbox_prepare(release: dict, state: dict, *, launch_installer: bool) -> None:
    """Verified official PKG -> system Installer UI; no direct privileged writes."""
    url = release.get("installer_url")
    if not url:
        status("TourBox", "DEFERRED", "official Beta PKG link not safely resolvable; cannot automate download")
        return
    if not url.lower().split("?", 1)[0].endswith(".pkg"):
        status("TourBox", "DEFERRED", "official artifact is not a direct PKG; cannot safely invoke Installer")
        return
    if app_is_running(TOURBOX_APP):
        status("TourBox", "DEFERRED", "TourBox is running; quit it before opening macOS Installer")
        return
    old_team = identity(TOURBOX_APP)
    if not old_team:
        status("TourBox", "DEFERRED", "cannot read the existing app's signing Team ID")
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    release_id = f"{release['base']} Beta {release['beta']}"
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", release_id)
    pkg = CACHE_DIR / f"TourBox-{safe_name}.pkg"
    if not pkg.is_file():
        digest = download_official(url, pkg,
                                   {"cdn.tourboxtech.com", "r.tourboxtech.com", "www.tourboxtech.com", "tourboxtech.com"},
                                   max_bytes=1_500_000_000)
    else:
        digest = file_sha256(pkg)
    pkg_team = tourbox_pkg_team(pkg)
    if not pkg_team or pkg_team != old_team:
        status("TourBox", "DEFERRED", "official PKG Installer Team ID missing or differs from installed app")
        return
    if run("/usr/sbin/spctl", "--assess", "--type", "install", str(pkg), check=False).returncode:
        status("TourBox", "DEFERRED", "Gatekeeper rejected downloaded TourBox PKG")
        return
    before = executable_digest(TOURBOX_APP)
    if not before:
        status("TourBox", "DEFERRED", "installed app executable cannot be fingerprinted")
        return
    expected = expected_tourbox_from_pkg(pkg, release["base"], pkg_team)
    if (expected is not None and before == expected["executable_sha256"] and
            code_hash(TOURBOX_APP) == expected["cdhash"] and
            app_info(TOURBOX_APP).get("version") == release["base"]):
        state["tourbox"] = {
            "release": release_id, "installed_version": release["base"],
            "executable_sha256": before, "team": pkg_team,
            "official_pkg_sha256": digest,
            "confirmed_utc": datetime.now(timezone.utc).isoformat(),
            "confirmation": "installed-app-matches-vendor-signed-package"}
        state.pop("tourbox_pending", None)
        state_write(state)
        status("TourBox", "CURRENT", release_id + " (exact local binary matches official signed PKG)")
        return
    if expected is None:
        print("TourBox: embedded signed app could not be independently identified; post-install will need confirmation")
    if not launch_installer:
        status("TourBox", "READY", f"signed official PKG downloaded: {pkg}; use --apply-tourbox")
        return
    pending = {"release": release_id, "base": release["base"],
               "beta": release["beta"], "url": url, "page": release["url"],
               "pkg_sha256": digest, "team": pkg_team,
               "before_binary_sha256": before, "expected_app": expected, "path": str(pkg),
               "created_utc": datetime.now(timezone.utc).isoformat()}
    state["tourbox_pending"] = pending
    state_write(state)
    opened = run("/usr/bin/open", "-a", "Installer", str(pkg), check=False)
    if opened.returncode:
        raise RuntimeError("cannot open macOS Installer: " + opened.stderr[-400:])
    status("TourBox", "AWAITING_AUTH", "official signed PKG opened in macOS Installer; authorization/install not yet verified")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_tourbox(args, state: dict) -> None:
    print("\n▶ TourBox Console / Beta")
    installed = app_info(TOURBOX_APP)
    releases = tourbox_releases()
    latest = releases[0]
    if installed.get("bundle_id") != "com.tourbox.ui.launch":
        status("TourBox", "UNVERIFIED", "app not found at expected path")
        return
    release_id = latest["base"] + " Beta " + latest["beta"]
    print(f"Installed: {installed['version']} (Beta number not exposed in Info.plist)")
    print(f"Official:  {release_id} — {latest['url']}")
    others = [r for r in releases[1:] if (r["base"], r["beta"]) != (latest["base"], latest["beta"])]
    if others:
        print("Note: official regional pages disagree; selecting highest Beta without assuming older regional links are valid")
    # Collect links only from the highest-beta official pages. Never mix a
    # lower-beta download URL with a higher-beta version number.
    urls = {link for release in releases if (release["base"], release["beta"]) == (latest["base"], latest["beta"])
            for link in release.get("installers", [])}
    if len(urls) == 1:
        latest["installer_url"] = next(iter(urls))
        print("Official direct installer:", latest["installer_url"])
    else:
        print("Official direct installer: not uniquely published in inspectable HTML")
    saved = state.get("tourbox", {})
    # The old v1 state had no executable fingerprint; never trust it.
    fingerprint = executable_digest(TOURBOX_APP)
    same_publisher = identity(TOURBOX_APP)
    proven = (saved.get("release") == release_id
              and saved.get("installed_version") == installed["version"]
              and isinstance(fingerprint, str)
              and saved.get("executable_sha256") == fingerprint
              and saved.get("team") == same_publisher)
    pending = state.get("tourbox_pending", {})
    if pending.get("release") == release_id:
        changed = bool(fingerprint and fingerprint != pending.get("before_binary_sha256"))
        expected = pending.get("expected_app")
        exact = (isinstance(expected, dict) and
                 expected.get("executable_sha256") == fingerprint and
                 expected.get("cdhash") == code_hash(TOURBOX_APP))
        if changed and installed["version"] == latest["base"] and same_publisher == pending.get("team"):
            if getattr(args, "check", False) and exact:
                status("TourBox", "READY", "verified installed binary matches pending official package; next Topgrade will finalize state")
                return
            if exact or args.ack_tourbox:
                # Exact signed binary is cryptographically attributable to
                # payload from the vendor's signed Beta PKG; otherwise a user
                # attestation + changed binary is required.
                state["tourbox"] = {
                    "release": release_id, "installed_version": installed["version"],
                    "executable_sha256": fingerprint, "team": same_publisher,
                    "official_pkg_sha256": pending["pkg_sha256"],
                    "confirmed_utc": datetime.now(timezone.utc).isoformat(),
                    "confirmation": "signed-pkg-payload-matches-installed-app" if exact else "user-attested-signed-pkg"}
                state.pop("tourbox_pending", None)
                state_write(state)
                status("TourBox", "CURRENT", release_id + " (verified signed install)" if exact else release_id + " (user confirmed signed install)")
                return
            status("TourBox", "UNVERIFIED", "app changed but PKG payload was not verifiable; --ack-tourbox only after confirming Installer completed")
            return
        if args.ack_tourbox:
            status("TourBox", "UNVERIFIED", "PKG install not proven: app executable unchanged, base version mismatched, or signing changed")
            return
        status("TourBox", "AWAITING_AUTH", "Installer was launched; app not yet replaced; will not reopen on each Topgrade run")
        return
    if args.ack_tourbox:
        status("TourBox", "UNVERIFIED", "no pending verified signed PKG; manual claims cannot establish Beta build identity")
        return
    if proven:
        status("TourBox", "CURRENT", release_id + " (previous signed PKG + confirmation)")
        return
    if numeric_base_version(installed["version"]) > numeric_base_version(latest["base"]):
        status("TourBox", "UNVERIFIED", "installed base version is newer than official pages; refusing downgrade")
        return
    if args.open_tourbox:
        run("/usr/bin/open", latest["url"], check=False)
    if not (args.apply_tourbox or args.topgrade):
        status("TourBox", "UNVERIFIED/AVAILABLE", "unknown local Beta number; use --apply-tourbox to prepare official Installer")
        return
    tourbox_prepare(latest, state, launch_installer=True)


def fetch_betterdisplay_feed(url: str = BETTERDISPLAY_FEED) -> bytes:
    # BetterDisplay maintainer explicitly recommends this Sparkle appcast,
    # rather than GitHub Releases (which omit some internal test builds).
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml, text/xml"})
    with urllib.request.urlopen(req, timeout=25) as response:
        if response.status != 200 or urllib.parse.urlsplit(response.url).hostname != "betterdisplay.pro":
            raise RuntimeError("BetterDisplay official feed not reachable via expected HTTPS host")
        raw = response.read(3_000_001)
    if len(raw) > 3_000_000:
        raise RuntimeError("BetterDisplay appcast is unexpectedly large")
    return raw


def _numbers(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text or ""))


def parse_betterdisplay_feed(raw: bytes, system_version: str) -> list[dict]:
    root = ET.fromstring(raw)
    installed_os = _numbers(system_version)
    if not installed_os:
        raise RuntimeError("Cannot determine local macOS version")
    # macOS arm64 receives its own internal/pre/stable channels; legacy
    # universal channels are also eligible. Never fall back to a different
    # architecture or treat latest GitHub stable as latest internal.
    eligible = {"", "pre", "internal", "arm64", "arm64_pre", "arm64_internal"}
    found = []
    for item in root.findall(".//item"):
        ch = (item.findtext(f"{{{SPARKLE_NS}}}channel") or "").strip()
        if ch not in eligible:
            continue
        lower = item.findtext(f"{{{SPARKLE_NS}}}minimumSystemVersion") or ""
        upper = item.findtext(f"{{{SPARKLE_NS}}}maximumSystemVersion") or ""
        if lower and installed_os < _numbers(lower):
            continue
        if upper and installed_os > _numbers(upper):
            continue
        enc = item.find("enclosure")
        if enc is None:
            continue
        ver = (enc.get(f"{{{SPARKLE_NS}}}version") or
               item.findtext(f"{{{SPARKLE_NS}}}version") or "").strip()
        pretty = (enc.get(f"{{{SPARKLE_NS}}}shortVersionString") or
                  item.findtext(f"{{{SPARKLE_NS}}}shortVersionString") or ver).strip()
        link = enc.get("url", "")
        u = urllib.parse.urlsplit(link)
        if not ver or not _numbers(ver) or u.scheme != "https":
            continue
        found.append({"version": ver, "pretty": pretty, "channel": ch or "universal-stable",
                      "url": link, "title": item.findtext("title") or ""})
    # Numeric version first, internal channel on ties; Sparkle remains the
    # authority for channel eligibility, delta selection and signatures.
    rank = {"arm64_internal": 6, "internal": 5, "arm64_pre": 4,
            "pre": 3, "arm64": 2, "universal-stable": 1}
    return sorted(found, key=lambda x: (_numbers(x["version"]), rank.get(x["channel"], 0)), reverse=True)


def betterdisplay_native_auto() -> None:
    info = app_info(BETTERDISPLAY_APP)
    if info.get("bundle_id") != BETTERDISPLAY_ID:
        raise RuntimeError("BetterDisplay missing or unexpected bundle ID; preferences unchanged")
    for key in ("SUEnableAutomaticChecks", "SUAutomaticallyUpdate"):
        completed = run("/usr/bin/defaults", "write", BETTERDISPLAY_ID, key, "-bool", "true", check=False)
        if completed.returncode:
            raise RuntimeError(f"cannot set Sparkle {key}: {completed.stderr.strip()}")
    print("BetterDisplay: enabled Sparkle automatic checks and background download/install preference")
    print("BetterDisplay: native internal pre-release selection is app-specific and was NOT changed")
    print("BetterDisplay: preferences may take effect after app restart; no restart forced")


def sparkle_native_status() -> str:
    """Audit documented Sparkle preferences; vendor private channel remains unknown."""
    parts = []
    for key in ("SUEnableAutomaticChecks", "SUAutomaticallyUpdate"):
        proc = run("/usr/bin/defaults", "read", BETTERDISPLAY_ID, key, check=False)
        if proc.returncode:
            value = "UNKNOWN"
        elif proc.stdout.strip().lower() in {"1", "true", "yes"}:
            value = "ON"
        elif proc.stdout.strip().lower() in {"0", "false", "no"}:
            value = "OFF"
        else:
            value = "UNKNOWN"
        parts.append(f"{key}={value}")
    return ", ".join(parts)


def check_betterdisplay(args, state: dict) -> None:
    print("\n▶ BetterDisplay / Internal Pre-release")
    installed = app_info(BETTERDISPLAY_APP)
    if installed.get("bundle_id") != BETTERDISPLAY_ID:
        status("BetterDisplay", "UNVERIFIED", "expected /Applications/BetterDisplay.app with publisher bundle ID")
        return
    if args.enable_betterdisplay_auto:
        betterdisplay_native_auto()
    official = parse_betterdisplay_feed(fetch_betterdisplay_feed(), platform.mac_ver()[0])
    if not official:
        status("BetterDisplay", "RETRYABLE/FAILED", "official Sparkle feed contains no compatible arm64 update items")
        return
    latest = official[0]
    internal = [r for r in official if r["channel"] in {"internal", "arm64_internal"}]
    previews = [r for r in official if r["channel"] in {"pre", "arm64_pre"}]
    print(f"Installed: {installed['version']} (build {installed['build']})")
    print(f"Official:  {latest['pretty']} (build {latest['version']}, channel {latest['channel']})")
    print(f"Source:    {BETTERDISPLAY_FEED}")
    if internal:
        i = internal[0]
        print(f"Internal feed: {i['pretty']} (build {i['version']}, channel {i['channel']})")
    else:
        print("Internal feed: no compatible internal item published in current appcast")
    if previews:
        p = previews[0]
        print(f"Public pre-release feed: {p['pretty']} (build {p['version']}, channel {p['channel']})")
    print("Native Sparkle preferences:", sparkle_native_status())
    print("Native internal-update preference: UNVERIFIABLE externally (select in BetterDisplay Settings > Application > Updates)")
    actual = _numbers(installed["build"])
    remote = _numbers(latest["version"])
    if not actual:
        status("BetterDisplay", "UNVERIFIED", "CFBundleVersion cannot be compared; native Sparkle updater is authoritative")
    elif remote > actual:
        status("BetterDisplay", "AVAILABLE_IN_FEED", "newer compatible build in official feed; native Sparkle eligibility and installation not independently verified")
    elif remote == actual:
        status("BetterDisplay", "FEED_CURRENT/CHANNEL_UNVERIFIED", "build matches newest compatible feed item; internal preference and installed channel unknown")
    else:
        status("BetterDisplay", "LOCAL_NEWER_THAN_FEED", "installed build is newer than compatible feed results; preserving it")


def self_test() -> None:
    p = Links()
    p.feed('<a href="krita-6.1.0-prealpha-32bfa3f.dmg">x</a>')
    assert len(p.hrefs) == 1
    assert KRITA_IMAGE_RE.fullmatch(p.hrefs[0]).group(2) == "32bfa3f"
    h = StripHTML()
    h.feed('<h2>TourBox Console</h2><p>v5.12.0 Beta 10.2|2026-09-30</p>')
    assert TOURBOX_VERSION_RE.search(" ".join(h.text)).groups() == ("5.12.0", "10.2")
    sample = ('<h2>TourBox Console</h2>v5.12.0 Beta 10.2'
              '<a href="https://cdn.tourboxtech.com/console-beta102.pkg">Download</a>'
              '<h2>TourBox Console</h2>v5.11.3|2026-06-12'
              '<a href="https://cdn.tourboxtech.com/console-stable.pkg">Download</a>')
    assert tourbox_candidate_links(TOURBOX_URLS[0], sample, "5.12.0", "10.2") == ["https://cdn.tourboxtech.com/console-beta102.pkg"]
    assert not tourbox_candidate_links(TOURBOX_URLS[0], '<h2>TourBox</h2>v5.12.0 Beta 10.2', "5.12.0", "10.2")
    assert not tourbox_candidate_links(TOURBOX_URLS[0], '<h2>TourBox</h2>v5.12.0 Beta 10.2 <a href="http://evil.com/fake.pkg">download</a>', "5.12.0", "10.2")
    sample = (b'<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle"><channel>'
              b'<item><title>Internal</title><sparkle:channel>arm64_internal</sparkle:channel>'
              b'<sparkle:minimumSystemVersion>26.3</sparkle:minimumSystemVersion>'
              b'<enclosure url="https://betterdisplay.pro/x.dmg" sparkle:version="5064" '
              b'sparkle:shortVersionString="5.0.6" /></item>'
              b'<item><sparkle:channel>x86_64_internal</sparkle:channel>'
              b'<enclosure url="https://betterdisplay.pro/y.dmg" sparkle:version="9000" /></item>'
              b'</channel></rss>')
    assert parse_betterdisplay_feed(sample, "27.0")[0]["channel"] == "arm64_internal"
    assert parse_betterdisplay_feed(sample, "26.0") == []
    print("SELF-TEST: PASS (Krita; TourBox; BetterDisplay channel filtering)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Official preview macOS applications updater")
    ap.add_argument("--version", action="version", version="Preview Apps Workflow " + VERSION)
    ap.add_argument("--topgrade", action="store_true", help="Update Krita Next only; separate Topgrade commands own TourBox Beta and BetterDisplay")
    ap.add_argument("--initialize", action="store_true", help="One-time safe Krita bootstrap by matching the installed signed app to official DMG; does not replace a mismatching app")
    ap.add_argument("--check", action="store_true", help="Read-only check (default)")
    ap.add_argument("--apply", action="store_true", help="Install newer Krita Next if previously bootstrapped")
    ap.add_argument("--bootstrap-krita", action="store_true", help="Explicitly accept official latest Krita Next as initial trusted build; attempts install")
    ap.add_argument("--allow-unsigned-krita", action="store_true", help="Explicitly trust unsigned KDE Nightly DMGs from official CDN for initial bootstrap/initialize and subsequent Topgrade; no macOS security bypass")
    ap.add_argument("--ack-krita", action="store_true", help="Legacy command: no blind acknowledgement; use --verify-krita")
    ap.add_argument("--verify-krita", action="store_true", help="Compare current Krita CDHash to a cached signed official DMG; never claim current from version alone")
    ap.add_argument("--download-krita", action="store_true", help="Download and verify official Krita DMG into ~/Downloads/PreviewAppsUpdater")
    ap.add_argument("--ack-tourbox", action="store_true", help="Confirm successful install of updater-downloaded verified TourBox PKG after executable changed")
    ap.add_argument("--apply-tourbox", action="store_true", help="Prepare verified official TourBox Beta PKG and open macOS Installer GUI")
    ap.add_argument("--open-tourbox", action="store_true", help="Open official download page when TourBox Beta is unverified/new")
    ap.add_argument("--enable-betterdisplay-auto", action="store_true", help="Enable standard Sparkle automatic checks/downloads for BetterDisplay; does not alter app-specific internal channel")
    ap.add_argument("--self-test", action="store_true", help="Parser tests without network")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return 0
    if sys.platform != "darwin":
        print("Requires macOS", file=sys.stderr)
        return 2
    if args.check and (args.initialize or args.apply or args.bootstrap_krita or args.apply_tourbox or args.ack_tourbox or args.ack_krita or args.verify_krita or args.download_krita or args.enable_betterdisplay_auto or args.allow_unsigned_krita):
        ap.error("--check is strictly read-only")
    if args.allow_unsigned_krita and not (args.initialize or args.bootstrap_krita):
        ap.error("--allow-unsigned-krita must be combined with --initialize or --bootstrap-krita")
    if args.topgrade and (args.ack_tourbox or args.apply_tourbox or args.open_tourbox or args.enable_betterdisplay_auto):
        ap.error("--topgrade updates only Krita; use dedicated TourBox/BetterDisplay updater for other apps")
    if args.enable_betterdisplay_auto and not args.topgrade:
        try:
            betterdisplay_native_auto()
            return 0
        except Exception as exc:
            print(f"Cannot enable BetterDisplay native updater: {exc}", file=sys.stderr)
            return 1
    state = state_read()
    errors = []
    only_krita = args.topgrade or bool(args.initialize or args.bootstrap_krita or args.apply or args.download_krita or args.verify_krita or args.ack_krita)
    only_tourbox = bool(args.ack_tourbox or args.apply_tourbox or args.open_tourbox)
    if args.topgrade:
        print("Topgrade ownership: Krita Next=this updater; TourBox Beta=update-tourbox-beta; BetterDisplay=update-preview-extras --betterdisplay")
    if not only_tourbox:
        try:
            check_krita(args, state)
        except AppMetadataError as e:
            status("Krita Next", "FAILED_METADATA", str(e))
            errors.append("krita-metadata")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            status("Krita Next", "RETRYABLE", str(e))
            errors.append("krita-network")
        except Exception as e:
            status("Krita Next", "FAILED", f"{type(e).__name__}: {e}")
            errors.append("krita")
    if not only_krita:
        try:
            check_tourbox(args, state)
        except Exception as e:
            status("TourBox", "RETRYABLE/FAILED", str(e))
            errors.append("tourbox")
    if not only_krita and not only_tourbox:
        try:
            check_betterdisplay(args, state)
        except Exception as e:
            status("BetterDisplay", "RETRYABLE/FAILED", str(e))
            errors.append("betterdisplay")
    if errors:
        summary = "FAILED — " + ", ".join(errors)
    elif all(x in {"CURRENT", "UPDATED"} for x in STATUS.values()):
        summary = "CURRENT/UPDATED"
    else:
        summary = "ATTENTION — " + ", ".join(f"{k}={v}" for k, v in STATUS.items() if v not in {"CURRENT", "UPDATED"})
    if not args.topgrade:
        print("\nSummary:", summary)
    # Only a verified not-newer build is a successful no-op.
    # Other deferred, unverified and actual failure states remain nonzero.
    if errors or (args.topgrade and any(value not in {"CURRENT", "UPDATED", "DEFERRED_NOT_NEWER"} for value in STATUS.values())):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
