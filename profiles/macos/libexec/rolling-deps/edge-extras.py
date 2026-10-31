#!/usr/bin/env python3.15

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from pathlib import Path


SKIP_DIRS = {
    ".git",
    ".gradle",
    "build",
    "target",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
}


def run(
    cmd: list[str],
    cwd: Path,
) -> int:
    print("$", " ".join(cmd), flush=True)

    return subprocess.run(
        cmd,
        cwd=cwd,
    ).returncode


def capture(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:

    p = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )

    return p.returncode, p.stdout


def crane_capture(
    cmd: list[str],
    cwd: Path,
) -> tuple[int, str]:

    force_anonymous = False

    def broken_helper(text: str) -> bool:

        lowered = text.lower()

        return (
            "error getting credentials"
            in lowered
            or (
                "docker-credential-"
                in lowered
                and (
                    "not found"
                    in lowered
                    or
                    "executable file not found"
                    in lowered
                )
            )
        )

    def transient(text: str) -> bool:

        lowered = text.lower()

        markers = (
            "retrying eof",
            "unexpected eof",
            ": eof",
            "connection reset by peer",
            "tls handshake timeout",
            "i/o timeout",
            "context deadline exceeded",
            "temporary failure",
            "too many requests",
            "429 too many requests",
            "502 bad gateway",
            "503 service unavailable",
            "504 gateway timeout",
        )

        return any(
            marker in lowered
            for marker in markers
        )

    def anonymous_capture():

        with tempfile.TemporaryDirectory(
            prefix="rolling-crane-anon-"
        ) as temp:

            root = Path(temp)

            (
                root
                / "config.json"
            ).write_text(
                '{"auths": {}}\n'
            )

            env = os.environ.copy()

            env["DOCKER_CONFIG"] = str(
                root
            )

            env.pop(
                "REGISTRY_AUTH_FILE",
                None,
            )

            return capture(
                cmd,
                cwd,
                env=env,
            )

    def attempt():

        nonlocal force_anonymous

        if force_anonymous:
            return anonymous_capture()

        rc, output = capture(
            cmd,
            cwd,
        )

        if (
            rc != 0
            and broken_helper(output)
        ):
            force_anonymous = True

            print(
                "Registry credential helper unavailable; "
                "retrying anonymously."
            )

            rc, output = anonymous_capture()

            if rc == 0:
                print(
                    "Anonymous registry retry succeeded."
                )

        return rc, output

    rc, output = attempt()

    if rc == 0:
        return rc, output

    # crane/go-containerregistry already performs its own
    # short retries. These are slower outer retries for
    # registry/network hiccups that survive those attempts.
    if transient(output):

        for number, delay in enumerate(
            (2, 5),
            start=1,
        ):
            print(
                "Registry network error; "
                f"outer retry {number}/2 "
                f"in {delay}s."
            )

            time.sleep(
                delay
            )

            rc, output = attempt()

            if rc == 0:
                print(
                    "Registry outer retry succeeded."
                )

                return rc, output

            if not transient(output):
                break

    return rc, output


# =====================================================================
# Zig
# =====================================================================

def brace_block(
    text: str,
    start: int,
) -> tuple[int, int] | None:

    brace = text.find("{", start)

    if brace < 0:
        return None

    depth = 0
    in_string = False
    escaped = False

    for i in range(brace, len(text)):
        ch = text[i]

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False

            continue

        if ch == '"':
            in_string = True

        elif ch == "{":
            depth += 1

        elif ch == "}":
            depth -= 1

            if depth == 0:
                return brace + 1, i

    return None


def zig_dependencies(text: str):
    m = re.search(
        r"\.dependencies\s*=\s*\.\{",
        text,
    )

    if not m:
        return []

    outer = brace_block(
        text,
        m.start(),
    )

    if not outer:
        return []

    body = text[
        outer[0]:outer[1]
    ]

    pattern = re.compile(
        r'''(?mx)
        ^[ \t]*\.
        (?:
            @"(?P<quoted>[^"]+)"
            |
            (?P<plain>[A-Za-z_][A-Za-z0-9_]*)
        )
        [ \t]*=[ \t]*\.\{
        '''
    )

    result = []
    pos = 0

    while True:
        match = pattern.search(
            body,
            pos,
        )

        if not match:
            break

        block = brace_block(
            body,
            match.end() - 1,
        )

        if not block:
            break

        name = (
            match.group("quoted")
            or match.group("plain")
        )

        dep_body = body[
            block[0]:block[1]
        ]

        url_match = re.search(
            r'\.url\s*=\s*"([^"]+)"',
            dep_body,
        )

        if url_match:
            result.append(
                (
                    name,
                    url_match.group(1),
                )
            )

        pos = block[1] + 1

    return result


