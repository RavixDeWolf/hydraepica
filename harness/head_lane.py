"""head_lane.py - Telegram thread "the main head" <-> the Claude Code head on the host (TIK-0361/0362).

Rav 2026-10-23 23:46: "ClaudeCode on the home server ... when I am mobile and my laptop is in the bag, I can still talk to the agent ...
same under models as in CDT so less variables." 2026-10-24 00:02: "go".

Path IN: Rav's message in their main head thread -> tg-gateway -> this lane -> head_bridge.py (unix socket,
<state_dir>/head.sock) -> typed into the Claude Code REPL in tmux.
Path OUT (v2, 2026-10-24): the head's own hook events (claude-head/hooks/head_hook.py -> events.jsonl), not its
screen: PreToolUse feeds one silent status bubble edited in place, PermissionRequest becomes an Allow / Deny card
the hook waits on, Stop carries the final reply text (rendered to Telegram HTML, numbered parts, MEDIA:/path lines
sent as files). The pane is read only for login/trust gates and as the fallback when a terminal prompt appears.

GATE: the configured owner only. The head runs as the agent with full lab tools; it is not a family feature.
One message at a time (a queue). /stop interrupts, /new clears the session, /status reports. Photos and files are
saved for the head to read; voice notes are transcribed on the host (the STT server). Everything is logged.
"""
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
from collections import deque

import tgapi       # Telegram send/reply/upload/download + the lane log
import chat_ledger  # the Telegram rows of the lab chat history: reply / forward context (TIK-1027)

# TIK-1082: one lane per head thread. "main" = the main head, the original (paths unchanged). A second head (e.g.
# "techsup") is this same module loaded again with _LANE set (see _load_lane): its own state dir, socket, thread
# file, queue and worker, so two threads never share a REPL, a queue or a reply target.
import config

HERE = os.path.dirname(os.path.abspath(__file__))
OWNER = config.USERS.get(config.OWNER_ID, "the owner")
AGENT = config.AGENT_NAME

_LANE = globals().get("_LANE", "main")
BASE_DIR = config.STATE_DIR
STATE_DIR = BASE_DIR if _LANE == "main" else os.path.join(BASE_DIR, _LANE)
HEAD_UIDS = {config.OWNER_ID: config.USERS.get(config.OWNER_ID, "owner")}
HEAD_THREADS = {config.OWNER_ID: None}   # filled with the real thread id at creation (see THREAD_FILE)
THREAD_FILE = os.path.join(STATE_DIR, "tg_threads.json")
SOCK = os.path.join(STATE_DIR, "head.sock")
EVENTS = os.path.join(STATE_DIR, "events.jsonl")
REMOTE = os.path.join(STATE_DIR, "remote.json")
PERM_DIR = os.path.join(STATE_DIR, "perm")
INBOX = os.path.join(STATE_DIR, "inbox")
# GPU the STT server first; the GPU broker stops it for image jobs on purpose, then the CPU copy on :8003 answers.
STT_URLS = config.STT_URLS
TURN_TIMEOUT_S = 30 * 60
POLL_S = 3
TG_LIMIT = 3900

_q = deque()
_qlock = threading.Lock()
_wake = threading.Event()
_state = {"waiting_permission": False, "worker": None}


def _load_threads():
    try:
        with open(THREAD_FILE) as f:
            for k, v in json.load(f).items():
                HEAD_THREADS[int(k)] = int(v)
    except Exception:
        pass


_load_threads()

# TIK-1131: a "/j only" lane shares its Telegram topic with another system. Rav's Email topic belongs to the email
# bot (tg_gateway EMAIL_LANES); only messages that start with "/j" come to this head, everything else stays the
# email bot's exactly as before. Replies still go into that topic.
J_ONLY_LANES = ("mail", "jmail", "notes")   # topics shared with another system (email bot, notes capture)
KEEP_PLAIN_LANES = ("notes",)               # plain text stays a note; only "/j ..." reaches the head
J_ONLY = _LANE in J_ONLY_LANES
_J_PREFIX = re.compile(r"^/j(?:@\w+)?(?:\s+|$)", re.I)


def j_body(text):
    """'/j check the queue' -> 'check the queue'; '/j' -> ''; '/jinbox', '/jmail', 'hello', None -> None. Pure."""
    if not isinstance(text, str):
        return None
    t = text.lstrip()
    mt = _J_PREFIX.match(t)
    return t[mt.end():].strip() if mt else None


# Rav 2026-10-30: "we don't need a /j for the email thread" - every email-bot command is a /command, and its plain
# text went to the host's v4 endpoint and nothing came back. One exception, kept: after Reply/Forward the email bot waits
# for the body as plain text (its session "awaiting"), so that text stays the email bot's.
EMAIL_SESSION = os.path.expanduser(config.section("email").get("session_file", "/nonexistent"))   # optional email bot


def email_awaiting(path=None):
    """True while the email bot waits for a reply/forward body. Unreadable session = True (the bot keeps it)."""
    try:
        with open(path or EMAIL_SESSION) as f:
            return bool((json.load(f) or {}).get("awaiting"))
    except FileNotFoundError:
        return False
    except Exception:
        return True


def shared_body(text, awaiting=email_awaiting):
    """A shared-topic message -> what this head gets, or None (the topic's own bot keeps it). Pure but for awaiting.
    '/j x' -> 'x'; '/inbox', '/email ...' -> None; 'hello' -> 'hello', unless the email bot is awaiting a body."""
    if not isinstance(text, str):
        return None
    b = j_body(text)
    if b is not None:
        return b
    t = text.strip()
    if not t or t.startswith("/") or awaiting():
        return None
    return t


def lane_body(text, lane=None):
    """shared_body for this lane: a Notes topic keeps its plain text (it is a note), so only '/j x' -> 'x'."""
    if (lane or _LANE) in KEEP_PLAIN_LANES:
        return j_body(text) if isinstance(text, str) else None
    return shared_body(text)


# ---------------------------------------------------------------- pure
_SEP = re.compile(r"^[─━]{10,}")
_DONE = re.compile(r"^✻ ")
_TOOLISH = re.compile(r"^● (\w[\w\-]*\(|.*\(MCP\)|.* - .*\(MCP\))")


def extract_reply(lines):
    """The assistant text of the LAST turn on the pane, or ''. Pure.

    A turn = the last non-empty '❯ <text>' input line; the reply = the '● ' blocks after it, up to the
    '✻ ... done' line or the next separator. Tool-call blocks ('● Bash(...)', '... (MCP)') are skipped."""
    start = None
    for i, ln in enumerate(lines):
        s = ln.rstrip()
        if s.startswith("❯ ") and s[2:].strip():
            start = i
    if start is None:
        return ""
    out, cur, keep = [], [], False
    for ln in lines[start + 1:]:
        s = ln.rstrip()
        if _DONE.match(s.strip()) or _SEP.match(s.strip()):
            break
        if s.startswith("● "):
            if cur and keep:
                out.append("\n".join(cur))
            cur, keep = [s[2:]], not _TOOLISH.match(s)
        elif cur and (s.startswith("  ") or s == ""):
            if s.strip().startswith("⎿"):
                continue
            cur.append(s[2:] if s.startswith("  ") else "")
    if cur and keep:
        out.append("\n".join(cur))
    return "\n\n".join(t.strip() for t in out if t.strip())


_RULE = re.compile(r"^[\s─━╌┄┈═\-_│|]*$")
_OPT = re.compile(r"^\s*(?:❯\s*)?(\d)\.\s+(.*)$")


def parse_permission(lines):
    """Claude Code's permission box, read off the pane -> {title, detail, question, options:[(n,label)]}. Pure.

    The box is the text after the LAST horizontal rule above the options: tool name, description, the
    command, a warning or two, 'Do you want to proceed?', numbered options (labels may wrap), then 'Esc to cancel'."""
    rows = [l.rstrip() for l in lines]

    def block(at):
        opts, i = [], at
        while i < len(rows):
            mo = _OPT.match(rows[i])
            if mo:
                opts.append([mo.group(1), mo.group(2).strip()])
            elif rows[i].strip().lower().startswith("esc to") or not rows[i].strip():
                break
            elif opts:
                opts[-1][1] += " " + rows[i].strip()
            i += 1
        return opts

    # The real box is the LAST "1." block on screen whose options are Yes/No answers. Taking the FIRST "1." picked
    # a numbered list in the head's own reply ("1. Channel: Skywolf...") and posted it as the question, with its
    # lines as buttons, again on every re-check (TIK-1416, 2026-11-01 19:02).
    first, opts = None, []
    for j, l in enumerate(rows):
        mo = _OPT.match(l)
        if mo and mo.group(1) == "1":
            b = block(j)
            if any(lab.lower().startswith(("yes", "no")) for _, lab in b):
                first, opts = j, b
    if first is None:
        return None
    body = [l.strip().lstrip("│|").strip() for l in rows[:first]]
    # the box starts after the last rule that has text below it
    start = 0
    for j, l in enumerate(body):
        if l and _RULE.match(l) and len(l) > 10:
            start = j + 1
    body = [l for l in body[start:] if l and not _RULE.match(l)]
    question = body.pop() if body and body[-1].endswith("?") else "Do you want to proceed?"
    title = body.pop(0) if body else "Permission"
    body = [l for l in body if not l.lower().startswith("tip:")]
    return {"title": title, "detail": "\n".join(body)[-1200:], "question": question,
            "options": [(n, re.sub(r"\s+", " ", lab)) for n, lab in opts]}


def button_label(n, label):
    """Short words for a Telegram button. 'No' always cancels with Escape."""
    low = label.lower()
    if low.startswith("no"):
        return "❌ No"
    if "auto mode" in low:
        return "⚡ Yes + auto mode"
    if "don't ask again" in low or "always" in low:
        return "✅ Always allow"
    if low.startswith("yes"):
        return "✅ Yes"
    return f"{n}. {label[:28]}"


def permission_card(perm):
    """Text + inline keyboard for the permission question. Callback data: hd:<key>."""
    text = f"🔐 {perm['title']}\n\n{perm['detail']}\n\n{perm['question']}".strip()
    rows = []
    for n, lab in perm["options"]:
        key = "Escape" if lab.lower().startswith("no") else n
        rows.append([{"text": button_label(n, lab), "callback_data": f"hd:{key}"}])
    return text[:TG_LIMIT], {"inline_keyboard": rows}


def chunks(text, n=TG_LIMIT):
    text = text or ""
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


SPILL_AT = 7000          # head_bridge.MAX_SEND is 8000; a longer prompt is saved to a file the head reads


def fit(prompt, folder=None):
    """A prompt too long for the bridge -> its start + where the whole text is saved (Rav 2026-10-30, a 4000+ paste)."""
    if len(prompt) <= SPILL_AT:
        return prompt
    folder = folder or os.path.join(STATE_DIR, "long")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, time.strftime("%Y%m%d-%H%M%S") + f"_{len(prompt)}.txt")
    with open(path, "w") as f:
        f.write(prompt)
    tgapi._log("head", event2="prompt_spilled", chars=len(prompt), path=path)
    return prompt[:1500] + f" ... [message is {len(prompt)} characters; the WHOLE message is saved at {path} - Read it first]"


def flatten(text):
    """The REPL submits on Enter, so a multi-line Telegram message is joined into one line."""
    return re.sub(r"\s*\n\s*", " / ", (text or "").strip())


# "always" is NOT a text answer: on 2026-10-24 it mapped to option 2 = "switch to auto mode" and silently changed
# the head's mode. Typed answers are yes or no; anything else is a button.
PERM_ANSWERS = {"yes": "1", "y": "1", "1": "1", "ok": "1", "allow": "1",
                "no": "Escape", "n": "Escape", "deny": "Escape"}


def perm_answer(text):
    """A typed answer to a permission question -> (key, rest) or (None, None). The first word decides; what follows
    rides along as a note (Rav 2026-11-05: "/j Yes, and also that button doesn't seem to work" was refused). Pure."""
    t = re.sub(r"^/j(?:\s+|$)", "", (text or "").strip(), flags=re.I)
    mt = re.match(r"([a-z0-9]+)(?:[\s,.!:;-]+(.*))?$", t, re.I | re.S)
    if not mt or mt.group(1).lower() not in PERM_ANSWERS:
        return None, None
    return PERM_ANSWERS[mt.group(1).lower()], (mt.group(2) or "").strip() or None


def tool_brief(name, inp):
    """One short line for the status bubble. Pure."""
    inp = inp or {}
    if name == "Bash":
        return "⚙️ " + (inp.get("description") or inp.get("command") or "").strip().split("\n")[0][:70]
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        return {"Read": "📖 ", "Edit": "✏️ ", "Write": "📝 ", "NotebookEdit": "✏️ "}[name] + os.path.basename(
            inp.get("file_path") or inp.get("notebook_path") or "")
    if name in ("Grep", "Glob"):
        return "🔍 " + (inp.get("pattern") or "")[:60]
    if name.startswith("mcp__"):
        parts = name.split("__")
        act = inp.get("action")
        return "🛰 " + parts[-1] + (f" {act}" if isinstance(act, str) else "")
    if name in ("Agent", "Task"):
        return "🧠 BOB: " + (inp.get("description") or "")[:60]
    if name in ("WebSearch", "WebFetch"):
        return "🌐 " + (inp.get("query") or inp.get("url") or "")[:60]
    return "• " + name


def status_text(tools, t0, now, done=False):
    """The status bubble: last 6 steps + elapsed. Pure."""
    secs = int(now - t0)
    head = (f"✅ Done · {len(tools)} step(s) · {secs}s" if done else f"⏳ Working · {secs}s")
    shown = tools[-6:]
    more = len(tools) - len(shown)
    body = ([f"… {more} earlier"] if more > 0 else []) + shown
    return head + ("\n" + "\n".join(body) if body else "")


_MEDIA = re.compile(r"^\s*MEDIA:\s*(/\S+)\s*$", re.M)


def media_lines(text):
    """'MEDIA:/abs/path' lines -> files to send. Returns (text without them, [paths]). Pure."""
    paths = _MEDIA.findall(text or "")
    return _MEDIA.sub("", text or "").strip(), paths


TURNS = os.path.join(STATE_DIR, "turns.json")           # answer message id -> transcript span (for /thoughts)
OPEN_THOUGHTS = os.path.join(STATE_DIR, "thoughts_open.json")
TURNS_KEEP = 300


