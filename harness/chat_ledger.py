"""chat_ledger.py - the Telegram side of the lab chat history (chat_log in the v4 chat_history.db). TIK-1027.

Rav 2026-10-24 16:45: "we need to keep track of transcripts and chat history for the heads, and we already
have this right? That just becomes another entry into that same system."

So this adds to the SAME table, never a new store. tg-gateway already inserted one row per inbound update
(session_id tg_<update_id>, text only). Now every row also carries, in its `extra` JSON, what a reply needs:
message_id, chat, the forum thread, what it replies to (and the quoted part), where a forward came from,
and the media (kind + file_id, so it can be fetched again later). The bot's own sends are rows too
(role assistant, session_id tgout_<chat>_<message_id>).

Deterministic code only: no LLM. Every failure is logged by the caller's logger and returns quietly.
"""
import json
import os
import sqlite3
import time

import config

CHATDB = os.environ.get("CHAT_DB", os.path.join(config.STATE_DIR, "chat_history.db"))
OWNER = config.USERS.get(config.OWNER_ID, "owner")
AGENT = config.AGENT_NAME
SCHEMA = """CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'web',
    mode TEXT NOT NULL DEFAULT 'talk', role TEXT NOT NULL, content TEXT NOT NULL, model TEXT,
    ts REAL NOT NULL DEFAULT (strftime('%s','now')), thread_id TEXT, extra TEXT, user_id TEXT);
CREATE INDEX IF NOT EXISTS idx_session ON chat_log(session_id);
CREATE INDEX IF NOT EXISTS idx_ts ON chat_log(ts);"""
MEDIA_KINDS = ("photo", "video", "video_note", "voice", "audio", "document", "animation", "sticker")
CONTEXT_AROUND = 3
SNIP = 400


def media_of(m):
    for k in MEDIA_KINDS:
        v = m.get(k)
        if v:
            o = v[-1] if k == "photo" else v
            return {"kind": k, "file_id": o.get("file_id"), "file_name": o.get("file_name"),
                    "duration": o.get("duration")}
    return None


def is_real_reply(m):
    """In a forum topic every message 'replies' to the topic's first service message. That is not a reply."""
    r = m.get("reply_to_message")
    if not r or r.get("forum_topic_created"):
        return False
    return r.get("message_id") != m.get("message_thread_id")


def _who(u):
    if not u:
        return None
    return u.get("username") or u.get("first_name") or u.get("title") or str(u.get("id"))


def forward_of(m):
    """-> {from, date} for a forwarded message (Bot API forward_origin), else None."""
    o = m.get("forward_origin")
    if not o:
        return None
    src = (o.get("sender_user") and _who(o["sender_user"])) or o.get("sender_user_name")\
        or (o.get("chat") and _who(o["chat"])) or (o.get("sender_chat") and _who(o["sender_chat"])) or o.get("type")
    return {"from": src, "date": o.get("date"), "type": o.get("type")}


def extra_of(m):
    """The JSON stored with a row: everything needed to rebuild context around this message later."""
    if not m:
        return {}
    x = {"from": _who(m.get("from")), "message_id": m.get("message_id"), "chat_id": (m.get("chat") or {}).get("id"),
         "tg_thread": m.get("message_thread_id")}
    if is_real_reply(m):
        r = m["reply_to_message"]
        x["reply_to"] = r.get("message_id")
        if (m.get("quote") or {}).get("text"):
            x["quote"] = m["quote"]["text"][:SNIP]
    fw = forward_of(m)
    if fw:
        x["forward"] = fw
    md = media_of(m)
    if md:
        x["media"] = md
    if m.get("caption"):
        x["caption"] = m["caption"][:SNIP]
    if m.get("media_group_id"):
        x["album"] = m["media_group_id"]
    return x


def _conn(db=None):
    c = sqlite3.connect(db or CHATDB, timeout=10)
    c.executescript(SCHEMA)
    return c


