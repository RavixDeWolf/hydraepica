"""audit.py - one append-only line per tool call: who asked, what, and how it ended.

Identity comes from the caller's own MCP config (HUB_IDENTITY in the head's environment, e.g. "head:main"), so every
call is attributed to the head that made it. A failed audit write is printed to stderr and never blocks the call:
the call's own result still says what happened.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness"))
import config  # noqa: E402

LOG = os.path.join(config.STATE_DIR, "hub_audit.jsonl")


def identity():
    return os.environ.get("HUB_IDENTITY", "unknown")


def record(tool, action, outcome, **kw):
    row = {"ts": round(time.time(), 3), "who": identity(), "tool": tool, "action": action, "outcome": outcome, **kw}
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        print(f"audit write failed: {e}", file=sys.stderr)
    return row
