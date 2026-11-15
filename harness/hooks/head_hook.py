#!/usr/bin/env python3
"""claude-head hook (TIK-0361): Claude Code runs this on each hook event (settings: deploy/settings.example.json).

Every event is appended to <state_dir>/events.jsonl, so the Telegram lane follows the head's OWN events
(prompt in, tool calls, the final reply text) instead of scraping the terminal.

PermissionRequest is the one event that answers: while a Telegram turn is running (remote.json, written by
head_lane), the request is parked as perm/<id>.req, the lane shows Rav Allow / Deny buttons, and this hook waits
for perm/<id>.ans and returns the decision. No remote turn, no answer in time, or any error -> prints nothing,
so Claude Code shows its normal prompt in the terminal. Everything is logged.
"""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # hooks/ (leakcheck)
import time
import uuid

def _default_state():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # harness/
    import config
    return config.STATE_DIR


D = os.path.expanduser(os.environ.get("HEAD_STATE_DIR") or _default_state())
EV = os.path.join(D, "events.jsonl")
REMOTE = os.path.join(D, "remote.json")
PERM = os.path.join(D, "perm")
WAIT_S = float(os.environ.get("HEAD_PERM_WAIT_S", "560"))     # under the 600 s hook timeout in settings.json
REMOTE_MAX_AGE_S = 45 * 60


def append(rec):
    rec.setdefault("ts", time.time())
    tp = (rec.get("data") or {}).get("transcript_path")
    if tp and "tsize" not in rec:                  # where this event sits in the transcript (for /thoughts)
        try:
            rec["tsize"] = os.path.getsize(tp)
        except OSError:
            pass
    with open(EV, "a") as f:
        f.write(json.dumps(rec) + "\n")


def remote_active(now=None):
    try:
        r = json.load(open(REMOTE))
        return (now or time.time()) - float(r.get("since", 0)) < REMOTE_MAX_AGE_S
    except Exception:
        return False


def decision(behavior, message=None):
    d = {"behavior": behavior}
    if behavior == "deny":
        d["message"] = message or "Rav denied this from Telegram."
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}}


def wait_answer(pid, sleep=time.sleep, now=time.time, wait_s=None):
    """-> 'allow' | 'deny' | None (no answer in time)."""
    ans = os.path.join(PERM, pid + ".ans")
    end = now() + (WAIT_S if wait_s is None else wait_s)
    while now() < end:
        if os.path.exists(ans):
            try:
                b = json.load(open(ans)).get("behavior")
            except Exception:
                b = None
            if b in ("allow", "deny"):
                return b
        sleep(0.5)
    return None


MIDTURN = os.path.join(D, "midturn.jsonl")
MIDTURN_OFF = os.path.join(D, "midturn.off")
MIDTURN_DONE = os.path.join(D, "midturn_done.jsonl")   # TIK-1421: keys the running turn received


def midturn_note():
    """Messages Rav sent while this turn runs (head_lane writes them) -> one context line, each told once. ''."""
    try:
        off = int(open(MIDTURN_OFF).read().strip() or 0)
    except (OSError, ValueError):
        off = 0
    try:
        with open(MIDTURN, "rb") as f:
            f.seek(off)
            raw = f.read()
    except OSError:
        return ""
    raw = raw[:raw.rfind(b"\n") + 1]                 # a half-written line waits for the next tool call
    if not raw:
        return ""
    rows = []
    for line in raw.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("text"):
            rows.append(r)
    try:                                             # already run as its own turn by the queue: never re-tell it
        done = {json.loads(l).get("k") for l in open(MIDTURN_DONE).readlines()[-2000:] if l.strip()}
    except (OSError, ValueError):
        done = set()
    rows = [r for r in rows if not (r.get("k") and r["k"] in done)]
    keys = [r["k"] for r in rows if r.get("k")]
    if keys:                                         # TIK-1421: written BEFORE the offset moves, so a crash re-delivers, never loses
        with open(MIDTURN_DONE, "a") as f:
            for k in keys:
                f.write(json.dumps({"k": k, "ts": round(time.time(), 1)}) + "\n")
    with open(MIDTURN_OFF, "w") as f:
        f.write(str(off + len(raw)))
    now_ = [r["text"] for r in rows if r.get("k")]
    also = [r["text"] for r in rows if not r.get("k")]
    out = []
    if now_:
        out.append("[Rav, mid-turn] Rav sent this from Telegram while you are working. It is delivered to you NOW and will "
                   "NOT come again as a new message, so treat it as part of this turn: " + " | ".join(f'"{t}"' for t in now_)
                   + ". If it changes what you are doing (stop, wrong target, never mind, extra context), act on it now. "
                   "Rav sees ONLY your final reply (text you write between steps never reaches him) and this message only "
                   "gets a 👌, so your FINAL reply must answer it in its own line.")
    if also:
        out.append("Rav sent this from Telegram while you are working (it is also queued as your next message): "
                   + " | ".join(f'"{t}"' for t in also) + ". If it changes what you are doing, act on it now.")
    return "\n".join(out)