def github_edge_url(
    url: str,
) -> str | None:

    m = re.match(
        r"git\+https://github\.com/"
        r"([^/]+)/([^/?#]+)",
        url,
    )

    if m:
        owner, repo = m.groups()
        repo = repo.removesuffix(".git")

        return (
            f"git+https://github.com/"
            f"{owner}/{repo}.git"
        )

    m = re.match(
        r"https://github\.com/"
        r"([^/]+)/([^/]+)/archive/",
        url,
    )

    if m:
        owner, repo = m.groups()

        return (
            f"git+https://github.com/"
            f"{owner}/{repo}.git"
        )

    return None


def update_zig(
    root: Path,
) -> int:

    zon = root / "build.zig.zon"

    if not zon.exists():
        return 0

    deps = zig_dependencies(
        zon.read_text(errors="replace")
    )

    rollable = []

    for name, old_url in deps:
        new_url = github_edge_url(
            old_url
        )

        if new_url:
            rollable.append(
                (
                    name,
                    old_url,
                    new_url,
                )
            )

    if not rollable:
        print(
            "No rollable GitHub "
            "Zig dependencies detected."
        )
        return 0

    failures = 0

    for name, old_url, new_url in rollable:
        print()
        print(name)
        print(f"  {old_url}")
        print(f"  -> {new_url}")

        rc = run(
            [
                "zig",
                "fetch",
                f"--save={name}",
                new_url,
            ],
            root,
        )

        if rc:
            failures += 1

    return 1 if failures else 0


# =====================================================================
# Docker / OCI
# =====================================================================

def split_image_ref(
    ref: str,
) -> tuple[str, str]:

    base = ref.split("@", 1)[0]

    slash = base.rfind("/")
    colon = base.rfind(":")

    if colon > slash:
        return (
            base[:colon],
            base[colon + 1:],
        )

    return base, "latest"


def canonical_image_repo(
    repo: str,
) -> str:

    # ubuntu -> docker.io/library/ubuntu
    if "/" not in repo:
        return (
            "docker.io/library/"
            + repo
        )

    first = repo.split("/", 1)[0]

    # user/image -> docker.io/user/image
    if (
        "." not in first
        and ":" not in first
        and first != "localhost"
    ):
        return "docker.io/" + repo

    return repo


def numeric_tag_parts(
    tag: str,
):
    m = re.fullmatch(
        r"(v?)"
        r"(\d+(?:\.\d+)*)"
        r"(.*)",
        tag,
    )

    if not m:
        return None

    prefix = m.group(1)

    version = tuple(
        int(x)
        for x in m.group(2).split(".")
    )

    suffix = m.group(3)

    return (
        prefix,
        version,
        suffix,
    )


def choose_edge_tag(
    current: str,
    tags: list[str],
) -> str:

    moving = {
        "latest",
        "edge",
        "nightly",
        "main",
        "master",
        "rolling",
        "unstable",
        "canary",
        "dev",
    }

    if current.lower() in moving:
        return current

    parsed = numeric_tag_parts(
        current
    )

    if not parsed:
        # A named channel such as bookworm/alpine/etc.
        # Preserve the channel and roll its digest.
        return current

    prefix, _, suffix = parsed

    candidates = []

    for tag in tags:
        parsed_tag = numeric_tag_parts(
            tag
        )

        if not parsed_tag:
            continue

        pfx, version, sfx = parsed_tag

        # Preserve the exact flavour:
        # 1.90-bookworm -> newest *-bookworm
        # 3.13-slim     -> newest *-slim
        if (
            pfx == prefix
            and sfx == suffix
        ):
            candidates.append(
                (
                    version,
                    tag,
                )
            )

    if not candidates:
        return current

    return max(
        candidates,
        key=lambda x: x[0],
    )[1]


