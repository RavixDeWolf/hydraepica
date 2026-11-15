#!/usr/bin/env python3
"""recheck - hold a final reply ONCE when it claims success or absence without a real look. Optional, off by default:
turn it on for every head with [head] recheck = true, or for one head by touching <its state dir>/recheck_on.
Why: an audit of a few hundred corrections in the lab this came from found the top two slips were "done" with
nothing checked after the last change, and "can't / doesn't exist" with nothing searched.

Pure decision: decide(stop_payload, turn_tools) -> None (let it through) or a reason string (hold it once).
  * a success claim (done / sent / fixed / live / working / restarted ...) is held when the turn's LAST tool call
    was a change (Edit/Write/a mutating shell command) or there was no tool call at all: nothing looked at the result.
  * an absence claim (can't / doesn't exist / no such / not possible ...) is held when the turn ran no search or
    read at all.
  * stop_hook_active (already held once this turn) always lets it through: one hold per turn, never a loop.
Every decision is a row in recheck.jsonl (both outcomes) so the slip rate can be compared before/after.
"""
import json, os, re, time

SUCCESS = re.compile(r"\b(done|sent|fixed|shipped|deployed|restarted|is live|now live|are live|working now|"
                     r"it works|all set|finished|resolved|committed|playing)\b", re.I)
ABSENCE = re.compile(r"\b(can'?t|cannot|can not|doesn'?t exist|does not exist|no such|not possible|isn'?t possible|"
                     r"there is no|there'?s no|hasn'?t (happened|arrived|answered)|not available|unable to)\b", re.I)
CHANGE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
LOOK_TOOLS = {"Read", "Grep", "Glob", "WebFetch", "WebSearch"}
MUTATING_SH = re.compile(r"\b(sed -i|tee |mv |cp |rm |git commit|git push|systemctl (--user )?(restart|start|stop|"
                         r"enable)|kill |pkill|chmod|>\s*[~/\w])")
LOOKING_SH = re.compile(r"\b(cat|head|tail|grep|rg|ls|find|stat|curl|ps|pgrep|systemctl (--user )?(status|show|"
                        r"is-active)|journalctl|git (status|log|diff|show)|sqlite3|pytest|python3 -m pytest|wc|diff)\b")


def is_change(tool, inp):
    if tool in CHANGE_TOOLS:
        return True
    if tool == "Bash":
        cmd = (inp or {}).get("command", "")
        return bool(MUTATING_SH.search(cmd)) and not LOOKING_SH.search(cmd.split("&&")[-1].split(";")[-1])
    return False


def is_look(tool, inp):
    if tool in LOOK_TOOLS or tool.startswith("mcp__"):
        return True
    return tool == "Bash" and bool(LOOKING_SH.search((inp or {}).get("command", "")))


def turn_tools(transcript_path):
    """[(tool, input)] for the current turn: tool_use blocks after the last real user prompt."""
    out = []
    try:
        lines = open(transcript_path, encoding="utf-8").read().splitlines()
    except OSError:
        return None
    for l in lines:
        try:
            r = json.loads(l)
        except ValueError:
            continue
        m = r.get("message") or {}
        c = m.get("content")
        if r.get("type") == "user":
            if isinstance(c, str) or (isinstance(c, list) and any(b.get("type") == "text" for b in c
                                                                   if isinstance(b, dict))):
                out = []                                   # a real prompt starts a new turn
        elif r.get("type") == "assistant" and isinstance(c, list):
            out += [(b.get("name") or "", b.get("input") or {}) for b in c
                    if isinstance(b, dict) and b.get("type") == "tool_use"]
    return out


def decide(d, tools):
    if d.get("stop_hook_active"):
        return None
    text = d.get("last_assistant_message") or ""
    if tools is None:                                      # transcript unreadable: never block blind
        return None
    if SUCCESS.search(text) and (not tools or is_change(*tools[-1])):
        return ("Re-check before sending : this reply says something is done/working, but "
                + ("no tool ran this turn" if not tools else "the last step was a change and nothing looked at the "
                   "result after it")
                + ". Check the result the way the user will meet it (read the file back, run the test, reload the URL, "
                "check the process), then send the reply, corrected if the check says so. If the reply only mentions the word "
                "and claims nothing, send it unchanged.")
    if ABSENCE.search(text) and not any(is_look(*t) for t in tools):
        return ("Re-check before sending : this reply says something can't be done or doesn't exist, "
                "but nothing was searched or read this turn. Search first (files, memory, ls/ps, the device itself), "
                "then send the reply, corrected if the search says so. If it claims no absence, send it unchanged.")
    return None


def enabled(state_dir):
    if os.path.exists(os.path.join(state_dir, "recheck_on")):
        return True
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import config
        return bool(config.section("head").get("recheck"))
    except (Exception, SystemExit):
        return False


def log(state_dir, rec):
    rec.setdefault("ts", time.time())
    try:
        with open(os.path.join(state_dir, "recheck.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
