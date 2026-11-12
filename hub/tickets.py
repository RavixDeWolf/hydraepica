"""tickets.py - the shared status board between heads: file, list, get, comment, resolve.

Heads do not message each other's REPLs; they write status where every head (and the owner) can read it. Every
change is a row in `history` naming who made it; nothing is ever hard-deleted.
"""
import os
import sqlite3
import time

import audit

DB = os.path.join(audit.config.STATE_DIR, "tickets.db")
STATUSES = ("open", "in-progress", "blocked", "resolved", "wontfix")
SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT, status TEXT NOT NULL,
  reporter TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY, ticket INTEGER, who TEXT, body TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, ticket INTEGER, who TEXT, change TEXT, ts REAL);
"""


class TicketError(ValueError):
    pass


def _db(path=None):
    c = sqlite3.connect(path or DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def key(n):
    return f"T-{n:04d}"


def _id(k):
    s = str(k).upper().removeprefix("T-")
    if not s.isdigit():
        raise TicketError(f"not a ticket key: {k!r}")
    return int(s)


def _hist(c, tid, change):
    c.execute("INSERT INTO history (ticket, who, change, ts) VALUES (?,?,?,?)", (tid, audit.identity(), change, time.time()))


def file(title, body="", db=None):
    if not title.strip():
        raise TicketError("a ticket needs a title")
    with _db(db) as c:
        now = time.time()
        tid = c.execute("INSERT INTO tickets (title, body, status, reporter, created, updated) VALUES (?,?,?,?,?,?)",
                        (title.strip(), body, "open", audit.identity(), now, now)).lastrowid
        _hist(c, tid, "filed")
    audit.record("tickets", "file", "ok", ticket=key(tid))
    return {"key": key(tid), "title": title.strip(), "status": "open"}


def get(k, db=None):
    tid = _id(k)
    with _db(db) as c:
        t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if not t:
            raise TicketError(f"{key(tid)} does not exist")
        cs = [dict(r) for r in c.execute("SELECT who, body, ts FROM comments WHERE ticket=? ORDER BY id", (tid,))]
        hs = [dict(r) for r in c.execute("SELECT who, change, ts FROM history WHERE ticket=? ORDER BY id", (tid,))]
    return {**dict(t), "key": key(tid), "comments": cs, "history": hs}


def list_(status=None, db=None):
    with _db(db) as c:
        q, a = "SELECT id, title, status, updated FROM tickets", ()
        if status:
            q, a = q + " WHERE status=?", (status,)
        rows = c.execute(q + " ORDER BY updated DESC LIMIT 100", a).fetchall()
    return [{"key": key(r["id"]), "title": r["title"], "status": r["status"]} for r in rows]


def comment(k, body, db=None):
    tid = _id(k)
    get(k, db)
    if not body.strip():
        raise TicketError("empty comment")
    with _db(db) as c:
        c.execute("INSERT INTO comments (ticket, who, body, ts) VALUES (?,?,?,?)", (tid, audit.identity(), body, time.time()))
        c.execute("UPDATE tickets SET updated=? WHERE id=?", (time.time(), tid))
        _hist(c, tid, "comment")
    audit.record("tickets", "comment", "ok", ticket=key(tid))
    return {"key": key(tid), "comments": len(get(k, db)["comments"])}


def set_status(k, status, resolution="", db=None):
    if status not in STATUSES:
        raise TicketError(f"status must be one of {STATUSES}")
    if status in ("resolved", "wontfix") and not resolution.strip():
        raise TicketError("closing a ticket needs a resolution")
    tid = _id(k)
    old = get(k, db)["status"]
    with _db(db) as c:
        c.execute("UPDATE tickets SET status=?, updated=? WHERE id=?", (status, time.time(), tid))
        _hist(c, tid, f"status {old} -> {status}" + (f": {resolution}" if resolution else ""))
    audit.record("tickets", "status", "ok", ticket=key(tid), to=status)
    return {"key": key(tid), "status": status}
