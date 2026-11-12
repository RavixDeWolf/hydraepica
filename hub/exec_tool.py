"""exec_tool.py - run a command on a host the config names, and say exactly what happened.

  [exec.hosts]            name -> ssh alias ("local" always means this machine)
  build = "build-box"

Three layers, outermost first:
  1. BLOCKED patterns never run, whoever asks (wiping disks, formatting, fork bombs, piping the web into a shell).
  2. Everything else that is not on the read-only list is RISKY: it runs only with confirm=true. The tool is also
     marked destructive, so Claude Code asks first, and the harness's permission hook turns that into Allow / Deny
     buttons on the owner's phone.
  3. Every call (run, refused, timed out) is one audit row naming the head that asked.
"""
import re
import shlex
import subprocess

import audit

TIMEOUT_S = 60
MAX_OUT = 20_000
BLOCKED = [r"\brm\s+-[a-z]*r[a-z]*f?\s+/(\s|$)", r"\bmkfs(\.|\s)", r"\bdd\b.*\bof=/dev/", r":\(\)\s*\{\s*:\|:&\s*\};:",
           r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b", r">\s*/dev/sd[a-z]", r"\bshutdown\b|\breboot\b|\bpoweroff\b"]
READ_ONLY = {"ls", "cat", "head", "tail", "grep", "df", "du", "free", "uptime", "uname", "whoami", "hostname", "ps",
             "systemctl", "journalctl", "git", "date", "id", "ip", "ss", "nproc", "lsblk", "find", "wc", "stat"}
READ_ONLY_SUBCMDS = {"systemctl": {"status", "is-active", "is-enabled", "list-units", "show"},
                     "git": {"status", "log", "diff", "show", "branch", "rev-parse", "remote"}}


class ExecRefused(ValueError):
    pass


def hosts():
    return {"local": None, **(audit.config.section("exec").get("hosts") or {})}


def classify(cmd):
    """-> 'blocked' | 'read' | 'risky'. Pure."""
    if any(re.search(p, cmd) for p in BLOCKED):
        return "blocked"
    if re.search(r"[;&|`$<>]", cmd):          # chaining, pipes, substitution, redirects: not provably read-only
        return "risky"
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return "risky"
    if not argv or argv[0] not in READ_ONLY:
        return "risky"
    sub = READ_ONLY_SUBCMDS.get(argv[0])
    if sub:
        rest = [x for x in argv[1:] if x != "--user"]
        if not rest or rest[0] not in sub:
            return "risky"
    if argv[0] == "find" and any(x in argv for x in ("-delete", "-exec", "-execdir", "-ok")):
        return "risky"
    return "read"


def run(host, cmd, confirm=False, timeout=TIMEOUT_S, runner=subprocess.run):
    hs = hosts()
    if host not in hs:
        audit.record("exec", "run", "refused", host=host, cmd=cmd[:200], why="unknown host")
        raise ExecRefused(f"unknown host {host!r}; configured: {sorted(hs)}")
    kind = classify(cmd)
    if kind == "blocked":
        audit.record("exec", "run", "refused", host=host, cmd=cmd[:200], why="blocked pattern")
        raise ExecRefused("that command matches a blocked pattern and never runs")
    if kind == "risky" and not confirm:
        audit.record("exec", "run", "refused", host=host, cmd=cmd[:200], why="risky without confirm")
        raise ExecRefused("not on the read-only list: call again with confirm=true (the owner approves on the phone)")
    argv = ["bash", "-lc", cmd] if hs[host] is None else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", hs[host], cmd]
    try:
        r = runner(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        audit.record("exec", "run", "timeout", host=host, cmd=cmd[:200], kind=kind, timeout=timeout)
        return {"ok": False, "host": host, "error": f"timed out after {timeout}s"}
    out = {"ok": r.returncode == 0, "host": host, "rc": r.returncode, "kind": kind,
           "stdout": r.stdout[-MAX_OUT:], "stderr": r.stderr[-MAX_OUT:]}
    audit.record("exec", "run", "ok" if out["ok"] else f"rc={r.returncode}", host=host, cmd=cmd[:200], kind=kind)
    return out