def _jload(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default


def _jsave(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, p)


def remember_turn(ids, span, path=None):
    path = path or TURNS
    t = _jload(path, {})
    for i in ids:
        t[str(i)] = span
    if len(t) > TURNS_KEEP:
        for k in sorted(t, key=int)[:-TURNS_KEEP]:
            t.pop(k)
    _jsave(path, t)


def thoughts_for(span):
    """The thinking + tool steps of one turn, read from the transcript bytes [start, end). Pure over the file.
    -> list of ("think", text) | ("step", line)."""
    try:
        with open(span["path"], "rb") as f:
            f.seek(int(span.get("start") or 0))
            data = f.read(max(0, int(span["end"]) - int(span.get("start") or 0)))
    except Exception:
        return []
    out = []
    for line in data.split(b"\n"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        for b in (d.get("message") or {}).get("content") or []:
            if b.get("type") == "thinking" and (b.get("thinking") or "").strip():
                out.append(("think", b["thinking"].strip()))
            elif b.get("type") == "tool_use":
                out.append(("step", tool_brief(b.get("name") or "", b.get("input"))))
    return out


def render_thoughts(items, limit=3600):
    """-> Telegram HTML parts: thinking in expandable quotes, tool steps between them. Pure."""
    if not any(k == "think" for k, _ in items):
        head = "💭 No thinking was recorded for this answer."
        steps = [v for k, v in items if k == "step"]
        return [head + ("\n\nSteps:\n" + _esc("\n".join(steps)) if steps else "")]
    parts, cur = [], "💭 <b>How I got there</b>\n"
    for k, v in items:
        piece = (f"<blockquote expandable>{_esc(v)}</blockquote>\n" if k == "think" else _esc(v) + "\n")
        if len(cur) + len(piece) > limit and cur.strip():
            parts.append(cur); cur = ""
        while len(piece) > limit:                  # a single huge thought: cut it into quotes
            body = v[:limit - 60]; v = v[limit - 60:]
            parts.append(f"<blockquote expandable>{_esc(body)}</blockquote>")
            piece = f"<blockquote expandable>{_esc(v)}</blockquote>\n"
        cur += piece
    if cur.strip():
        parts.append(cur)
    return parts


def show_thoughts(tok, m):
    """/thoughts as a reply to one of the head's answers -> the thinking behind it, with a Close button."""
    rt = (m.get("reply_to_message") or {}).get("message_id")
    span = _jload(TURNS, {}).get(str(rt)) if rt else None
    if not span:
        tgapi._reply(tok, m, "Reply to one of my answers in this thread with /thoughts to see how I got there.")
        return
    key = os.urandom(4).hex()
    kb = {"inline_keyboard": [[{"text": "✖ Close", "callback_data": f"hd:x:{key}"}]]}
    sent = []
    parts = render_thoughts(thoughts_for(span))
    for i, html in enumerate(parts):
        kw = {"chat_id": m["chat"]["id"], "text": html, "parse_mode": "HTML", "disable_notification": True,
              "reply_to_message_id": rt}
        if m.get("message_thread_id") is not None:
            kw["message_thread_id"] = m["message_thread_id"]
        if i == len(parts) - 1:
            kw["reply_markup"] = kb
        try:
            sent.append(tgapi._api(tok, "sendMessage", **kw)["result"]["message_id"])
        except Exception as e:
            tgapi._log("head", event2="thoughts_fail", error=str(e)[:200])
    o = _jload(OPEN_THOUGHTS, {})
    o[key] = {"chat": m["chat"]["id"], "ids": sent + [m.get("message_id")]}
    _jsave(OPEN_THOUGHTS, o)
    tgapi._log("head", event2="thoughts", answer=rt, parts=len(sent))


def close_thoughts(cq, data, tok, answer):
    """hd:x:<key> -> delete the thoughts messages (and the /thoughts command). Rav only."""
    if (cq.get("from") or {}).get("id") not in HEAD_UIDS:
        answer("Not yours."); return
    o = _jload(OPEN_THOUGHTS, {})
    g = o.pop(data.split(":", 2)[2], None)
    if not g:
        answer("Already closed."); return
    for mid in g["ids"]:
        if mid is None:
            continue
        try:
            tgapi._api(tok, "deleteMessage", chat_id=g["chat"], message_id=mid)
        except Exception as e:
            tgapi._log("head", event2="thoughts_delete_fail", message_id=mid, error=str(e)[:150])
    _jsave(OPEN_THOUGHTS, o)
    answer("Closed.")


RICH_LIMIT = 30000       # Telegram's rich-message cap is 32768 characters; leave room for escapes
MD_KEEP = 20000          # answer text kept per turn for /show


def rich_md(md):
    """Claude markdown -> Telegram rich markdown. Pure. '$' starts a formula there, so prices like $10 and $20
    would render as math: escape '$' outside code (inside `code` and ``` blocks it stays literal)."""
    out, fence = [], False
    for line in (md or "").split("\n"):
        if line.strip().startswith("```"):
            fence = not fence
            out.append(line)
            continue
        if fence:
            out.append(line)
            continue
        parts = line.split("`")
        for i in range(0, len(parts), 2):          # even pieces are outside inline code
            parts[i] = re.sub(r"(?<!\\)\$", r"\\$", parts[i])
        out.append("`".join(parts))
    return "\n".join(out)


_VOICE = re.compile(r"^\s*VOICE:\s*(.+)$", re.M)
SPEAK_MAX = 700


def voice_block(md):
    """'VOICE: ...' lines -> what the head chose to say aloud; returned body has them removed. Pure."""
    said = " ".join(x.strip() for x in _VOICE.findall(md or ""))
    return _VOICE.sub("", md or "").strip(), said


def speakable(md, limit=SPEAK_MAX):
    """A reply as something a voice can say: no code blocks, tables, links or markup; cut at a sentence. Pure."""
    s = re.sub(r"```.*?```", " ", md or "", flags=re.S)
    s = "\n".join(l for l in s.split("\n") if not l.strip().startswith("|"))
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"[*_#>~]+", "", s)
    s = re.sub(r"^\s*[-•]\s+", "", s, flags=re.M)
    s = " ".join(s.split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > limit // 2 else cut.rstrip() + "…")


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_to_html(md):
    """Claude's markdown -> Telegram HTML (b, i, code, pre, a). Pure. Unknown syntax stays as text."""
    out, blocks = [], []

    def stash(html):
        blocks.append(html)
        return f"\x00{len(blocks) - 1}\x00"

    s = md or ""
    s = re.sub(r"```[\w+-]*\n?(.*?)```", lambda m: stash("<pre>" + _esc(m.group(1).rstrip("\n")) + "</pre>"), s, flags=re.S)
    s = re.sub(r"`([^`\n]+)`", lambda m: stash("<code>" + _esc(m.group(1)) + "</code>"), s)
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
               lambda m: stash(f'<a href="{_esc(m.group(2))}">' + _esc(m.group(1)) + "</a>"), s)
    s = _esc(s)
    for line in s.split("\n"):
        h = re.match(r"^#{1,6}\s+(.*)$", line)
        if h:
            line = f"<b>{h.group(1)}</b>"
        line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
        out.append(line)
    s = "\n".join(out)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m.group(1))], s)
    return s


def split_parts(md, n=3500):
    """Long markdown -> parts under n chars, cut at line ends, never inside a code fence; '(i/N)' when N > 1. Pure."""
    parts, cur, fence = [], [], False
    for line in (md or "").split("\n"):
        while len(line) > n:                      # one giant line: hard cut
            parts.append("\n".join(cur + [line[:n]])); cur = []; line = line[n:]
        if sum(len(x) + 1 for x in cur) + len(line) > n and cur:
            if fence:
                cur.append("```")
            parts.append("\n".join(cur))
            cur = ["```"] if fence else []
        cur.append(line)
        if line.strip().startswith("```"):
            fence = not fence
    if cur:
        parts.append("\n".join(cur))
    parts = [p for p in parts if p.strip()] or [""]
    if len(parts) > 1:
        parts = [f"({i}/{len(parts)}) " + p for i, p in enumerate(parts, 1)]
    return parts


def read_events(off, path=None):
    """New events.jsonl records from byte offset off -> (records, new_off). A half-written last line waits."""
    path = path or EVENTS
    try:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read()
    except FileNotFoundError:
        return [], off
    recs, used = [], 0
    for line in data.split(b"\n")[:-1]:
        used += len(line) + 1
        try:
            recs.append(json.loads(line))
        except Exception:
            pass
    return recs, off + used


def events_size(path=None):
    try:
        return os.path.getsize(path or EVENTS)
    except OSError:
        return 0


# ---------------------------------------------------------------- indicators
# Rav 2026-10-24: "all the indicators wired intelligently". A reaction on HIS message carries the state
# (queued -> working -> needs you -> done / failed); the chat's typing bar runs only while the head works.
REACT = {"queued": "👀", "working": "✍", "permission": "🙏", "done": "👍", "failed": "👎",
         "seen": "👌"}   # seen: taken into the turn that was running (TIK-1421); its answer is in that turn's reply


def _react(tok, m, state):
    try:
        tgapi._api(tok, "setMessageReaction", chat_id=m["chat"]["id"], message_id=m["message_id"],
                        reaction=[{"type": "emoji", "emoji": REACT[state]}])
    except Exception as e:
        tgapi._log("head", event2="react_fail", state=state, error=str(e)[:150])


# TIK-1100 (Rav 2026-10-28): "if I delete it in time, great". The Bot API sends NO deletion updates in private chats, so the
# only way to know is to ask, at the moment a queued message is about to be handed to the head. Reacting to a message that no
# longer exists answers "Bad Request: message to react not found" (probed 2026-10-31 with an id that does not exist); the
# reaction we set (the queued eyes) is the one it already carries, so a live message sees no change. NO hold: nothing is ever
# delayed to give the check a window. FAIL OPEN: any other answer or error delivers the message, logged.
GONE_MARK = "message to react not found"
GONE_MIN_AGE_S = 2.0          # a message that was not queued behind anything cannot have been deleted in the last 2 s
GONE_OFF = os.path.join(STATE_DIR, "gone_check.off")      # touch to switch the check off


def message_gone(tok, m, api=None):
    """-> True only when Telegram says this message no longer exists. Anything else (network, 429, other 400s) is False."""
    api = api or tgapi._api
    try:
        api(tok, "setMessageReaction", chat_id=m["chat"]["id"], message_id=m["message_id"],
            reaction=[{"type": "emoji", "emoji": REACT["queued"]}])
        return False
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        if e.code == 400 and GONE_MARK in body:
            return True
        tgapi._log("head", event2="gone_check_failed", lane=_LANE, message_id=m.get("message_id"),
                        error=f"HTTP {e.code} {body[:120]}", delivered=True)
        return False
    except Exception as e:
        tgapi._log("head", event2="gone_check_failed", lane=_LANE, message_id=m.get("message_id"),
                        error=str(e)[:150], delivered=True)
        return False


def drop_if_deleted(tok, m, now=time.time, gone=None):
    """-> True when Rav deleted this still-queued message: it is dropped (never handed to the head) and the drop is logged in
    the print log and the chat ledger (Iron Rule 12: no silent drops). Only plain inbound text carries the mark."""
    if not m.get("_gone_check") or os.path.exists(GONE_OFF):
        return False
    age = now() - (m.get("_q_ts") or now())
    if age < GONE_MIN_AGE_S:
        return False
    if not (gone or message_gone)(tok, m):
        return False
    tgapi._log("head", event2="dropped_deleted", lane=_LANE, message_id=m.get("message_id"), age_s=round(age, 1))
    try:
        chat_ledger.record_dropped(m["chat"]["id"], m.get("message_id"), m.get("message_thread_id"), _LANE, age)
    except Exception as e:
        tgapi._log("head", event2="drop_ledger_fail", lane=_LANE, error=str(e)[:150])
    return True


# Rav 2026-10-26: "say sending image while you are editing/processing an image to be sent". A tool call that works
# on an image switches the chat indicator from typing to upload_photo; it holds through a run of image steps.
_IMG_WORK = re.compile(r"jedit\.py|layout\.py|ba\.py|render\.py|vectorize|cover\.py|retouch|upright|"
                       r"\.(png|jpe?g|webp|gif|heic|tiff?)\b|from PIL|Image\.open|stage\.py .*image", re.I)
# Rav 2026-10-26: "while video work is happening can the indicator say sending video". Checked BEFORE images:
# a video job touches stills too, and the bigger job should win.
_VID_WORK = re.compile(r"ffmpeg|ffprobe|motion\.py|itk_run|infinitetalk|latentsync|assemble\.sh|"
                       r"clip\.py|\.(mp4|mov|webm|mkv)\b", re.I)
_IMG_TOOLS = ("browser_take_screenshot", "image", "screenshot", "comfy", "flux", "genfill")
PHOTO_HOLD = 25.0


def chat_action_for(tool, inp):
    """'upload_video' for video work, 'upload_photo' for image work, else 'typing'. Pure."""
    tool = (tool or "").lower()
    inp = inp or {}
    blob = " ".join(str(inp.get(k) or "") for k in ("command", "file_path", "path", "code", "filename"))
    if _VID_WORK.search(blob):
        return "upload_video"
    if any(k in tool for k in _IMG_TOOLS):
        return "upload_photo"
    return "upload_photo" if _IMG_WORK.search(blob) else "typing"


class _Typing:
    """sendChatAction lasts ~5 s, so it is renewed every 4 s until the turn ends (or the upload finishes).
    action: typing, or upload_photo / upload_document while a file goes up (Rav 2026-10-26: a photo that lagged
    on mobile looked missing; with the indicator it reads as on its way)."""
    def __init__(self, tok, m, action="typing"):
        self.tok, self.m, self.stop, self.action = tok, m, threading.Event(), action

    def __enter__(self):
        def run():
            kw = {"chat_id": self.m["chat"]["id"]}
            if self.m.get("message_thread_id") is not None:
                kw["message_thread_id"] = self.m["message_thread_id"]
            fails = 0
            while not self.stop.is_set():
                kw["action"] = self.action() if callable(self.action) else self.action
                try:
                    tgapi._api(self.tok, "sendChatAction", **kw)
                except Exception as e:
                    fails += 1
                    if fails == 1:
                        tgapi._log("head", event2="typing_fail", error=str(e)[:150])
                self.stop.wait(4)
        threading.Thread(target=run, daemon=True, name="head-typing").start()
        return self

    def __exit__(self, *a):
        self.stop.set()


def _send_card(tok, m, text, kb):
    kw = {"chat_id": m["chat"]["id"], "text": text, "reply_markup": kb}
    if m.get("message_thread_id") is not None:
        kw["message_thread_id"] = m["message_thread_id"]
    try:
        tgapi._api(tok, "sendMessage", **kw)
    except Exception as e:
        tgapi._log("head", event2="card_fail", error=str(e)[:200])
        tgapi._reply(tok, m, text + "\n\nAnswer: yes / always / no")


def card_fingerprint(perm, lines):
    """TIK-1416: one prompt = one card. The same box seen again (the turn re-followed, a button pressed, a re-check) is
    NOT a new question; its card was already posted. Keyed by what the card would say, or by the pane tail when unparsed."""
    if perm:
        return json.dumps([perm.get("title"), perm.get("detail"), perm.get("question"), perm.get("options")], sort_keys=True)
    tail = [l.rstrip() for l in (lines or [])[-18:] if l.strip() and not _RULE.match(l)]
    return "tail:" + "\n".join(tail)[-1500:] if tail else None


def _perm_closed():
    """The prompt is answered or gone: the next box is a new question and gets its own card."""
    _state["waiting_permission"] = False
    _state["legacy_card"] = None


def live_box(call):
    """The permission box on the pane NOW (parsed), or None. Read fresh every time: the bridge's state word is a
    classifier, the box itself is the evidence (TIK-1416: taps on a gone box typed '1321' into the REPL)."""
    lines = (call({"t": "tail", "lines": 120}, wait=10) or {}).get("lines") or []
    return parse_permission(list(lines)[-60:])


def key_allowed(perm, key):
    """A key goes to the REPL only while the box is on screen and the key is one of ITS options (Escape = cancel it)."""
    if perm is None:
        return False
    return key == "Escape" or key in [n for n, _ in perm.get("options") or []]


