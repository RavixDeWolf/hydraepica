#!/usr/bin/env python3
"""handoff.py - hand a task to another head thread.

  handoff.py --to techsup "Forwarded error: ... Diagnose, ticket it, report the ticket number."
  handoff.py --to main "TechSup finished; review the result."

The task is dropped in the target lane's spool (<state_dir>[/<lane>]/task_spool); the Telegram gateway
posts it visibly in that lane's thread ("📋 Task from ...") and queues it there as a turn. This head's own lane
(HEAD_LANE, default main) is the sender. Prints one JSON line.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # harness/
import config  # noqa: E402

BASE = config.STATE_DIR


def _lanes():
    """main + every lane in lanes.json (a new topic head is reachable at once, 2026-11-02). Unreadable = main only."""
    try:
        return tuple(["main"] + json.load(open(os.path.join(BASE, "lanes.json"))))
    except (OSError, ValueError):
        return ("main",)


def spool_for(lane):
    return os.path.join(BASE if lane == "main" else os.path.join(BASE, lane), "task_spool")


def handoff(to, text, frm=None):
    frm = frm or os.environ.get("HEAD_LANE", "main")
    if to not in _lanes():
        raise SystemExit(f"--to must be one of {_lanes()}")
    if to == frm:
        raise SystemExit("that is this head's own thread")
    if not text.strip():
        raise SystemExit("empty task")
    d = spool_for(to)
    os.makedirs(d, exist_ok=True)
    name = f"{time.time():.3f}-{frm}.json"
    tmp = os.path.join(d, name + ".tmp")
    with open(tmp, "w") as f:
        json.dump({"from": frm, "text": text, "ts": time.time()}, f)
    os.replace(tmp, os.path.join(d, name))          # the gateway only reads *.json: never half a file
    return {"ok": True, "to": to, "from": frm, "job": name}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True, choices=_lanes())
    ap.add_argument("text")
    a = ap.parse_args(argv)
    print(json.dumps(handoff(a.to, a.text)))


if __name__ == "__main__":
    sys.exit(main())
