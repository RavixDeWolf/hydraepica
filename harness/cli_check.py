#!/usr/bin/env python3
"""cli_check.py - is this head about to start the Claude Code you expect? Runs before every head starts.

Why: after a power-cut reboot, every head in the lab this came from started an old system-wide install (it could not
self-update) instead of the user's own, because user units came up before the login PATH existed. Every turn then
failed with an API error, and nothing said why. This check makes that loud.

  expected = realpath of [head] cli in the config (default ~/.local/bin/claude, the self-updating native install)
  resolved = what `claude` resolves to on the unit's own PATH
  match    -> one log line
  mismatch -> log line + marker file + ONE Telegram line in that head's topic, once per boot + lane + binary.
              A failed send is logged and marked too. Never blocks the head (always exit 0).

  python3 cli_check.py --dry-run      print the verdict, send nothing
"""
import json
import os
import shutil
import subprocess
import sys
import time

_H = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _H)


def _paths():
    d = os.path.expanduser(os.environ.get("HEAD_STATE_DIR") or os.path.join(_state_root(), "main"))
    return d, os.path.join(d, "cli_check.log"), os.path.join(d, "CLI_MISMATCH"), os.path.join(d, "cli_check.sent.json")


def _state_root():
    import config
    return config.STATE_DIR


def expected_cli():
    try:
        import config
        return os.path.expanduser(config.section("head").get("cli", "~/.local/bin/claude"))
    except (Exception, SystemExit):
        return os.path.expanduser("~/.local/bin/claude")


def send_tg(text):
    try:
        r = subprocess.run([sys.executable, os.path.join(_H, "tg", "tg.py"), "send", text],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[-300:]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def check(path_env, expected_link):
    """-> (ok, resolved, expected). Pure apart from reading the filesystem."""
    expected = os.path.realpath(expected_link) if os.path.exists(expected_link) else None
    found = shutil.which("claude", path=path_env)
    resolved = os.path.realpath(found) if found else None
    return expected is not None and resolved == expected, resolved, expected


def run(lane, path_env, expected_link=None, dry_run=False, send=send_tg, boot=boot_id):
    d, log, marker, sent_f = _paths()
    os.makedirs(d, exist_ok=True)

    def note(line):
        with open(log, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")

    ok, resolved, expected = check(path_env, expected_link or expected_cli())
    head = f"lane={lane} resolved={resolved} expected={expected}"
    if dry_run:
        print(("OK " if ok else "MISMATCH ") + head)
        return ok
    if ok:
        note("OK " + head)
        return True
    note(f"MISMATCH {head} PATH={path_env}")
    with open(marker, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + head + "\n")
    key = f"{boot()}|{lane}|{resolved}"
    try:
        sent = json.load(open(sent_f))
    except (OSError, ValueError):
        sent = {}
    if key in sent:
        note(f"MISMATCH already said this boot ({key}), not re-sent")
        return False
    text = (f"⚠️ Head '{lane}' is starting {resolved or 'no claude on its PATH'} instead of "
            f"{expected or (expected_link or expected_cli()) + ' (missing)'}. Fix the unit's PATH (or [head] cli in "
            "the config) and restart it.")
    sent_ok, detail = send(text)
    if sent_ok:
        sent[key] = time.time()
        with open(sent_f, "w") as f:
            json.dump(sent, f)
        note(f"SAID tg ok: {detail[:120]}")
    else:
        note(f"SAY FAILED tg: {detail}")
        with open(marker, "a") as f:
            f.write(f"TG SEND FAILED: {detail}\n")
    return False


def main(argv):
    try:
        run(os.environ.get("HEAD_LANE") or "main", os.environ.get("PATH", ""), dry_run="--dry-run" in argv)
    except Exception as e:                       # never block the head, never be silent
        print(f"cli_check error: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