def resolve_image_edge(
    ref: str,
    cwd: Path,
) -> tuple[str | None, str | None]:

    repo, current_tag = split_image_ref(
        ref
    )

    repo = canonical_image_repo(
        repo
    )

    parsed = numeric_tag_parts(
        current_tag
    )

    moving_or_named = (
        parsed is None
        or current_tag.lower()
        in {
            "latest",
            "edge",
            "nightly",
            "main",
            "master",
            "rolling",
            "unstable",
            "canary",
            "dev",
        }
    )

    if moving_or_named:
        target_tag = current_tag

    else:
        rc, output = crane_capture(
            [
                "crane",
                "ls",
                repo,
            ],
            cwd,
        )

        if rc:
            return (
                None,
                (
                    f"crane ls failed for {repo}: "
                    f"{output.strip()}"
                ),
            )

        tags = [
            line.strip()
            for line in output.splitlines()
            if line.strip()
        ]

        target_tag = choose_edge_tag(
            current_tag,
            tags,
        )

    target = (
        f"{repo}:{target_tag}"
    )

    rc, digest_output = crane_capture(
        [
            "crane",
            "digest",
            target,
        ],
        cwd,
    )

    if rc:
        return (
            None,
            (
                f"crane digest failed for {target}: "
                f"{digest_output.strip()}"
            ),
        )

    digest = digest_output.strip()

    if not digest.startswith(
        "sha256:"
    ):
        return (
            None,
            f"unexpected digest for {target}: "
            f"{digest}",
        )

    return (
        f"{target}@{digest}",
        None,
    )


def update_dockerfile(
    path: Path,
) -> tuple[int, int, int]:

    original = path.read_text(
        errors="replace"
    )

    lines = original.splitlines(
        keepends=True
    )

    aliases = set()

    changed = 0
    failures = 0
    upstream_missing = 0

    pattern = re.compile(
        r"^(\s*FROM\s+"
        r"(?:--platform=\S+\s+)?)"
        r"(\S+)"
        r"(.*)$",
        re.I,
    )

    for i, line in enumerate(lines):
        newline = (
            "\n"
            if line.endswith("\n")
            else ""
        )

        raw = (
            line[:-1]
            if newline
            else line
        )

        match = pattern.match(
            raw
        )

        if not match:
            continue

        prefix = match.group(1)
        ref = match.group(2)
        rest = match.group(3)

        alias_match = re.search(
            r"\s+AS\s+([^\s]+)",
            rest,
            re.I,
        )

        alias = (
            alias_match.group(1).lower()
            if alias_match
            else None
        )

        ref_lower = ref.lower()

        if (
            ref_lower == "scratch"
            or ref_lower in aliases
        ):
            if alias:
                aliases.add(alias)

            continue

        if "$" in ref:
            print(
                f"{path}: dynamic FROM skipped: "
                f"{ref}"
            )

            failures += 1

            if alias:
                aliases.add(alias)

            continue

        new_ref, error = resolve_image_edge(
            ref,
            path.parent,
        )

        if error:
            lowered = error.lower()

            if (
                "manifest_unknown"
                in lowered
                or
                "404 not found"
                in lowered
                or
                "manifest unknown"
                in lowered
            ):
                print(
                    f"{path}: upstream image "
                    f"manifest is missing:"
                )

                print(
                    f"  {ref}"
                )

                print(
                    f"  {error}"
                )

                upstream_missing += 1

            else:
                print(
                    f"{path}: {error}"
                )

                failures += 1

            if alias:
                aliases.add(alias)

            continue

        if (
            new_ref
            and new_ref != ref
        ):
            print(
                f"{path.name}:"
            )

            print(
                f"  {ref}"
            )

            print(
                f"  -> {new_ref}"
            )

            lines[i] = (
                prefix
                + new_ref
                + rest
                + newline
            )

            changed += 1

        if alias:
            aliases.add(alias)

    new_text = "".join(
        lines
    )

    if new_text != original:
        path.write_text(
            new_text
        )

    return (
        changed,
        failures,
        upstream_missing,
    )