def _mark_card(tok, msg, data, fallback):
    try:
        chosen = next((b["text"] for row in (msg.get("reply_markup") or {}).get("inline_keyboard", [])
                       for b in row if b.get("callback_data") == data), fallback)
        tgapi._api(tok, "editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                        text=(msg.get("text") or "") + f"\n\n→ {chosen}")
    except Exception as e:
        tgapi._log("head", event2="card_edit_fail", error=str(e)[:150])


def on_perm_button(cq, data, tok, answer):
    """hd:p:<pid>:a|d -> the waiting hook's answer file. Rav only; a request no longer parked is closed."""
    uid = (cq.get("from") or {}).get("id")
    parts = data.split(":")
    if uid not in HEAD_UIDS or len(parts) != 4 or parts[3] not in ("a", "d") or not re.fullmatch(r"[0-9a-f]{12}", parts[2]):
        tgapi._log("head_refused_button", uid=uid, data=data[:30])
        answer("Not yours."); return
    pid, verdict = parts[2], ("allow" if parts[3] == "a" else "deny")
    if not os.path.exists(os.path.join(PERM_DIR, pid + ".req")):
        answer("That question is already closed."); return
    tmp = os.path.join(PERM_DIR, pid + ".ans.tmp")
    with open(tmp, "w") as f:
        json.dump({"behavior": verdict, "by": uid, "ts": time.time()}, f)
    os.replace(tmp, os.path.join(PERM_DIR, pid + ".ans"))
    tgapi._log("head", event2="perm_button", perm_id=pid, behavior=verdict)
    answer("Allowed." if verdict == "allow" else "Denied.")
    _mark_card(tok, cq.get("message") or {}, data, verdict)


def on_button(cq, data, tok, call=None):
    """hd:<key> pressed on a terminal-prompt card (fallback). Rav only; the head must still be on the prompt.
    hd:p:... is the hook path (on_perm_button)."""
    call = call or bridge
    uid = (cq.get("from") or {}).get("id")
    msg = cq.get("message") or {}
    key = data.split(":", 1)[1]
    def answer(t):
        try:
            tgapi._api(tok, "answerCallbackQuery", callback_query_id=cq.get("id"), text=t)
        except Exception as e:
            tgapi._log("head", event2="cb_answer_fail", error=str(e)[:150])
    if data.startswith("hd:p:"):
        return on_perm_button(cq, data, tok, answer)
    if data.startswith("hd:x:"):
        return close_thoughts(cq, data, tok, answer)
    if data.startswith("hd:m:"):
        return on_model_button(cq, data, tok, answer, call=call)
    if data.startswith("hd:u:"):
        return on_choice_button(cq, data, tok, answer)
    if uid not in HEAD_UIDS or key not in ("1", "2", "3", "4", "Escape"):
        tgapi._log("head_refused_button", uid=uid, data=data[:20])
        answer("Not yours."); return
    st = (call({"t": "status"}) or {}).get("state")
    if st != "permission_prompt":
        answer("That question is already closed."); return
    perm = live_box(call)
    if not key_allowed(perm, key):                     # TIK-1416: the box is gone or never offered this number: no key
        tgapi._log("head", event2="button_stale", key=key, box=bool(perm))
        _perm_closed()
        answer("That question is already closed."); return
    call({"t": "key", "key": key})
    _perm_closed()
    tgapi._log("head", event2="button", key=key)
    answer("Denied." if key == "Escape" else "Allowed.")
    try:
        chosen = next((b["text"] for row in (msg.get("reply_markup") or {}).get("inline_keyboard", [])
                       for b in row if b.get("callback_data") == data), key)
        tgapi._api(tok, "editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                        text=(msg.get("text") or "") + f"\n\n→ {chosen}")
    except Exception as e:
        tgapi._log("head", event2="card_edit_fail", error=str(e)[:150])
    # follow the rest of the turn and relay it, in the queue like any message
    src = dict(msg, message_id=msg.get("message_id"))
    with _qlock:
        _q.append((src, None))
        if _state["worker"] is None:
            t = threading.Thread(target=_serve, args=(tok,), daemon=True, name="head-lane")
            t.start(); _state["worker"] = t
    _wake.set()


# ---------------------------------------------------------------- bridge
def bridge(req, wait=5, sock=None):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(wait)
    s.connect(sock or SOCK)
    s.sendall((json.dumps(req) + "\n").encode())
    buf = b""
    try:
        while b"\n" not in buf:
            c = s.recv(65536)
            if not c:
                break
            buf += c
    finally:
        s.close()
    return json.loads(buf.split(b"\n")[0] or b"{}")


GATES = ("dead", "login_required", "trust_prompt", "setup_prompt")


# 2026-10-24 05:05: the head was editing its own bridge tests; the screen showed the test line
# `assert c(["Please run /login", ...]) == "login_required"` and the bridge called the head logged out, so the lane
# dropped a live turn and refused 3 messages. A soft gate is real only when its words sit in the LAST lines of the
# screen (where Claude Code actually prompts) - and, mid-turn, only after a quiet spell with no hook events.
_GATE_WORDS = {
    "login_required": re.compile(r"(please run /login|not logged in|select login method|paste code here|log in to continue)", re.I),
    "trust_prompt": re.compile(r"(do you trust the files in this folder|trust this folder)", re.I),
    "setup_prompt": re.compile(r"(choose the text style|select a theme|let's get started)", re.I),
}
GATE_TAIL = 6
GATE_QUIET_S = 30


def gate_of(resp):
    """The bridge's status -> a gate that is really on screen ('dead' / 'login_required' / ...), or None. Pure."""
    st = (resp or {}).get("state")
    if st == "dead":
        return st
    words = _GATE_WORDS.get(st)
    tail = [l for l in ((resp or {}).get("tail") or []) if l.strip()][-GATE_TAIL:]
    if words and any(words.search(l) for l in tail):
        return st
    return None


def follow(off, io, call=bridge, read=read_events, sleep=time.sleep, now=time.time, timeout=TURN_TIMEOUT_S):
    """Follow the head's hook events from byte offset off until the turn ends.

    -> (outcome, payload, off): ('done', reply_text) | ('failed', why) | ('tui_prompt', None) | ('timeout', None).
    PreToolUse -> status bubble; PermissionRequest (parked by the hook) -> Allow/Deny card; Stop -> the reply.
    The pane is only checked for a dead/login gate and for a terminal prompt nobody parked (the fallback)."""
    t0 = last_pane = now()
    pending = set()
    answered_at, tui_seen = -1e9, 0      # a just-answered prompt can linger on screen: 10 s grace, 2 sightings
    last_event = now()
    while now() - t0 < timeout:
        recs, off = read(off)
        if recs:
            last_event = now()
        for r in recs:
            ev, d = r.get("event"), r.get("data") or {}
            if ev == "UserPromptSubmit" and d.get("transcript_path"):
                io.span = {"path": d["transcript_path"], "start": r.get("tsize", 0)}
            if ev == "PreToolUse":
                io.step(tool_brief(d.get("tool_name") or "", d.get("tool_input")))
                if hasattr(io, "note_tool"):
                    io.note_tool(d.get("tool_name") or "", d.get("tool_input"))
            elif ev == "PermissionRequest" and r.get("perm_id"):
                pending.add(r["perm_id"])
                io.perm_card(r["perm_id"], d.get("tool_name") or "", d.get("tool_input") or {})
            elif ev == "PermissionAnswered":
                pending.discard(r.get("perm_id"))
                answered_at = now()
                io.react("working")
            elif ev == "Stop":
                if getattr(io, "span", None) is not None:
                    io.span["end"] = r.get("tsize")
                return "done", d.get("last_assistant_message") or "", off
            elif ev == "LaneStop":
                return "stopped", None, off
            elif ev == "StopFailure":
                return "failed", str(d.get("error") or d.get("reason") or "the head stopped with an error")[:300], off
        if now() - last_pane >= 5:
            last_pane = now()
            resp = call({"t": "status"}) or {}
            st, gate = resp.get("state"), gate_of(resp)
            if gate == "dead" or (gate and now() - last_event > GATE_QUIET_S):
                return "failed", f"the head isn't ready (state: {gate})", off
            tui_seen = tui_seen + 1 if (st == "permission_prompt" and not pending
                                        and now() - answered_at > 10) else 0
            if tui_seen >= 2:
                return "tui_prompt", None, off
        sleep(1)
    return "timeout", None, off


class _IO:
    """Everything a turn shows in the thread. Tests swap it for a recorder."""
    def __init__(self, tok, m):
        self.tok, self.m = tok, m
        self.t0 = time.time()
        self.tools, self.bubble, self.last_edit = [], None, 0
        self.span = None
        _state["tools"] = self.tools

    def _kw(self, **kw):
        kw["chat_id"] = self.m["chat"]["id"]
        if self.m.get("message_thread_id") is not None:
            kw["message_thread_id"] = self.m["message_thread_id"]
        return kw

    def react(self, st):
        _react(self.tok, self.m, st)

    def typing(self):
        return _Typing(self.tok, self.m, self.chat_action)

    def chat_action(self):
        return getattr(self, "media_action", "typing") if time.time() < getattr(self, "photo_until", 0) else "typing"

    def note_tool(self, tool, inp):
        act = chat_action_for(tool, inp)
        if act != "typing":
            self.media_action, self.photo_until = act, time.time() + PHOTO_HOLD
        else:
            self.photo_until = 0

    def step(self, line):
        self.tools.append(line)
        self._bubble(False)

    def _bubble(self, done):
        text = status_text(self.tools, self.t0, time.time(), done)
        try:
            if self.bubble is None:
                r = tgapi._api(self.tok, "sendMessage", **self._kw(text=text, disable_notification=True))
                self.bubble = r["result"]["message_id"]
                self.last_edit = time.time()
            elif done or time.time() - self.last_edit >= 2:
                tgapi._api(self.tok, "editMessageText", chat_id=self.m["chat"]["id"],
                                message_id=self.bubble, text=text)
                self.last_edit = time.time()
        except Exception as e:
            tgapi._log("head", event2="bubble_fail", error=str(e)[:150])

    def finish(self):
        if self.bubble is not None:
            self._bubble(True)

    def perm_card(self, pid, tool, inp):
        text, kb = perm_card_hook(pid, tool, inp)
        _send_card(self.tok, self.m, text, kb)
        self.react("permission")

    def legacy_card(self, lines):
        perm = parse_permission(lines[-60:])
        if perm:
            _send_card(self.tok, self.m, *permission_card(perm))
        else:
            ask = "\n".join(l.rstrip() for l in lines[-18:] if l.strip() and not _RULE.match(l))[-1500:]
            tgapi._reply(self.tok, self.m, "🔐 The head is asking permission:\n\n" + ask + "\n\nAnswer: yes / no")
        self.react("permission")

    def say(self, text):
        tgapi._reply(self.tok, self.m, text)

    def answer(self, md):
        """-> message ids of the parts sent. Each part is also a row in the lab chat history (TIK-1027)."""
        ids = self._answer(md)
        for i in ids:
            try:
                rel = (self.m.get("_relay") or {}).get("origin")
                chat_ledger.record_out(self.m["chat"]["id"], self.m.get("message_thread_id"), i, md,
                                       reply_to=self.m.get("message_id"), origin=rel,
                                       label=(jrelay.ORIGINS.get(rel) or {}).get("label") if rel else None)
            except Exception as e:
                tgapi._log("head", event2="ledger_out_fail", error=str(e)[:150])
        return ids

    def _answer(self, md):
        """-> message ids of the parts sent (for /thoughts and /show).
        A rich message first (real tables, task lists, headings; Bot API 10.1+), then Telegram HTML parts."""
        if self.m.get("_relay"):                 # TIK-1083: posted by a lane bot; the prefix is the boundary
            md = jrelay.PREFIX + md
        self.md = md
        # TIK-1028 tangents: the answer is a reply to the message it answers, so each reply chain stays together
        rp = {"reply_parameters": {"message_id": self.m["message_id"], "allow_sending_without_reply": True}}\
            if self.m.get("message_id") else {}
        if len(md) <= RICH_LIMIT:
            try:
                r = tgapi._api(self.tok, "sendRichMessage", **self._kw(rich_message={"markdown": rich_md(md)}, **rp))
                return [r["result"]["message_id"]]
            except Exception as e:                 # rejected markdown must never lose the reply: logged, then HTML
                tgapi._log("head", event2="rich_fallback", error=str(e)[:150])
        ids = []
        for part in split_parts(md):
            try:
                r = tgapi._api(self.tok, "sendMessage", **self._kw(text=md_to_html(part), parse_mode="HTML",
                                                                       disable_web_page_preview=True,
                                                                       **({} if ids else rp)))
                ids.append(r["result"]["message_id"])
            except Exception as e:                 # bad HTML must never lose the reply: send it plain, logged
                tgapi._log("head", event2="html_fallback", error=str(e)[:150])
                tgapi._reply(self.tok, self.m, part)
        return ids

    def remember(self, ids):
        if ids and self.span and self.span.get("end"):
            remember_turn(ids, dict(self.span, md=(getattr(self, "md", "") or "")[:MD_KEEP]))

    def voice(self, text, render=None):
        """Speak `text` in the lab voice (claude-head/render/voice.py, file only, never the speakers) and send it
        as a Telegram voice note captioned with exactly that text. A failure is said, never silent."""
        text = (text or "").strip()
        if not text:
            return
        try:
            if render is None:
                sys.path.insert(0, os.path.join(HERE, "render"))
                import voice as _voice
                render = _voice.render
            path = render(text)
            caption = text if len(text) <= 1000 else text[:990] + "…"
            tgapi._send_document(self.tok, self.m, path, "🔊 " + caption, method="sendVoice", field="voice",
                                      ctype="audio/ogg")
            tgapi._log("head", event2="voice_out", chars=len(text))
        except BaseException as e:                   # voice.py exits via SystemExit on a render failure
            tgapi._log("head", event2="voice_out_fail", error=str(e)[:200])
            self.say(f"⚠️ Couldn't make the voice reply: {e}")

    def send_file(self, path):
        if not os.path.isfile(path):
            self.say(f"⚠️ The head named a file that isn't there: {path}")
            return
        ext = os.path.splitext(path)[1].lower()
        photo = ext in (".jpg", ".jpeg", ".png", ".webp")
        video = ext in (".mp4", ".mov", ".webm")
        try:
            with _Typing(self.tok, self.m, "upload_photo" if photo else "upload_video" if video else "upload_document"):
                if photo:
                    tgapi._send_photo(self.tok, self.m, path, os.path.basename(path))
                else:
                    tgapi._send_document(self.tok, self.m, path, os.path.basename(path))
        except Exception as e:
            tgapi._log("head", event2="file_fail", path=path, error=str(e)[:150])
            self.say(f"⚠️ Couldn't send {os.path.basename(path)}: {e}")


def perm_card_hook(pid, tool, inp):
    """Allow / Deny card for a request the hook parked. Callback data: hd:p:<pid>:a|d. Pure."""
    inp = inp or {}
    if tool == "Bash":
        what = inp.get("description") or ""
        detail = (what + "\n\n" if what else "") + "$ " + (inp.get("command") or "")
    elif tool in ("Edit", "Write", "Read", "NotebookEdit"):
        detail = inp.get("file_path") or inp.get("notebook_path") or ""
    else:
        detail = json.dumps(inp, ensure_ascii=False)[:900]
    text = f"🔐 {tool} wants to run\n\n{detail[:3000]}"
    kb = {"inline_keyboard": [[{"text": "✅ Allow", "callback_data": f"hd:p:{pid}:a"},
                               {"text": "❌ Deny", "callback_data": f"hd:p:{pid}:d"}]]}
    return text, kb


REACTIONS = os.path.join(STATE_DIR, "reactions.jsonl")   # Rav's reactions to head answers: feedback, never a turn
REACTIONS_OFF = os.path.join(STATE_DIR, "reactions.off")  # byte offset already folded into a prompt


def _emojis(rs):
    return [r.get("emoji") or ("custom:" + str(r.get("custom_emoji_id"))) if r.get("type") != "paid" else "⭐"
            for r in (rs or [])]


def on_reaction(r, path=None, turns=None, now=time.time):
    """message_reaction update -> recorded as feedback (Rav 2026-10-24: "without it activating a turn ... I do want
    you to know about them next turn"). Only Rav's reactions on a head ANSWER count (turns.json knows them).
    -> the record written, or None. Never starts a turn, never replies."""
    uid = (r.get("user") or {}).get("id")
    mid = r.get("message_id")
    span = (_jload(turns or TURNS, {})).get(str(mid))
    if uid not in HEAD_UIDS or not span:
        tgapi._log("head", event2="reaction_ignored", uid=uid, message_id=mid, known=bool(span))
        return None
    old, new = set(_emojis(r.get("old_reaction"))), _emojis(r.get("new_reaction"))
    rec = {"ts": round(now(), 1), "local": time.strftime("%F %T"), "who": HEAD_UIDS[uid], "message_id": mid,
           "added": [e for e in new if e not in old], "removed": sorted(old - set(new)),
           "answer": flatten((span.get("md") or "")[:160])}
    if not rec["added"] and not rec["removed"]:
        return None
    os.makedirs(os.path.dirname(path or REACTIONS), exist_ok=True)
    with open(path or REACTIONS, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tgapi._log("head", event2="reaction", message_id=mid, added=rec["added"], removed=rec["removed"])
    return rec


# TIK-1017 (asked by the main head): when an answer never reached Rav, or his messages were refused, the head must
# hear it at the start of its next prompt - once - instead of learning it from "that was weird" 23 min later.
UNDELIVERED = os.path.join(STATE_DIR, "undelivered.json")
# TIK-1017: a message Rav sends while a turn runs is ALSO written here; head_hook hands it to the head at its next
# tool call (PostToolUse additionalContext), so "stop, wrong screen" lands mid-turn. It still runs as the next turn.
MIDTURN = os.path.join(STATE_DIR, "midturn.jsonl")


def note_undelivered(why=None, refused=None, path=None):
    """Record a failed turn (why) and/or one refused message (refused text). Never raises."""
    path = path or UNDELIVERED
    try:
        d = _jload(path, {"why": None, "refused": []})
        if why:
            d["why"] = why
            d["at"] = time.strftime("%H:%M")
        if refused:
            d.setdefault("refused", []).append(flatten(refused)[:120])
            d["refused"] = d["refused"][-10:]
        _jsave(path, d)
    except Exception as e:
        tgapi._log("head", event2="undelivered_note_fail", error=str(e)[:150])


def pending_notice(path=None, commit=True):
    """The one line for the next prompt about what did not get through, or ''. commit=True clears it."""
    path = path or UNDELIVERED
    d = _jload(path, None)
    if not d or not (d.get("why") or d.get("refused")):
        return ""
    bits = []
    if d.get("why"):
        bits.append(f"Your last reply was not delivered ({d['why']}, {d.get('at', '?')})")
    if d.get("refused"):
        bits.append(f"{OWNER}'s messages refused meanwhile: " + "; ".join(f'"{r}"' for r in d["refused"]))
    if commit:
        try:
            os.remove(path)
        except OSError:
            pass
    return "[" + ". ".join(bits) + ".] "


def note_midturn(text, path=None, now=time.time, key=None):
    """-> True when written. With a key (TIK-1421) the hook that hands it to the running turn records the key in
    MIDTURN_DONE, and the queued copy of the same message is then never run as a turn of its own."""
    try:
        os.makedirs(os.path.dirname(path or MIDTURN), exist_ok=True)
        row = {"ts": round(now(), 1), "text": flatten(text)[:1800]}
        if key:
            row["k"] = key
        with open(path or MIDTURN, "a") as f:
            f.write(json.dumps(row) + "\n")
        return True
    except Exception as e:
        tgapi._log("head", event2="midturn_note_fail", error=str(e)[:150])
        return False


# TIK-1421 (Rav 2026-11-01: add context on the fly, like the Notion / Claude apps, without a second turn). A message that
# arrives while a turn runs is written to MIDTURN with a key AND queued as before. The head's PostToolUse hook hands it to
# the running turn at the next tool call and appends the key here. When the queue reaches that message, a delivered key =
# the turn already had it: no second turn (logged, 👍). No tool call after it arrived = never delivered = it runs as its
# own turn, exactly as before. Never lost either way.
MIDTURN_DONE = os.path.join(STATE_DIR, "midturn_done.jsonl")
MIDTURN_DONE_KEEP = 2000


def midturn_delivered(key, path=None):
    if not key:
        return False
    try:
        with open(path or MIDTURN_DONE) as f:
            lines = f.readlines()[-MIDTURN_DONE_KEEP:]
    except OSError:
        return False
    for line in lines:
        try:
            if json.loads(line).get("k") == key:
                return True
        except ValueError:
            pass
    return False


def claim_midturn(m, text, path=None):
    """The queue is about to run this (possibly joined) message as its own turn. Its mid-turn keys go into MIDTURN_DONE
    so the next turn's hook does not hand it over AGAIN (live 2026-11-02: "Should" | "See" re-told a turn later)."""
    keys = [k for k in (m.get("_mt"), *(m.get("_mt_parts") or [])) if k]
    if not keys:
        return
    p = path or MIDTURN_DONE
    try:
        with open(p, "a") as f:
            for k in keys:
                f.write(json.dumps({"k": k, "ts": round(time.time(), 1), "by": "queue"}) + "\n")
        trim_midturn_done(p)
    except OSError as e:
        tgapi._log("head", event2="midturn_claim_fail", error=str(e)[:150])


def trim_midturn_done(path=None, keep=MIDTURN_DONE_KEEP):
    """Readers only look at the last `keep` keys; past 2x that the file is cut back to them (atomic replace, logged)."""
    p = path or MIDTURN_DONE
    with open(p) as f:
        lines = f.readlines()
    if len(lines) <= 2 * keep:
        return 0
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(lines[-keep:])
    os.replace(tmp, p)
    tgapi._log("head", event2="midturn_done_trimmed", dropped=len(lines) - keep)
    return len(lines) - keep


PROBE_ONLY = re.compile(r"^ALIVE headsafe-probe-[0-9a-f]+$")   # 2026-11-02 #28912: it reached Rav's thread mid-turn


def _midturn_key():
    import uuid
    return uuid.uuid4().hex[:16]


def skip_if_delivered(tok, m, path=None):
    """-> True when the running turn already received this message through the hook: logged + 👍, not run again."""
    if not midturn_delivered(m.get("_mt"), path):
        return False
    tgapi._log("head", event2="midturn_consumed", lane=_LANE, message_id=m.get("message_id"), key=m.get("_mt"))
    _react(tok, m, "seen")
    return True


def mark_stopped(path=None, now=time.time):
    """An interrupt (Escape) ends the head's turn WITHOUT a Stop hook event (06:00 2026-10-24: Rav's /stop worked,
    the lane kept waiting and the typing bar ran on). /stop writes its own marker into the event log so the
    following turn ends and /status reads idle."""
    try:
        with open(path or EVENTS, "a") as f:
            f.write(json.dumps({"ts": now(), "event": "LaneStop", "data": {}}) + "\n")
    except Exception as e:
        tgapi._log("head", event2="stop_mark_fail", error=str(e)[:150])


def head_state(read=None, size=None):
    """busy / idle from the head's OWN hook events, not its screen (TIK-1017). -> 'busy' | 'idle' | 'unknown'."""
    read, size = read or read_events, size or events_size
    recs, _ = read(max(0, size() - 200000))
    for r in reversed(recs):
        ev = r.get("event")
        if ev in ("Stop", "StopFailure", "SessionStart", "LaneStop"):
            return "idle"
        if ev in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest", "PermissionAnswered"):
            return "busy"
    return "unknown"


def pending_feedback(path=None, off_path=None, commit=True):
    """Reactions recorded since the last prompt -> one line to put in front of the next one ('' when none).
    commit=True moves the offset, so each reaction is told exactly once."""
    path, off_path = path or REACTIONS, off_path or REACTIONS_OFF
    try:
        off = int(open(off_path).read().strip() or 0)
    except (OSError, ValueError):
        off = 0
    try:
        with open(path, "rb") as f:
            f.seek(off)
            raw = f.read()
    except OSError:
        return ""
    if raw and not raw.endswith(b"\n"):
        raw = raw[:raw.rfind(b"\n") + 1]          # a half-written last line waits for the next prompt
    bits = []
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        what = " ".join(d.get("added") or []) or ("removed " + " ".join(d.get("removed") or []))
        bits.append(f'{what} on "{d.get("answer", "")[:70]}" ({d.get("local", "")[11:16]})')
    if commit and raw:
        with open(off_path, "w") as f:
            f.write(str(off + len(raw)))
    if not bits:
        return ""
    return (f"[Feedback since your last turn: {OWNER} reacted " + "; ".join(bits) +
            ". Note it; act on it only if it is relevant.] ")


# TIK-1028: Rav fixes typos by editing ("wipe vs welp"). An edit to a message still in the queue replaces it; an
# edit mid-turn is a mid-turn note; otherwise the head hears it once, in front of its next prompt.
EDITS = os.path.join(STATE_DIR, "edits.json")


EDIT_NEW_MAX = 1500        # TIK-1044: the head must see the WHOLE edited text (the first cut kept 300 chars and the hook preview cut it again)
EDIT_OLD_MAX = 200


def on_edit(e, path=None, tok=None, enqueue=None):
    """edited_message update -> 'requeued' | 'midturn' | 'turn' | 'noted' | 'unchanged' | None (not Rav's, not this thread, no text).
    TIK-1044 (Rav: "If you don't see the edit in this message, we have a gap/bug"): an edit that lands AFTER the head answered becomes its own
    turn with the full new text (when a token is given; without one it is noted for the next prompt as before); an edit to a message still
    waiting in the queue replaces its text AND says it was edited; an edit that changes nothing is recorded and ignored."""
    uid = (e.get("from") or {}).get("id")
    tid, mid = e.get("message_thread_id"), e.get("message_id")
    new = e.get("text") or e.get("caption")
    if J_ONLY:                                 # TIK-1131: an edit to an email-bot message is not this head's
        new = lane_body(new)
    if uid not in HEAD_UIDS or HEAD_THREADS.get(uid) != tid or not new:
        return None
    chat = (e.get("chat") or {}).get("id")
    old = None
    try:
        r = chat_ledger.find(chat, mid, tid)
        old = r and r.get("text")
        chat_ledger.record_edit(chat, mid, tid, new)
    except Exception as ex:
        tgapi._log("head", event2="ledger_edit_fail", error=str(ex)[:150])
    with _qlock:
        for i, (qm, qt) in enumerate(_q):
            if qm.get("message_id") == mid:
                first = qm.get("text") or old or ""
                if " ".join(str(first).split()) == " ".join(str(new).split()):
                    return "unchanged"
                said = f"[{OWNER} edited this message before you saw it; the first version was: \"{flatten(first)[:EDIT_OLD_MAX]}\"] "
                _q[i] = (dict(qm, text=new), (qm["_ctx"] + "\n\n" if qm.get("_ctx") else "") + said + new)
                tgapi._log("head", event2="edit_requeued", message_id=mid)
                return "requeued"
    if old is not None and " ".join(str(old).split()) == " ".join(str(new).split()):
        tgapi._log("head", event2="edit_unchanged", message_id=mid)
        return "unchanged"
    note = f"[{OWNER} edited message #{mid}: \"{flatten(old or '?')[:EDIT_OLD_MAX]}\" -> \"{flatten(new)[:EDIT_NEW_MAX]}\"]"
    if _state.get("running"):
        note_midturn(note)
        tgapi._log("head", event2="edit_midturn", message_id=mid)
        return "midturn"
    if tok is not None:                        # idle: the edit is its OWN turn, with the whole new text
        turn = (f"[{OWNER} edited their earlier message #{mid}" + (f"; it was: \"{flatten(old)[:EDIT_OLD_MAX]}\"" if old else "")
                + ". The edit may add what the first version was missing, so read it as his message; if it changes nothing you would act on, "
                "say so in one short line.]\n" + new[:EDIT_NEW_MAX * 2])
        try:
            (enqueue or _enqueue)(tok, dict(e, text=new), turn)
            tgapi._log("head", event2="edit_turn", message_id=mid)
            return "turn"
        except Exception as ex:
            tgapi._log("head", event2="edit_turn_fail", message_id=mid, error=str(ex)[:150])      # falls through to the note: never lost
    p = path or EDITS
    d = _jload(p, [])
    d.append(note)
    _jsave(p, d[-10:])
    tgapi._log("head", event2="edit_noted", message_id=mid)
    return "noted"


def pending_edits(path=None, commit=True):
    p = path or EDITS
    d = _jload(p, [])
    if commit and d:
        _jsave(p, [])
    return (" ".join(d) + " ") if d else ""


def write_remote(m):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = REMOTE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"since": time.time(), "chat_id": m["chat"]["id"], "thread_id": m.get("message_thread_id")}, f)
    os.replace(tmp, REMOTE)


def clear_remote():
    try:
        os.remove(REMOTE)
    except OSError:
        pass


HEAL_WAIT_S = 40


def heal(call, io, sleep=time.sleep, now=time.time):
    """Claude exited inside tmux: ask the bridge to start it again (claude --continue keeps the conversation),
    wait for it to come up, tell Rav either way. -> the state after the attempt. Logged (Iron Rule 12)."""
    tgapi._log("head", event2="heal_start")
    r = call({"t": "ensure"}) or {}
    t0 = now()
    st = "dead"
    while now() - t0 < HEAL_WAIT_S:
        sleep(2)
        st = (call({"t": "status"}) or {}).get("state")
        if st in ("idle", "busy"):
            break
    tgapi._log("head", event2="heal_end", state=st, ensure=str(r.get("detail"))[:100])
    io.say(f"♻️ The head had stopped; I restarted it (same conversation)." if st in ("idle", "busy")
           else f"⚠️ The head had stopped and didn't come back (state: {st}). It needs a look on the host.")
    return st


def _turn(*a, **k):
    """TIK-1028: while a lane turn runs the follow-up relay stays out of the event log, and when it ends everything up to now
    is settled (the turn's own reply was delivered by follow()), so the relay's baseline moves to the end of the log."""
    _state["busy_n"] = _state.get("busy_n", 0) + 1
    try:
        return _turn_run(*a, **k)
    finally:
        _state["busy_n"] -= 1
        _state["fu_off"] = events_size()


def _turn_run(tok, m, text, call=bridge, io=None, read=read_events, sleep=time.sleep, now=time.time):
    io = io or _IO(tok, m)
    resp = call({"t": "status"}) or {}
    st = resp.get("state")
    if st in GATES and gate_of(resp) is None:      # the word is in the scrollback, not at the prompt
        tgapi._log("head", event2="gate_ignored", state=st)
        st = "unknown"
    if st == "dead" and text is not None:          # TIK-1005: a crashed head is restarted, and says so
        st = heal(call, io, sleep, now)
    if text is not None and _state["waiting_permission"] and st == "permission_prompt":
        key = perm_answer(text)[0]
        if key and not key_allowed(live_box(call), key):   # TIK-1416: the box is gone -> his words are a message, not a key
            tgapi._log("head", event2="typed_answer_stale", key=key)
            _perm_closed()
    if text is None:
        off = _state.get("ev_off", events_size())  # a button answered a terminal prompt; keep following
    elif _state["waiting_permission"] and st == "permission_prompt":
        key, rest = perm_answer(text)
        if not key:
            io.say("The head is waiting on a permission question. Use the buttons, or answer yes or no.")
            io.react("permission")
            return
        off = _state.get("ev_off", events_size())
        call({"t": "key", "key": key})
        _perm_closed()
        if rest:                                   # his words after the yes reach the turn at its next step
            note_midturn(f"(said with the answer) {rest}")
    elif st in GATES:
        tgapi._log("head", event2="not_ready", state=st)
        note_undelivered(refused=text)
        io.say(f"⚠️ The head isn't ready (state: {st}). It needs a look on the host.")
        io.react("failed")
        return
    else:
        off = events_size()
        write_remote(m)
        r = call({"t": "send", "text": fit(pending_notice() + pending_feedback() + pending_edits() + flatten(text))})
        if not r.get("ok"):
            clear_remote()
            note_undelivered(refused=text)
            io.say(f"⚠️ Couldn't reach the head: {r.get('error') or r}")
            io.react("failed")
            return
        save_inflight(m, off)
    if text is None:
        io.span = _state.get("span")
    io.react("working")
    _state["running"] = True
    try:
        with io.typing():
            outcome, payload, off = follow(off, io, call=call, read=read, sleep=sleep, now=now)
    finally:
        _state["running"] = False
    _state["ev_off"] = off
    _state["span"] = io.span
    if outcome == "tui_prompt":
        _state["waiting_permission"] = True
        lines = (call({"t": "tail", "lines": 120}, wait=10) or {}).get("lines", [])
        fp = card_fingerprint(parse_permission(list(lines)[-60:]), lines)
        if fp and _state.get("legacy_card") == fp:     # TIK-1416: the same box again -> its card is already up, one per prompt
            tgapi._log("head", event2="card_dup")
            io.react("permission")
            return
        _state["legacy_card"] = fp
        tgapi._log("head", event2="tui_prompt")
        io.legacy_card(lines)
        return
    clear_remote()
    io.finish()
    tgapi._log("head", event2="turn", outcome=outcome, chars=len(payload or ""), steps=len(io.tools))
    if outcome == "done" and PROBE_ONLY.match((payload or "").strip()):
        tgapi._log("head", event2="probe_answer_not_relayed", lane=_LANE)   # headsafe's REPL check, not Rav's answer
        return
    if outcome == "done":
        body, files = media_lines(payload)
        body, said = voice_block(body)
        if body or not files:
            io.remember(io.answer(body or "(no text reply)"))
        for p in files:
            io.send_file(p)
        if m.get("_from_voice"):                     # TIK-1019 B: voice in -> voice out, with its exact text
            words = said or speakable(body)
            # Live mode: out of the phone's speaker, streamed while it is made; a voice note only if Live is closed
            if not (m.get("_live") and live_speak(words)):
                io.voice(words)
        io.react("done")
        if m.get("_live"):
            live_status("answered")                  # the Live page drops its thinking bar
    elif outcome == "stopped":
        io.say("⏹ Stopped. The head is waiting for you.")
        io.react("done")
    elif outcome == "failed":
        note_undelivered(why=f"the lane gave up: {payload}")
        io.say(f"⚠️ {payload}")
        io.react("failed")
    else:
        note_undelivered(why="no reply within 30 min")
        try:                                      # TIK-1087: say what is on the screen, not "may still be working"
            tl = [l.strip() for l in ((bridge({"t": "tail", "lines": 12}) or {}).get("lines") or []) if l.strip()]
        except Exception as e:
            tl = [f"(screen unreadable: {type(e).__name__})"]
        io.say("⚠️ No reply after 30 min. The head may still be working; /status shows what it's doing."
               + ("\nIts screen ends with:\n" + "\n".join(l[:120] for l in tl[-4:]) if tl else ""))
        io.react("failed")
    clear_inflight()


# Choice buttons the head sends with tg.py buttons (TIK-1028 "use all the telegram features"): Rav taps one, the
# head gets it as a turn, the message shows what he chose. tg.py writes BUTTONS; each question answers once.
BUTTONS = os.path.join(STATE_DIR, "buttons.json")


def on_choice_button(cq, data, tok, answer, path=None, enqueue=None):
    """hd:u:<key>:<i> -> 'chosen' | None."""
    uid = (cq.get("from") or {}).get("id")
    msg = cq.get("message") or {}
    try:
        _, _, key, i = data.split(":", 3)
        i = int(i)
    except ValueError:
        answer("That button is broken."); return None
    if uid not in HEAD_UIDS:
        tgapi._log("head_refused_button", uid=uid, data=data[:20])
        answer("Not yours."); return None
    p = path or BUTTONS
    d = _jload(p, {})
    q = d.pop(key, None)
    if not q or not 0 <= i < len(q.get("labels") or []):
        answer("That question is already answered."); return None
    _jsave(p, d)
    label = q["labels"][i]
    answer(label)
    try:
        tgapi._api(tok, "editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                        text=f"{q['text']}\n\n✅ {label}")
    except Exception as e:
        tgapi._log("head", event2="choice_edit_fail", error=str(e)[:150])
    m = {"from": {"id": uid}, "chat": msg.get("chat") or {"id": uid}, "message_id": msg.get("message_id"),
         "message_thread_id": msg.get("message_thread_id")}
    tgapi._log("head", event2="choice", key=key, label=label[:60])
    (enqueue or _enqueue)(tok, m, f"[{OWNER} tapped “{label}” on your question: “{q['text'][:300]}”]")
    return "chosen"


# 18:44 (MOT): restarting tg-gateway mid-turn lost the head's answer: the new process knew nothing of the turn.
# The turn in flight is written down when its prompt is sent and crossed off when its answer is delivered;
# a gateway that starts with one on file follows it from the same event offset and delivers it.
INFLIGHT = os.path.join(STATE_DIR, "inflight.json")
_KEEP_KEYS = ("chat", "from", "message_id", "message_thread_id", "_from_voice", "_live")


def save_inflight(m, off, path=None, now=time.time):
    try:
        _jsave(path or INFLIGHT, {"m": {k: m[k] for k in _KEEP_KEYS if k in m}, "off": off, "ts": now()})
    except Exception as e:
        tgapi._log("head", event2="inflight_save_fail", error=str(e)[:150])


def clear_inflight(path=None):
    try:
        os.remove(path or INFLIGHT)
    except FileNotFoundError:
        pass


def recover_inflight(tok, path=None, now=time.time, enqueue=None):
    """At gateway start: a turn that was in flight when the last gateway stopped is followed and delivered.
    -> 'recovered' | 'stale' | None."""
    d = _jload(path or INFLIGHT, None)
    if not d or not d.get("m"):
        return None
    if now() - d.get("ts", 0) > TURN_TIMEOUT_S:
        clear_inflight(path)
        tgapi._log("head", event2="inflight_stale", age_s=round(now() - d.get("ts", 0)))
        return "stale"
    _state["ev_off"] = d["off"]
    tgapi._log("head", event2="inflight_recovered", message_id=d["m"].get("message_id"), off=d["off"])
    if enqueue:
        enqueue(tok, d["m"])
    else:
        with _qlock:
            _q.appendleft((d["m"], None))           # first in line: follow it before any new prompt is sent
            if _state["worker"] is None:
                _state["worker"] = threading.Thread(target=_serve, args=(tok,), daemon=True, name="head-lane")
                _state["worker"].start()
        _wake.set()
    return "recovered"


MEDIA_MARK = object()      # queue item: build the prompt from the attachment in the worker, not the gateway thread


def rich_text(rm):
    """A rich message (Bot API 10.1: {"blocks": [{"type": "paragraph", "text": ["plain", {"type": "bold", "text": "x"}]}]})
    -> plain text, one line per block. Rav 2026-10-31: a forwarded agent answer has no "text", only "rich_message", and
    the lane said "That kind of message isn't supported". Unknown block types still give up their words. Pure."""
    def words(x):
        if isinstance(x, str):
            return x
        if isinstance(x, list):
            return "".join(words(i) for i in x)
        if isinstance(x, dict):
            parts = [("\n" if k in ("items", "rows", "blocks") else " " if k == "cells" else "").join(
                         words(i) for i in x[k]) if isinstance(x[k], list) and k != "text" else words(x[k])
                     for k in ("text", "items", "rows", "cells", "blocks", "content") if k in x]
            return "\n".join(p for p in parts if p)
        return ""
    blocks = (rm or {}).get("blocks") if isinstance(rm, dict) else None
    return "\n".join(t for t in (words(b).strip() for b in blocks or []) if t)


def media_kind(m):
    # video_note = Telegram's round video (TIK-1025: Rav's clip at 14:49 was refused as "not supported")
    return next((k for k in ("photo", "document", "voice", "audio", "video", "video_note") if m.get(k)), None)


CLIP = os.path.join(HERE, "render", "clip.py")
CLIP_TIMEOUT_S = 240
CLIP_REPORT_MAX = 3500


def run_clip(path, run=None):
    """TIK-1025 item 2: the lane runs clip.py on every video, so the head cannot skip looking.
    -> clip.py's report (contact sheet path, frames folder, scene changes, transcript), or None (logged)."""
    import subprocess
    run = run or subprocess.run
    try:
        r = run(["python3", CLIP, path], capture_output=True, text=True, timeout=CLIP_TIMEOUT_S)
    except Exception as e:
        tgapi._log("head", event2="clip_fail", path=path, error=str(e)[:200])
        return None
    if r.returncode != 0 or not (r.stdout or "").strip():
        tgapi._log("head", event2="clip_fail", path=path, rc=r.returncode, error=(r.stderr or "")[-200:])
        return None
    out = r.stdout.strip()
    return out if len(out) <= CLIP_REPORT_MAX else out[:CLIP_REPORT_MAX] + "\n… (report cut)"


def media_prompt(kind, path, caption, transcript=None, clip=None):
    """What the head is told about an attachment. Pure."""
    cap = (caption or "").strip()
    if kind in ("voice", "audio") and transcript is not None:
        return (f"({OWNER} sent a voice note from Telegram, transcribed on the host) {transcript.strip()}" + (f" / caption: {cap}" if cap else "")
                + " / (The lane will speak your answer back to him as a voice note with its exact text: put a line "
                "'VOICE: <what to say aloud>' to choose the spoken version, else a cleaned form of your reply is spoken. "
                "Do not send a voice note yourself.)")
    what = {"photo": "a photo", "document": "a file", "video": "a video", "video_note": "a round video message",
            "voice": "a voice note", "audio": "an audio file"}[kind]
    if kind in ("video", "video_note") and clip:
        tail = (" The lane already ran clip.py on it; Read the contact sheet before you answer. Report:\n" + clip)
    elif kind in ("video", "video_note"):
        tail = f" (see it: python3 {CLIP} <that path>, then Read the sheet)"
    else:
        tail = ""
    return (cap + " / " if cap else "") + f"[{OWNER} sent {what} from Telegram, saved at {path}]{tail}"


def transcribe(path, post=None):
    """ogg/mp3 -> 16 kHz wav -> the STT server on the host. -> text, or None (logged)."""
    import subprocess
    import urllib.request
    wav = path + ".wav"
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-ar", "16000", "-ac", "1", wav],
                       check=True, timeout=60)
    except Exception as e:
        tgapi._log("head", event2="stt_fail", stage="ffmpeg", error=str(e)[:200])
        return None
    b = "----hl" + os.urandom(8).hex()
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"v.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + open(wav, "rb").read() + f"\r\n--{b}--\r\n".encode()
    for url in STT_URLS:
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={b}"})
            text = json.load((post or urllib.request.urlopen)(req, timeout=120)).get("text")
            if url != STT_URLS[0]:
                tgapi._log("head", event2="stt_fallback", url=url)
            return text
        except Exception as e:
            # the GPU copy refusing = the broker stopped it on purpose (empty lab -> no STT on GPU), not a fault
            off = url == STT_URLS[0] and "refused" in str(e).lower()
            tgapi._log("head", event2="stt_gpu_off" if off else "stt_fail", url=url, error=str(e)[:200])
    return None


def inbox_path(name):
    """A fresh INBOX path for a download. Claimed with O_EXCL so album items landing in the same second
    never overwrite each other (2026-11-02: 7 photos arrived, 3 were lost to same-second names)."""
    os.makedirs(INBOX, exist_ok=True)
    stem, ext = os.path.splitext(time.strftime("%Y%m%d-%H%M%S_") + re.sub(r"[^\w.\-]", "_", name)[-80:])
    for n in range(1, 1000):
        path = os.path.join(INBOX, stem + (f"-{n}" if n > 1 else "") + ext)
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"inbox_path: no free name for {stem}{ext}")


