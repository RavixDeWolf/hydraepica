#!/usr/bin/env python3
"""scan.py - refuse a commit that carries anything private: secrets, tokens, real ids, addresses, local paths.

  python3 tools/scan.py            # scan every tracked + staged file; exit 1 with file:line for each hit
  python3 tools/scan.py --install  # add it as the repo's pre-commit hook

Generic rules ship here. Words that are private to YOUR install (names, hostnames, ids) go in a file that is never
committed: ~/.config/harness/scan_private.txt, one regex per line. The scan fails on those too.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRIVATE = os.path.expanduser(os.environ.get("SCAN_PRIVATE", "~/.config/harness/scan_private.txt"))
RULES = {
    "telegram bot token": r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b",
    "anthropic key": r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    "openai-style key": r"\bsk-(proj-)?[A-Za-z0-9]{32,}",
    "github token": r"\bgh[pousr]_[A-Za-z0-9]{36,}",
    "aws key": r"\bAKIA[0-9A-Z]{16}\b",
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "tailscale/CGNAT ip": r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b",
    "lan ip": r"\b(192\.168|10\.\d{1,3}|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b",
    "home path": r"/home/[a-z_][a-z0-9_-]*/",
    "email address": r"\b[A-Za-z0-9._%+-]+@(?!example\.(com|org)\b)[A-Za-z0-9.-]+\.[a-z]{2,}\b",
}
ALLOW = [r"127\.0\.0\.1", r"noreply@", r"/home/x/", r"@[\w.-]*\.service$"]   # loopback, placeholders, systemd unit@instance
VENDOR_IN_PATH = re.compile(r"anthropic|claude|gemini|grok|openai|codex|ollama|openrouter|gpt", re.I)
SKIP_DIRS = {".git", ".venv", "__pycache__"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pyc", ".ico", ".mp4", ".onnx"}


def files():
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.split()
    except Exception:
        out = [os.path.relpath(os.path.join(d, f), ROOT) for d, ds, fs in os.walk(ROOT)
               for f in fs if not (set(os.path.relpath(d, ROOT).split(os.sep)) & SKIP_DIRS)]
    return [f for f in out if os.path.splitext(f)[1].lower() not in SKIP_EXT and not (set(f.split("/")) & SKIP_DIRS)]


def rules():
    r = dict(RULES)
    try:
        for i, line in enumerate(open(PRIVATE)):
            line = line.strip()
            if line and not line.startswith("#"):
                r[f"private word #{i + 1}"] = line
    except FileNotFoundError:
        print(f"note: no {PRIVATE}; only the generic rules ran", file=sys.stderr)
    return {k: re.compile(v, re.I if k.startswith("private") else 0) for k, v in r.items()}


def scan():
    rs, hits = rules(), []
    for f in files():
        try:
            text = open(os.path.join(ROOT, f), errors="replace").read()
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for name, rx in rs.items():
                m = rx.search(line)
                if m and not any(re.search(a, m.group(0)) for a in ALLOW):
                    hits.append(f"{f}:{n}: {name}")
        if any(rx.search(f) for k, rx in rs.items() if k.startswith("private")):
            hits.append(f"{f}: private word in the file name")
        if VENDOR_IN_PATH.search(f):
            hits.append(f"{f}: a vendor or model name in a file or folder name (keep those inside the code)")
    return hits


def main():
    if "--install" in sys.argv:
        hook = os.path.join(ROOT, ".git", "hooks", "pre-commit")
        with open(hook, "w") as h:
            h.write("#!/bin/sh\nexec python3 tools/scan.py\n")
        os.chmod(hook, 0o755)
        print("pre-commit hook installed")
        return 0
    hits = scan()
    for h in hits:
        print(h)
    print(f"scan: {len(hits)} finding(s)" if hits else "scan: clean")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