BG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tg", "bg.py")
SELF_REPORTING = ("bg.py", "progress_driver", "tg.py progress")      # these already show their own bar


def watch_background(d, spawn=None):
    """Rav 2026-10-27: every long job should show itself without anyone remembering. A Bash call that went to the
    background (asked for, or moved there by its timeout) gets bg.py --watch on its output file: silent if it ends
    within 60 s, otherwise the right indicator + a pinned bar + one done/FAILED line. Returns the watcher argv or None."""
    import glob
    import subprocess
    if d.get("tool_name") != "Bash":
        return None
    tid = (d.get("tool_response") or {}).get("backgroundTaskId") if isinstance(d.get("tool_response"), dict) else None
    cmd = (d.get("tool_input") or {}).get("command", "")
    if not tid or any(x in cmd for x in SELF_REPORTING):
        return None
    hits = glob.glob(f"/tmp/claude-*/*/{d.get('session_id', '*')}/tasks/{tid}.output")
    if not hits:
        append({"event": "bg_watch_no_file", "task": tid}); return None
    sys.path.insert(0, os.path.dirname(BG))
    import bg
    name = ((d.get("tool_input") or {}).get("description") or "background job")[:40]
    argv = [sys.executable, BG, "--watch", hits[0], "--name", name, "--kind", bg.kind_of(cmd)]
    for f in bg.redirect_logs(cmd, d.get("cwd")):         # the job writes to its own log: read progress there too
        argv += ["--also", f]
    (spawn or (lambda a: subprocess.Popen(a, stdout=subprocess.DEVNULL, stderr=open(os.path.join(D, "bg_watch.err"), "a"),
                                          stdin=subprocess.DEVNULL, start_new_session=True)))(argv)
    append({"event": "bg_watch_started", "task": tid, "name": name})
    return argv


def handle(d, out=sys.stdout):
    ev = d.get("hook_event_name")
    if ev == "PreToolUse":
        try:                                       # demo-safe lanes: step labels + mid-turn sends
            import leakcheck
            if leakcheck.enabled(D):
                probs = leakcheck.scan_tool(d.get("tool_name") or "", d.get("tool_input"))
                if probs:
                    append({"event": "leakcheck", "held": True, "tool": d.get("tool_name"), "problems": probs[:8]})
                    leakcheck.log(D, probs)
                    out.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                              "permissionDecision": "deny", "permissionDecisionReason": leakcheck.tool_reason(probs)}}))
                    return
        except Exception as e:
            append({"event": "leakcheck_error", "error": str(e)[:200]})
    if ev == "Stop":
        try:                                       # demo-safe lanes never send IPs/paths/keys, text or image
            import leakcheck
            if leakcheck.enabled(D):
                probs = leakcheck.scan_reply(d.get("last_assistant_message") or "")
                append({"event": "leakcheck", "held": bool(probs), "problems": probs[:8]})
                leakcheck.log(D, probs)
                if probs:                          # held, NOT logged as Stop, so the lane keeps waiting for the fix
                    out.write(json.dumps({"decision": "block", "reason": leakcheck.reason(probs)}))
                    return
        except Exception as e:                     # a broken check must not let a leak out silently: log loudly
            append({"event": "leakcheck_error", "error": str(e)[:200]})
    if ev != "PermissionRequest" or not remote_active():
        append({"event": ev, "data": d})
        if ev == "PostToolUse":
            try:
                watch_background(d)
            except Exception as e:                    # never break the head for a progress bar; log it
                append({"event": "bg_watch_error", "error": str(e)[:200]})
            note = midturn_note()
            if note:
                append({"event": "MidturnDelivered", "chars": len(note)})
                out.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                             "additionalContext": note}}))
        return
    os.makedirs(PERM, exist_ok=True)
    pid = uuid.uuid4().hex[:12]
    req = os.path.join(PERM, pid + ".req")
    with open(req, "w") as f:
        json.dump({"tool_name": d.get("tool_name"), "tool_input": d.get("tool_input")}, f)
    append({"event": ev, "perm_id": pid, "data": d})
    b = wait_answer(pid)
    append({"event": "PermissionAnswered", "perm_id": pid, "behavior": b or "timeout_to_terminal"})
    for p in (req, os.path.join(PERM, pid + ".ans")):
        try:
            os.remove(p)
        except OSError:
            pass
    if b:
        out.write(json.dumps(decision(b)))


if __name__ == "__main__":
    os.makedirs(D, exist_ok=True)
    raw = ""
    try:
        raw = sys.stdin.read()
        handle(json.loads(raw or "{}"))
    except Exception as e:                        # never break the head; say so in the log (Iron Rule 12)
        try:
            append({"event": "hook_error", "error": str(e)[:300], "raw": raw[:500]})
        except Exception:
            pass