def media_to_text(tok, m):
    kind = media_kind(m)
    obj = m[kind][-1] if kind == "photo" else m[kind]
    os.makedirs(INBOX, exist_ok=True)
    name = obj.get("file_name") or {"photo": "photo.jpg", "voice": "voice.ogg", "audio": "audio", "video": "video.mp4",
                                     "video_note": "videonote.mp4"}.get(kind, "file")
    path = inbox_path(name)
    tgapi._download(tok, obj["file_id"], path)
    tr = transcribe(path) if kind in ("voice", "audio") else None
    if kind in ("voice", "audio") and tr is None:
        tgapi._reply(tok, m, "⚠️ Couldn't transcribe that voice note; passing the audio file to the head instead.")
    if kind in ("voice", "audio") and tr is not None:
        # TIK-1019 A: Rav sees what was heard at once, so he can /stop on a misheard word
        tgapi._reply(tok, m, "📝 heard: " + tr.strip())
        m["_from_voice"] = True                      # TIK-1019 B: the answer is spoken back
    clip = run_clip(path) if kind in ("video", "video_note") else None
    if kind in ("video", "video_note") and clip is None:
        tgapi._reply(tok, m, "⚠️ Couldn't pre-process that video; the head will look at it itself.")
    tgapi._log("head", event2="media_in", kind=kind, path=path, transcribed=tr is not None, clip=clip is not None)
    return media_prompt(kind, path, m.get("caption"), tr, clip)