def enrich(session_id, m, db=None):
    """Add the reply/forward/media fields to the row tg-gateway's mirror() just wrote (same session_id)."""
    c = _conn(db)
    try:
        row = c.execute("SELECT id, extra FROM chat_log WHERE session_id=? AND source='telegram'", (session_id,)).fetchone()
        if not row:
            return False
        cur = json.loads(row[1] or "{}")
        cur.update({k: v for k, v in extra_of(m).items() if v is not None})
        c.execute("UPDATE chat_log SET extra=? WHERE id=?", (json.dumps(cur), row[0]))
        c.commit()
        return True
    finally:
        c.close()


def record_update(m, update_id, db=None):
    """The gateway's inbound row for one update (session tg_<update_id>), with reply/forward/media detail.
    Idempotent: a re-delivered update writes nothing."""
    frm = m.get("from") or {}
    sid = f"tg_{update_id}"
    x = {"from": frm.get("username") or frm.get("first_name") or str(frm.get("id"))}
    x.update({k: v for k, v in extra_of(m).items() if v is not None})
    c = _conn(db)
    try:
        if c.execute("SELECT 1 FROM chat_log WHERE session_id=? AND source='telegram'", (sid,)).fetchone():
            return False
        c.execute("INSERT INTO chat_log (session_id, source, mode, role, content, model, ts, thread_id, extra, user_id)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (sid, "telegram", "inbox", "user", m.get("text") or m.get("caption") or "[non-text]", "tg_inbox",
                   float(m.get("date") or time.time()), sid, json.dumps(x), str(frm.get("id"))))
        c.commit()
        return True
    finally:
        c.close()


def record_in(m, text, origin, label=None, db=None, now=None):
    """Rav's /j message to another lab bot (a product bot, the ticket system...): tg-gateway never sees those updates (the lane bot
    polls them), so the head lane writes the row when it takes the job. Rav 2026-10-30: "Are all the chat transcripts
    from all the heads being saved in the chat history log/timeline?" -> they are now. Idempotent per message."""
    chat_id = (m.get("chat") or {}).get("id")
    sid = f"tgj_{origin}_{chat_id}_{m.get('message_id')}"
    x = {k: v for k, v in extra_of(m).items() if v is not None}
    x.update(origin=origin, bot=label or origin, via="/j")
    c = _conn(db)
    try:
        if c.execute("SELECT 1 FROM chat_log WHERE session_id=? AND source='telegram'", (sid,)).fetchone():
            return False
        c.execute("INSERT INTO chat_log (session_id, source, mode, role, content, model, ts, thread_id, extra, user_id)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (sid, "telegram", "inbox", "user", text or "[non-text message]", "tg_relay", float(now or time.time()),
                   sid, json.dumps(x), str((m.get("from") or {}).get("id"))))
        c.commit()
        return True
    finally:
        c.close()


SAME_MESSAGE_WINDOW_S = 6 * 3600   # tgout_<chat>_<message_id> is per BOT: two bots' message ids in Rav's chat can collide days apart


def _same_message(existing_extra, x, existing_ts, now):
    """Is the row already there THIS message? Same bot when both know their bot (bot_id / origin), and sent within hours:
    message 209 in the a product bot chat (2026-11-01) is not message 209 in the main bot's chat (2026-10-30)."""
    had = json.loads(existing_extra or "{}")
    for k in ("bot_id", "origin"):
        if had.get(k) is not None and x.get(k) is not None and had[k] != x[k]:
            return False
    return abs(float(now) - float(existing_ts)) <= SAME_MESSAGE_WINDOW_S