def update_docker(
    root: Path,
) -> int:

    paths = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        if any(
            part in SKIP_DIRS
            for part
            in path.relative_to(root).parts
        ):
            continue

        if (
            path.name == "Dockerfile"
            or path.name.startswith(
                "Dockerfile."
            )
        ):
            paths.append(
                path
            )

    if not paths:
        print(
            "No Dockerfiles found."
        )
        return 0

    changed = 0
    failures = 0
    upstream_missing = 0

    for path in paths:
        c, f, m = update_dockerfile(
            path
        )

        changed += c
        failures += f
        upstream_missing += m

    print(
        "Docker images: "
        f"{changed} changed, "
        f"{upstream_missing} upstream missing, "
        f"{failures} failed"
    )

    if failures:
        return 1

    if upstream_missing:
        # Special orchestrator state.
        return 6

    return 0


# =====================================================================
# Gradle
# =====================================================================

def gradle_executable(
    root: Path,
) -> str | None:

    current = root.resolve()

    # Project wrapper always wins.
    for base in (
        current,
        *current.parents,
    ):
        wrapper = base / "gradlew"

        if wrapper.exists():
            wrapper.chmod(
                wrapper.stat().st_mode
                | 0o111
            )

            print(
                f"Gradle wrapper found: {wrapper}"
            )

            return str(wrapper)

        if (base / ".git").exists():
            break

    # Rolling standalone nightly comes next.
    nightly = (
        Path.home()
        / ".local"
        / "bin"
        / "gradle-nightly"
    )

    if nightly.exists():
        print(
            "Using Gradle nightly: "
            f"{nightly}"
        )

        return str(nightly)

    # Stable Homebrew Gradle remains the fallback.
    system_gradle = shutil.which(
        "gradle"
    )

    if system_gradle:
        print(
            "Using system Gradle: "
            f"{system_gradle}"
        )

    return system_gradle