COMMANDS = ("/stop", "/new", "/status", "/help", "/thoughts", "/show", "/model", "/usage")
# TIK-1087 (2026-10-27 20:55): Rav's "/usage" was typed raw into the REPL; Claude Code opened its interactive usage
# panel, which waits for a key, and every later message sat behind it for an hour. Slash commands that open a screen
# are never typed raw: /usage is driven here (open, read, Escape, verify closed); the rest are refused with a reason.
INTERACTIVE = ("/config", "/cost", "/stats", "/resume", "/agents", "/mcp", "/permissions", "/hooks", "/memory",
               "/login", "/logout", "/doctor", "/ide", "/theme", "/privacy-settings", "/usage-credits", "/plugin",
               "/export", "/rewind", "/output-style", "/terminal-setup", "/vim")

# Rav 2026-10-24: "/model ... pop up 4 buttons. Haiku, Sonnet, Opus, Fable (others later); the default is slow/low
# and the latest version of each." Slow = fast mode left off (never toggled here); low = /effort low.
# Typing bare "/model" into the REPL opens an interactive picker the lane cannot drive (it hung there 02:34).
MODELS = [("haiku", "Haiku", "claude-haiku-5-5"), ("sonnet", "Sonnet", "claude-sonnet-5-5"),
          ("opus", "Opus", "claude-opus-5-5"), ("fable", "Fable", "claude-fable-5-2")]
MODEL_EFFORT = "low"
_SET_MODEL = re.compile(r"⎿\s*(Set model to .*|Kept model as .*|.*[Mm]odel .*not found.*|.*[Ii]nvalid model.*)")
_SET_EFFORT = re.compile(r"⎿\s*(Set effort level to \w+)")


def model_card():
    """2x2 buttons (Rav's UI rule: group, don't line up 1x4). Callback hd:m:<key>. Pure."""
    b = [{"text": label, "callback_data": f"hd:m:{key}"} for key, label, _ in MODELS]
    return (f"🧠 Model for the head — latest of each, effort low, fast mode off.",
            {"inline_keyboard": [b[0:2], b[2:4]]})


def model_confirm(lines):
    """The REPL's own confirmation lines after /model + /effort -> (model_line, effort_line). Pure."""
    ml = el = None
    for l in lines:
        a, b = _SET_MODEL.search(l), _SET_EFFORT.search(l)
        if a:
            ml = a.group(1).strip()
        if b:
            el = b.group(1).strip()
    return ml, el


def set_model(key, call=None, sleep=None):
    """Type /model <id> then /effort low into the idle head; read back what it confirmed. -> text for the card."""
    call = call or bridge
    sleep = sleep or time.sleep
    mid = next((m for k, _, m in MODELS if k == key), None)
    if not mid:
        return None
    call({"t": "send", "text": f"/model {mid}"})
    sleep(2.5)
    call({"t": "send", "text": f"/effort {MODEL_EFFORT}"})
    sleep(2.5)
    ml, el = model_confirm((call({"t": "tail", "lines": 30}, wait=10) or {}).get("lines", []))
    tgapi._log("head", event2="model", key=key, model=ml, effort=el)
    if ml and ml.startswith(("Set model", "Kept model")) and el:
        return f"✅ {ml.split(' and saved')[0]} · {el.replace('Set effort level to ', 'effort ')}"
    return f"⚠️ Couldn't confirm the switch (head said: {ml or 'nothing'}; {el or 'no effort line'})."


def on_model_button(cq, data, tok, answer, call=None):
    if (cq.get("from") or {}).get("id") not in HEAD_UIDS:
        answer("Not yours."); return
    key = data.split(":", 2)[2]
    if key not in {k for k, _, _ in MODELS}:
        answer("Unknown model."); return
    if _state.get("running"):
        answer("The head is mid-answer. Try again when it's done (or /stop first)."); return
    answer("Switching…")
    result = set_model(key, call=call)
    msg = cq.get("message") or {}
    try:
        tgapi._api(tok, "editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"], text=result)
    except Exception as e:
        tgapi._log("head", event2="model_card_edit_fail", error=str(e)[:150])
        tgapi._reply(tok, msg, result)
_USAGE_HEAD = re.compile(r"^\s*(Current session|Current week \([^)]*\))\s*$")
_USAGE_PCT = re.compile(r"(\d+)% used")
_USAGE_RESET = re.compile(r"^\s*Resets (.+?)\s*$")


def parse_usage(lines):
    """The REPL's /usage panel -> [(label, pct, resets)]. Pure."""
    out, cur = [], None
    for l in lines:
        h = _USAGE_HEAD.match(l)
        if h:
            cur = [h.group(1), None, ""]; out.append(cur); continue
        if cur is None:
            continue
        p = _USAGE_PCT.search(l)
        if p and cur[1] is None:
            cur[1] = int(p.group(1)); continue
        r = _USAGE_RESET.match(l)
        if r and not cur[2]:
            cur[2] = r.group(1)
    return [tuple(c) for c in out if c[1] is not None]


def usage_text(rows):
    """-> the Telegram reply. Pure."""
    if not rows:
        return "⚠️ Couldn't read the usage panel (no session/week lines on the screen)."
    return "📊 Claude usage\n" + "\n".join(f"{lab}: {pct}% used" + (f" · resets {rs}" if rs else "") for lab, pct, rs in rows)


def usage(call=None, sleep=time.sleep):
    """Open /usage in the idle REPL, read it, close it with Escape, confirm it closed. -> reply text. Never leaves the
    panel open: Escape is sent on every path, and a panel still on screen afterwards is said, not hidden."""
    call = call or bridge
    st = (call({"t": "status"}) or {}).get("state")
    if _state.get("running") or st not in ("idle", "unknown"):
        return f"The head is {('mid-answer' if _state.get('running') else st)}; /usage when it's idle (or /stop first)."
    rows = []
    try:
        call({"t": "send", "text": "/usage"})
        for _ in range(8):
            sleep(0.75)
            rows = parse_usage((call({"t": "tail", "lines": 80}) or {}).get("lines") or [])
            if len(rows) >= 2:
                break
    finally:
        call({"t": "key", "key": "Escape"})
    sleep(0.75)
    still = parse_usage((call({"t": "tail", "lines": 40}) or {}).get("lines") or [])
    tgapi._log("head", event2="usage", rows=len(rows), still_open=bool(still))
    if still:
        call({"t": "key", "key": "Escape"})
        return usage_text(rows) + "\n⚠️ The usage panel may still be open on the head; /stop closes it."
    return usage_text(rows)


RENDER = os.path.join(HERE, "render", "render.py")


def show_prompt(answer_md, extra=""):
    """What the head is asked when Rav replies /show to an answer. Pure."""
    return (f"{OWNER} replied /show to one of your answers (from Telegram): they want to SEE it. Make the single best "
            "visual of it as a PNG: a diagram -> mermaid via `python3 " + RENDER + " mermaid FILE`; a chart -> "
            "matplotlib, save a PNG; a table or notes -> `" + RENDER + " md FILE`; a page -> `" + RENDER +
            " html FILE`. Reply with one short caption line and the line MEDIA:/path/to.png. "
            + (f"He added: {extra}. " if extra else "") + "The answer was: " + flatten(answer_md)[:6000])


def show(tok, m):
    """/show as a reply to an answer -> a visual of it, queued like any message."""
    rt = m.get("reply_to_message") or {}
    rec = _jload(TURNS, {}).get(str(rt.get("message_id"))) if rt else None
    md = (rec or {}).get("md") or rt.get("text") or rt.get("caption") or ""
    if not md.strip():
        tgapi._reply(tok, m, "Reply to one of my answers with /show and I'll draw it (diagram, chart or table).")
        return
    extra = re.sub(r"^/show(@\w+)?[?!.\s]*", "", (m.get("text") or "").strip(), flags=re.I).strip()
    _enqueue(tok, m, show_prompt(md, extra))
    tgapi._log("head", event2="show", answer=rt.get("message_id"))


def command_of(text):
    """'/thoughts?' or '/stopthe bot' -> '/thoughts' / '/stop'; anything else -> None. Pure."""
    if not text or not text.strip().startswith("/"):
        return None
    c = re.sub(r"[?!.\s]+$", "", text.strip().split()[0]).split("@")[0].lower()
    return c if c in COMMANDS or c in INTERACTIVE else None


def command(m, tok, cmd, call=None):
    """Thread commands, answered at once (not queued behind a running turn)."""
    call = call or bridge
    say = lambda t: tgapi._reply(tok, m, t)
    st = (call({"t": "status"}) or {}).get("state")
    tgapi._log("head", event2="command", cmd=cmd, state=st)
    if cmd == "/thoughts":
        show_thoughts(tok, m)
        return
    if cmd == "/show":
        show(tok, m)
        return
    if cmd in INTERACTIVE:
        say(f"{cmd} opens an interactive screen the head can't drive from Telegram, so I didn't type it (TIK-1087). "
            "/usage and /model work here.")
        return
    if cmd == "/usage":
        say(usage(call=call))
        return
    if cmd == "/model":
        text, kb = model_card()
        _send_card(tok, m, text, kb)
        return
    if cmd == "/stop":
        call({"t": "key", "key": "Escape"})
        mark_stopped()
        say("⏹ Sent stop to the head.")
    elif cmd == "/new":
        call({"t": "key", "key": "Escape"})
        call({"t": "send", "text": "/clear"})
        _perm_closed()
        say("🆕 Cleared. The head starts a fresh conversation (same brain, same CLAUDE.md).")
    elif cmd == "/status":
        recs, _ = read_events(max(0, events_size() - 200000))
        mode = next((r["data"].get("permission_mode") for r in reversed(recs) if (r.get("data") or {}).get("permission_mode")), "?")
        tools = _state.get("tools") or []
        live = "dead" if st == "dead" else head_state()   # busy/idle from its own events, not its screen (TIK-1017)
        say(f"State: {live} · mode: {mode} · queued: {len(_q)}" + ("\nLast steps:\n" + "\n".join(tools[-5:]) if tools else ""))
    else:
        say(f"Talk to the head here. Text, photos, files and voice notes all work.\n"
            "/stop interrupt the current turn\n/new start a fresh conversation\n/status what it's doing\n"
            "/thoughts (as a reply to an answer) how it got there, with a Close button\n"
            "/show (as a reply to an answer) draw it: diagram, chart or table as a picture\n"
            "/model pick Haiku / Sonnet / Opus / Fable (latest, effort low)\n"
            "/usage Claude usage: session and week, with reset times\n"
            "Permission questions come as Allow / Deny buttons.")


def _serve(tok):
    while True:
        _wake.wait(timeout=60)
        _wake.clear()
        while True:
            with _qlock:
                if not _q:
                    break
                m, text = _q.popleft()
            tk = tok
            try:
                tk = _tok_for(m, tok)                      # a relayed turn speaks with the origin bot's token
                if skip_if_delivered(tk, m):               # TIK-1421: the turn it arrived in already had it
                    continue
                m, text = _burst(tk, m, text)              # Rav 2026-11-01: a multi-part send is ONE turn
                if m is not None:
                    claim_midturn(m, text)                 # TIK-1421: run as its own turn -> the hook must not re-tell it
                if m is None or drop_if_deleted(tk, m):    # TIK-1100: Rav deleted it while it waited: the head never sees it
                    continue
                if text is MEDIA_MARK:
                    text = media_to_text(tk, m)
                _turn(tk, m, text)
            except Exception as e:
                tgapi._log("head", event2="error", error=str(e)[:300])
                note_undelivered(why=f"lane error: {str(e)[:100]}")
                _react(tk, m, "failed")
                try:
                    tgapi._reply(tk, m, f"⚠️ head error: {e}")
                except Exception:
                    pass



