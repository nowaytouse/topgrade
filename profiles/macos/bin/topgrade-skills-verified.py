#!/usr/bin/env python3
"""Keep the Skills updater's failed source checks from becoming false success."""
import os
import re
import subprocess
import sys

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

def verify_output(output, returncode):
    plain = ANSI.sub("", output).replace("\r", "\n")
    if returncode:
        return returncode
    if re.search(r"✗|✘|(?:^|\n)\s*Error:|Failed to (?:check|update)\b", plain):
        return 1
    if "appear to have been deleted upstream" in plain:
        return 1
    if not re.search(r"All global skills are up to date|Updated \d+ skill\(s\)|No global skills to check", plain):
        return 1
    return 0

def update(environment=None):
    env = dict(os.environ if environment is None else environment)
    try:
        subprocess.run([sys.executable, os.path.expanduser("~/.local/bin/normalize-gh-skill-lock.py")],
                       env=env, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL: Shared skill lock normalization failed: {exc}", file=sys.stderr)
        return 1
    if not env.get("GITHUB_TOKEN") and not env.get("GH_TOKEN"):
        credential = subprocess.run(
            ["/opt/homebrew/bin/gh", "auth", "token", "--hostname", "github.com"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        token = credential.stdout.strip() if credential.returncode == 0 else ""
        if token and not any(c.isspace() for c in token):
            env["GITHUB_TOKEN"] = token
    secrets = [env[k] for k in ("GITHUB_TOKEN", "GH_TOKEN") if env.get(k)]
    command = [os.path.expanduser("~/.local/bin/proxy-auto-run"),
               "/opt/homebrew/bin/npx", "--ignore-scripts",
               "--registry=https://registry.npmjs.org", "--yes",
               "skills", "update", "--global", "--yes"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=env, text=True)
    output = []
    for line in process.stdout:
        for secret in secrets:
            line = line.replace(secret, "[REDACTED]")
        sys.stdout.write(line)
        sys.stdout.flush()
        output.append(line)
    returncode = process.wait()
    result = verify_output("".join(output), returncode)
    if result:
        print("FAIL: Skills update did not verify every source; review the reported failures.", file=sys.stderr)
    return result

if __name__ == "__main__":
    raise SystemExit(update())