def gradle_nightly() -> str | None:
    try:
        request = urllib.request.Request(
            "https://services.gradle.org/"
            "versions/nightly",
            headers={
                "User-Agent":
                    "rolling-deps/1.2",
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:
            data = json.load(
                response
            )

        value = data.get(
            "version"
        )

        if (
            isinstance(value, str)
            and value
        ):
            return value

    except Exception as exc:
        print(
            "Gradle nightly lookup "
            f"failed: {exc}"
        )

    return None


def gradle_source_files(
    root: Path,
):
    result = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        rel = path.relative_to(
            root
        )

        if any(
            part in SKIP_DIRS
            for part in rel.parts
        ):
            continue

        if (
            path.name.endswith(
                (
                    ".gradle",
                    ".gradle.kts",
                    ".properties",
                )
            )
            or path.name
            == "libs.versions.toml"
        ):
            result.append(path)

    return result


def edge_gradle_version(
    dependency: dict,
) -> str | None:

    available = (
        dependency.get(
            "available"
        )
        or {}
    )

    # User policy is explicitly edge-first.
    # A newer prerelease beats a release here.
    for key in (
        "preRelease",
        "integration",
        "milestone",
        "release",
    ):
        value = available.get(
            key
        )

        if (
            isinstance(value, str)
            and value
        ):
            return value

    return None


def rewrite_gradle_dependency(
    root: Path,
    group: str,
    name: str,
    current: str,
    target: str,
) -> bool:

    files = gradle_source_files(
        root
    )

    changed_any = False

    # Gradle plugin marker artifacts look like:
    #
    # com.android.application:
    # com.android.application.gradle.plugin
    #
    # The actual source declaration is usually:
    #
    # id("com.android.application") version "8.x"
    #
    plugin_id = None

    if name == (
        group
        + ".gradle.plugin"
    ):
        plugin_id = group

    coordinate = (
        f"{group}:{name}"
    )

    variable_names = set()

    coordinate_var_pattern = re.compile(
        re.escape(
            coordinate + ":"
        )
        + r"\$\{?"
        + r"([A-Za-z_]"
        + r"[A-Za-z0-9_.-]*)"
        + r"\}?"
    )

    for path in files:
        try:
            original = path.read_text(
                errors="replace"
            )
        except Exception:
            continue

        text = original

        # -------------------------------------------------
        # Normal direct dependency:
        #
        # implementation("g:n:1.2")
        # implementation 'g:n:1.2'
        # classpath("g:n:1.2")
        # -------------------------------------------------

        text = text.replace(
            f"{coordinate}:{current}",
            f"{coordinate}:{target}",
        )

        # -------------------------------------------------
        # Gradle plugin DSL:
        #
        # id("com.foo.bar") version "1.2"
        # id 'com.foo.bar' version '1.2'
        # id("com.foo.bar").version("1.2")
        # -------------------------------------------------

        if plugin_id:
            patterns = [
                re.compile(
                    r'('
                    r'id\s*\(\s*["\']'
                    + re.escape(plugin_id)
                    + r'["\']\s*\)'
                    r'\s*version\s*["\'])'
                    + re.escape(current)
                    + r'(["\'])'
                ),

                re.compile(
                    r'('
                    r'id\s+["\']'
                    + re.escape(plugin_id)
                    + r'["\']'
                    r'\s+version\s+["\'])'
                    + re.escape(current)
                    + r'(["\'])'
                ),

                re.compile(
                    r'('
                    r'id\s*\(\s*["\']'
                    + re.escape(plugin_id)
                    + r'["\']\s*\)'
                    r'\s*\.version\s*\(\s*["\'])'
                    + re.escape(current)
                    + r'(["\']\s*\))'
                ),
            ]

            for pattern in patterns:
                text = pattern.sub(
                    lambda m:
                        m.group(1)
                        + target
                        + m.group(2),
                    text,
                )

        # -------------------------------------------------
        # TOML version catalog inline declarations.
        #
        # plugin = {
        #   id = "com.android.application",
        #   version = "8.7.3"
        # }
        #
        # library = {
        #   module = "group:name",
        #   version = "1.2"
        # }
        # -------------------------------------------------

        lines = text.splitlines(
            keepends=True
        )

        for index, line in enumerate(
            lines
        ):
            relevant = False

            if (
                plugin_id
                and plugin_id in line
                and "id" in line
            ):
                relevant = True

            if (
                coordinate in line
                and (
                    "module" in line
                    or "group" in line
                )
            ):
                relevant = True

            if not relevant:
                continue

            lines[index] = re.sub(
                r'(["\'])'
                + re.escape(current)
                + r'\1',
                lambda m:
                    m.group(1)
                    + target
                    + m.group(1),
                line,
            )

        text = "".join(
            lines
        )

        # -------------------------------------------------
        # version.ref support:
        #
        # [versions]
        # agp = "8.7.3"
        #
        # [plugins]
        # android = {
        #   id = "...",
        #   version.ref = "agp"
        # }
        # -------------------------------------------------

        refs = set()

        for line in original.splitlines():
            relevant = False

            if (
                plugin_id
                and plugin_id in line
            ):
                relevant = True

            if coordinate in line:
                relevant = True

            if not relevant:
                continue

            ref_match = re.search(
                r'version\.ref\s*=\s*'
                r'["\']([^"\']+)["\']',
                line,
            )

            if ref_match:
                refs.add(
                    ref_match.group(1)
                )

        for ref in refs:
            pattern = re.compile(
                r'(?m)^(\s*'
                + re.escape(ref)
                + r'\s*=\s*["\'])'
                + re.escape(current)
                + r'(["\']\s*(?:#.*)?$)'
            )

            text = pattern.sub(
                lambda m:
                    m.group(1)
                    + target
                    + m.group(2),
                text,
            )

        # -------------------------------------------------
        # Gradle property / variable references:
        #
        # implementation("g:n:$fooVersion")
        #
        # fooVersion=1.2
        # val fooVersion = "1.2"
        # -------------------------------------------------

        for var in coordinate_var_pattern.findall(
            original
        ):
            variable_names.add(
                var
            )

        if text != original:
            path.write_text(
                text
            )

            print(
                "  rewritten: "
                f"{path.relative_to(root)}"
            )

            changed_any = True

    # Update variables discovered through $fooVersion.

    if variable_names:
        for path in files:
            try:
                original = path.read_text(
                    errors="replace"
                )
            except Exception:
                continue

            text = original

            for var in variable_names:

                # gradle.properties
                pattern = re.compile(
                    r"(?m)^(\s*"
                    + re.escape(var)
                    + r"\s*=\s*)"
                    + re.escape(current)
                    + r"(\s*(?:#.*)?$)"
                )

                text = pattern.sub(
                    lambda m:
                        m.group(1)
                        + target
                        + m.group(2),
                    text,
                )

                # Kotlin/Groovy variable declaration
                pattern = re.compile(
                    r'(?m)^(\s*'
                    r'(?:val|var|def)?\s*'
                    + re.escape(var)
                    + r'\s*=\s*["\'])'
                    + re.escape(current)
                    + r'(["\'])'
                )

                text = pattern.sub(
                    lambda m:
                        m.group(1)
                        + target
                        + m.group(2),
                    text,
                )

            if text != original:
                path.write_text(
                    text
                )

                print(
                    "  rewritten: "
                    f"{path.relative_to(root)}"
                )

                changed_any = True

    return changed_any

def _rolling_check_gradle_report(report: Path) -> list[str]:
    data = json.loads(report.read_text())
    if not isinstance(data, dict):
        raise ValueError("Gradle report must be an object")

    groups = (
        ("outdated", "dependencies"),
        ("unresolved", "dependencies"),
        ("skipped", "configurations"),
    )
    for section, field in groups:
        group = data.get(section)
        if not isinstance(group, dict):
            raise ValueError(f"Gradle report is missing {section}")
        entries = group.get(field)
        count = group.get("count")
        if (not isinstance(entries, list) or
            not isinstance(count, int) or isinstance(count, bool) or
            count < 0 or count != len(entries) or
            not all(isinstance(entry, dict) for entry in entries)):
            raise ValueError(f"invalid Gradle report section: {section}")

    issues = []
    for dep in data["unresolved"]["dependencies"]:
        group = dep.get("group") or "?"
        name = dep.get("name") or "?"
        issues.append(f"unresolved dependency: {group}:{name}")

    for config in data["skipped"]["configurations"]:
        project = config.get("project") or "?"
        name = config.get("name") or "?"
        issues.append(f"uninspected configuration: {project}:{name}")

    channels = data.get("gradle")
    if not isinstance(channels, dict) or not isinstance(channels.get("enabled"), bool):
        raise ValueError("Gradle report is missing channel status")
    if channels["enabled"] and channels.get("nightly") is None:
        raise ValueError("Gradle nightly channel status is missing")
    for name in ("running", "current", "releaseCandidate", "nightly"):
        status = channels.get(name)
        if status is None:
            continue
        if not isinstance(status, dict) or not isinstance(status.get("isFailure"), bool):
            raise ValueError(f"invalid Gradle channel status: {name}")
        if status["isFailure"]:
            issues.append(f"Gradle channel lookup failed: {name}")

    return issues


def _rolling_classify_gradle_report(
    report: Path,
) -> tuple[list[str], list[str]]:
    data = json.loads(report.read_text())
    entries = data.get("outdated", {}).get("dependencies", [])

    if not isinstance(entries, list):
        raise ValueError("Unexpected Gradle dependencies report")

    internal = {
        "kotlinCompilerClasspath",
        "kotlinBuildToolsApiClasspath",
        "kotlinAbiValidationCompatClasspath",
        "kotlinKlibCommonizerClasspath",
        "kotlinBouncyCastleConfiguration",
        "androidLintTool",
    }

    managed = []
    unresolved = []

    for dep in entries:
        module = f"{dep.get('group', '?')}:{dep.get('name', '?')}"
        configs = dep.get("configurations") or []

        if not isinstance(configs, list):
            configs = []

        internal_config = bool(configs) and all(
            isinstance(name, str)
            and (
                name in internal
                or name.startswith("unified-test-platform-")
                or (
                    name.startswith("kotlinCompilerPluginClasspath")
                    and name != "kotlinCompilerPluginClasspath"
                )
            )
            for name in configs
        )

        plugin_managed = (
            dep.get("contributed") is True
            or internal_config
        )

        if plugin_managed:
            managed.append(module)
        else:
            unresolved.append(module)

    return managed, unresolved


def rewrite_gradle_report(
    root: Path,
    report: Path,
) -> tuple[int, int]:

    data = json.loads(
        report.read_text()
    )

    outdated = (
        data.get("outdated", {})
        .get("dependencies", [])
    )

    discovered = 0
    rewritten = 0

    for dep in outdated:
        group = dep.get("group")
        name = dep.get("name")
        current = dep.get("version")

        target = edge_gradle_version(
            dep
        )

        if not all(
            isinstance(x, str)
            and x
            for x in (
                group,
                name,
                current,
                target,
            )
        ):
            continue

        if current == target:
            continue

        discovered += 1

        print(
            f"{group}:{name}"
        )
        print(
            f"  {current} -> {target}"
        )

        if rewrite_gradle_dependency(
            root,
            group,
            name,
            current,
            target,
        ):
            rewritten += 1

    return (
        discovered,
        rewritten,
    )


def _gradle_proxy_args(root: Path, env=None) -> list[str]:
    from urllib.parse import urlsplit
    import ipaddress

    env = os.environ if env is None else env
    explicit = r"-D(?:https?\.(?:proxy|nonProxy)|socksProxy|java\.net\.useSystemProxies)"
    if any(re.search(explicit, env.get(key, "")) for key in
           ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "JAVA_OPTS", "GRADLE_OPTS")):
        return []
    user_home = Path(env.get("GRADLE_USER_HOME") or Path.home() / ".gradle")
    if not user_home.is_absolute():
        user_home = root / user_home
    for config in (user_home / "gradle.properties", root / "gradle.properties"):
        if not config.exists():
            continue
        contents = config.read_text()
        jvmargs = re.findall(r"(?m)^\s*org\.gradle\.jvmargs\s*[=:].*$",
                             re.sub(r"\\\r?\n[ \t]*", "", contents))
        if re.search(
            r"(?m)^\s*systemProp\.(?:https?\.(?:proxy|nonProxy)|socksProxy|java\.net\.useSystemProxies)",
            contents) or any(re.search(explicit, line) for line in jvmargs):
            return []

    def value(key):
        return env.get(key.lower(), env.get(key.upper(), ""))

    proxies = {scheme: value(scheme + "_proxy") for scheme in ("http", "https")}
    if not any(proxies.values()):
        if value("all_proxy"):
            raise ValueError("Gradle needs explicit JVM settings for an ALL_PROXY-only route")
        return []
    bypass = value("no_proxy")
    if bypass.strip() == "*":
        return []
    patterns = []
    for token in filter(None, (item.strip() for item in bypass.split(","))):
        try:
            address = ipaddress.ip_address(token.strip("[]"))
            patterns.append(f"[{address}]" if address.version == 6 else str(address))
        except ValueError:
            if not re.fullmatch(r"\.?[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", token):
                raise ValueError("NO_PROXY cannot be mapped exactly to Gradle; configure JVM nonProxyHosts explicitly") from None
            host = token.lstrip(".")
            patterns.extend((host, "*." + host))
    arguments = []
    for scheme, proxy in proxies.items():
        if not proxy:
            continue
        try:
            parsed = urlsplit(proxy)
        except ValueError:
            raise ValueError("Gradle proxy URL is invalid") from None
        if (parsed.scheme != "http" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ValueError("Gradle proxy inheritance requires an unauthenticated HTTP proxy; use explicit JVM settings otherwise")
        try:
            port = 80 if parsed.port is None else parsed.port
            if port == 0:
                raise ValueError("port zero")
        except ValueError:
            raise ValueError("Gradle proxy port is invalid") from None
        arguments.extend((f"-D{scheme}.proxyHost={parsed.hostname}", f"-D{scheme}.proxyPort={port}"))
    if patterns:
        arguments.append("-Dhttp.nonProxyHosts=" + "|".join(dict.fromkeys(patterns)))
    return arguments


def update_gradle(
    root: Path,
) -> int:

    gradle = gradle_executable(
        root
    )

    if not gradle:
        print(
            "Gradle executable not found."
        )
        return 1

    try:
        gradle_command = [gradle, *_gradle_proxy_args(root)]
    except (OSError, ValueError) as error:
        print(f"Gradle proxy configuration: {error}")
        return 1

    failures = 0
    unmapped_discovery = False

    init_script = r'''
import com.github.benmanes.gradle.versions.VersionsSettingsPlugin

initscript {
    repositories {
        gradlePluginPortal()
    }

    dependencies {
        classpath 'io.github.ben-manes:gradle-versions-plugin:+'
    }
}

settingsEvaluated { settings ->
    if (!settings.pluginManager.hasPlugin(
        "io.github.ben-manes.versions.settings"
    )) {
        settings.pluginManager.apply(
            VersionsSettingsPlugin
        )
    }
}
'''

    with tempfile.TemporaryDirectory(
        prefix="rolling-gradle-"
    ) as temp:

        temp_root = Path(temp)

        init_path = (
            temp_root
            / "rolling.init.gradle"
        )

        report_dir = (
            temp_root
            / "report"
        )

        init_path.write_text(
            init_script
        )

        report_dir.mkdir()

        cmd = [
            *gradle_command,
            "--init-script",
            str(init_path),
            "dependencyUpdates",
            "--revision",
            "integration",
            "--no-reject-out-of-bounds",
            "--output-formatter",
            "json",
            "--output-dir",
            str(report_dir),
            "--report-file-name",
            "report",
            "--gradle-release-channel",
            "nightly",
        ]

        rc = run(
            cmd,
            root,
        )

        if rc:
            failures += 1

        else:
            candidates = list(
                report_dir.rglob(
                    "report.json"
                )
            )

            if not candidates:
                print(
                    "Gradle JSON report "
                    "was not produced."
                )

                failures += 1

            elif len(candidates) != 1:
                print("Gradle produced multiple JSON reports; refusing an ambiguous result.")
                return 1

            else:
                try:
                    issues = _rolling_check_gradle_report(candidates[0])
                except (OSError, ValueError, TypeError) as error:
                    print(f"Gradle JSON report is invalid: {error}")
                    return 1

                if issues:
                    print("Gradle report is incomplete; dependency and wrapper versions were not changed:")
                    for issue in issues:
                        print(f"  - {issue}")
                    print("See Gradle output above for the underlying errors.")
                    return 1

                discovered, rewritten = (
                    rewrite_gradle_report(
                        root,
                        candidates[0],
                    )
                )

                print(
                    "Gradle dependencies: "
                    f"{discovered} reported, "
                    f"{rewritten} rewritten"
                )

                if discovered > 0 and rewritten == 0:
                    managed, unresolved = _rolling_classify_gradle_report(candidates[0])

                    if len(managed) + len(unresolved) != discovered:
                        unmapped_discovery = True
                        print(
                            "Gradle report classification incomplete; "
                            "keeping DISCOVERED."
                        )
                    else:
                        for module in managed:
                            print(
                                f"Gradle plugin-managed update "
                                f"(informational): {module}"
                            )

                        if unresolved:
                            unmapped_discovery = True
                            print(
                                f"Gradle: {len(unresolved)} updates "
                                "have no writable direct declaration."
                            )
                        else:
                            print(
                                "Gradle: only plugin-managed updates remain; "
                                "no direct dependency update is pending."
                            )

    # If the project has a wrapper, independently roll it
    # to the newest Gradle nightly.
    wrapper = root / "gradlew"

    if wrapper.exists():
        nightly = gradle_nightly()

        if nightly:
            print(
                f"Gradle wrapper -> "
                f"{nightly}"
            )

            rc = run(
                [
                    *gradle_command,
                    "wrapper",
                    "--gradle-version",
                    nightly,
                ],
                root,
            )

            if rc:
                failures += 1

    if failures:
        return 1

    if unmapped_discovery:
        # Special orchestrator status:
        # updates exist, but are not directly writable.
        return 5

    return 0


# =====================================================================
# Main
# =====================================================================

def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: edge-extras.py "
            "<zig|gradle|docker> "
            "<project-root>",
            file=sys.stderr,
        )
        return 2

    action = sys.argv[1]

    root = Path(
        sys.argv[2]
    ).expanduser().resolve()

    if action == "zig":
        return update_zig(
            root
        )

    if action == "gradle":
        return update_gradle(
            root
        )

    if action == "docker":
        return update_docker(
            root
        )

    print(
        f"Unknown action: {action}",
        file=sys.stderr,
    )

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