# Bursts (Rav 2026-11-01): "I should be able to forward messages without it stacking a crapton of queued messages. I like the
# instant speed when a single message is sent. However if another message comes in within half a second, it's probably a
# multi part message, pause and wait for a full second for messages to stop coming." A lone message waits at most
# BURST_GAP_S after it ARRIVED (usually already past by the time the worker picks it up). Plain text messages from the same
# person whose arrivals are BURST_QUIET_S or less apart are joined into one turn, also when they piled up behind a running
# turn. Media, relays, taps and edits are never joined. Every join is logged; nothing is dropped.
BURST_GAP_S = 0.5           # a second message this soon after the first = a multi-part send
BURST_QUIET_S = 1.0         # ...then it waits for this much quiet
BURST_MAX_S = 10.0          # a send that never stops still goes in after this long
BURST_MAX_N = 40


def _burstable(m, text):
    return isinstance(text, str) and bool(m.get("_gone_check")) and not m.get("_relay")


def _same_sender(a, b):
    return (a.get("from") or {}).get("id") == (b.get("from") or {}).get("id") and a.get("chat", {}).get("id") == b.get("chat", {}).get("id")


def burst_text(texts):
    if len(texts) == 1:
        return texts[0]
    return (f"[{OWNER} sent these {len(texts)} messages together, one after another within a second (a multi-part message or a "
            f"batch of forwards). Read them as ONE message and answer once.]\n\n" + "\n\n———\n\n".join(
                f"({i}/{len(texts)}) {t}" for i, t in enumerate(texts, 1)))


def _burst(tok, m, text, sleep=time.sleep, now=time.time, gone=None):
    """The item the worker just took -> (m, text) for ONE turn: itself, or itself joined with the rest of its burst.
    -> (None, None) when every message in the burst was deleted while it waited."""
    if not _burstable(m, text):
        return m, text
    t0 = m.get("_q_ts") or now()
    wait = t0 + BURST_GAP_S - now()
    if wait > 0:
        sleep(wait)
    got, last, stop_at = [(m, text)], t0, now() + BURST_MAX_S
    while len(got) < BURST_MAX_N:
        with _qlock:
            while _q and len(got) < BURST_MAX_N:
                qm, qt = _q[0]
                ts = qm.get("_q_ts") or now()
                if not (_burstable(qm, qt) and _same_sender(m, qm) and ts - last <= BURST_QUIET_S):
                    break
                got.append(_q.popleft())
                last = ts
                if skip_if_delivered(tok, got[-1][0]):     # TIK-1421: already given to the turn it arrived in
                    got.pop()
            more_possible = not _q
        if len(got) == 1 or not more_possible or now() >= stop_at:
            break
        rest = last + BURST_QUIET_S - now()
        if rest <= 0:
            break
        sleep(min(rest, 0.1))
    if len(got) == 1:
        return m, text
    keep = [(qm, qt) for qm, qt in got if not drop_if_deleted(tok, qm, gone=gone)]
    if not keep:
        return None, None
    tgapi._log("head", event2="burst_joined", lane=_LANE, n=len(keep), dropped=len(got) - len(keep),
                    ids=[qm.get("message_id") for qm, _ in keep][:BURST_MAX_N], span_s=round(last - t0, 2))
    for qm, _ in keep[1:]:
        _react(tok, qm, "done")              # its words went into the first one's turn
    first = dict(keep[0][0], _gone_check=False,   # each part was already checked above
                 _mt_parts=[qm.get("_mt") for qm, _ in keep[1:] if qm.get("_mt")])
    return first, burst_text([qt for _, qt in keep])

# Lead-ins (Rav 2026-10-27): "FYI" then a forward arrive as TWO Telegram messages, and the head answered "FYI" alone.
# A short text that only makes sense with what comes next ("FYI", "fwd", "take a look at this", "from a teammate", "see
# below", anything ending in ":") is HELD 2 s; the next message from him carries it as context. Nothing
# follows -> it goes on alone, logged. Never dropped.
LEAD_HOLD_S = 2.0                         # Rav: the comment and the forward are one button press; 2 s covers lag
_LEAD_RE = re.compile(
    r"^(fyi|fw|fwd|forward(ed|ing)?|see (this|below|next|the next)|look( at this)?|take a look( at (this|these|it))?|"
    r"check (this|these|it)( out)?|this|these|here|heads up|for context|context|re|from [\w .'-]{1,24}|"
    r"(this|these) (is|are) from [\w .'-]{1,24}|incoming|👇+|⬇️+)[\s.!:…]*$", re.I)
_lead = {}                                  # uid -> {"m", "text", "timer"}
_lead_lock = threading.Lock()


def is_lead_in(text):
    t = (text or "").strip()
    return bool(t) and len(t) <= 60 and (bool(_LEAD_RE.match(t)) or (t.endswith(":") and len(t) <= 40))


def _lead_flush(uid, tok, enqueue=None):
    """The hold ran out with nothing after it: deliver the lead-in on its own (it is never lost)."""
    with _lead_lock:
        h = _lead.pop(uid, None)
    if h:
        tgapi._log("head", event2="lead_in_alone", text=h["text"][:60])
        (enqueue or _enqueue)(tok, h["m"], (h["m"].get("_ctx", "") + "\n\n" if h["m"].get("_ctx") else "") + h["text"])


def _lead_take(uid):
    """-> the held lead-in text for this person (and cancel its timer), or ''."""
    with _lead_lock:
        h = _lead.pop(uid, None)
    if not h:
        return ""
    h["timer"].cancel()
    return h["text"]


def handle(m, tok, prep=None) -> bool:
    uid = (m.get("from") or {}).get("id")
    tid = m.get("message_thread_id")
    if tid is None or tid not in HEAD_THREADS.values():
        return False
    if J_ONLY:                                 # TIK-1131: the topic's own system keeps everything but "/j ..."
        cap = m.get("text") is None
        body = lane_body(m.get("caption") if cap else m.get("text"))
        if body is None and cap and tgapi.j_group(m.get("media_group_id")):
            body = ""                          # a captionless photo of a "/j" album (Rav 2026-11-02): the head's too
        if body is None:
            return False
        if uid not in HEAD_UIDS or HEAD_THREADS.get(uid) != tid:
            tgapi._log("head_refused_user", uid=uid, lane=_LANE)
            return True
        if jrelay.is_stop(body):
            command(m, tok, "/stop")
            return True
        if not body and cap is False:
            tgapi._reply(tok, m, "Send /j and your message, e.g. /j what's waiting in my inbox?")
            return True
        m = dict(m, **({"caption": body} if cap else {"text": body}))
    elif j_body(m.get("text")):                # "/j x" in a head's own topic is just "x" (Rav's habit; raw, the REPL
        m = dict(m, text=j_body(m["text"]))    # said "Unknown command: /j" and nothing came back, 2026-10-30)
    elif j_body(m.get("caption")):             # same for a photo's caption: raw, no turn ran and the lane waited (2026-11-04)
        m = dict(m, caption=j_body(m["caption"]))
    if uid not in HEAD_UIDS or HEAD_THREADS.get(uid) != tid:
        tgapi._log("head_refused_user", uid=uid)
        return True
    text = m.get("text")
    if text is None and isinstance(m.get("rich_message"), dict):   # a forwarded agent answer: words only, no "text"
        text = rich_text(m["rich_message"]) or None
        m = dict(m, text=text)
        tgapi._log("head", event2="rich_in", chars=len(text or ""))
    cmd = command_of(text)
    if cmd:
        _lead_flush(uid, tok)                  # a command is not what the lead-in pointed at
        command(m, tok, cmd)
        return True
    ctx = _context(m)
    lead = _lead_take(uid)
    if not lead and text and is_lead_in(text) and not m.get("forward_origin") and not chat_ledger.is_real_reply(m):
        if ctx:
            m["_ctx"] = ctx
        with _lead_lock:
            t = threading.Timer(LEAD_HOLD_S, _lead_flush, args=(uid, tok))
            t.daemon = True
            _lead[uid] = {"m": m, "text": text, "timer": t}
            t.start()
        _react(tok, m, "queued")
        tgapi._log("head", event2="lead_in_held", text=text[:60])
        return True
    if lead:
        ctx = f"[{OWNER} sent this just before the next message, as its lead-in: “{lead[:200]}”]" + ("\n" + ctx if ctx else "")
        tgapi._log("head", event2="lead_in_joined", text=lead[:60])
    if ctx:
        m["_ctx"] = ctx
    # TIK-1027: "look at this" as a reply to a video/photo/voice/file = that media, with his words as the caption
    rt = m.get("reply_to_message") or {}
    # ...but NOT when the media is the bot's OWN (Rav 2026-10-26: "This is great BTW!" as a reply to the 22 MB video
    # the agent had sent was turned into a re-download of that video, hit Telegram's 20 MB bot limit, HTTP 400, and his
    # words were dropped). The head made it and the reply context already names it: deliver his text as text.
    own_media = bool((rt.get("from") or {}).get("is_bot"))
    if text and chat_ledger.is_real_reply(m) and media_kind(rt) and not own_media:
        m = dict(m, **{k: rt[k] for k in ("photo", "document", "voice", "audio", "video", "video_note") if rt.get(k)},
                 caption=text, text=None)
        text = None
    if not text:
        if not media_kind(m):
            tgapi._reply(tok, m, "That kind of message isn't supported here yet (text, photos, files and voice notes are).")
            return True
        # Rav 2026-10-24 15:09: video/transcript processing must not hold up the conversation. Prepare the
        # attachment in its own thread NOW; text keeps flowing; the prepared prompt joins the queue when ready.
        _react(tok, m, "queued")
        (prep or _start_prep)(tok, m)
        return True
    mt = {}
    if _state.get("running") and isinstance(text, str):   # the head sees it at its next tool call (head_hook, TIK-1421)
        key = _midturn_key()
        if note_midturn((ctx + "\n" if ctx else "") + text, key=key):
            mt = {"_mt": key}
    _enqueue(tok, dict(m, _gone_check=True, **mt), (ctx + "\n\n" if ctx else "") + text)   # TIK-1100: plain text only (media is out of scope)
    return True


def _context(m):
    """Reply / forward context for the head (chat_ledger). A ledger failure never blocks the message."""
    try:
        return chat_ledger.context_block(m)
    except Exception as e:
        tgapi._log("head", event2="context_fail", error=str(e)[:200])
        return ""


def _prep_media(tok, m, to_text=None, enqueue=None):
    """Download + transcribe/clip off the queue; then enqueue the finished prompt (and tell a busy head)."""
    to_text, enqueue = to_text or media_to_text, enqueue or _enqueue
    try:
        text = to_text(tok, m)
    except Exception as e:
        tgapi._log("head", event2="media_prep_fail", error=str(e)[:300])
        if (m.get("caption") or "").strip():
            # the attachment failed, but his WORDS still reach the head (never drop what Rav typed), with the reason
            ctx = m.get("_ctx") or ""
            enqueue(tok, m, (ctx + "\n\n" if ctx else "") + f"[his {media_kind(m) or 'attachment'} could not be "
                    f"downloaded: {e}]\n{m['caption']}")
            tgapi._reply(tok, m, f"⚠️ Couldn't open that {media_kind(m) or 'attachment'} ({e}); your words went through.")
            return
        note_undelivered(refused=f"[{media_kind(m) or 'attachment'} that failed to process: {e}]")
        try:
            tgapi._reply(tok, m, f"⚠️ Couldn't process that {media_kind(m) or 'attachment'}: {e}")
        except Exception:
            pass
        _react(tok, m, "failed")
        return
    if m.get("_ctx"):
        text = m["_ctx"] + "\n\n" + text
    if m.get("media_group_id"):                  # Rav 2026-11-02: an album is ONE turn, not one per photo
        _album_add(tok, m, text, enqueue)
        return
    if _state.get("running"):
        note_midturn(f"[your {media_kind(m) or 'attachment'} from {OWNER} is processed and queued as the next turn] "
                     + text[:300])
    enqueue(tok, m, text)


ALBUM_GATHER_S = 2.5      # after the LAST album item finishes preparing (items prepare in parallel, any order)
_albums = {}              # media_group_id -> {"items": [(m, text)], "timer"}
_album_lock = threading.Lock()


def _album_add(tok, m, text, enqueue, timer=None):
    gid = m["media_group_id"]
    with _album_lock:
        a = _albums.setdefault(gid, {"items": [], "timer": None})
        a["items"].append((m, text))
        if a["timer"]:
            a["timer"].cancel()
        t = (timer or threading.Timer)(ALBUM_GATHER_S, _album_flush, args=(tok, gid, enqueue))
        t.daemon = True
        a["timer"] = t
    t.start()


def album_text(texts):
    if len(texts) == 1:
        return texts[0]
    return (f"[{OWNER} sent an album of {len(texts)} items together. Look at all of them and answer once.]\n"
            + "\n".join(f"({i}/{len(texts)}) {t}" for i, t in enumerate(texts, 1)))


def _album_flush(tok, gid, enqueue):
    with _album_lock:
        a = _albums.pop(gid, None)
    if not a:
        return
    items = sorted(a["items"], key=lambda it: it[0].get("message_id") or 0)
    tgapi._log("head", event2="album_joined", lane=_LANE, n=len(items), gid=gid)
    for m, _ in items[1:]:
        _react(tok, m, "done")
    text = album_text([t for _, t in items])
    if _state.get("running"):
        note_midturn(f"[an album of {len(items)} from {OWNER} is processed and queued as the next turn] " + text[:300])
    enqueue(tok, items[0][0], text)


def _start_prep(tok, m):
    threading.Thread(target=_prep_media, args=(tok, m), daemon=True, name="head-media-prep").start()


# ---- Live mode (TIK-1025 step 2): the Mini App's Live tab spools push-to-talk clips + camera frames here ----
LIVE_SPOOL = os.path.join(STATE_DIR, "live_spool")
# ---------------------------------------------------------------- /j relay (TIK-1083)
# Rav types "/j <text>" to a lane bot (a product bot first). The bot's shared jrelay.submit drops one JSON job in RELAY_SPOOL;
# the main lane takes it as a turn whose Telegram side is THAT bot: its token, its chat, a reply to his /j message,
# prefixed "the agent says:". The job carries no token: it is read from the bot's own .env at send time (jrelay.token_for).
sys.path.insert(0, HERE)
import jrelay  # noqa: E402
RELAY_SPOOL = os.path.join(STATE_DIR, "relay_spool")


def _tok_for(m, tok):
    """The token a turn's Telegram side uses: the lane bot's for a relayed message, else the head's own."""
    rel = (m or {}).get("_relay")
    return jrelay.token_for(rel["origin"]) if rel else tok


def relay_media(job, download=None, token=None):
    """A relay job's attachment -> its text plus where the file now is on the host, fetched with the lane bot's own token.
    A failed fetch still gives the head the text and says the file was lost (Iron Rule 12)."""
    med = job["media"]
    kind = med["kind"] if med.get("kind") in jrelay.MEDIA_KINDS else "document"
    name = med.get("file_name") or {"photo": "photo.jpg", "video": "video.mp4", "voice": "voice.ogg"}.get(kind, "file")
    path = inbox_path(name)
    try:
        os.makedirs(INBOX, exist_ok=True)
        (download or tgapi._download)((token or jrelay.token_for)(job["origin"]), med["file_id"], path)
    except Exception as e:
        tgapi._log("head", event2="relay_media_fail", origin=job["origin"], kind=kind, error=str(e)[:200])
        return job["text"] + f" / [{OWNER} attached {kind.replace('_', ' ')}, but the host couldn't fetch it: {str(e)[:120]}]"
    tgapi._log("head", event2="relay_media_in", origin=job["origin"], kind=kind, path=path)
    label = (jrelay.ORIGINS.get(job["origin"]) or {}).get("label", job["origin"])
    return media_prompt(kind, path, job["text"]).replace("from Telegram", f"through {label}")


def take_relay(tok, path, enqueue=None, call=None):
    """One relay job -> a queued turn in this lane, answered through the origin bot. The file is consumed either way.
    "/j stop" is not a turn: it stops this lane at once (relay_stop), ahead of everything queued."""
    if not os.path.exists(path):                 # a stop earlier in this sweep already cleared it
        return False
    job = jrelay.take(path)                      # re-runs the gate; None = refused and logged
    if job is None:
        return False
    if job.get("button"):                        # a press on this head's button in the lane bot's chat (2026-11-05)
        return relay_button(job, call=call)
    if jrelay.is_stop(job["text"]):
        return relay_stop(job, call=call, spool=os.path.dirname(path))   # the spool this job came from
    if job.get("media"):                         # "/j ..." as a photo/file caption (Rav 2026-10-30, a product bot)
        job = dict(job, text=relay_media(job))
    m = {"from": {"id": job["uid"]}, "chat": {"id": job["chat_id"], "type": job.get("chat_type", "private")},
         "message_id": job.get("message_id"), "text": job["text"], "_relay": {"origin": job["origin"]}}
    tgapi._log("head", event2="relay_in", origin=job["origin"], chars=len(job["text"]))
    try:                                         # the lab chat history gets Rav's side too (tg-gateway never sees it)
        chat_ledger.record_in(m, job["text"], job["origin"], (jrelay.ORIGINS.get(job["origin"]) or {}).get("label"))
    except Exception as e:
        tgapi._log("head", event2="ledger_in_fail", origin=job["origin"], error=str(e)[:150])
    prompt = jrelay.prompt_for(job)
    if _state.get("running"):                    # TIK-1421: a /j message mid-turn reaches the running turn too
        key = _midturn_key()
        if note_midturn(prompt, key=key):
            m["_mt"] = key
    (enqueue or _enqueue)(tok, m, prompt)
    return True