def record_out(chat_id, tg_thread, message_id, text, kind="text", turn=None, reply_to=None, db=None, now=None,
               origin=None, label=None, bot_id=None):
    """One row per message the bot sent (an answer part, a voice note, a file, a forward). origin/label: the lab bot
    a /j answer went out through (its message_id is in THAT bot's chat, not the bot's)."""
    x = {"from": AGENT, "message_id": message_id, "chat_id": chat_id, "tg_thread": tg_thread, "kind": kind}
    if origin:
        x.update(origin=origin, bot=label or origin, via="/j")
    if turn:
        x["turn"] = turn
    if reply_to:
        x["reply_to"] = reply_to
    if bot_id is not None:
        x["bot_id"] = str(bot_id)
    sid = f"tgout_{chat_id}_{message_id}"
    now = float(now or time.time())
    c = _conn(db)
    try:
        # TIK-1416: ONE row per sent message. print_lane._api records every send the moment it goes out (a generic
        # kind="post" row); a later, richer record_out for the same message (an answer part with its turn, a /j origin)
        # ENRICHES that row instead of adding a second one. Idempotent: the same call twice changes nothing.
        row = next((r for r in c.execute("SELECT id, content, extra, ts FROM chat_log WHERE session_id=? AND source='telegram'",
                                         (sid,)).fetchall() if _same_message(r[2], x, r[3], now)), None)
        if row:
            rid, content, extra, _ts = row
            had = json.loads(extra or "{}")
            merged = dict(had)
            for k, v in x.items():
                if v is not None and (k not in merged or merged[k] in (None, "post") or k == "kind" and kind != "post"):
                    merged[k] = v
            new_text = text if text else content
            c.execute("UPDATE chat_log SET content=?, extra=?, mode='head', model='hb_head' WHERE id=?",
                      (new_text, json.dumps(merged), rid))
            c.commit()
            return False
        c.execute("INSERT INTO chat_log (session_id, source, mode, role, content, model, ts, thread_id, extra, user_id)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (sid, "telegram", "head", "assistant", text or f"[{kind}]", "hb_head",
                   now, sid, json.dumps(x), None))
        c.commit()
        return True
    finally:
        c.close()


def record_sent_edit(chat_id, message_id, new_text, db=None, now=None, bot_id=None):
    """The bot edited its OWN message (a status bubble ticking, a card marked '-> Allowed'): the row follows the screen.
    The first text is kept in extra.edits. A message never recorded (sent before TIK-1416) is a quiet no-op."""
    sid = f"tgout_{chat_id}_{message_id}"
    now = float(now or time.time())
    x = {"bot_id": str(bot_id)} if bot_id is not None else {}
    c = _conn(db)
    try:
        row = next((r for r in c.execute("SELECT id, content, extra, ts FROM chat_log WHERE session_id=? AND source='telegram'",
                                         (sid,)).fetchall() if _same_message(r[2], x, r[3], now)), None)
        if not row:
            return False
        rid, content, extra, _ts = row
        x = json.loads(extra or "{}")
        if content != new_text and not x.get("edits"):
            x["edits"] = [{"was": content[:SNIP], "at": now or time.time()}]
        c.execute("UPDATE chat_log SET content=?, extra=? WHERE id=?", (new_text, json.dumps(x), rid))
        c.commit()
        return True
    finally:
        c.close()


def record_edit(chat_id, message_id, tg_thread, new_text, db=None, now=None):
    """Rav edited a message: the row's content becomes the new text; the old one is kept in extra.edits."""
    c = _conn(db)
    try:
        for rid, content, extra in c.execute("SELECT id, content, extra FROM chat_log WHERE source='telegram' AND extra LIKE ?",
                                             (f'%"message_id": {int(message_id)}%',)).fetchall():
            x = json.loads(extra or "{}")
            if x.get("chat_id") == chat_id and x.get("tg_thread") == tg_thread and x.get("message_id") == message_id:
                x.setdefault("edits", []).append({"was": content[:SNIP], "at": now or time.time()})
                c.execute("UPDATE chat_log SET content=?, extra=? WHERE id=?", (new_text, json.dumps(x), rid))
                c.commit()
                return True
        return False
    finally:
        c.close()


def record_dropped(chat_id, message_id, tg_thread, head, age_s, db=None, now=None):
    """TIK-1100: Rav deleted this message while it was still queued, so the head never saw it. The row stays (the lab history
    records that he wrote it) and says so in extra.dropped_deleted: which head, how long it had waited."""
    c = _conn(db)
    try:
        for rid, extra in c.execute("SELECT id, extra FROM chat_log WHERE source='telegram' AND extra LIKE ?",
                                    (f'%"message_id": {int(message_id)}%',)).fetchall():
            x = json.loads(extra or "{}")
            if x.get("chat_id") == chat_id and x.get("tg_thread") == tg_thread and x.get("message_id") == message_id:
                x["dropped_deleted"] = {"head": head, "age_s": round(float(age_s), 1), "at": now or time.time()}
                c.execute("UPDATE chat_log SET extra=? WHERE id=?", (json.dumps(x), rid))
                c.commit()
                return True
        return False
    finally:
        c.close()