def relay_button(job, call=None, press=None):
    """A head button pressed in a lane bot's chat (jrelay carried it; jrelay.take re-ran the gate): handled exactly as a
    press in a head topic, answered with the lane bot's own token. on_button re-checks who pressed."""
    b = job["button"]
    cq = {"id": b["id"], "from": {"id": job["uid"]}, "data": b["data"],
          "message": {"message_id": b["message_id"], "chat": {"id": job["chat_id"], "type": job.get("chat_type")},
                      "text": b.get("text") or "", "reply_markup": {"inline_keyboard": b.get("markup") or []},
                      "_relay": {"origin": job["origin"]}}}
    tgapi._log("head", event2="relay_button", origin=job["origin"], data=b["data"][:30])
    try:
        (press or on_button)(cq, b["data"], jrelay.token_for(job["origin"]), **({"call": call} if call else {}))
    except Exception as e:                       # never silent: the press failed, say so in that chat
        tgapi._log("head", event2="relay_button_fail", origin=job["origin"], error=str(e)[:200])
        try:
            tgapi._reply(jrelay.token_for(job["origin"]), cq["message"], f"⚠️ That button failed on the host: {str(e)[:150]}")
        except Exception as e2:
            tgapi._log("head", event2="relay_button_tell_fail", error=str(e2)[:150])
    return True


def relay_stop(job, call=None, spool=None, reply=None):
    """Rav's "/j stop" (TIK-1083, 2026-10-29): what /stop does in their main head thread, for THIS lane: Escape into
    the head's REPL + the LaneStop marker, then every message still waiting for this origin is dropped (queued turns
    and unread spool files), and the bot chat hears how many. The job already passed jrelay's gate in take()."""
    call = call or bridge
    origin = job["origin"]
    try:
        call({"t": "key", "key": "Escape"})
    except Exception as e:                       # a dead head has no turn to stop; the queue is still cleared
        tgapi._log("head", event2="relay_stop_key_fail", origin=origin, error=str(e)[:150])
    mark_stopped()
    mine = lambda qm: ((qm or {}).get("_relay") or {}).get("origin") == origin
    with _qlock:
        keep = [x for x in _q if not mine(x[0])]
        n = len(_q) - len(keep)
        _q.clear()
        _q.extend(keep)
    spool = spool or RELAY_SPOOL
    try:
        names = [x for x in os.listdir(spool) if x.endswith(".json") and ("_%s_" % origin) in x]
    except FileNotFoundError:
        names = []
    for x in names:
        try:
            os.remove(os.path.join(spool, x))
            n += 1
        except FileNotFoundError:
            pass
    m = {"from": {"id": job["uid"]}, "chat": {"id": job["chat_id"], "type": job.get("chat_type", "private")},
         "message_id": job.get("message_id"), "_relay": {"origin": origin}}
    label = jrelay.ORIGINS[origin]["label"]
    text = f"⏹ Stopped {AGENT} ({label}) — {n} queued message(s) cleared."
    tgapi._log("head", event2="relay_stop", origin=origin, cleared=n)
    jrelay.log("stopped", side="head", origin=origin, uid=job["uid"], cleared=n)
    try:
        (reply or tgapi._reply)(jrelay.token_for(origin), m, text)
    except Exception as e:
        tgapi._log("head", event2="relay_stop_reply_fail", origin=origin, error=str(e)[:150])
    return True


_live = {"thread": None}

# ---------------------------------------------------------------- head -> head tasks (TIK-1082)
# Rav 2026-10-27: "Can you pass tasks and invoke TechSup too?" One head drops a job in the other lane's TASK_SPOOL
# (claude-head/tg/handoff.py); the gateway posts it VISIBLY in that lane's thread ("📋 Task from ...") and queues it
# there as a turn, so Rav sees every handoff and the answer threads under it.
TASK_SPOOL = os.path.join(STATE_DIR, "task_spool")
LANE_NAMES = {"main": config.AGENT_NAME}


def take_task(tok, path, enqueue=None):
    """One handoff job -> a visible post in this lane's thread + a queued turn. The file is consumed either way."""
    taken = path + ".taken"
    os.replace(path, taken)
    try:
        job = json.load(open(taken))
    finally:
        os.remove(taken)
    uid = next(iter(HEAD_UIDS))
    text, frm = str(job.get("text") or "").strip(), str(job.get("from") or "?")
    # TIK-1135: a /j bot lane has no thread in the main bot's DM; its page goes to Rav's chat with ITS bot instead,
    # and the turn is a relay turn (_relay origin), so the reply comes back through that bot like a /j answer.
    bot = HEAD_THREADS.get(uid) is None and _LANE in jrelay.ORIGINS
    if not text or (HEAD_THREADS.get(uid) is None and not bot):
        tgapi._log("head", event2="task_refused", lane=_LANE, why="empty" if not text else "no thread")
        return False
    if bot:
        m = {"from": {"id": uid}, "chat": {"id": uid, "type": "private"}, "_relay": {"origin": _LANE}}
        where = {"chat_id": uid}
        try:
            tok = jrelay.token_for(_LANE)
        except Exception as e:                   # no token: the task still runs (logged); the reply will say so
            tgapi._log("head", event2="task_token_fail", lane=_LANE, error=str(e)[:200])
    else:
        m = {"from": {"id": uid}, "chat": {"id": uid}, "message_thread_id": HEAD_THREADS[uid]}
        where = {"chat_id": uid, "message_thread_id": HEAD_THREADS[uid]}
    try:
        sent = tgapi._api(tok, "sendMessage", **where,
                               text=f"📋 Task from {LANE_NAMES.get(frm, frm)}:\n{text[:3500]}")
        m["message_id"] = sent["result"]["message_id"]
    except Exception as e:                       # the post is a courtesy; the task still runs, and that is logged
        tgapi._log("head", event2="task_post_fail", lane=_LANE, error=str(e)[:200])
    tgapi._log("head", event2="task_in", lane=_LANE, frm=frm, chars=len(text))
    (enqueue or _enqueue)(tok, m, f"[Task handed to you by the {LANE_NAMES.get(frm, frm)} head (another {AGENT} head; "
                                  f"{OWNER} sees this handoff in your thread). Do it, and report the result here.]\n{text}")
    return True


def _take_tasks(lane, tok):
    try:
        names = sorted(n for n in os.listdir(lane.TASK_SPOOL) if n.endswith(".json"))
    except FileNotFoundError:
        return
    for n in names:
        try:
            lane.take_task(tok, os.path.join(lane.TASK_SPOOL, n))
        except Exception as e:
            tgapi._log("head", event2="task_fail", lane=lane._LANE, job=n, error=str(e)[:300])


def live_prompt(transcript, frames, sheet=None, hold_s=None):
    shown = "\n".join(frames) if frames else "(no camera frames arrived around this moment)"
    said = transcript.strip()
    if "\n" in said:                                 # several holds in one turn, each stamped m:ss into the turn
        head = f"[Live mode, from the Mini App camera] {OWNER} said, in several holds:\n{said}\n"
    else:
        head = f"[Live mode, from the Mini App camera] {OWNER} said: {said}\n"
    if sheet:
        # TIK-1025 house tour: one image covers the WHOLE hold, so one Read lines up with everything he said
        # 19:56: every path in a prompt becomes an attached image in the REPL; 21 of them overloaded the turn and
        # wrapped the input box. So ONE image (the sheet); the single frames are named by folder, not path.
        folder = os.path.dirname(frames[0]) if frames else "?"
        names = ", ".join(os.path.basename(f) for f in frames)
        return (head + f"He held the button {hold_s or 0:.0f} s. CONTACT SHEET of the whole hold (each frame labelled "
                f"m:ss into the hold) - Read it FIRST: {sheet}\n{len(frames)} single frames (oldest first) are in folder "
                f"{folder} - open one only for a closer look: {names}")
    return head + f"Camera frames from the last few seconds, oldest first; Read them before you answer:\n{shown}"


def take_live(tok, path, to_text=None):
    """One spool file -> heard line in the head thread + a queued turn. The file is consumed either way."""
    to_text = to_text or transcribe
    taken = path + ".taken"
    os.replace(path, taken)                       # a second pass never takes it twice
    try:
        job = json.load(open(taken))
    finally:
        os.remove(taken)
    uid = int(job.get("uid", 0))
    if uid not in HEAD_UIDS or HEAD_THREADS.get(uid) is None:
        tgapi._log("head", event2="live_refused", uid=uid)
        return False
    m = {"from": {"id": uid}, "chat": {"id": uid}, "message_thread_id": HEAD_THREADS[uid]}
    # an End-turn job arrives already transcribed (each hold heard the moment it was released, Rav 20:12)
    tr = job.get("transcript") or to_text(job["audio"])
    if not tr or not tr.strip():
        tgapi._log("head", event2="live_unheard", audio=job["audio"])
        tgapi._reply(tok, m, "⚠️ Live: I couldn't make out that clip. Hold the button while you talk.")
        live_status("unheard")
        return False
    live_status("heard", tr.strip())
    # 21:05: a Telegram TLS timeout on THIS post dropped Rav's whole turn (the job was already consumed). The heard
    # line is a courtesy; the turn is not. A failed post is logged and the turn still goes to the head.
    try:
        sent = tgapi._api(tok, "sendMessage", chat_id=uid, message_thread_id=HEAD_THREADS[uid],
                               text="👁 live, heard: " + tr.strip())
        m["message_id"] = sent["result"]["message_id"]
    except Exception as e:
        tgapi._log("head", event2="live_heard_post_fail", error=str(e)[:200])
    m["_from_voice"] = True                       # spoken in, so the answer is spoken back (TIK-1019 B)
    m["_live"] = True                             # ...out of the Mini App's speaker while Live is open
    tgapi._log("head", event2="live_in", frames=len(job.get("frames") or []), audio=job["audio"])
    _enqueue(tok, m, live_prompt(tr, job.get("frames") or [], job.get("sheet"), job.get("hold_s")))
    return True


MINIAPP_SOCK = os.path.expanduser(config.section("stage").get("socket", os.path.join(config.STATE_DIR, "stage.sock")))
MINIAPP_KEY = os.path.join(config.STATE_DIR, "stage", "push.key")   # the stage server writes it


def _miniapp_post(path, body):
    """POST to the Mini App over its unix socket with the agent's push key. -> its JSON."""
    import http.client
    import socket

    class _UDS(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.settimeout(5)
            self.sock.connect(MINIAPP_SOCK)

    c = _UDS("miniapp")
    c.request("POST", path, body=json.dumps(body),
              headers={"Content-Type": "application/json", "X-Push-Key": open(MINIAPP_KEY).read().strip()})
    return json.loads(c.getresponse().read() or b"{}")


def live_status(state, text="", post=None):
    """Rav 18:35: 'there was no indicator that anything was happening after I let go'. Tell the open Live page
    where its clip is: heard (with the words) -> thinking; or unheard. Best effort, logged, never blocks."""
    try:
        (post or _miniapp_post)("/api/push-live/rav", {"state": state, "text": text[:300]})
    except Exception as e:
        tgapi._log("head", event2="live_status_fail", state=state, error=str(e)[:150])


def live_speak(text, post=None):
    """Hand a Live-mode answer to the Mini App, which streams it in the agent's voice to the open Live page.
    -> True only when the Live tab was open and took it; False (logged) = send the voice note instead."""
    if not (text or "").strip():
        return False
    try:
        r = (post or _miniapp_post)("/api/push-speak/rav", {"text": text})
        tgapi._log("head", event2="live_speak", live=bool(r.get("live")), chars=len(text))
        return bool(r.get("live"))
    except Exception as e:
        tgapi._log("head", event2="live_speak_fail", error=str(e)[:200])
        return False


# TIK-1028 (main head, 2026-10-25): a turn the HEAD starts itself (a background job finishing, a CronCreate reminder) answered
# into the void: head_lane only relayed turns that Rav's message started. Evidence: the Grok edit finished 04:40, the head woke
# and wrote its summary, nothing reached Telegram until 05:22. While the lane is idle, any Stop event is such a turn: its final
# message goes to the thread, marked so Rav can tell it from an answer to him.
FOLLOWUP_OFF = os.path.join(STATE_DIR, "followup.off")      # touch to switch the relay off
FOLLOWUP_TAG = "⏰ follow-up"


def _lane_idle():
    return not (_state.get("running") or _state.get("busy_n") or _q)


def relay_followups(tok, read=read_events, post=None):
    """-> number of follow-ups relayed. Idle lanes only; a lane with no Telegram thread (a relay-only product bot) is skipped."""
    if os.path.exists(FOLLOWUP_OFF) or not any(t for t in HEAD_THREADS.values()):
        return 0
    if not _lane_idle():
        return 0
    off = _state.get("fu_off")
    if off is None:                                    # first look: start at the end, never replay history
        _state["fu_off"] = events_size()
        return 0
    recs, new_off = read(off)
    _state["fu_off"] = new_off
    sent = 0
    for r in recs:
        if r.get("event") != "Stop":
            continue
        body = ((r.get("data") or {}).get("last_assistant_message") or "").strip()
        if not body or "headsafe-probe-" in body:       # headsafe.py's liveness probe (TIK-1005) is answered in the REPL, not in the thread
            continue
        for uid, tid in HEAD_THREADS.items():
            if not tid:
                continue
            m = {"chat": {"id": uid}, "from": {"id": uid}, "message_thread_id": tid}
            try:
                (post or (lambda t, mm, txt: _IO(t, mm).answer(txt)))(tok, m, FOLLOWUP_TAG + "\n\n" + body)
                sent += 1
                tgapi._log("head", event2="followup_relayed", lane=_LANE, chars=len(body))
            except Exception as e:
                tgapi._log("head", event2="followup_fail", lane=_LANE, error=str(e)[:200])
    return sent


def _take_lives(lane, tok):
    """Every Live job in this lane's spool -> that lane's turn (the Mini App's head picker, Rav 2026-10-30)."""
    try:
        names = sorted(n for n in os.listdir(lane.LIVE_SPOOL) if n.endswith(".json"))
    except FileNotFoundError:
        names = []
    for n in names:
        try:
            lane.take_live(tok, os.path.join(lane.LIVE_SPOOL, n))
        except Exception as e:
            tgapi._log("head", event2="live_fail", lane=lane._LANE, job=n, error=str(e)[:300])


def _live_loop(tok, sleep=time.sleep):
    while True:
        _take_lives(sys.modules[__name__], tok)
        if _LANE == "main":
            for lane in list(globals().get("OTHERS", [])):
                _take_lives(lane, tok)
        for lane in [sys.modules[__name__]] + (list(globals().get("OTHERS", [])) if _LANE == "main" else []):
            try:
                lane.relay_followups(tok)           # TIK-1028: a turn the head started itself still reaches the thread
            except Exception as e:
                tgapi._log("head", event2="followup_loop_fail", lane=getattr(lane, "_LANE", "?"), error=str(e)[:200])
        if _LANE == "main":
            for lane in [sys.modules[__name__]] + list(globals().get("OTHERS", [])):
                _take_tasks(lane, tok)
                # /j relay jobs (TIK-1083): each origin's jobs land in ITS lane's spool (jrelay.spool_for) and run
                # as that lane's turn, so product-bot conversation never mixes into the main head (Rav 2026-10-27)
                try:
                    rnames = sorted(n for n in os.listdir(lane.RELAY_SPOOL) if n.endswith(".json"))
                except FileNotFoundError:
                    rnames = []
                for n in rnames:
                    try:
                        lane.take_relay(tok, os.path.join(lane.RELAY_SPOOL, n))
                    except Exception as e:
                        tgapi._log("head", event2="relay_fail", lane=lane._LANE, job=n, error=str(e)[:300])
        sleep(1)


def start_live(tok):
    """Called once by tg-gateway at start; idempotent. Also picks up a turn the last gateway left in flight."""
    if _live["thread"] is None:
        try:
            recover_inflight(tok)
        except Exception as e:
            tgapi._log("head", event2="inflight_recover_fail", error=str(e)[:200])
        _live["thread"] = threading.Thread(target=_live_loop, args=(tok,), daemon=True, name="head-live")
        _live["thread"].start()


def _enqueue(tok, m, text):
    if m.get("_gone_check"):
        m["_q_ts"] = time.time()               # TIK-1100: when it started waiting
    with _qlock:
        _q.append((m, text))
        depth = len(_q)
        if _state["worker"] is None:
            t = threading.Thread(target=_serve, args=(tok,), daemon=True, name="head-lane")
            t.start()
            _state["worker"] = t
    _wake.set()
    try:
        tok = _tok_for(m, tok)
    except Exception as e:                                 # no token for the origin: the turn will say so
        tgapi._log("head", event2="relay_token_fail", error=str(e)[:200])
    _react(tok, m, "queued")
    prev, _state["last_enq"] = _state.get("last_enq") or 0, time.time()
    if depth > 1 and _state["last_enq"] - prev > BURST_QUIET_S:      # part of a burst: no "Queued" line per part
        tgapi._reply(tok, m, f"⏳ Queued ({depth} in line).")


# ---------------------------------------------------------------- more head threads (TIK-1082)
LANES_FILE = os.path.join(BASE_DIR, "lanes.json")      # ["techsup", ...]: the extra head lanes to load


def _load_lane(name):
    """A second copy of this module with its own globals (state dir, socket, thread, queue). -> module."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"head_lane_{name}", os.path.abspath(__file__))
    mod = importlib.util.module_from_spec(spec)
    mod._LANE = name
    spec.loader.exec_module(mod)
    return mod


def _thread_of(msg):
    return (msg or {}).get("message_thread_id")


if _LANE == "main":
    OTHERS = []
    try:
        for _n in json.load(open(LANES_FILE)):
            if re.fullmatch(r"[a-z][a-z0-9_-]{1,20}", str(_n)) and _n != "main":
                OTHERS.append(_load_lane(_n))
                tgapi._log("head", event2="lane_loaded", lane=_n)
    except FileNotFoundError:
        pass
    except Exception as e:                       # a broken extra lane never takes the main head down; it is said loudly
        tgapi._log("head", event2="lane_load_fail", error=str(e)[:200])
    _own = {"handle": handle, "on_button": on_button, "on_reaction": on_reaction, "on_edit": on_edit}

    # TIK-1133: Rav makes a new topic in his DM with the bot -> it gets its own head, built by new_lane.py (Rav
    # 2026-10-30: "an automation to create ones if/when I start a new thread ... That would make that a CDT replacement")
    NEW_LANE = os.path.join(HERE, "new_lane.py")

    def add_lane(name):
        """Load a lane made while the gateway runs. -> True when it was loaded now (False = already there)."""
        if any(o._LANE == name for o in OTHERS):
            return False
        OTHERS.append(_load_lane(name))
        tgapi._log("head", event2="lane_loaded", lane=name, hot=True)
        return True

    def build_lane(tok, uid, tid, title, run=None, post=None):
        """new_lane.py in a worker; then load the lane and say so in the new topic, ready or failed (Iron Rule 12)."""
        import subprocess
        try:
            r = (run or subprocess.run)([sys.executable, NEW_LANE, "create", "--title", title, "--thread", str(tid),
                                         "--uid", str(uid), "--json"], capture_output=True, text=True, timeout=420)
            res = json.loads((r.stdout or "").strip().splitlines()[-1])
        except Exception as e:
            res = {"ok": False, "step": "run", "why": f"{type(e).__name__}: {e}"[:200]}
        if res.get("ok"):
            try:
                add_lane(res["lane"])
                text = f"✅ This topic has its own head now (lane {res['lane']}). Tell me what it's for and I'll keep to it."
            except Exception as e:
                res = {"ok": False, "step": "load", "why": f"{type(e).__name__}: {e}"[:200]}
        if not res.get("ok"):
            text = (f"⚠️ I couldn't set up a head for this topic (step {res.get('step')}: {res.get('why')}). "
                    "Logged in " + os.path.join(BASE_DIR, "new_lane.log") + ".")
        tgapi._log("head", event2="topic_lane", thread=tid, ok=bool(res.get("ok")), lane=res.get("lane"),
                        step=res.get("step"), why=str(res.get("why") or "")[:200])
        try:
            (post or tgapi._api)(tok, "sendMessage", chat_id=uid, message_thread_id=tid, text=text)
        except Exception as e:
            tgapi._log("head", event2="topic_lane_post_fail", thread=tid, error=str(e)[:200])
        return res

    def topic_created(m, tok, start=None):
        """A new topic: Rav's, in his own DM -> build its head in the background and return True.
        Anyone else, any other chat, a topic a head already answers: False / untouched (the gateway goes on)."""
        t = m.get("forum_topic_created")
        if not t:
            return False
        uid = (m.get("from") or {}).get("id")
        chat = m.get("chat") or {}
        tid = m.get("message_thread_id")
        if uid not in HEAD_UIDS or chat.get("id") != uid or chat.get("type") != "private" or tid is None:
            tgapi._log("head", event2="topic_refused", uid=uid, chat=chat.get("id"), thread=tid)
            return False
        if tid in HEAD_THREADS.values() or any(tid in o.HEAD_THREADS.values() for o in OTHERS):
            return True
        tgapi._log("head", event2="topic_new", thread=tid, title=str(t.get("name"))[:60])
        (start or (lambda f, *a: threading.Thread(target=f, args=a, daemon=True, name="new-lane").start()))(
            build_lane, tok, uid, tid, str(t.get("name") or "topic"))
        return True

    # /deletethread (Rav 2026-10-30): "the whole thing gets removed from TG. The history stays/gets archived.
    # Whatever memories and lessons stay in RAG." Telegram sends no update when a topic is deleted in a private chat
    # (Bot API: only forum_topic_created/edited/closed/reopened; closeForumTopic is supergroup-only), so the
    # command is the one way in. Only a lane new_lane.py built; Yes/No first; new_lane.py remove does the disk side
    # (lanes.json, unit + tmux, archive + RAG copy); then the lane is dropped here and the topic deleted.
    _DT = re.compile(r"^/deletethread(?:@\w+)?\s*$", re.I)

    def _new_lane_mod():
        import importlib.util
        spec = importlib.util.spec_from_file_location("new_lane_dt", NEW_LANE)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        return mod

    def delete_thread_cmd(m, tok, nl=None, card=None):
        """'/deletethread' in a topic -> Yes/No card (or a refusal) and True; anything else -> False."""
        if not _DT.match((m.get("text") or "").strip()):
            return False
        uid, chat, tid = (m.get("from") or {}).get("id"), m.get("chat") or {}, m.get("message_thread_id")
        say = lambda t: tgapi._reply(tok, m, t)
        if tid is None or uid not in HEAD_UIDS or chat.get("type") != "private" or chat.get("id") != uid:
            tgapi._log("head", event2="deletethread_refused", uid=uid, chat=chat.get("id"), thread=tid)
            return False                                       # the normal routing refuses or ignores it as before
        if tid in HEAD_THREADS.values():
            say(f"This is the main {AGENT} thread; it can't be deleted."); return True
        o = next((o for o in OTHERS if tid in o.HEAD_THREADS.values()), None)
        if o is None:
            say("This topic has no head, so there's nothing for me to remove."); return True
        why = (nl or _new_lane_mod()).removable(o._LANE, tid)
        if why:
            tgapi._log("head", event2="deletethread_refused", lane=o._LANE, why=why[:150])
            say(f"I won't delete this one: {why}."); return True
        kb = {"inline_keyboard": [[{"text": "🗑 Yes, delete it", "callback_data": f"hd:dt:y:{o._LANE}:{tid}"},
                                   {"text": "Keep it", "callback_data": f"hd:dt:n:{o._LANE}:{tid}"}]]}
        (card or _send_card)(tok, m, f"Delete this topic and its head (lane {o._LANE})?\n\nThe head stops, its folder, "
                             "memory and transcripts are archived (and searchable in RAG), the chat history log keeps "
                             "every message, and the topic disappears from Telegram.", kb)
        tgapi._log("head", event2="deletethread_asked", lane=o._LANE, thread=tid)
        return True

    def delete_thread_button(cq, data, tok, run=None, api=None, start=None):
        """hd:dt:y|n:<lane>:<tid>. Rav only, his DM only. Yes -> remove in a worker, then drop + delete the topic."""
        api = api or tgapi._api
        uid, msg = (cq.get("from") or {}).get("id"), cq.get("message") or {}
        parts = data.split(":")
        def answer(t):
            try:
                api(tok, "answerCallbackQuery", callback_query_id=cq.get("id"), text=t)
            except Exception as e:
                tgapi._log("head", event2="cb_answer_fail", error=str(e)[:150])
        if (uid not in HEAD_UIDS or (msg.get("chat") or {}).get("id") != uid or len(parts) != 5
                or parts[2] not in ("y", "n") or not parts[4].isdigit()):
            tgapi._log("head_refused_button", uid=uid, data=data[:40]); answer("Not yours."); return
        lane, tid = parts[3], int(parts[4])
        _mark_card(tok, msg, data, parts[2])
        if parts[2] == "n":
            tgapi._log("head", event2="deletethread_kept", lane=lane); answer("Kept."); return
        o = next((o for o in OTHERS if o._LANE == lane and tid in o.HEAD_THREADS.values()), None)
        if o is None:
            answer("That topic's head is already gone."); return
        answer("Deleting…")
        run_bg = start or (lambda f, *a: threading.Thread(target=f, args=a, daemon=True, name="del-lane").start())
        run_bg(_delete_thread, tok, uid, tid, o, run, api)

    def _delete_thread(tok, uid, tid, o, run=None, api=None):
        import subprocess
        api = api or tgapi._api
        lane = o._LANE
        home = HEAD_THREADS.get(uid)                          # the result is said in the main head: the topic is gone
        def tell(t, thread):
            try:
                api(tok, "sendMessage", chat_id=uid, message_thread_id=thread, text=t)
            except Exception as e:
                tgapi._log("head", event2="deletethread_post_fail", lane=lane, error=str(e)[:150])
        try:
            r = (run or subprocess.run)([sys.executable, NEW_LANE, "remove", "--lane", lane, "--thread", str(tid), "--json"],
                                        capture_output=True, text=True, timeout=180)
            res = json.loads((r.stdout or "").strip().splitlines()[-1])
        except Exception as e:
            res = {"ok": False, "step": "run", "why": f"{type(e).__name__}: {e}"[:200]}
        tgapi._log("head", event2="deletethread_removed", lane=lane, ok=bool(res.get("ok")),
                        step=res.get("step"), why=str(res.get("why") or "")[:200], archive=res.get("archive"))
        if not res.get("ok"):
            tell(f"⚠️ I couldn't delete this topic's head (step {res.get('step')}: {res.get('why')}). Nothing after "
                 "that step ran and the topic is still here. Logged in " + os.path.join(BASE_DIR, "new_lane.log") + ".", tid)
            return res
        if o in OTHERS:
            OTHERS.remove(o)                                  # the gateway stops routing to it, no restart
        tgapi._log("head", event2="lane_unloaded", lane=lane)
        try:
            api(tok, "deleteForumTopic", chat_id=uid, message_thread_id=tid)
            tgapi._log("head", event2="topic_deleted", lane=lane, thread=tid)
            tell(f"🗑 Deleted the topic for lane {lane}. Its head is stopped; folder, memory and transcripts are "
                 f"archived at {res.get('archive')} (RAG picks the transcripts up at the nightly ingest). "
                 "The chat history log keeps every message.", home)
        except Exception as e:
            tgapi._log("head", event2="topic_delete_fail", lane=lane, thread=tid, error=str(e)[:200])
            tell(f"⚠️ Lane {lane}'s head is removed and archived, but Telegram refused to delete the topic "
                 f"({str(e)[:120]}). Delete it by hand; nothing answers there now.", home)
        return res

    # TIK-1454 (Rav 2026-11-03): a topic rename reaches the head's title at once; the internal name moves only on
    # /renamehead, because that stops and restarts the head.
    _RH = re.compile(r"^/renamehead(?:@\w+)?(?:\s+(.+?))?\s*$", re.I | re.S)
    _RENAMING = set()

    def _rav_lane(m):
        """Rav in his own DM, in a topic with a lane -> that lane module, else None."""
        uid, chat, tid = (m.get("from") or {}).get("id"), m.get("chat") or {}, m.get("message_thread_id")
        if uid not in HEAD_UIDS or tid is None or chat.get("type") != "private" or chat.get("id") != uid:
            return None
        return next((o for o in OTHERS if tid in o.HEAD_THREADS.values()), None)

    def topic_edited(m, tok, run=None):
        """Telegram says a topic was renamed: update that head's title. An icon-only edit has no name."""
        t = m.get("forum_topic_edited")
        if not t:
            return False
        o, name = _rav_lane(m), t.get("name")
        if o is None or not name:
            return True
        import subprocess
        try:
            r = (run or subprocess.run)([sys.executable, NEW_LANE, "retitle", "--lane", o._LANE, "--title", str(name)],
                                        capture_output=True, text=True, timeout=30)
            res = json.loads((r.stdout or "").strip().splitlines()[-1])
        except Exception as e:
            res = {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
        tgapi._log("head", event2="topic_retitled", lane=o._LANE, ok=bool(res.get("ok")), why=str(res.get("why") or "")[:200])
        tgapi._reply(tok, m, (f"Title updated to \"{name}\". Its internal name is still {o._LANE}; /renamehead changes that too."
                                   if res.get("ok") else f"⚠️ I couldn't update this head's title: {res.get('why')}."))
        return True

    def rename_head_cmd(m, tok, nl=None, start=None):
        """/renamehead [title] in a topic -> the rename runs in a worker; False for anything else."""
        hit = _RH.match((m.get("text") or "").strip())
        if not hit:
            return False
        o, tid = _rav_lane(m), m.get("message_thread_id")
        if o is None:
            tgapi._reply(tok, m, "/renamehead only works in a topic with its own head."); return True
        mod = nl or _new_lane_mod()
        why = mod.removable(o._LANE, tid) or mod.busy(o._LANE)
        if why:
            tgapi._log("head", event2="renamehead_refused", lane=o._LANE, why=why[:150])
            tgapi._reply(tok, m, f"Not renaming right now: {why}. Nothing changed."); return True
        made = mod._jload(os.path.join(BASE_DIR, o._LANE, mod.MADE_BY), {})
        title = (hit.group(1) or made.get("title") or "").strip()
        new = mod.slug(title, width=20) if title else ""
        if not title or new == o._LANE:
            tgapi._reply(tok, m, "Tell me the new name: /renamehead Network DNS Firewall"); return True
        tgapi._reply(tok, m, f"Renaming {o._LANE} to {new}. The head restarts, so hold messages for about a minute.")
        _RENAMING.add(tid)
        go = start or (lambda f, *a: threading.Thread(target=f, args=a, daemon=True, name="rename-lane").start())
        go(_rename_head, tok, m, o, new, title)
        return True

    def _rename_head(tok, m, o, new, title, run=None):
        import subprocess
        tid, old = m.get("message_thread_id"), o._LANE
        if o in OTHERS:
            OTHERS.remove(o)                                  # its files are about to move; _RENAMING answers meanwhile
        try:
            r = (run or subprocess.run)([sys.executable, NEW_LANE, "rename", "--lane", old, "--to", new, "--title", title,
                                         "--thread", str(tid)], capture_output=True, text=True, timeout=420)
            res = json.loads((r.stdout or "").strip().splitlines()[-1])
        except Exception as e:
            res = {"ok": False, "step": "run", "why": f"{type(e).__name__}: {e}"[:200], "restored": False}
        try:
            if res.get("ok"):
                add_lane(new)
                text = f"✅ Renamed to {new}. Same conversation, and the title is now \"{title}\"."
            elif res.get("restored"):
                OTHERS.append(o)
                text = f"⚠️ Rename failed at {res.get('step')}: {res.get('why')}. I put everything back; the head is still {old}."
            else:
                text = (f"❌ Rename failed at {res.get('step')}: {res.get('why')}, and the undo did not finish. "
                        f"This topic has no working head until it's repaired. Log: {os.path.join(BASE_DIR, 'new_lane.log')}")
        except Exception as e:
            text = f"❌ The rename finished but the gateway couldn't load the lane ({type(e).__name__}: {str(e)[:120]}). Restart tg-gateway."
        finally:
            _RENAMING.discard(tid)
        tgapi._log("head", event2="renamehead_done", old=old, new=new, ok=bool(res.get("ok")), step=res.get("step"),
                        why=str(res.get("why") or "")[:200], restored=res.get("restored"))
        tgapi._reply(tok, m, text)
        return res

    def handle(m, tok, prep=None) -> bool:      # noqa: F811 - routes by thread; the main head first, unchanged
        if topic_created(m, tok):
            return True
        if topic_edited(m, tok):
            return True
        if m.get("message_thread_id") in _RENAMING:          # never dropped silently: he's told to resend
            tgapi._reply(tok, m, "This head is restarting for a rename. I didn't take that message; send it again in a minute.")
            return True
        if rename_head_cmd(m, tok):
            return True
        if delete_thread_cmd(m, tok):
            return True
        if _own["handle"](m, tok, prep):
            return True
        return any(o.handle(m, tok, prep) for o in OTHERS)

    def on_button(cq, data, tok, call=None):    # noqa: F811 - the card's thread says which head it belongs to
        if data.startswith("hd:dt:"):
            return delete_thread_button(cq, data, tok)
        tid = _thread_of(cq.get("message"))
        for o in OTHERS:
            if tid is not None and tid in o.HEAD_THREADS.values():
                return o.on_button(cq, data, tok, call)
        return _own["on_button"](cq, data, tok, call)

    def on_reaction(r, path=None, turns=None, now=time.time):   # noqa: F811 - no thread on a reaction: each lane
        out = _own["on_reaction"](r, path, turns, now)          # checks its own answer ids
        if path is None and turns is None:
            for o in OTHERS:
                out = o.on_reaction(r) or out
        return out

    def on_edit(e, path=None, tok=None, enqueue=None):     # noqa: F811
        tid = _thread_of(e)
        for o in OTHERS:
            if tid is not None and tid in o.HEAD_THREADS.values():
                return o.on_edit(e, tok=tok, enqueue=enqueue)
        return _own["on_edit"](e, path, tok, enqueue)