def _rows(chat_id, tg_thread, db=None):
    c = _conn(db)
    try:
        q = ("SELECT role, content, ts, extra FROM chat_log WHERE source='telegram' AND extra LIKE ? "
             "ORDER BY ts DESC LIMIT 2000")
        out = []
        for role, content, ts, extra in c.execute(q, (f'%"chat_id": {int(chat_id)}%',)):
            x = json.loads(extra or "{}")
            if x.get("chat_id") == chat_id and x.get("tg_thread") == tg_thread and x.get("message_id"):
                out.append({"role": role, "text": content, "ts": ts, **x})
        return sorted(out, key=lambda r: r["message_id"])
    finally:
        c.close()


def find(chat_id, message_id, tg_thread=None, db=None):
    for r in _rows(chat_id, tg_thread, db):
        if r["message_id"] == message_id:
            return r
    return None


def around(chat_id, message_id, tg_thread=None, n=CONTEXT_AROUND, db=None):
    """-> (before, target, after) rows around one message, from the ledger. target may be None."""
    rows = _rows(chat_id, tg_thread, db)
    idx = next((i for i, r in enumerate(rows) if r["message_id"] == message_id), None)
    if idx is None:
        before = [r for r in rows if r["message_id"] < message_id][-n:]
        after = [r for r in rows if r["message_id"] > message_id][:n]
        return before, None, after
    return rows[max(0, idx - n):idx], rows[idx], rows[idx + 1:idx + 1 + n]


def search(chat_id, words, tg_thread=None, limit=10, db=None):
    w = [x.lower() for x in words.split() if x]
    hits = [r for r in _rows(chat_id, tg_thread, db) if all(x in (r["text"] or "").lower() for x in w)]
    return hits[-limit:]


def line(r):
    t = time.strftime("%H:%M", time.localtime(r["ts"])) if r.get("ts") else "?"
    who = AGENT if r.get("role") == "assistant" else (r.get("from") or OWNER)
    body = (r.get("text") or "").replace("\n", " ")
    if r.get("media"):
        body = f"[{r['media']['kind']}] " + body
    return f"  #{r['message_id']} {t} {who}: {body[:SNIP]}"


def context_block(m, db=None):
    """The lines a head gets in front of a message: what it replies to (and around it), and/or forward origin.
    Empty string for a plain message."""
    out = []
    fw = forward_of(m)
    if fw:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(fw["date"])) if fw.get("date") else "unknown time"
        out.append(f"[Forwarded message: originally from {fw['from']}, sent {when}]")
    if is_real_reply(m):
        r = m["reply_to_message"]
        chat, tid = (m.get("chat") or {}).get("id"), m.get("message_thread_id")
        try:
            before, target, after = around(chat, r["message_id"], tid, db=db)
        except Exception:
            before, target, after = [], None, []
        own = target.get("text") if target else None
        body = own or r.get("text") or r.get("caption") or ""
        md = media_of(r)
        kind = f"[{md['kind']}] " if md else ""
        who = f"{AGENT} (you)" if ((r.get("from") or {}).get("is_bot")) else (_who(r.get("from")) or OWNER)
        out.append(f"[{OWNER} is replying to message #{r['message_id']} by {who}: {kind}{body[:1500]}]")
        if (m.get("quote") or {}).get("text"):
            out.append(f"[The highlighted part: \"{m['quote']['text'][:SNIP]}\"]")
        if target and target.get("turn"):
            out.append(f"[That answer came from your turn {target['turn']}]")
        if before or after:
            out.append("[Around it in this thread:]")
            out += [line(x) for x in before] + ["  >> (the message they replied to)"] + [line(x) for x in after]
    return "\n".join(out)
