"""Tests for head_lane (TIK-0361). Run: python3 tests/test_head_lane.py"""
import _harness_env  # noqa: F401  throwaway config + state (must be first)
import _tmpguard  # noqa: F401  TIK-0707: all temp dirs of this suite land under ONE per-run root removed at exit (must stay the first import)
import os
import sys
import json
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
import tgapi as pl  # noqa: E402
import head_lane as hl   # noqa: E402

# TIK-1436: fake "STOP" / "buy oat milk" prompts from this suite's mutant runs reached the LIVE mail head for days (a path
# helper bug left SOCK live). Whatever a future bug or ship_check mutant does, this suite can never CONNECT to a real head
# socket: a unix connect under <state_dir> is refused here, at the one place every path ends up.
import socket as _socket  # noqa: E402
_LIVE_HEADS = os.path.expanduser(os.environ.get("HARNESS_LIVE_STATE", "~/.local/state/harness")) + os.sep   # a real install: never touched
_real_connect = _socket.socket.connect
def _guarded_connect(self, addr):   # noqa: E302
    if isinstance(addr, (str, bytes)) and os.fsdecode(addr).startswith(_LIVE_HEADS):
        raise PermissionError("TIK-1436: the test suite must not connect to a live head socket: %r" % (addr,))
    return _real_connect(self, addr)
_socket.socket.connect = _guarded_connect
_TMP = tempfile.mkdtemp(prefix="headlane_test_")
pl.LOG_PATH = os.path.join(_TMP, "tg_print.log")
# every lane state file a send touches points at the temp dir, for EVERY test class (never the live ones)
hl.REACTIONS, hl.REACTIONS_OFF = os.path.join(_TMP, "reactions.jsonl"), os.path.join(_TMP, "reactions.off")
hl.UNDELIVERED, hl.MIDTURN = os.path.join(_TMP, "undelivered.json"), os.path.join(_TMP, "midturn.jsonl")
hl.TURNS, hl.OPEN_THOUGHTS = os.path.join(_TMP, "turns.json"), os.path.join(_TMP, "thoughts_open.json")
# TIK-1026: /stop tests wrote LaneStop into the LIVE events.jsonl ~1/s during every ship_check, and each one
# ended Rav's real turn as "Stopped" (16:00 plant video, 16:09 "look at tik-1026"). The event log is temp too.
_LIVE_EVENTS = hl.EVENTS
hl.EVENTS, hl.LIVE_SPOOL = os.path.join(_TMP, "events.jsonl"), os.path.join(_TMP, "live_spool")
hl.EDITS = os.path.join(_TMP, "edits.json")          # every turn test reads + clears it: never the live one
hl.MINIAPP_SOCK = os.path.join(_TMP, "no-miniapp.sock")   # a test can never speak on Rav's open Live page
hl.INFLIGHT = os.path.join(_TMP, "inflight.json")         # a test turn must never look in flight to the real gateway
hl.BUTTONS = os.path.join(_TMP, "buttons.json")
hl.RETRIES = os.path.join(_TMP, "retry_buttons.json")
# ...and the lab chat history (chat_ledger) is a temp DB with the real schema: answers are rows there now.
import sqlite3  # noqa: E402
import chat_ledger as cl  # noqa: E402
_LIVE_CHATDB = cl.CHATDB
cl.CHATDB = os.path.join(_TMP, "chat_history.db")
with sqlite3.connect(cl.CHATDB) as _c:
    _c.execute("CREATE TABLE chat_log (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, source TEXT NOT NULL,"
               " mode TEXT, role TEXT NOT NULL, content TEXT NOT NULL, model TEXT, ts REAL, thread_id TEXT, extra TEXT,"
               " user_id TEXT)")
SENT = []
pl._reply = lambda tok, m, text: SENT.append(text)

def _mail_lane(name="mail"):
    """The real "mail" lane module, every state path pointed at a temp dir (never the live <state_dir>/mail)."""
    ms = hl._load_lane(name)
    d = tempfile.mkdtemp(prefix=name + "_lane_")
    live = ms.STATE_DIR                         # TIK-1436: fixed first; the loop rewrites STATE_DIR itself, and comparing
    for k, v in list(vars(ms).items()):         # against the new value left SOCK on the live head (fake prompts reached it)
        if isinstance(v, str) and v.startswith(live):
            setattr(ms, k, d + v[len(live):])
    assert not ms.SOCK.startswith(live), ms.SOCK
    for k in ("REACTIONS", "REACTIONS_OFF", "UNDELIVERED", "MIDTURN", "TURNS", "OPEN_THOUGHTS", "EVENTS", "LIVE_SPOOL",
              "EDITS", "INFLIGHT", "BUTTONS"):
        setattr(ms, k, getattr(hl, k))
    ms.MINIAPP_SOCK = hl.MINIAPP_SOCK
    return ms

API = []
pl._api = lambda tok, method, **kw: API.append((method, kw)) or {"ok": True}   # no real Telegram in tests
import contextlib  # noqa: E402
OWNER, USER_B, USER_C = 111111111, 222222222, 333333333

# The real pane from the first bridge test (2026-10-24 00:20).
PANE = """ ▐▛███▛█   Claude Code v2.1.310
▝▜██████▀  Opus 5.5 · Claude Max
❯ Test from the laptop thread: in one short line, say who you are, which machine you are on, and run tickets get
  TIK-0361 to tell me its title.
  Called hub
● I'm Atlas, running as the main head (Claude Code in tmux on the home server), and TIK-0361 is titled "EPIC: headless
  head — Claude Code REPL + multi-provider gate + fal media pipelines + voice/Telegram I/O".
✻ Crunched for 9s · done 12:20 AM
                                                                                                    ◐ medium · /effort
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ⏸ manual mode on · ? for shortcuts""".splitlines()


class Extract(unittest.TestCase):
    def test_real_pane(self):
        r = hl.extract_reply(PANE)
        self.assertTrue(r.startswith("I'm Atlas, running as the main head"))
        self.assertIn("voice/Telegram I/O", r)
        self.assertNotIn("Called hub", r)
        self.assertNotIn("Crunched", r)
        self.assertNotIn("TIK-0361 to tell me its title", r)    # the question's wrapped line is not the answer

    def test_only_the_last_turn(self):
        older = ["❯ first question", "● first answer", "✻ Done", "────────────────"]
        newer = ["❯ second question", "● second answer", "  more of it", "✻ Done"]
        r = hl.extract_reply(older + newer)
        self.assertEqual(r, "second answer\nmore of it")

    def test_tool_blocks_skipped(self):
        lines = ["❯ do it", "● Bash(ls -la)", "  ⎿  file1", "● hub - tickets (MCP)(action: \"get\")",
                 "  ⎿  {...}", "● Done: 3 files.", "✻ Brewed"]
        self.assertEqual(hl.extract_reply(lines), "Done: 3 files.")

    def test_several_text_blocks_kept(self):
        lines = ["❯ q", "● part one", "● Bash(x)", "  ⎿  out", "● part two", "✻ done"]
        self.assertEqual(hl.extract_reply(lines), "part one\n\npart two")

    def test_stops_at_the_end_marker(self):
        self.assertEqual(hl.extract_reply(["❯ q", "● the answer", "✻ Done", "● stray text after"]), "the answer")
        self.assertEqual(hl.extract_reply(["❯ q", "● the answer", "────────────────", "● footer"]), "the answer")

    def test_nothing(self):
        self.assertEqual(hl.extract_reply(["❯ ", "nothing"]), "")

    def test_chunks_and_flatten(self):
        self.assertEqual(len(hl.chunks("x" * 9000)), 3)
        self.assertEqual(hl.flatten("a\n\n b\nc"), "a / b / c")


class Gate(unittest.TestCase):
    def setUp(self):
        SENT.clear()
        hl.HEAD_THREADS[OWNER] = 777
        hl._q.clear()
        hl._state["worker"] = object()     # do not start a real worker in tests

    def test_slash_j_in_own_topic_is_plain(self):
        """2026-10-30: "/j x" typed in a head's own topic reached the REPL raw ("Unknown command: /j")."""
        self.assertTrue(hl.handle(self.msg(text="/j why no extrusion?"), "t"))
        self.assertEqual(hl._q[0][1].count("/j"), 0)
        self.assertIn("why no extrusion?", hl._q[0][1])

    def test_slash_j_caption_in_own_topic_is_plain(self):
        """2026-11-04: a photo captioned "/j x" hit the REPL raw: no turn, and the lane waited on a Stop that never came."""
        m = dict(self.msg(text=None), caption="/j looks mirrored?", photo=[{"file_id": "f"}])
        got = []
        self.assertTrue(hl.handle(m, "t", prep=lambda tok, mm: got.append(mm)))   # media is prepared in a thread
        self.assertEqual(got[0]["caption"], "looks mirrored?")

    def msg(self, uid=OWNER, tid=777, text="hi"):
        m = {"from": {"id": uid}, "chat": {"id": uid, "type": "private"}, "message_id": 1, "message_thread_id": tid}
        if text is not None:
            m["text"] = text
        return m

    def test_head_is_rav_only(self):
        self.assertEqual(set(hl.HEAD_UIDS), {OWNER})

    def test_rav_queued(self):
        API.clear()
        self.assertTrue(hl.handle(self.msg(), "t"))
        self.assertEqual(len(hl._q), 1)
        self.assertIn(("setMessageReaction", {"chat_id": OWNER, "message_id": 1,
                       "reaction": [{"type": "emoji", "emoji": "👀"}]}), API)

    def test_others_never_reach_the_head(self):
        for u in (USER_B, USER_C, 999):
            self.assertTrue(hl.handle(self.msg(uid=u), "t"))
        self.assertEqual(len(hl._q), 0)

    def test_other_threads_fall_through(self):
        self.assertFalse(hl.handle(self.msg(tid=123), "t"))
        self.assertFalse(hl.handle(self.msg(tid=None), "t"))
        self.assertEqual(len(hl._q), 0)

    def test_unsupported_refused(self):
        m = self.msg(text=None); m["sticker"] = {"file_id": "x"}
        self.assertTrue(hl.handle(m, "t"))
        self.assertEqual(len(hl._q), 0)
        self.assertTrue(any("isn't supported" in s for s in SENT))

    def test_forwarded_rich_message_is_read(self):
        """2026-10-31: a forwarded agent answer (rich_message, no "text") got "That kind of message isn't supported"."""
        SENT.clear()
        m = self.msg(text=None)
        m["forward_origin"] = {"type": "user", "sender_user": {"first_name": "Atlas", "is_bot": True}, "date": 1793000000}
        m["rich_message"] = {"blocks": [{"type": "paragraph", "text": [{"type": "bold", "text": "Servos:"}, " Inland 9G"]},
                                        {"type": "list", "items": [{"text": ["608 bearing"]}, {"text": ["5 V 3 A supply"]}]}]}
        self.assertTrue(hl.handle(m, "t"))
        self.assertEqual(len(hl._q), 1)
        self.assertIn("Servos: Inland 9G\n608 bearing\n5 V 3 A supply", hl._q[0][1])
        self.assertFalse(any("isn't supported" in x for x in SENT))

    def test_empty_rich_message_is_still_refused(self):
        SENT.clear()
        m = self.msg(text=None); m["rich_message"] = {"blocks": []}
        self.assertTrue(hl.handle(m, "t"))
        self.assertEqual(len(hl._q), 0)
        self.assertTrue(any("isn't supported" in x for x in SENT))

    def test_lead_in_rides_with_the_forward(self):
        hl._lead.clear()
        self.assertTrue(hl.handle(self.msg(text="FYI"), "t"))
        self.assertEqual(len(hl._q), 0)                     # held, not answered alone
        fw = self.msg(text="WALL ERROR: x"); fw["message_id"] = 2
        fw["forward_origin"] = {"type": "user", "sender_user": {"first_name": "AtlasBot"}, "date": 1793000000}
        hl.handle(fw, "t")
        self.assertEqual(len(hl._q), 1)
        m, text = hl._q[0]
        self.assertEqual(m["message_id"], 2)
        self.assertIn("“FYI”", text)
        self.assertIn("WALL ERROR: x", text)

    def test_lead_in_alone_still_arrives(self):
        hl._lead.clear()
        old = hl.LEAD_HOLD_S
        hl.LEAD_HOLD_S = 0.05
        try:
            hl.handle(self.msg(text="take a look at this"), "t")
            import time; time.sleep(0.3)
        finally:
            hl.LEAD_HOLD_S = old
        self.assertEqual([t for _, t in hl._q], ["take a look at this"])

    def test_ordinary_short_text_is_not_held(self):
        hl._lead.clear()
        for t in ("Yes", "Do it", "This is great", "What is this?"):
            self.assertFalse(hl.is_lead_in(t), t)
        hl.handle(self.msg(text="Yes"), "t")
        self.assertEqual(len(hl._q), 1)

    def test_second_thread_has_its_own_head(self):
        """TIK-1082: a message in TechSup lands in the techsup lane's queue, never in Atlas's."""
        ts = hl._load_lane("techsup_test")
        self.assertTrue(ts.STATE_DIR.endswith("/techsup_test"))
        self.assertTrue(ts.SOCK.startswith(ts.STATE_DIR))
        self.assertNotEqual(ts.SOCK, hl.SOCK)
        ts.HEAD_THREADS[OWNER] = 888
        ts._state["worker"] = object()
        hl.OTHERS.append(ts)
        try:
            self.assertTrue(hl.handle(self.msg(tid=888, text="work TIK-1"), "t"))
            self.assertTrue(hl.handle(self.msg(tid=777, text="hello hb"), "t"))
            self.assertEqual([t for _, t in ts._q], ["work TIK-1"])
            self.assertEqual([t for _, t in hl._q], ["hello hb"])
            self.assertFalse(hl.handle(self.msg(tid=999), "t"))        # an unknown topic is still not ours
        finally:
            hl.OTHERS.remove(ts)

    def test_long_prompt_spills_to_a_file(self):
        """2026-10-30: a paste past the bridge's 8000 cap is saved and the head is pointed at it."""
        d = tempfile.mkdtemp()
        self.assertEqual(hl.fit("short", d), "short")
        big = "x" * 9000 + "END"
        out = hl.fit(big, d)
        self.assertLess(len(out), 8000)
        path = out.rsplit("saved at ", 1)[1].split(" ")[0]
        self.assertEqual(open(path).read(), big)

    def test_relay_media_fetched_with_the_lane_token(self):
        """2026-10-30: a photo captioned "/j ..." in Shop reached the head as text only (or not at all)."""
        got = []
        job = {"origin": "crm", "text": "Sam says this", "media": {"kind": "photo", "file_id": "F", "file_name": ""}}
        out = hl.relay_media(job, download=lambda tok, fid, dest: got.append((tok, fid, dest)), token=lambda o: "TOK-" + o)
        self.assertEqual(got[0][:2], ("TOK-crm", "F"))
        self.assertIn("saved at " + got[0][2], out)
        self.assertTrue(out.startswith("Sam says this"))
        self.assertIn("through CRM", out)
        def boom(*a):
            raise OSError("410 gone")
        out = hl.relay_media(job, download=boom, token=lambda o: "T")
        self.assertIn("couldn't fetch it", out)
        self.assertTrue(out.startswith("Sam says this"))

    def test_notes_lane_keeps_plain_text(self):
        """2026-10-30: the Notes topic saves plain text as a note; only "/j ..." reaches its head. JMail is like Email."""
        self.assertIn("jmail", hl.J_ONLY_LANES)
        self.assertIn("notes", hl.J_ONLY_LANES)
        self.assertIsNone(hl.lane_body("buy milk", "notes"))
        self.assertEqual(hl.lane_body("/j what did I note?", "notes"), "what did I note?")
        self.assertIsNone(hl.lane_body(None, "notes"))
        old = hl.EMAIL_SESSION
        hl.EMAIL_SESSION = os.path.join(tempfile.mkdtemp(), "none.json")
        try:
            self.assertEqual(hl.lane_body("hello", "jmail"), "hello")
        finally:
            hl.EMAIL_SESSION = old
        ns = _mail_lane("notes")
        ns.HEAD_THREADS[OWNER] = 556
        edit = lambda t: {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 7, "message_thread_id": 556, "text": t}
        self.assertIsNone(ns.on_edit(edit("buy oat milk")))             # an edited note stays a note
        self.assertIsNotNone(ns.on_edit(edit("/j what's on the list?")))

    def test_j_only_lane_takes_only_slash_j(self):
        """TIK-1131: the mail head shares Rav's Email topic with the email bot: only "/j ..." is the head's."""
        self.assertEqual(hl.j_body("/j check my inbox"), "check my inbox")
        self.assertEqual(hl.j_body("/J@AtlasBot  hi"), "hi")
        self.assertEqual(hl.j_body("/j"), "")
        for t in ("/jinbox", "/jmail x", "/jlist", "hello /j x", None, "/inbox"):
            self.assertIsNone(hl.j_body(t), t)
        free = lambda: False
        self.assertEqual(hl.shared_body("/j hi", free), "hi")
        self.assertEqual(hl.shared_body(" hello ", free), "hello")
        for t in ("/inbox", "/jmail x", "  ", None):
            self.assertIsNone(hl.shared_body(t, free), t)
        self.assertIsNone(hl.shared_body("hello", lambda: True))         # email bot awaiting a body
        self.assertEqual(hl.shared_body("/j hi", lambda: True), "hi")     # /j still reaches the head
        import tempfile as _tf2
        d = _tf2.mkdtemp()
        self.assertFalse(hl.email_awaiting(os.path.join(d, "none.json")))
        open(os.path.join(d, "bad.json"), "w").write("{")
        self.assertTrue(hl.email_awaiting(os.path.join(d, "bad.json")))
        ms = _mail_lane()
        self.assertTrue(ms.J_ONLY)
        self.assertFalse(hl.J_ONLY)                                   # Atlas still owns its whole topic
        ms.HEAD_THREADS[OWNER] = 555
        ms._state["worker"] = object()
        import tempfile as _tf
        sess = os.path.join(_tf.mkdtemp(), "session.json")
        ms.EMAIL_SESSION = sess
        hl.OTHERS.append(ms)
        try:
            self.assertFalse(hl.handle(self.msg(tid=555, text="/inbox"), "t"))      # the email bot keeps these
            self.assertFalse(hl.handle(self.msg(tid=555, text="/jinbox"), "t"))
            self.assertFalse(hl.handle(self.msg(tid=555, text="/email bob@example.com hi"), "t"))
            ms._jsave(sess, {"awaiting": {"kind": "reply", "uid": "7"}})               # mid Reply flow: the body is the bot's
            self.assertFalse(hl.handle(self.msg(tid=555, text="Thanks, see you Friday"), "t"))
            self.assertEqual(len(ms._q), 0)
            ms._jsave(sess, {"awaiting": None})
            self.assertTrue(hl.handle(self.msg(tid=555, text="/j summarise today's mail"), "t"))
            self.assertTrue(hl.handle(self.msg(tid=555, text="archive the lot"), "t"))   # plain text: no /j needed
            self.assertEqual([t for _, t in ms._q], ["summarise today's mail", "archive the lot"])
            self.assertEqual(ms._q[0][0]["message_thread_id"], 555)              # the reply goes back to that topic
            self.assertTrue(hl.handle(self.msg(uid=USER_B, tid=555, text="/j hi"), "t"))   # refused, not queued
            self.assertEqual(len(ms._q), 2)
            self.assertIsNone(ms.on_edit(self.msg(tid=555, text="/email edited")))
        finally:
            hl.OTHERS.remove(ms)
            ms._q.clear()

    def test_live_head_socket_cannot_be_connected(self):
        """TIK-1436: independent of any path helper, a connect to a live head socket is refused."""
        s = _socket.socket(_socket.AF_UNIX)
        try:
            with self.assertRaises(PermissionError):
                s.connect(os.path.join(_LIVE_HEADS, "mail", "head.sock"))
        finally:
            s.close()

    def test_mail_lane_helper_leaves_no_live_path(self):
        """TIK-1436: this suite's mail lane must have no path under the real <state_dir>."""
        base = _LIVE_HEADS.rstrip(os.sep)
        for name in ("mail", "notes"):
            ms = _mail_lane(name)
            bad = {k: v for k, v in vars(ms).items() if isinstance(v, str) and v.startswith(base + "/") and k != "LANES_FILE"}
            self.assertEqual(bad, {})

    def test_j_only_lane_stop(self):
        ms = _mail_lane()
        ms.HEAD_THREADS[OWNER] = 556
        calls = []
        ms.bridge = lambda req, wait=5: calls.append(req) or {"state": "busy", "ok": True}
        self.assertTrue(ms.handle(self.msg(tid=556, text="/j STOP"), "t"))
        self.assertIn({"t": "key", "key": "Escape"}, calls)
        self.assertEqual(len(ms._q), 0)

    def test_task_handoff_posts_and_queues_in_the_target_thread(self):
        import tempfile as _tf
        ts = hl._load_lane("techsup_test2")
        ts.HEAD_THREADS[OWNER] = 888
        ts._state["worker"] = object()
        d = _tf.mkdtemp()
        f = os.path.join(d, "1.json")
        json.dump({"from": "main", "text": "Diagnose the wall error"}, open(f, "w"))
        API.clear()
        self.assertTrue(ts.take_task("t", f))
        self.assertFalse(os.path.exists(f))                       # consumed
        (m, text), = ts._q
        self.assertEqual(m["message_thread_id"], 888)
        self.assertIn("Diagnose the wall error", text)
        self.assertIn("Atlas", text)
        posts = [kw for meth, kw in API if meth == "sendMessage"]
        self.assertTrue(posts and posts[-1]["message_thread_id"] == 888 and "📋 Task from Atlas" in posts[-1]["text"])

    def test_task_page_to_a_bot_lane_goes_to_its_own_bot_chat(self):
        """TIK-1135: /j bot lanes have no thread in the main bot; the page goes to Rav's chat with that lane's bot."""
        import tempfile as _tf
        bl = hl._load_lane("crm")
        bl.HEAD_THREADS.pop(OWNER, None)
        bl._state["worker"] = object()
        real = bl.jrelay.token_for
        bl.jrelay.token_for = lambda o: "bottok-" + o
        try:
            d = _tf.mkdtemp(); f = os.path.join(d, "1.json")
            json.dump({"from": "techsup", "text": "Stand down for the build box"}, open(f, "w"))
            API.clear()
            self.assertTrue(bl.take_task("tok", f))
            self.assertFalse(os.path.exists(f))
            (m, text), = bl._q
            self.assertEqual(m["_relay"], {"origin": "crm"})                  # reply goes back through the product bot
            self.assertNotIn("message_thread_id", m)
            self.assertIn("Stand down for the build box", text)
            posts = [kw for meth, kw in API if meth == "sendMessage"]
            self.assertTrue(posts and "message_thread_id" not in posts[-1] and posts[-1]["chat_id"] == OWNER)
            # a lane that is neither a thread nor a bot origin is still refused, and the file is consumed
            nl = hl._load_lane("nobody_test")
            nl.HEAD_THREADS.pop(OWNER, None)
            f2 = os.path.join(d, "2.json"); json.dump({"from": "hb", "text": "x"}, open(f2, "w"))
            self.assertFalse(nl.take_task("t", f2))
            self.assertFalse(os.path.exists(f2))
        finally:
            bl.jrelay.token_for = real
            bl._q.clear()

    def test_photo_prepared_off_the_queue(self):
        m = self.msg(text=None); m["photo"] = [{"file_id": "s"}, {"file_id": "big"}]
        started = []
        self.assertTrue(hl.handle(m, "t", prep=lambda tok, mm: started.append(mm)))
        self.assertEqual(started, [m])
        self.assertEqual(len(hl._q), 0)                     # nothing blocks the queue while it is processed

    def test_commands_run_now_not_queued(self):
        calls = []
        real = hl.bridge
        hl.bridge = lambda req, wait=5: calls.append(req) or {"state": "busy", "ok": True}
        try:
            self.assertTrue(hl.handle(self.msg(text="/stop"), "t"))
            self.assertIn({"t": "key", "key": "Escape"}, calls)
            hl.handle(self.msg(text="/new"), "t")
            self.assertIn({"t": "send", "text": "/clear"}, calls)
            hl.EVENTS = os.path.join(_TMP, "status_events.jsonl")        # busy/idle comes from events now
            with open(hl.EVENTS, "w") as f:
                f.write(json.dumps({"event": "UserPromptSubmit", "data": {}}) + "\n")
            hl.handle(self.msg(text="/status"), "t")
            self.assertTrue(any(s.startswith("State: busy") for s in SENT))
        finally:
            hl.bridge = real
        self.assertEqual(len(hl._q), 0)

    def test_others_cannot_use_commands(self):
        calls = []
        real = hl.bridge
        hl.bridge = lambda req, wait=5: calls.append(req) or {"state": "busy", "ok": True}
        try:
            hl.handle(self.msg(uid=USER_B, text="/stop"), "t")
        finally:
            hl.bridge = real
        self.assertEqual(calls, [])


class FakeIO:
    def __init__(self):
        self.log, self.tools = [], []
        self.span = None

    def react(self, st): self.log.append(("react", st))
    def typing(self): return contextlib.nullcontext()
    def step(self, line): self.tools.append(line); self.log.append(("step", line))
    def finish(self): self.log.append(("finish",))
    def perm_card(self, pid, tool, inp): self.log.append(("perm", pid, tool))
    def legacy_card(self, lines): self.log.append(("legacy",))
    def say(self, t): self.log.append(("say", t))
    def answer(self, md): self.log.append(("answer", md)); return [100]
    def remember(self, ids): self.log.append(("remember", ids, dict(self.span or {})))
    def send_file(self, p): self.log.append(("file", p))
    def voice(self, t): self.log.append(("voice", t))

    def kinds(self, k): return [x for x in self.log if x[0] == k]


def _pane(x):
    """PERM_PANE is a list in one class and a str in another; the bridge wants lines."""
    return x.splitlines() if isinstance(x, str) else list(x)


def ev(event, **data):
    return {"event": event, "data": data}


class Clock:
    def __init__(self): self.t = 0.0
    def now(self): self.t += 1; return self.t
    def sleep(self, s): self.t += s


REAL_PROMPT = {"login_required": ["", "  Please run /login to continue", ""],
               "trust_prompt": ["  Do you trust the files in this folder?", " ❯ 1. Yes, proceed"]}
# the screen at 05:05 on 2026-10-24: the head reading its OWN bridge tests, then a finished turn below them
SCROLLBACK_LOGIN = ['63      assert c(["Please run /login", "? for shortcuts"]) == "login_required"',
                    "● The live screen still classifies as busy", "✻ Sautéed for 2m 8s · done 5:06 AM",
                    "────────", "❯ ", "────────", "  ⏵⏵ auto mode on (shift+tab to cycle)"]


class Gates(unittest.TestCase):
    def test_gate_of(self):
        self.assertEqual(hl.gate_of({"state": "dead"}), "dead")
        self.assertEqual(hl.gate_of({"state": "login_required", "tail": REAL_PROMPT["login_required"]}), "login_required")
        self.assertIsNone(hl.gate_of({"state": "login_required", "tail": SCROLLBACK_LOGIN}))
        self.assertIsNone(hl.gate_of({"state": "login_required"}))
        self.assertEqual(hl.gate_of({"state": "trust_prompt", "tail": REAL_PROMPT["trust_prompt"]}), "trust_prompt")
        self.assertIsNone(hl.gate_of({"state": "idle", "tail": ["Please run /login"]}))


class Turn(unittest.TestCase):
    def setUp(self):
        hl._state["waiting_permission"] = False
        hl._state.pop("ev_off", None)
        self.tmp = tempfile.mkdtemp(prefix="hl_turn_")
        hl.REMOTE = os.path.join(self.tmp, "remote.json")
        hl.EVENTS = os.path.join(self.tmp, "events.jsonl")
        hl.REACTIONS, hl.REACTIONS_OFF = os.path.join(self.tmp, "r.jsonl"), os.path.join(self.tmp, "r.off")
        hl.UNDELIVERED = os.path.join(self.tmp, "undelivered.json")       # one test's failure never prefixes another's

    def bridge(self, states=("idle",), pane=(), tail=None):
        calls, it = [], iter(states)
        def call(req, wait=5):
            calls.append(req)
            if req["t"] == "status":
                st = next(it, states[-1])
                return {"state": st, "tail": list(tail) if tail is not None else REAL_PROMPT.get(st, ["❯ "])}
            if req["t"] == "tail":
                return {"lines": list(pane)}
            return {"ok": True}
        return call, calls

    def feed(self, *batches):
        it = iter(batches)
        return lambda off: (next(it, []), off + 1)

    def run_turn(self, text, events, states=("idle",), pane=(), io=None, tail=None):
        io = io or FakeIO()
        call, calls = self.bridge(states, pane, tail)
        c = Clock()
        hl._turn("t", {"chat": {"id": OWNER, "type": "private"}, "message_id": 3, "message_thread_id": 777}, text,
                 call=call, io=io, read=self.feed(*events), sleep=c.sleep, now=c.now)
        return io, calls

    def test_pending_reaction_rides_in_front_of_the_next_prompt(self):
        hl.TURNS = os.path.join(self.tmp, "turns.json")
        hl.remember_turn([501], {"path": "/t", "start": 0, "end": 5, "md": "the stage answer"})
        hl.on_reaction({"message_id": 501, "user": {"id": OWNER}, "old_reaction": [],
                        "new_reaction": [{"type": "emoji", "emoji": "👍"}]})
        io, calls = self.run_turn("next thing", [[ev("Stop", last_assistant_message="ok")]])
        sent = [c["text"] for c in calls if c["t"] == "send"][0]
        self.assertTrue(sent.startswith("[Feedback since your last turn: alex reacted 👍"), sent)
        self.assertTrue(sent.endswith("next thing"))
        io, calls = self.run_turn("again", [[ev("Stop", last_assistant_message="ok")]])
        self.assertIn({"t": "send", "text": "again"}, calls)

    def test_edit_after_the_answer_rides_in_front_of_the_next_prompt(self):
        hl.EDITS = os.path.join(self.tmp, "edits.json")
        self.addCleanup(setattr, hl, "EDITS", os.path.join(_TMP, "edits.json"))
        hl.HEAD_THREADS[OWNER] = 777
        self.assertEqual(hl.on_edit({"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 9,
                                     "message_thread_id": 777, "text": "welp"}), "noted")
        io, calls = self.run_turn("next", [[ev("Stop", last_assistant_message="ok")]])
        sent = [c["text"] for c in calls if c["t"] == "send"][0]
        self.assertIn('[alex edited message #9: "?" -> "welp"]', sent)
        self.assertTrue(sent.endswith("next"))

    def test_turn_in_flight_is_written_then_crossed_off(self):
        seen = []
        old = hl.follow
        def follow(off, io, **k):
            seen.append(json.load(open(hl.INFLIGHT)))            # on file while the head works
            return old(off, io, **k)
        hl.follow = follow
        self.addCleanup(setattr, hl, "follow", old)
        self.run_turn("q", [[ev("Stop", last_assistant_message="ok")]])
        self.assertEqual(seen[0]["m"]["message_id"], 3)
        self.assertFalse(os.path.exists(hl.INFLIGHT))            # delivered -> crossed off

    def test_restart_mid_turn_delivers_the_answer(self):
        """18:44: a gateway restart mid-turn lost the head's answer."""
        hl.save_inflight({"chat": {"id": OWNER, "type": "private"}, "message_id": 3, "message_thread_id": 777, "_from_voice": True}, 40)
        got = []
        self.assertEqual(hl.recover_inflight("t", enqueue=lambda tok, m: got.append(m)), "recovered")
        self.assertEqual((got[0]["message_id"], hl._state["ev_off"]), (3, 40))
        io = FakeIO()
        call, _ = self.bridge(("idle",))
        c = Clock()
        hl._turn("t", got[0], None, call=call, io=io, read=self.feed([ev("Stop", last_assistant_message="the lost answer")]),
                 sleep=c.sleep, now=c.now)
        self.assertEqual(io.kinds("answer"), [("answer", "the lost answer")])
        self.assertTrue(io.kinds("voice"))                       # voice-in turns still answer by voice
        self.assertFalse(os.path.exists(hl.INFLIGHT))

    def test_old_inflight_is_dropped_not_replayed(self):
        hl.save_inflight({"chat": {"id": OWNER, "type": "private"}, "message_id": 4}, 10, now=lambda: 0)
        got = []
        self.assertEqual(hl.recover_inflight("t", now=lambda: hl.TURN_TIMEOUT_S + 5, enqueue=lambda *a: got.append(a)), "stale")
        self.assertEqual(got, []); self.assertFalse(os.path.exists(hl.INFLIGHT))
        self.assertIsNone(hl.recover_inflight("t", enqueue=lambda *a: got.append(a)))

    def test_reply_comes_from_the_stop_event_not_the_screen(self):
        io, calls = self.run_turn("hello\nthere", [[ev("UserPromptSubmit")],
                                                   [ev("PreToolUse", tool_name="Bash", tool_input={"command": "uptime"})],
                                                   [ev("Stop", last_assistant_message="**up** 3 days")]])
        self.assertIn({"t": "send", "text": "hello / there"}, calls)
        self.assertEqual(io.kinds("answer"), [("answer", "**up** 3 days")])
        self.assertEqual(io.tools, ["⚙️ uptime"])
        self.assertEqual([x[1] for x in io.kinds("react")], ["working", "done"])
        self.assertTrue(io.kinds("finish"))
        self.assertFalse(os.path.exists(hl.REMOTE))         # the remote flag is cleared after the turn
        self.assertFalse(hl._state["running"])               # or /model would refuse forever

    def test_remote_flag_set_while_the_turn_runs(self):
        seen = []
        def read(off):
            seen.append(os.path.exists(hl.REMOTE))
            return ([ev("Stop", last_assistant_message="ok")] if len(seen) > 1 else []), off
        call, _ = self.bridge()
        c = Clock()
        hl._turn("t", {"chat": {"id": OWNER, "type": "private"}, "message_id": 3}, "hi", call=call, io=FakeIO(), read=read,
                 sleep=c.sleep, now=c.now)
        self.assertTrue(seen[0])

    def test_hook_permission_becomes_a_card_and_the_turn_keeps_following(self):
        io, _ = self.run_turn("rm it", [[{"event": "PermissionRequest", "perm_id": "abc123abc123",
                                          "data": {"tool_name": "Bash", "tool_input": {"command": "rm x"}}}],
                                        [{"event": "PermissionAnswered", "perm_id": "abc123abc123"}],
                                        [ev("Stop", last_assistant_message="removed")]],
                              states=("idle", "permission_prompt"))
        self.assertEqual(io.kinds("perm"), [("perm", "abc123abc123", "Bash")])
        self.assertFalse(io.kinds("legacy"))                 # a parked request is NOT also a terminal prompt
        self.assertEqual(io.kinds("answer"), [("answer", "removed")])

    def test_unparked_terminal_prompt_falls_back_to_the_pane_card(self):
        io, _ = self.run_turn("x", [[]] * 20, states=("idle", "permission_prompt"), pane=PERM_PANE)
        self.assertTrue(io.kinds("legacy"))
        self.assertTrue(hl._state["waiting_permission"])

    def test_typed_yes_answers_a_terminal_prompt_with_a_key(self):
        hl._state["waiting_permission"] = True
        io, calls = self.run_turn("yes", [[ev("Stop", last_assistant_message="done")]],
                                  states=("permission_prompt", "busy"), pane=_pane(PERM_PANE))
        self.assertIn({"t": "key", "key": "1"}, calls)
        self.assertFalse(any(c["t"] == "send" for c in calls))
        self.assertFalse(hl._state["waiting_permission"])

    def test_typed_yes_with_words_answers_and_keeps_the_words(self):
        """alex 2026-11-05: '/j Yes, and also that button doesn't seem to work' was refused as not an answer."""
        hl._state["waiting_permission"] = True
        notes = []
        orig = hl.note_midturn
        hl.note_midturn = lambda t, **k: notes.append(t) or True
        try:
            _, calls = self.run_turn("/j Yes, and also that button doesn't seem to work.",
                                     [[ev("Stop", last_assistant_message="ok")]], states=("permission_prompt", "busy"),
                                     pane=_pane(PERM_PANE))
        finally:
            hl.note_midturn = orig
        self.assertIn({"t": "key", "key": "1"}, calls)
        self.assertFalse(any(c["t"] == "send" for c in calls))
        self.assertTrue(notes and "button doesn't seem to work" in notes[0])

    def test_perm_answer_reads_the_first_word_only(self):
        self.assertEqual(hl.perm_answer("No - wrong folder"), ("Escape", "wrong folder"))
        self.assertEqual(hl.perm_answer("yes"), ("1", None))
        self.assertEqual(hl.perm_answer("yesterday was fine"), (None, None))
        self.assertEqual(hl.perm_answer("why?"), (None, None))

    def test_no_means_escape(self):
        hl._state["waiting_permission"] = True
        _, calls = self.run_turn("no", [[ev("Stop", last_assistant_message="ok")]], states=("permission_prompt", "busy"),
                                 pane=_pane(PERM_PANE))
        self.assertIn({"t": "key", "key": "Escape"}, calls)

    def test_typed_yes_with_the_box_gone_is_a_plain_message(self):
        """TIK-1416: the state word said prompt but the pane has no box: 'yes' is sent as text, no key, nothing stuck."""
        hl._state["waiting_permission"] = True
        _, calls = self.run_turn("yes", [[ev("Stop", last_assistant_message="ok")]], states=("permission_prompt", "busy"),
                                 pane=["● done", "❯ ", "  ⏵⏵ auto mode on (shift+tab to cycle)"])
        self.assertFalse(any(c["t"] == "key" for c in calls))
        self.assertTrue(any(c["t"] == "send" and "yes" in c["text"] for c in calls))
        self.assertFalse(hl._state["waiting_permission"])

    def test_same_box_gets_one_card(self):
        """TIK-1416: the turn re-followed with the same box on screen -> no second card; a different box -> a new card."""
        hl._state["legacy_card"] = None
        io, _ = self.run_turn("x", [[]] * 20, states=("idle", "permission_prompt"), pane=_pane(PERM_PANE))
        self.assertEqual(len(io.kinds("legacy")), 1)
        io2 = FakeIO()
        self.run_turn(None, [[]] * 20, states=("permission_prompt",), pane=_pane(PERM_PANE), io=io2)
        self.assertEqual(len(io2.kinds("legacy")), 0)
        other = [l.replace("ls", "rm -rf /tmp/zzz") for l in _pane(PERM_PANE)]
        io3 = FakeIO()
        self.run_turn(None, [[]] * 20, states=("permission_prompt",), pane=other, io=io3)
        self.assertEqual(len(io3.kinds("legacy")), 1)
        hl._perm_closed()
        self.assertIsNone(hl._state["legacy_card"])

    def test_always_is_not_a_typed_answer(self):
        hl._state["waiting_permission"] = True
        io, calls = self.run_turn("always", [], states=("permission_prompt",))
        self.assertFalse(any(c["t"] == "key" for c in calls))
        self.assertTrue(io.kinds("say"))

    def test_not_ready_head_reported(self):
        io, calls = self.run_turn("hi", [], states=("login_required",))
        self.assertTrue("isn't ready" in io.kinds("say")[0][1])
        self.assertFalse(any(c["t"] == "send" for c in calls))
        self.assertEqual(io.kinds("react"), [("react", "failed")])

    def test_login_words_in_the_scrollback_do_not_stop_a_turn(self):
        io, calls = self.run_turn("hi", [[]] * 3 + [[ev("Stop", last_assistant_message="fine")]],
                                  states=("login_required",), tail=SCROLLBACK_LOGIN)
        self.assertIn({"t": "send", "text": "hi"}, calls)
        self.assertEqual(io.kinds("answer"), [("answer", "fine")])

    def test_real_login_prompt_mid_turn_waits_for_a_quiet_head(self):
        busy = [[ev("PreToolUse", tool_name="Bash", tool_input={"command": "x"})]] * 12   # events keep coming
        io, _ = self.run_turn("hi", busy + [[ev("Stop", last_assistant_message="done anyway")]],
                              states=("idle", "login_required"))
        self.assertEqual(io.kinds("answer"), [("answer", "done anyway")])
        io2, _ = self.run_turn("hi", [[]] * 80, states=("idle", "login_required"))      # quiet: a real gate
        self.assertIn("isn't ready (state: login_required)", io2.kinds("say")[0][1])

    def test_dead_is_dead_even_while_events_arrive(self):
        busy = [[ev("PreToolUse", tool_name="Bash", tool_input={"command": "x"})]] * 12
        io, _ = self.run_turn("hi", busy + [[ev("Stop", last_assistant_message="late")]], states=("idle", "dead"))
        self.assertFalse(io.kinds("answer"))
        self.assertIn("state: dead", io.kinds("say")[0][1])

    def test_head_dies_mid_turn(self):
        io, _ = self.run_turn("hi", [[]] * 20, states=("idle", "dead"))
        self.assertEqual(io.kinds("react")[-1], ("react", "failed"))
        self.assertIn("isn't ready (state: dead)", io.kinds("say")[0][1])     # noticed, not a 30-min timeout

    def test_answered_prompt_lingering_on_screen_is_not_a_new_question(self):
        class Still:                                      # time moves only by sleep: 1 s per loop
            t = 0.0
            def now(self): return self.t
            def sleep(self, s): self.t += s
        c = Still()
        call, _ = self.bridge(("idle", "permission_prompt"))
        batches = iter([[{"event": "PermissionRequest", "perm_id": "abc123abc123", "data": {"tool_name": "Bash"}}],
                        [{"event": "PermissionAnswered", "perm_id": "abc123abc123"}]] + [[]] * 10
                       + [[ev("Stop", last_assistant_message="ok")]])
        io = FakeIO()
        hl._turn("t", {"chat": {"id": OWNER, "type": "private"}, "message_id": 3}, "x", call=call, io=io,
                 read=lambda off: (next(batches, []), off), sleep=c.sleep, now=c.now)
        self.assertFalse(io.kinds("legacy"))
        self.assertEqual(io.kinds("answer"), [("answer", "ok")])

    def test_media_lines_become_files(self):
        io, _ = self.run_turn("shot", [[ev("Stop", last_assistant_message="Here.\nMEDIA:/tmp/a.png")]])
        self.assertEqual(io.kinds("answer"), [("answer", "Here.")])
        self.assertEqual(io.kinds("file"), [("file", "/tmp/a.png")])

    def test_stop_failure_reported(self):
        io, _ = self.run_turn("hi", [[ev("StopFailure", error="rate limited")]])
        self.assertIn("rate limited", io.kinds("say")[0][1])
        self.assertEqual(io.kinds("react")[-1], ("react", "failed"))

    def test_continuation_sends_nothing(self):
        io, calls = self.run_turn(None, [[ev("Stop", last_assistant_message="ok")]], states=("busy",))
        self.assertFalse(any(c["t"] in ("send", "key") for c in calls))
        self.assertEqual(io.kinds("answer"), [("answer", "ok")])

    def test_typing_bar_renews_in_the_thread(self):
        API.clear()
        m = {"chat": {"id": OWNER, "type": "private"}, "message_id": 5, "message_thread_id": 777}
        with hl._Typing("t", m):
            import time as _t; _t.sleep(0.2)
        self.assertIn(("sendChatAction", {"chat_id": OWNER, "action": "typing", "message_thread_id": 777}), API)


class Visuals(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="hl_vis_")
        hl.TURNS = os.path.join(d, "turns.json")
        hl._q.clear(); hl._state["worker"] = object(); SENT.clear()

    def test_rich_md_escapes_money_not_code(self):
        self.assertEqual(hl.rich_md("costs $10 and $20"), "costs \\$10 and \\$20")
        self.assertEqual(hl.rich_md("run `echo $HOME` now $5"), "run `echo $HOME` now \\$5")
        self.assertEqual(hl.rich_md("```\nx=$y\n```\n$1"), "```\nx=$y\n```\n\\$1")
        self.assertEqual(hl.rich_md("already \\$9"), "already \\$9")

    def capture(self, fail_rich=False):
        calls = []
        def api(tok, method, **kw):
            calls.append((method, kw))
            if method == "sendRichMessage" and fail_rich:
                raise RuntimeError("Bad Request: can't parse rich message")
            return {"ok": True, "result": {"message_id": 40 + len(calls)}}
        pl._api = api
        return calls

    def tearDown(self):
        pl._api = lambda tok, method, **kw: API.append((method, kw)) or {"ok": True}

    def test_answer_goes_as_a_rich_message(self):
        calls = self.capture()
        io = hl._IO("t", {"chat": {"id": OWNER, "type": "private"}, "message_id": 1, "message_thread_id": 777})
        ids = io.answer("| a | b |\n|---|---|\n| $1 | 2 |")
        self.assertEqual([c[0] for c in calls], ["sendRichMessage"])
        self.assertEqual(calls[0][1]["message_thread_id"], 777)
        self.assertIn("\\$1", calls[0][1]["rich_message"]["markdown"])
        self.assertEqual(ids, [41])

    def test_rejected_rich_falls_back_to_html(self):
        calls = self.capture(fail_rich=True)
        io = hl._IO("t", {"chat": {"id": OWNER, "type": "private"}, "message_id": 1})
        ids = io.answer("**hi**")
        self.assertEqual([c[0] for c in calls], ["sendRichMessage", "sendMessage"])
        self.assertEqual(calls[1][1]["parse_mode"], "HTML")
        self.assertEqual(ids, [42])

    def test_too_long_for_rich_goes_in_parts(self):
        calls = self.capture()
        io = hl._IO("t", {"chat": {"id": OWNER, "type": "private"}, "message_id": 1})
        io.answer("x\n" * (hl.RICH_LIMIT // 2 + 10))
        self.assertNotIn("sendRichMessage", [c[0] for c in calls])

    def test_answer_text_kept_for_show(self):
        io = hl._IO("t", {"chat": {"id": OWNER, "type": "private"}, "message_id": 1})
        io.md = "the answer"; io.span = {"path": "/t", "start": 0, "end": 5}
        io.remember([88])
        self.assertEqual(json.load(open(hl.TURNS))["88"]["md"], "the answer")

    def test_show_queues_a_visual_request(self):
        hl.remember_turn([88], {"path": "/t", "start": 0, "end": 5, "md": "A -> B -> C pipeline"})
        m = {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 9, "message_thread_id": 777,
             "text": "/show as a flowchart", "reply_to_message": {"message_id": 88}}
        hl.show("t", m)
        prompt = hl._q[0][1]
        self.assertIn("A -> B -> C pipeline", prompt)
        self.assertIn("as a flowchart", prompt)
        self.assertIn("MEDIA:", prompt)
        self.assertIn("render.py mermaid", prompt)

    def test_show_typed_in_the_thread_reaches_show(self):
        hl.HEAD_THREADS[OWNER] = 777
        hl.remember_turn([88], {"path": "/t", "start": 0, "end": 5, "md": "X then Y"})
        real = hl.bridge
        hl.bridge = lambda req, wait=5: {"state": "idle", "ok": True}
        try:
            self.assertTrue(hl.handle({"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 9, "message_thread_id": 777,
                                       "text": "/show", "reply_to_message": {"message_id": 88}}, "t"))
        finally:
            hl.bridge = real
        self.assertIn("X then Y", hl._q[0][1])

    def test_show_needs_a_reply(self):
        hl.show("t", {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 9, "text": "/show"})
        self.assertEqual(len(hl._q), 0)
        self.assertTrue(any("Reply to one of my answers with /show" in s for s in SENT))


PANE_MODEL = ["❯ /model claude-sonnet-5-5", "  ⎿  Set model to Sonnet 5.5 and saved as your default for new sessions",
              "❯ /effort low", "  ⎿  Set effort level to low (saved as your default for new sessions): Quick"]


class Model(unittest.TestCase):
    def setUp(self):
        hl._state["running"] = False
        self.edits, self.said = [], []
        pl._api = lambda tok, method, **kw: self.edits.append((method, kw)) or {"ok": True, "result": {"message_id": 5}}

    def tearDown(self):
        pl._api = lambda tok, method, **kw: API.append((method, kw)) or {"ok": True}

    def bridge(self, pane):
        calls = []
        def call(req, wait=5):
            calls.append(req)
            return {"lines": pane} if req["t"] == "tail" else {"ok": True, "state": "idle"}
        return call, calls

    def press(self, data="hd:m:sonnet", uid=OWNER, pane=PANE_MODEL):
        call, calls = self.bridge(pane)
        cq = {"id": "c", "from": {"id": uid}, "message": {"chat": {"id": OWNER, "type": "private"}, "message_id": 4}}
        hl.on_model_button(cq, data, "t", self.said.append, call=call)
        return calls

    def test_card_is_2x2_latest_four(self):
        text, kb = hl.model_card()
        rows = kb["inline_keyboard"]
        self.assertEqual([len(r) for r in rows], [2, 2])
        self.assertEqual([b["text"] for r in rows for b in r], ["Haiku", "Sonnet", "Opus", "Fable"])
        self.assertEqual(dict((k, m) for k, _, m in hl.MODELS)["opus"], "claude-opus-5-5")
        self.assertEqual(hl.MODEL_EFFORT, "low")

    def test_press_sets_model_then_effort_and_confirms(self):
        import time as _t
        real = _t.sleep; hl.time.sleep = lambda s: None
        try:
            calls = self.press()
        finally:
            hl.time.sleep = real
        sends = [c["text"] for c in calls if c["t"] == "send"]
        self.assertEqual(sends, ["/model claude-sonnet-5-5", "/effort low"])
        edit = [kw["text"] for m, kw in self.edits if m == "editMessageText"][-1]
        self.assertEqual(edit, "✅ Set model to Sonnet 5.5 · effort low")

    def test_unconfirmed_switch_is_said(self):
        hl.time.sleep, real = (lambda s: None), hl.time.sleep
        try:
            self.press(pane=["❯ /model claude-fable-5-1", "  ⎿  Model claude-fable-5-1 not found"])
        finally:
            hl.time.sleep = real
        edit = [kw["text"] for m, kw in self.edits if m == "editMessageText"][-1]
        self.assertTrue(edit.startswith("⚠️ Couldn't confirm"))
        self.assertIn("not found", edit)

    def test_only_rav_and_only_when_idle(self):
        self.assertEqual([c for c in self.press(uid=USER_B) if c["t"] == "send"], [])
        hl._state["running"] = True
        self.assertEqual([c for c in self.press() if c["t"] == "send"], [])
        self.assertIn("mid-answer", self.said[-1])
        hl._state["running"] = False
        self.assertEqual([c for c in self.press(data="hd:m:gpt") if c["t"] == "send"], [])

    def test_bare_model_never_reaches_the_repl(self):
        self.assertEqual(hl.command_of("/model"), "/model")
        hl.HEAD_THREADS[OWNER] = 777
        calls = []
        real = hl.bridge
        hl.bridge = lambda req, wait=5: calls.append(req) or {"state": "idle", "ok": True}
        try:
            hl.handle({"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 2, "message_thread_id": 777, "text": "/model"}, "t")
        finally:
            hl.bridge = real
        self.assertFalse(any(c["t"] == "send" for c in calls))
        self.assertTrue(any(m == "sendMessage" and "reply_markup" in kw for m, kw in self.edits))

    def test_dispatch(self):
        hl.time.sleep, real = (lambda s: None), hl.time.sleep
        try:
            call, calls = self.bridge(PANE_MODEL)
            hl.on_button({"id": "c", "from": {"id": OWNER}, "message": {"chat": {"id": OWNER, "type": "private"}, "message_id": 4}},
                         "hd:m:opus", "t", call=call)
        finally:
            hl.time.sleep = real
        self.assertIn({"t": "send", "text": "/model claude-opus-5-5"}, calls)


class Heal(unittest.TestCase):
    def setUp(self):
        hl._state["waiting_permission"] = False
        tmp = tempfile.mkdtemp(prefix="hl_heal_")
        hl.REMOTE = os.path.join(tmp, "remote.json"); hl.EVENTS = os.path.join(tmp, "events.jsonl")

    def go(self, states, events):
        calls, it = [], iter(states)
        def call(req, wait=5):
            calls.append(req)
            return {"state": next(it, states[-1])} if req["t"] == "status" else {"ok": True, "detail": "started"}
        io, c, ev = FakeIO(), Clock(), iter(events)
        hl._turn("t", {"chat": {"id": OWNER, "type": "private"}, "message_id": 3}, "hi", call=call, io=io,
                 read=lambda off: (next(ev, []), off), sleep=c.sleep, now=c.now)
        return io, calls

    def test_dead_head_restarted_then_answers(self):
        io, calls = self.go(["dead", "idle"], [[ev("Stop", last_assistant_message="back")]])
        self.assertIn({"t": "ensure"}, calls)
        self.assertTrue(any("restarted it" in x[1] for x in io.kinds("say")))
        self.assertIn({"t": "send", "text": "hi"}, calls)
        self.assertEqual(io.kinds("answer"), [("answer", "back")])

    def test_heal_that_fails_is_said(self):
        io, calls = self.go(["dead"], [])
        self.assertTrue(any("didn't come back" in x[1] for x in io.kinds("say")))
        self.assertFalse(any(c["t"] == "send" for c in calls))


class Pure(unittest.TestCase):
    def test_tool_brief(self):
        self.assertEqual(hl.tool_brief("Bash", {"command": "ls -la\nmore", "description": "List files"}), "⚙️ List files")
        self.assertEqual(hl.tool_brief("Read", {"file_path": "/a/b/c.py"}), "📖 c.py")
        self.assertEqual(hl.tool_brief("mcp__hub__tickets", {"action": "get"}), "🛰 tickets get")

    def test_status_text(self):
        t = hl.status_text([f"s{i}" for i in range(9)], 0, 12)
        self.assertTrue(t.startswith("⏳ Working · 12s"))
        self.assertIn("… 3 earlier", t)
        self.assertNotIn("s2", t)
        self.assertTrue(hl.status_text(["a"], 0, 5, done=True).startswith("✅ Done · 1 step(s)"))

    def test_md_to_html(self):
        h = hl.md_to_html("# Title\n**bold** and *it* `a<b>` [x](https://e.com)\n- item\n```py\nif a < b:\n```")
        self.assertIn("<b>Title</b>", h)
        self.assertIn("<b>bold</b>", h)
        self.assertIn("<i>it</i>", h)
        self.assertIn("<code>a&lt;b&gt;</code>", h)
        self.assertIn('<a href="https://e.com">x</a>', h)
        self.assertIn("• item", h)
        self.assertIn("<pre>if a &lt; b:</pre>", h)
        self.assertEqual(hl.md_to_html("5 < 6 & 7"), "5 &lt; 6 &amp; 7")

    def test_split_parts(self):
        self.assertEqual(hl.split_parts("short"), ["short"])
        parts = hl.split_parts("\n".join(["line %d" % i for i in range(2000)]), n=1000)
        self.assertTrue(len(parts) > 1 and parts[0].startswith("(1/") and all(len(p) < 1100 for p in parts))
        code = "```\n" + "\n".join("x" * 50 for _ in range(60)) + "\n```"
        for p in hl.split_parts(code, n=1000):
            self.assertEqual(p.count("```") % 2, 0)       # every part's fences are balanced

    def test_media_lines(self):
        self.assertEqual(hl.media_lines("hi\nMEDIA: /a/b.png\nbye"), ("hi\n\nbye", ["/a/b.png"]))

    def test_read_events_waits_for_a_whole_line(self):
        p = os.path.join(tempfile.mkdtemp(), "e.jsonl")
        open(p, "w").write('{"event": "A"}\n{"event": "B"')
        recs, off = hl.read_events(0, p)
        self.assertEqual([r["event"] for r in recs], ["A"])
        open(p, "a").write("}\n")
        recs, off = hl.read_events(off, p)
        self.assertEqual([r["event"] for r in recs], ["B"])

    def test_perm_card_hook(self):
        text, kb = hl.perm_card_hook("abc123abc123", "Bash", {"command": "rm x", "description": "Remove x"})
        self.assertIn("$ rm x", text)
        self.assertEqual([b["callback_data"] for b in kb["inline_keyboard"][0]], ["hd:p:abc123abc123:a", "hd:p:abc123abc123:d"])

    def test_media_prompt(self):
        self.assertIn("saved at /x.jpg", hl.media_prompt("photo", "/x.jpg", "look"))
        vp = hl.media_prompt("voice", "/v.ogg", None, "hello there")
        self.assertIn("transcribed on the host) hello there", vp)
        self.assertIn("VOICE:", vp)                                         # the head knows the lane speaks back


class Thoughts(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="hl_th_")
        hl.TURNS, hl.OPEN_THOUGHTS = os.path.join(d, "turns.json"), os.path.join(d, "open.json")
        self.tp = os.path.join(d, "t.jsonl")
        old = json.dumps({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "OLD TURN"}]}}) + "\n"
        new = "".join(json.dumps(x) + "\n" for x in [
            {"type": "user", "message": {"content": "q"}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "first <idea>"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "uptime"}}]}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "answer"}]}}])
        open(self.tp, "w").write(old + new)
        self.span = {"path": self.tp, "start": len(old.encode()), "end": len((old + new).encode())}
        API.clear(); SENT.clear()

    def test_only_this_turns_thinking(self):
        items = hl.thoughts_for(self.span)
        self.assertEqual(items, [("think", "first <idea>"), ("step", "⚙️ uptime")])

    def test_render(self):
        html = "".join(hl.render_thoughts(hl.thoughts_for(self.span)))
        self.assertIn("<blockquote expandable>first &lt;idea&gt;</blockquote>", html)
        self.assertIn("⚙️ uptime", html)
        self.assertIn("No thinking was recorded", hl.render_thoughts([("step", "x")])[0])
        big = hl.render_thoughts([("think", "y" * 9000)])
        self.assertTrue(len(big) >= 3 and all(len(p) < 3800 for p in big))

    def test_turn_span_recorded_from_events(self):
        io, _ = Turn.run_turn(Turn("run"), "q", [[{"event": "UserPromptSubmit", "tsize": 5, "data": {"transcript_path": "/t"}}],
                                                [{"event": "Stop", "tsize": 90, "data": {"last_assistant_message": "a"}}]])
        self.assertEqual(io.kinds("remember"), [("remember", [100], {"path": "/t", "start": 5, "end": 90})])

    def msg(self, reply_to=None, uid=OWNER):
        m = {"from": {"id": uid}, "chat": {"id": OWNER, "type": "private"}, "message_id": 50, "message_thread_id": 777, "text": "/thoughts?"}
        if reply_to:
            m["reply_to_message"] = {"message_id": reply_to}
        return m

    def test_show_needs_a_reply(self):
        hl.show_thoughts("t", self.msg())
        self.assertTrue(any("Reply to one of my answers" in x for x in SENT))

    def test_show_and_close(self):
        hl.remember_turn([70, 71], self.span)
        pl._api = lambda tok, method, **kw: API.append((method, kw)) or {"ok": True, "result": {"message_id": 99}}
        try:
            hl.show_thoughts("t", self.msg(71))
            sends = [kw for mth, kw in API if mth == "sendMessage"]
            self.assertEqual(sends[0]["parse_mode"], "HTML")
            self.assertEqual(sends[0]["reply_to_message_id"], 71)
            cb = sends[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            self.assertTrue(cb.startswith("hd:x:"))
            said = []
            hl.close_thoughts({"from": {"id": USER_B}}, cb, "t", said.append)
            self.assertFalse([1 for mth, _ in API if mth == "deleteMessage"])
            hl.close_thoughts({"from": {"id": OWNER}}, cb, "t", said.append)
            deleted = sorted(kw["message_id"] for mth, kw in API if mth == "deleteMessage")
            self.assertEqual(deleted, [50, 99])                       # the thoughts and the /thoughts command
            hl.close_thoughts({"from": {"id": OWNER}}, cb, "t", said.append)
            self.assertEqual(said[-1], "Already closed.")
        finally:
            pl._api = lambda tok, method, **kw: API.append((method, kw)) or {"ok": True}

    def test_real_io_remembers_only_a_closed_span(self):
        io = hl._IO("t", {"chat": {"id": OWNER, "type": "private"}, "message_id": 1})
        io.span = {"path": self.tp, "start": 0}
        io.remember([5])
        self.assertFalse(os.path.exists(hl.TURNS))           # no end yet: nothing to point at
        io.span["end"] = 10
        io.remember([5, 6])
        self.assertEqual(sorted(json.load(open(hl.TURNS))), ["5", "6"])

    def test_turns_capped(self):
        for i in range(hl.TURNS_KEEP + 20):
            hl.remember_turn([i], self.span)
        t = json.load(open(hl.TURNS))
        self.assertEqual(len(t), hl.TURNS_KEEP)
        self.assertIn(str(hl.TURNS_KEEP + 19), t)

    def test_command_of(self):
        self.assertEqual(hl.command_of("/thoughts?"), "/thoughts")
        self.assertEqual(hl.command_of("/stop@AtlasBot"), "/stop")
        self.assertIsNone(hl.command_of("/scan doc"))
        self.assertIsNone(hl.command_of("thoughts"))

    def test_stt_falls_back_to_cpu(self):
        import io as _io
        tried = []
        def post(req, timeout=0):
            tried.append(req.full_url)
            if ":8002" in req.full_url:
                raise OSError("stopped for an image job")
            return _io.BytesIO(b'{"text": "hello"}')
        src = os.path.join(tempfile.mkdtemp(), "v.ogg")
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "0.3", src], check=True)
        self.assertEqual(hl.transcribe(src, post=post), "hello")
        self.assertEqual([u.split(":")[2][:4] for u in tried], ["8002", "8003"])


class PermButton(unittest.TestCase):
    def setUp(self):
        hl.PERM_DIR = tempfile.mkdtemp(prefix="hl_perm_")
        open(os.path.join(hl.PERM_DIR, "abc123abc123.req"), "w").write("{}")
        self.said = []

    def press(self, uid=OWNER, data="hd:p:abc123abc123:a"):
        cq = {"id": "c", "from": {"id": uid}, "message": {"chat": {"id": OWNER, "type": "private"}, "message_id": 4, "text": "🔐"}}
        hl.on_perm_button(cq, data, "t", self.said.append)

    def ans(self):
        p = os.path.join(hl.PERM_DIR, "abc123abc123.ans")
        return json.load(open(p))["behavior"] if os.path.exists(p) else None

    def test_allow_and_deny(self):
        self.press(); self.assertEqual(self.ans(), "allow")
        self.press(data="hd:p:abc123abc123:d"); self.assertEqual(self.ans(), "deny")

    def test_others_refused(self):
        for u in (USER_B, USER_C, 999):
            self.press(uid=u)
        self.assertIsNone(self.ans())

    def test_bad_ids_refused(self):
        self.press(data="hd:p:../../x:a"); self.press(data="hd:p:abc123abc123:x")
        self.assertIsNone(self.ans())

    def test_path_traversal_refused(self):
        outside = tempfile.mkdtemp(prefix="hl_out_")          # its own dir: no leftovers from an earlier mutant run
        hl.PERM_DIR = os.path.join(outside, "perm"); os.makedirs(hl.PERM_DIR)
        open(os.path.join(outside, "evil.req"), "w").close()
        self.press(data="hd:p:../evil:a")
        self.assertFalse(os.path.exists(os.path.join(outside, "evil.ans")))

    def test_closed_request(self):
        self.press(data="hd:p:ffffffffffff:a")
        self.assertIn("already closed", self.said[-1])

    def test_dispatch_from_on_button(self):
        cq = {"id": "c", "from": {"id": OWNER}, "message": {"chat": {"id": OWNER, "type": "private"}, "message_id": 4, "text": "🔐"}}
        hl.on_button(cq, "hd:p:abc123abc123:a", "t", call=lambda r, wait=5: {"state": "busy"})
        self.assertEqual(self.ans(), "allow")

# The real permission box alex saw on his phone (2026-10-24 00:38).
PERM_PANE = """● Bash(ls /tmp/.X11-unix/)
────────────────────────────────────────────────────────────────────────────────
 Bash command

   ls /tmp/.X11-unix/; loginctl list-sessions 2>/dev/null; for d in :0 :1; do echo "== $d";
   DISPLAY=$d xrandr --listmonitors 2>&1 | head; done
   Find displays and screenshot tools on the host

 Tip: auto mode handles these prompts for you — choose "switch to auto mode" below
 A variable in this command can't be checked before it runs
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and switch to auto mode · auto mode
   handles these prompts for you
   3. No

 Esc to cancel · Tab to amend""".splitlines()


class Permission(unittest.TestCase):
    def test_parse_real_box(self):
        p = hl.parse_permission(PERM_PANE)
        self.assertEqual(p["title"], "Bash command")
        self.assertIn("xrandr", p["detail"])
        self.assertNotIn("Tip:", p["detail"])
        self.assertNotIn("───", p["detail"])
        self.assertEqual(p["question"], "Do you want to proceed?")
        self.assertEqual([n for n, _ in p["options"]], ["1", "2", "3"])
        self.assertIn("handles these prompts", p["options"][1][1])     # wrapped label joined

    def test_card_buttons(self):
        text, kb = hl.permission_card(hl.parse_permission(PERM_PANE))
        btns = [r[0] for r in kb["inline_keyboard"]]
        self.assertEqual([b["callback_data"] for b in btns], ["hd:1", "hd:2", "hd:Escape"])
        self.assertEqual([b["text"] for b in btns], ["✅ Yes", "⚡ Yes + auto mode", "❌ No"])
        self.assertTrue(text.startswith("🔐 Bash command"))

    def test_no_box(self):
        self.assertIsNone(hl.parse_permission(["❯ hi", "● hello"]))

    def cq(self, uid=OWNER, data="hd:1"):
        return {"id": "c1", "data": data, "from": {"id": uid},
                "message": {"chat": {"id": OWNER, "type": "private"}, "message_id": 9, "message_thread_id": 777, "text": "🔐 x",
                            "reply_markup": {"inline_keyboard": [[{"text": "✅ Yes", "callback_data": "hd:1"}]]}}}

    def bridge(self, state, pane=None):
        calls = []
        def call(req, wait=5):
            calls.append(req)
            if req["t"] == "status":
                return {"state": state}
            if req["t"] == "tail":
                return {"lines": _pane(PERM_PANE) if pane is None else list(pane)}
            return {"ok": True}
        return call, calls

    def test_tap_with_the_box_gone_sends_no_key(self):
        """TIK-1416: '1321' was four taps typed into an idle REPL. No box on the pane -> no key, card closed."""
        call, calls = self.bridge("permission_prompt", pane=["● done", "❯ ", "  ⏵⏵ auto mode on (shift+tab to cycle)"])
        hl._state["waiting_permission"] = True
        hl.on_button(self.cq(), "hd:1", "t", call=call)
        self.assertFalse(any(c["t"] == "key" for c in calls))
        self.assertFalse(hl._state["waiting_permission"])
        self.assertEqual(len(hl._q), 0)

    def test_tap_on_a_number_the_box_does_not_offer_sends_no_key(self):
        call, calls = self.bridge("permission_prompt")          # PERM_PANE offers 1, 2, 3
        hl._state["waiting_permission"] = True
        hl.on_button(self.cq(), "hd:4", "t", call=call)
        self.assertFalse(any(c["t"] == "key" for c in calls))

    def setUp(self):
        hl._q.clear(); hl._state["worker"] = object(); API.clear()

    def test_button_presses_the_key_and_follows_the_turn(self):
        call, calls = self.bridge("permission_prompt")
        hl._state["waiting_permission"] = True
        hl.on_button(self.cq(), "hd:1", "t", call=call)
        self.assertIn({"t": "key", "key": "1"}, calls)
        self.assertFalse(hl._state["waiting_permission"])
        self.assertEqual(len(hl._q), 1)
        self.assertIsNone(hl._q[0][1])
        self.assertTrue(any(mth == "editMessageText" for mth, _ in API))

    def test_button_others_refused(self):
        for u in (USER_B, USER_C, 999):
            call, calls = self.bridge("permission_prompt")
            hl.on_button(self.cq(uid=u), "hd:1", "t", call=call)
            self.assertFalse(any(c["t"] == "key" for c in calls))
        self.assertEqual(len(hl._q), 0)

    def test_button_bad_key_refused(self):
        call, calls = self.bridge("permission_prompt")
        hl.on_button(self.cq(data="hd:rm -rf"), "hd:rm -rf", "t", call=call)
        self.assertFalse(any(c["t"] == "key" for c in calls))

    def test_stale_button_does_nothing(self):
        call, calls = self.bridge("busy")
        hl.on_button(self.cq(), "hd:1", "t", call=call)
        self.assertFalse(any(c["t"] == "key" for c in calls))
        self.assertEqual(len(hl._q), 0)



class Undelivered(unittest.TestCase):
    """TIK-1017 item 1: a reply that never reached alex is told to the head ONCE, at the start of its next prompt."""
    def setUp(self):
        for p in (hl.UNDELIVERED, hl.MIDTURN):
            if os.path.exists(p):
                os.remove(p)
        hl._state["waiting_permission"] = False
        self.t = Turn("run"); self.t.setUp()

    def test_failed_turn_then_one_notice(self):
        self.t.run_turn("look at the TV", [[]] * 20, states=("idle", "dead"))          # lane gives up
        _, calls = self.t.run_turn("next", [[ev("Stop", last_assistant_message="ok")]])
        sent = [c["text"] for c in calls if c["t"] == "send"][0]
        self.assertTrue(sent.startswith("[Your last reply was not delivered (the lane gave up"))
        self.assertTrue(sent.endswith("next"))
        _, calls = self.t.run_turn("again", [[ev("Stop", last_assistant_message="ok")]])
        self.assertEqual([c["text"] for c in calls if c["t"] == "send"][0], "again")       # told exactly once

    def test_refused_messages_listed(self):
        self.t.run_turn("I see it on the SHARP", [], states=("dead",))                     # dead and no heal
        _, calls = self.t.run_turn("that was weird", [[ev("Stop", last_assistant_message="ok")]])
        sent = [c["text"] for c in calls if c["t"] == "send"][0]
        self.assertIn('refused meanwhile: "I see it on the SHARP"', sent)

    def test_timeout_noted(self):
        hl.note_undelivered(why="no reply within 30 min")
        self.assertIn("no reply within 30 min", hl.pending_notice(commit=False))
        self.assertTrue(hl.pending_notice())
        self.assertEqual(hl.pending_notice(), "")


class Midturn(unittest.TestCase):
    """TIK-1017 item 5: a message sent while a turn runs is written for the hook to hand over."""
    def setUp(self):
        if os.path.exists(hl.MIDTURN):
            os.remove(hl.MIDTURN)
        hl.HEAD_THREADS[OWNER] = 777
        hl._q.clear(); hl._state["worker"] = object()

    def m(self, text):
        return {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 3, "message_thread_id": 777, "text": text}

    def test_written_only_while_running(self):
        hl._state["running"] = False
        hl.handle(self.m("hello"), "t")
        self.assertFalse(os.path.exists(hl.MIDTURN))
        hl._state["running"] = True
        try:
            hl.handle(self.m("stop, wrong screen"), "t")
        finally:
            hl._state["running"] = False
        self.assertIn("stop, wrong screen", open(hl.MIDTURN).read())
        self.assertEqual(len(hl._q), 2)                                       # still queued as the next turn


class Voice(unittest.TestCase):
    """TIK-1019: a voice note gets its transcript echoed at once, and the answer comes back as a voice note with its text."""
    def test_voice_block_and_speakable(self):
        body, said = hl.voice_block("Here's the plan.\nVOICE: Short version, all good.\n| a | b |")
        self.assertEqual(said, "Short version, all good.")
        self.assertNotIn("VOICE:", body)
        s = hl.speakable("## Title\n**Bold** and `code` [link](https://x.y)\n```\nprint(1)\n```\n| a | b |\n- item")
        self.assertEqual(s, "Title Bold and code link item")
        long = hl.speakable("One sentence. " * 100)
        self.assertTrue(len(long) <= hl.SPEAK_MAX and long.endswith("."))

    def test_voice_prompt_turn_speaks_the_answer(self):
        t = Turn("run"); t.setUp()
        io = FakeIO()
        call, calls = t.bridge(("idle",))
        c = Clock()
        m = {"chat": {"id": OWNER, "type": "private"}, "message_id": 3, "message_thread_id": 777, "_from_voice": True}
        hl._turn("t", m, "what's up", call=call, io=io, read=t.feed([ev("Stop", last_assistant_message="**All** good.\nVOICE: All good, Rav.")]),
                 sleep=c.sleep, now=c.now)
        self.assertEqual(io.kinds("answer"), [("answer", "**All** good.")])
        self.assertEqual(io.kinds("voice"), [("voice", "All good, Rav.")])
        io2 = FakeIO()
        hl._turn("t", m, "again", call=call, io=io2, read=t.feed([ev("Stop", last_assistant_message="**Done**.")]),
                 sleep=c.sleep, now=c.now)
        self.assertEqual(io2.kinds("voice"), [("voice", "Done.")])                # no VOICE line: cleaned reply

    def test_live_answer_goes_to_the_phone_speaker_else_a_voice_note(self):
        t = Turn("run"); t.setUp()
        c = Clock()
        m = {"chat": {"id": OWNER, "type": "private"}, "message_id": 3, "message_thread_id": 777, "_from_voice": True, "_live": True}
        said = []
        old = hl.live_speak
        self.addCleanup(setattr, hl, "live_speak", old)
        for open_, want_note in ((True, False), (False, True)):
            said.clear()
            hl.live_speak = lambda text, o=open_: said.append(text) or o
            call, _ = t.bridge(("idle",))
            io = FakeIO()
            hl._turn("t", m, "q", call=call, io=io, read=t.feed([ev("Stop", last_assistant_message="Hi.\nVOICE: Hello Rav.")]),
                     sleep=c.sleep, now=c.now)
            self.assertEqual(said, ["Hello Rav."])
            self.assertEqual(bool(io.kinds("voice")), want_note)
        st = []
        oldst, hl.live_status = hl.live_status, lambda s, t="": st.append(s)
        try:
            call, _ = t.bridge(("idle",))
            hl._turn("t", m, "q", call=call, io=FakeIO(), read=t.feed([ev("Stop", last_assistant_message="ok")]),
                     sleep=c.sleep, now=c.now)
        finally:
            hl.live_status = oldst
        self.assertEqual(st, ["answered"])                                   # the page drops its thinking bar

    def test_live_speak_reports_only_an_open_live_tab(self):
        self.assertTrue(hl.live_speak("hi", post=lambda p, b: {"ok": True, "live": True}))
        self.assertFalse(hl.live_speak("hi", post=lambda p, b: {"ok": True, "live": False}))
        def down(p, b):
            raise ConnectionRefusedError("no socket")
        self.assertFalse(hl.live_speak("hi", post=down))
        self.assertIn("live_speak_fail", open(pl.LOG_PATH).read())
        self.assertFalse(hl.live_speak("  ", post=lambda p, b: {"live": True}))

    def test_text_prompt_gets_no_voice(self):
        t = Turn("run"); t.setUp()
        io, _ = t.run_turn("typed", [[ev("Stop", last_assistant_message="hi")]])
        self.assertFalse(io.kinds("voice"))

    def test_io_voice_sends_a_voice_note_with_the_exact_text(self):
        sent = []
        real = pl._send_document
        pl._send_document = lambda tok, m, path, caption, **k: sent.append((path, caption, k))
        try:
            io = hl._IO("t", {"chat": {"id": OWNER, "type": "private"}, "message_id": 1})
            io.voice("Hello Rav.", render=lambda t: "/tmp/x.ogg")
        finally:
            pl._send_document = real
        self.assertEqual(sent[0][0], "/tmp/x.ogg")
        self.assertEqual(sent[0][1], "🔊 Hello Rav.")
        self.assertEqual(sent[0][2]["method"], "sendVoice")

    def test_io_voice_failure_said(self):
        said = []
        io = hl._IO("t", {"chat": {"id": OWNER, "type": "private"}, "message_id": 1})
        io.say = said.append
        def boom(t):
            raise SystemExit("piper failed")
        io.voice("Hi.", render=boom)
        self.assertIn("piper failed", said[0])

    def test_transcript_echoed_and_flagged(self):
        SENT.clear()
        real_dl, real_tr = pl._download, hl.transcribe
        pl._download = lambda tok, fid, dest: open(dest, "wb").write(b"ogg")
        hl.transcribe = lambda p: "turn the lights on"
        hl.INBOX = os.path.join(_TMP, "inbox")
        try:
            m = {"chat": {"id": OWNER, "type": "private"}, "message_id": 5, "voice": {"file_id": "v"}}
            prompt = hl.media_to_text("t", m)
        finally:
            pl._download, hl.transcribe = real_dl, real_tr
        self.assertIn("📝 heard: turn the lights on", SENT)
        self.assertTrue(m.get("_from_voice"))
        self.assertIn("turn the lights on", prompt)


class Stop(unittest.TestCase):
    """06:00 2026-10-24: an interrupt sends no Stop hook; Rav's /stop worked but the lane waited on and typed forever."""
    def setUp(self):
        self.t = Turn("run"); self.t.setUp()

    def test_lane_stop_ends_the_turn(self):
        io, _ = self.t.run_turn("go", [[ev("PreToolUse", tool_name="Bash", tool_input={"command": "x"})],
                                      [{"event": "LaneStop", "data": {}}]] + [[]] * 5)
        self.assertIn("Stopped", io.kinds("say")[0][1])
        self.assertFalse(io.kinds("answer"))
        self.assertFalse(hl._state["running"])

    def test_stop_command_writes_the_marker(self):
        p = os.path.join(_TMP, "stop_events.jsonl")
        if os.path.exists(p):
            os.remove(p)
        hl.mark_stopped(path=p)
        self.assertEqual(json.loads(open(p).read())["event"], "LaneStop")
        hl.EVENTS = p
        calls = []
        hl.HEAD_THREADS[OWNER] = 777
        real = hl.bridge
        hl.bridge = lambda req, wait=5: calls.append(req) or {"state": "busy", "ok": True}
        try:
            hl.handle({"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 2, "message_thread_id": 777, "text": "/stop"}, "t")
        finally:
            hl.bridge = real
        self.assertIn({"t": "key", "key": "Escape"}, calls)
        self.assertEqual(open(p).read().count("LaneStop"), 2)
        self.assertEqual(hl.head_state(), "idle")


class EventState(unittest.TestCase):
    """TIK-1017 item 2: busy/idle comes from the head's own hook events."""
    def run_(self, events):
        return hl.head_state(read=lambda off: (events, 0), size=lambda: 0)

    def test_lane_stop_is_idle(self):
        self.assertEqual(self.run_([ev("PreToolUse"), {"event": "LaneStop", "data": {}}]), "idle")

    def test_states(self):
        self.assertEqual(self.run_([ev("UserPromptSubmit"), ev("PreToolUse")]), "busy")
        self.assertEqual(self.run_([ev("UserPromptSubmit"), ev("Stop")]), "idle")
        self.assertEqual(self.run_([ev("Stop"), ev("Notification")]), "idle")             # a Notification after Stop is still idle
        self.assertEqual(self.run_([]), "unknown")


class Reactions(unittest.TestCase):
    """alex 2026-10-24: reactions on head answers are feedback, recorded, never a turn; told once, next turn."""
    def setUp(self):
        d = tempfile.mkdtemp(dir=_TMP)
        hl.TURNS = os.path.join(d, "turns.json")
        self.rx, self.off = os.path.join(d, "r.jsonl"), os.path.join(d, "r.off")
        hl.remember_turn([501], {"path": "/t", "start": 0, "end": 5, "md": "Stage part 1 is live\nwith undo"})
        hl._q.clear(); API.clear(); SENT.clear()

    def r(self, uid=OWNER, mid=501, old=(), new=("👍",)):
        return {"chat": {"id": uid, "type": "private"}, "message_id": mid, "user": {"id": uid}, "date": 1,
                "old_reaction": [{"type": "emoji", "emoji": e} for e in old],
                "new_reaction": [{"type": "emoji", "emoji": e} for e in new]}

    def test_reaction_recorded_and_no_turn(self):
        rec = hl.on_reaction(self.r(), path=self.rx)
        self.assertEqual(rec["added"], ["👍"]); self.assertIn("Stage part 1 is live / with undo", rec["answer"])
        self.assertEqual(list(hl._q), []); self.assertEqual(SENT, [])     # nothing queued, nothing said
        self.assertEqual([m for m, _ in API if m.startswith("send")], [])

    def test_only_rav_and_only_head_answers(self):
        self.assertIsNone(hl.on_reaction(self.r(uid=USER_B), path=self.rx))
        self.assertIsNone(hl.on_reaction(self.r(mid=999), path=self.rx))
        self.assertFalse(os.path.exists(self.rx))

    def test_feedback_told_once_next_turn(self):
        hl.on_reaction(self.r(), path=self.rx)
        hl.on_reaction(self.r(old=("👍",), new=("👍", "🔥")), path=self.rx)
        line = hl.pending_feedback(self.rx, self.off)
        self.assertIn("👍", line); self.assertIn("🔥", line); self.assertIn("Feedback since your last turn", line)
        self.assertEqual(hl.pending_feedback(self.rx, self.off), "")      # consumed: never told twice

    def test_removal_and_no_change(self):
        self.assertIsNone(hl.on_reaction(self.r(old=("👍",), new=("👍",)), path=self.rx))
        hl.on_reaction(self.r(old=("👍",), new=()), path=self.rx)
        self.assertIn("removed 👍", hl.pending_feedback(self.rx, self.off))

    def test_no_reactions_no_prefix(self):
        self.assertEqual(hl.pending_feedback(self.rx, self.off), "")


class Video(unittest.TestCase):
    """TIK-1025: Telegram's round video messages reach the head, and any video comes with how to see it."""
    def test_round_video_is_a_media_kind(self):
        self.assertEqual(hl.media_kind({"video_note": {"file_id": "x"}}), "video_note")
        self.assertEqual(hl.media_kind({"video": {"file_id": "x"}}), "video")

    def test_video_prompt_points_at_clip_tool(self):
        p = hl.media_prompt("video_note", "/in/v.mp4", None)
        self.assertIn("round video", p); self.assertIn("/in/v.mp4", p); self.assertIn("clip.py", p)
        self.assertNotIn("clip.py", hl.media_prompt("photo", "/in/p.jpg", None))

    def test_round_video_not_refused(self):
        SENT.clear(); hl._q.clear()
        m = {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 5,
             "message_thread_id": hl.HEAD_THREADS[OWNER], "video_note": {"file_id": "x"}}
        started = []
        self.assertTrue(hl.handle(m, "t", prep=lambda tok, mm: started.append(mm)))
        self.assertFalse(any("isn't supported" in x for x in SENT))
        self.assertEqual(len(started), 1)
        hl._q.clear()


class Prep(unittest.TestCase):
    """alex 15:09: video/transcript processing must not hold up the conversation."""
    def setUp(self):
        hl._q.clear(); hl._state["worker"] = object(); hl._state["running"] = False
        if os.path.exists(hl.MIDTURN):
            os.remove(hl.MIDTURN)
        SENT.clear()
        self.m = {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 8, "message_thread_id": 777, "video": {"file_id": "v"}}

    def test_prepared_prompt_joins_the_queue(self):
        got = []
        hl._prep_media("t", self.m, to_text=lambda tok, m: "prompt with sheet", enqueue=lambda tok, m, t: got.append(t))
        self.assertEqual(got, ["prompt with sheet"])

    def test_text_is_not_held_behind_a_video(self):
        hl.HEAD_THREADS[OWNER] = 777
        hl.handle(self.m, "t", prep=lambda tok, m: None)                # the video is still being processed
        hl.handle({"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 9, "message_thread_id": 777, "text": "hello"}, "t")
        self.assertEqual([t for _, t in hl._q], ["hello"])                # text went straight in
        hl._q.clear()

    def test_busy_head_told_when_it_is_ready(self):
        hl._state["running"] = True
        try:
            hl._prep_media("t", self.m, to_text=lambda tok, m: "the clip report", enqueue=lambda *a: None)
        finally:
            hl._state["running"] = False
        self.assertIn("processed and queued", open(hl.MIDTURN).read())

    def test_failure_said_and_noted(self):
        hl.UNDELIVERED = os.path.join(_TMP, "undelivered_prep.json")
        if os.path.exists(hl.UNDELIVERED):
            os.remove(hl.UNDELIVERED)
        def boom(tok, m):
            raise RuntimeError("download failed")
        got = []
        hl._prep_media("t", self.m, to_text=boom, enqueue=lambda *a: got.append(a))
        self.assertEqual(got, [])
        self.assertTrue(any("Couldn't process that video" in x for x in SENT))
        self.assertIn("failed to process", hl.pending_notice())


class ClipAuto(unittest.TestCase):
    """TIK-1025 item 2: the lane runs clip.py itself, so the head always gets the sheet + transcript."""
    def res(self, rc=0, out="sheet: /s.png\ntranscript: hi", err=""):
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": err})()

    def test_run_clip_ok_fail_and_cut(self):
        seen = []
        self.assertEqual(hl.run_clip("/v.mp4", run=lambda cmd, **k: seen.append(cmd) or self.res()), "sheet: /s.png\ntranscript: hi")
        self.assertEqual(seen[0], ["python3", hl.CLIP, "/v.mp4"])
        self.assertIsNone(hl.run_clip("/v.mp4", run=lambda cmd, **k: self.res(rc=1, out="", err="ffmpeg died")))
        def boom(cmd, **k):
            raise OSError("timeout")
        self.assertIsNone(hl.run_clip("/v.mp4", run=boom))
        long = hl.run_clip("/v.mp4", run=lambda cmd, **k: self.res(out="x" * 9000))
        self.assertTrue(len(long) < 3600 and long.endswith("(report cut)"))

    def test_prompt_carries_the_report(self):
        p = hl.media_prompt("video", "/in/v.mp4", "look", None, "sheet: /s.png\ntranscript: hi")
        self.assertIn("already ran clip.py", p)
        self.assertIn("sheet: /s.png", p)

    def test_media_to_text_runs_it_for_video_only(self):
        SENT.clear()
        real_dl, real_clip = pl._download, hl.run_clip
        calls = []
        pl._download = lambda tok, fid, dest: open(dest, "wb").write(b"mp4")
        hl.run_clip = lambda p: calls.append(p) or "sheet: /s.png"
        hl.INBOX = os.path.join(_TMP, "inbox")
        try:
            p = hl.media_to_text("t", {"chat": {"id": OWNER, "type": "private"}, "message_id": 5, "video_note": {"file_id": "v"}})
            self.assertEqual(len(calls), 1)
            self.assertIn("sheet: /s.png", p)
            hl.media_to_text("t", {"chat": {"id": OWNER, "type": "private"}, "message_id": 6, "photo": [{"file_id": "p"}]})
            self.assertEqual(len(calls), 1)                             # photos are not clipped
            hl.run_clip = lambda p: None
            p2 = hl.media_to_text("t", {"chat": {"id": OWNER, "type": "private"}, "message_id": 7, "video": {"file_id": "v"}})
            self.assertIn("clip.py <that path>", p2)                    # falls back to the hint
            self.assertTrue(any("Couldn't pre-process" in x for x in SENT))
        finally:
            pl._download, hl.run_clip = real_dl, real_clip

    def test_gpu_off_is_not_a_failure(self):
        def post(req, timeout=0):
            if ":8002" in req.full_url:
                raise OSError("<urlopen error [Errno 111] Connection refused>")
            import io as _io
            return _io.BytesIO(b'{"text": "ok"}')
        src = os.path.join(_TMP, "g.ogg")
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "0.3", src], check=True)
        self.assertEqual(hl.transcribe(src, post=post), "ok")
        log = open(pl.LOG_PATH).read()
        self.assertIn("stt_gpu_off", log.splitlines()[-2])


class Live(unittest.TestCase):
    """TIK-1025 step 2: a Mini App push-to-talk clip becomes a heard line + a queued turn."""
    def setUp(self):
        hl._q.clear(); hl._state["worker"] = object(); SENT.clear(); API.clear()
        hl.HEAD_THREADS[OWNER] = 777
        self.dir = tempfile.mkdtemp(dir=_TMP)
        self.got = []
        self._enq, hl._enqueue = hl._enqueue, lambda tok, m, t: self.got.append((m, t))
        self._api = pl._api
        pl._api = lambda tok, method, **kw: API.append((method, kw)) or {"ok": True, "result": {"message_id": 55}}

    def tearDown(self):
        hl._enqueue, pl._api = self._enq, self._api

    def job(self, uid=OWNER, frames=("/f/1.jpg", "/f/2.jpg")):
        p = os.path.join(self.dir, "1.json")
        json.dump({"uid": uid, "audio": "/a/1.webm", "frames": list(frames), "ts": 1}, open(p, "w"))
        return p

    def test_clip_becomes_a_spoken_turn_with_frames(self):
        p = self.job()
        self.assertTrue(hl.take_live("t", p, to_text=lambda a: "what is this"))
        self.assertEqual(os.listdir(self.dir), [])          # consumed, nothing left behind
        m, t = self.got[0]
        self.assertEqual((m["message_thread_id"], m["message_id"]), (777, 55))
        self.assertTrue(m["_from_voice"]); self.assertTrue(m["_live"])
        self.assertIn("what is this", t); self.assertIn("/f/2.jpg", t)
        self.assertEqual(API[0][1]["message_thread_id"], 777)
        self.assertIn("heard: what is this", API[0][1]["text"])

    def test_page_is_told_heard_or_unheard(self):
        st = []
        old, hl.live_status = hl.live_status, lambda s, t="": st.append((s, t))
        try:
            hl.take_live("t", self.job(), to_text=lambda a: " what is this ")
            hl.take_live("t", self.job(), to_text=lambda a: None)
        finally:
            hl.live_status = old
        self.assertEqual(st, [("heard", "what is this"), ("unheard", "")])
        hl.live_status("heard", "x", post=lambda p, b: (_ for _ in ()).throw(OSError("down")))   # never raises
        self.assertIn("live_status_fail", open(pl.LOG_PATH).read())

    def test_telegram_down_never_loses_the_turn(self):
        """21:05: a TLS timeout posting the heard line dropped Rav's whole Live turn."""
        def down(tok, method, **kw):
            raise OSError("_ssl.c:983: The handshake operation timed out")
        pl._api = down
        self.assertTrue(hl.take_live("t", self.job(), to_text=lambda a: "are you there"))
        m, t = self.got[0]
        self.assertIn("are you there", t)
        self.assertNotIn("message_id", m)                     # no heard line to reply to, but the turn is queued
        self.assertIn("live_heard_post_fail", open(pl.LOG_PATH).read())

    def test_stranger_refused(self):
        hl.HEAD_THREADS[USER_B] = 778                       # even with a thread on file, she is not the head's owner
        try:
            self.assertFalse(hl.take_live("t", self.job(uid=USER_B), to_text=lambda a: "hi"))
        finally:
            del hl.HEAD_THREADS[USER_B]
        self.assertEqual(self.got, []); self.assertEqual(API, [])

    def test_unheard_clip_said_not_queued(self):
        p = self.job()
        self.assertFalse(hl.take_live("t", p, to_text=lambda a: None))
        self.assertEqual(self.got, [])
        self.assertTrue(any("couldn't make out" in x for x in SENT))
        self.assertFalse(os.path.exists(p))

    def test_a_long_hold_brings_its_contact_sheet(self):
        p = os.path.join(self.dir, "2.json")
        json.dump({"uid": OWNER, "audio": "/a/2.webm", "frames": ["/f/a.jpg", "/f/z.jpg"], "sheet": "/t/2_sheet.jpg",
                   "hold_s": 61.4}, open(p, "w"))
        hl.take_live("t", p, to_text=lambda a: "here is the kitchen")
        t = self.got[0][1]
        self.assertIn("held the button 61 s", t)
        self.assertIn("Read it FIRST: /t/2_sheet.jpg", t)
        self.assertNotIn("/f/a.jpg", t)                                     # 19:56: a path = an attached image
        self.assertEqual(t.count(".jpg"), 3)                                # sheet path + 2 bare names, nothing else
        self.assertIn("2 single frames (oldest first) are in folder /f", t)

    def test_an_ended_turn_is_not_transcribed_twice(self):
        p = os.path.join(self.dir, "3.json")
        json.dump({"uid": OWNER, "audio": "/a/3.webm", "transcript": "[0:00] the kitchen\n[0:16] the dojo",
                   "frames": [], "sheet": "/t/3_sheet.jpg", "hold_s": 25}, open(p, "w"))
        def no(a):
            raise AssertionError("transcribed again")
        self.assertTrue(hl.take_live("t", p, to_text=no))
        t = self.got[0][1]
        self.assertIn("in several holds:\n[0:00] the kitchen\n[0:16] the dojo", t)

    def test_no_frames_is_said(self):
        hl.take_live("t", self.job(frames=()), to_text=lambda a: "hi")
        self.assertIn("no camera frames", self.got[0][1])

    def test_loop_takes_every_job_and_survives_a_bad_one(self):
        hl.LIVE_SPOOL, old = self.dir, hl.LIVE_SPOOL
        open(os.path.join(self.dir, "0.json"), "w").write("{broken")
        self.job()
        seen = []
        def stop(_):
            raise SystemExit
        old_t, hl.transcribe = hl.transcribe, lambda a: seen.append(a) or "hello"
        try:
            with self.assertRaises(SystemExit):
                hl._live_loop("t", sleep=stop)
        finally:
            hl.LIVE_SPOOL, hl.transcribe = old, old_t
        self.assertEqual(len(self.got), 1)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".json")], [])


class Ledger(unittest.TestCase):
    """TIK-1027: replies, forwards and answers in the lab chat history; context handed to the head."""
    def setUp(self):
        hl._q.clear(); hl._state["worker"] = object(); hl._state["running"] = False
        hl.HEAD_THREADS[OWNER] = 777
        SENT.clear(); API.clear()
        with sqlite3.connect(cl.CHATDB) as c:
            c.execute("DELETE FROM chat_log")

    def msg(self, mid, text=None, **kw):
        m = {"from": {"id": OWNER, "username": "alex_owner"}, "chat": {"id": OWNER, "type": "private"}, "message_id": mid,
             "message_thread_id": 777, "date": 1, "text": text}
        m.update(kw); return m

    def inbound(self, m, uid_):
        with sqlite3.connect(cl.CHATDB) as c:
            c.execute("INSERT INTO chat_log (session_id, source, role, content, ts, extra) VALUES (?,?,?,?,?,?)",
                      (f"tg_{uid_}", "telegram", "user", m.get("text") or "[media]", 100 + m["message_id"], '{"from": "alex_owner"}'))
        self.assertTrue(cl.enrich(f"tg_{uid_}", m))

    def test_topic_root_is_not_a_reply(self):
        m = self.msg(5, "hi", reply_to_message={"message_id": 777, "forum_topic_created": {"name": "Atlas"}})
        self.assertFalse(cl.is_real_reply(m))
        self.assertEqual(cl.context_block(m), "")
        # each guard alone holds: the service message marker, and the topic's root id
        self.assertFalse(cl.is_real_reply(self.msg(6, "hi", reply_to_message={"message_id": 1, "forum_topic_created": {"name": "x"}})))
        self.assertFalse(cl.is_real_reply(self.msg(7, "hi", reply_to_message={"message_id": 777})))

    def test_reply_to_an_answer_brings_it_and_its_neighbours(self):
        self.inbound(self.msg(10, "what plant is that"), 1)
        cl.record_out(OWNER, 777, 11, "That is a neon pothos.", now=111)
        self.inbound(self.msg(12, "thanks"), 2)
        m = self.msg(13, "are you sure?", reply_to_message={"message_id": 11, "from": {"id": 1, "is_bot": True},
                                                             "text": "That is a neon…"}, quote={"text": "neon pothos"})
        ctx = cl.context_block(m)
        self.assertIn("replying to message #11 by Atlas (you): That is a neon pothos.", ctx)   # full text from the ledger
        self.assertIn('highlighted part: "neon pothos"', ctx)
        self.assertIn("#10", ctx); self.assertIn("#12", ctx)
        hl.handle(m, "t")
        self.assertTrue(hl._q[0][1].startswith("[alex is replying"))
        self.assertTrue(hl._q[0][1].endswith("are you sure?"))
        hl._q.clear()

    def test_forward_is_named(self):
        m = self.msg(20, "look", forward_origin={"type": "user", "sender_user": {"first_name": "Sam"}, "date": 1700000000})
        self.assertIn("originally from Sam", cl.context_block(m))
        self.assertEqual(cl.extra_of(m)["forward"]["from"], "Sam")

    def test_reply_to_a_video_hands_over_the_video(self):
        started = []
        m = self.msg(30, "identify this plant", reply_to_message={"message_id": 29, "from": {"id": OWNER},
                                                                   "video_note": {"file_id": "VN"}})
        self.assertTrue(hl.handle(m, "t", prep=lambda tok, mm: started.append(mm)))
        self.assertEqual(len(hl._q), 0)
        got = started[0]
        self.assertEqual((got["video_note"]["file_id"], got["caption"]), ("VN", "identify this plant"))
        self.assertIn("replying to message #29", got["_ctx"])

    def test_prepared_media_keeps_the_context(self):
        got = []
        m = dict(self.msg(31), video={"file_id": "v"}, _ctx="[alex is replying to message #29]")
        hl._prep_media("t", m, to_text=lambda tok, mm: "video prompt", enqueue=lambda tok, mm, t: got.append(t))
        self.assertEqual(got, ["[alex is replying to message #29]\n\nvideo prompt"])

    def test_answers_are_recorded(self):
        io = hl._IO("t", self.msg(40, "q"))
        io._answer = lambda md: [41, 42]
        io.answer("long answer")
        b, target, a = cl.around(OWNER, 41, 777)
        self.assertEqual((target["role"], target["text"], target["reply_to"]), ("assistant", "long answer", 40))
        self.assertIsNotNone(cl.find(OWNER, 42, 777))

    def test_ledger_down_never_blocks_a_message(self):
        old, cl.CHATDB = cl.CHATDB, "/nonexistent/dir/x.db"
        try:
            m = self.msg(50, "still here", reply_to_message={"message_id": 49, "from": {"id": OWNER}, "text": "earlier"})
            hl.handle(m, "t")
        finally:
            cl.CHATDB = old
        self.assertTrue(hl._q[0][1].endswith("still here"))
        self.assertIn("earlier", hl._q[0][1])              # the reply's own text still arrives
        hl._q.clear()
        def boom(m):
            raise sqlite3.DatabaseError("disk I/O error")
        old, cl.context_block = cl.context_block, boom
        try:
            hl.handle(self.msg(51, "and this"), "t")
        finally:
            cl.context_block = old
        self.assertEqual(hl._q[0][1], "and this")
        hl._q.clear()

    def test_answer_replies_into_the_chain(self):
        old = pl._api
        n = [0]
        def api(tok, method, **kw):
            API.append((method, kw)); n[0] += 1
            return {"ok": True, "result": {"message_id": 1000 + n[0]}}
        pl._api = api
        self.addCleanup(setattr, pl, "_api", old)
        io = hl._IO("t", self.msg(70, "q"))
        io.answer("short")
        rich = [kw for meth, kw in API if meth == "sendRichMessage"][0]
        self.assertEqual(rich["reply_parameters"]["message_id"], 70)
        API.clear()
        big = "\n\n".join(["word " * 700] * 10)                   # long: HTML parts, only the first is the reply
        self.assertGreater(len(big), hl.RICH_LIMIT)
        io.answer(big)
        parts = [kw for meth, kw in API if meth == "sendMessage"]
        self.assertGreater(len(parts), 1)
        self.assertEqual(parts[0]["reply_parameters"]["message_id"], 70)
        self.assertTrue(all("reply_parameters" not in p for p in parts[1:]))

    def edit(self, mid, text):
        return {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": mid, "message_thread_id": 777, "text": text}

    def test_edit_while_queued_replaces_the_text(self):
        self.inbound(self.msg(80, "wipe the board"), 8)
        hl._q.append((dict(self.msg(80, "wipe the board"), _ctx="[ctx]"), "[ctx]\n\nwipe the board"))
        self.assertEqual(hl.on_edit(self.edit(80, "welp the board")), "requeued")
        self.assertTrue(hl._q[0][1].startswith("[ctx]\n\n[alex edited this message before you saw it; the first version was: \"wipe the board\"] "), hl._q[0][1])
        self.assertTrue(hl._q[0][1].endswith("welp the board"), hl._q[0][1])
        r = cl.find(OWNER, 80, 777)
        self.assertEqual((r["text"], r["edits"][0]["was"]), ("welp the board", "wipe the board"))
        hl._q.clear()

    def test_edit_after_the_answer_is_told_once(self):
        p = os.path.join(_TMP, "edits_t.json")
        if os.path.exists(p):
            os.remove(p)
        self.inbound(self.msg(81, "wipe"), 9)
        self.assertEqual(hl.on_edit(self.edit(81, "welp"), path=p), "noted")
        n = hl.pending_edits(path=p)
        self.assertIn('#81: "wipe" -> "welp"', n)
        self.assertEqual(hl.pending_edits(path=p), "")

    def test_edit_mid_turn_is_a_note(self):
        hl._state["running"] = True
        try:
            self.assertEqual(hl.on_edit(self.edit(82, "fixed")), "midturn")
        finally:
            hl._state["running"] = False
        self.assertIn("fixed", open(hl.MIDTURN).read())

    def test_edit_after_the_answer_is_its_own_turn_with_the_full_text(self):          # TIK-1044
        got = []
        long = "the thing I wanted was cut off at it'd be fast and the rest was " + "x" * 900 + " END"
        self.inbound(self.msg(85, "the thing I wanted was cut off at it'd be fast"), 10)
        r = hl.on_edit(self.edit(85, long), tok="T", enqueue=lambda tok, m, t: got.append((tok, m, t)))
        self.assertEqual(r, "turn")
        tok, m, t = got[0]
        self.assertEqual((tok, m["message_id"], m["message_thread_id"]), ("T", 85, 777))
        self.assertIn("alex edited their earlier message #85", t)
        self.assertIn("it'd be fast", t)                      # what the first version said
        self.assertTrue(t.rstrip().endswith("END"), t[-60:])  # the WHOLE new text, not a preview
        self.assertEqual(m["text"], long)

    def test_edit_that_changes_nothing_is_not_a_turn(self):                          # TIK-1044
        got = []
        self.inbound(self.msg(86, "same words"), 11)
        self.assertEqual(hl.on_edit(self.edit(86, "same  words"), tok="T", enqueue=lambda *a: got.append(a)), "unchanged")
        self.assertEqual(got, [])

    def test_edit_turn_that_cannot_be_queued_is_noted_not_lost(self):                # TIK-1044
        p = os.path.join(_TMP, "edits_t2.json")
        if os.path.exists(p):
            os.remove(p)
        self.inbound(self.msg(87, "old words"), 12)
        def boom(*a):
            raise RuntimeError("queue down")
        self.assertEqual(hl.on_edit(self.edit(87, "new words"), path=p, tok="T", enqueue=boom), "noted")
        self.assertIn('#87: "old words" -> "new words"', hl.pending_edits(path=p))

    def test_mid_turn_edit_keeps_the_long_text(self):                                # TIK-1044: the hook preview cut it at 600/300
        hl._state["running"] = True
        long = "start " + "y" * 1200 + " TAIL"
        try:
            self.inbound(self.msg(88, "start"), 13)
            self.assertEqual(hl.on_edit(self.edit(88, long), tok="T", enqueue=lambda *a: self.fail("must not start a turn mid-turn")), "midturn")
        finally:
            hl._state["running"] = False
        self.assertIn("TAIL", open(hl.MIDTURN).read().splitlines()[-1])

    def test_edits_by_others_or_elsewhere_ignored(self):
        e = self.edit(83, "x"); e["from"] = {"id": USER_B}
        self.assertIsNone(hl.on_edit(e))
        e = self.edit(84, "x"); e["message_thread_id"] = 5
        self.assertIsNone(hl.on_edit(e))

    def test_choice_tap_becomes_a_turn_once(self):
        hl._jsave(hl.BUTTONS, {"k1": {"text": "Which pumpkin?", "labels": ["Left", "Right"]}})
        cq = {"id": "c", "from": {"id": OWNER}, "message": {"chat": {"id": OWNER, "type": "private"}, "message_id": 90, "message_thread_id": 777}}
        said, got = [], []
        self.assertEqual(hl.on_choice_button(cq, "hd:u:k1:1", "t", said.append, enqueue=lambda tok, m, t: got.append((m, t))), "chosen")
        self.assertEqual(got[0][1], "[alex tapped “Right” on your question: “Which pumpkin?”]")
        self.assertEqual((got[0][0]["message_id"], got[0][0]["message_thread_id"]), (90, 777))
        self.assertIn("✅ Right", [kw for meth, kw in API if meth == "editMessageText"][0]["text"])
        self.assertIsNone(hl.on_choice_button(cq, "hd:u:k1:0", "t", said.append, enqueue=lambda *a: got.append(a)))
        self.assertEqual(len(got), 1)                                       # answered once
        self.assertIn("already answered", said[-1])

    def test_choice_tap_routed_from_the_gateway_callback(self):
        hl._jsave(hl.BUTTONS, {"k3": {"text": "Go?", "labels": ["Yes", "No"]}})
        cq = {"id": "c", "from": {"id": OWNER}, "message": {"chat": {"id": OWNER, "type": "private"}, "message_id": 92, "message_thread_id": 777}}
        hl.on_button(cq, "hd:u:k3:0", "t")
        self.assertEqual(hl._q[-1][1], "[alex tapped “Yes” on your question: “Go?”]")
        hl._q.clear()

    def test_choice_tap_by_anyone_else_refused(self):
        hl._jsave(hl.BUTTONS, {"k2": {"text": "q", "labels": ["A", "B"]}})
        cq = {"id": "c", "from": {"id": USER_B}, "message": {"chat": {"id": OWNER, "type": "private"}, "message_id": 91}}
        said, got = [], []
        self.assertIsNone(hl.on_choice_button(cq, "hd:u:k2:0", "t", said.append, enqueue=lambda *a: got.append(a)))
        self.assertEqual((got, said), ([], ["Not yours."]))
        self.assertIn("k2", hl._jload(hl.BUTTONS, {}))                      # still open for Rav

    def test_other_threads_not_mixed_in(self):
        cl.record_out(OWNER, 999, 61, "other thread", now=1)
        cl.record_out(OWNER, 777, 62, "this thread", now=2)
        b, t, a = cl.around(OWNER, 62, 777)
        self.assertEqual(b, [])



class Relay(unittest.TestCase):
    """/j through a lane bot (TIK-1083): the job becomes a turn whose Telegram side is that bot."""
    def setUp(self):
        import jrelay as jr
        self.jr = jr
        self.spool = tempfile.mkdtemp(prefix="relay_spool_")
        jr.LOG = os.path.join(self.spool, "relay.log")
        self.old_tok = jr.token_for
        jr.token_for = lambda origin, env_path=None: "SHOP-TOKEN" if origin == "shop" else self.old_tok(origin)

    def tearDown(self):
        self.jr.token_for = self.old_tok

    def _job(self, uid=OWNER, chat_type="private"):
        m = {"message_id": 9001, "from": {"id": uid}, "chat": {"id": uid, "type": chat_type}}
        return self.jr.submit("shop", m, "add a thing to Shop", spool=self.spool)

    def test_take_relay_brings_the_photo(self):
        m = {"message_id": 9002, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "photo": [{"file_id": "P"}]}
        path = self.jr.submit("shop", m, "Sam says this", spool=self.spool)
        got, real = [], hl.relay_media
        hl.relay_media = lambda job: job["text"] + " / [photo " + job["media"]["file_id"] + "]"
        try:
            hl.take_relay("HEAD-TOKEN", path, enqueue=lambda tok, m, text: got.append(text))
        finally:
            hl.relay_media = real
        self.assertTrue(got[0].endswith("Sam says this / [photo P]"))

    def test_take_relay_enqueues_through_the_origin(self):
        got = []
        self.assertTrue(hl.take_relay("HEAD-TOKEN", self._job(), enqueue=lambda tok, m, text: got.append((tok, m, text))))
        tok, m, text = got[0]
        self.assertEqual(m["_relay"], {"origin": "shop"})
        self.assertEqual((m["chat"]["id"], m["message_id"], m["from"]["id"]), (OWNER, 9001, OWNER))
        self.assertIn("through Shop", text)
        self.assertTrue(text.endswith("add a thing to Shop"))
        self.assertEqual(hl._tok_for(m, "HEAD-TOKEN"), "SHOP-TOKEN")
        self.assertEqual(hl._tok_for({"chat": {"id": 1, "type": "private"}}, "HEAD-TOKEN"), "HEAD-TOKEN")
        self.assertEqual(os.listdir(self.spool), ["relay.log"])             # consumed

    def _rows(self, like):
        with sqlite3.connect(cl.CHATDB) as c:
            return [(r, json.loads(x)) for r, x in c.execute(
                "SELECT role, extra FROM chat_log WHERE session_id LIKE ? ORDER BY id", (like,)).fetchall()]

    def test_relay_both_sides_land_in_chat_history(self):
        """alex 2026-10-30: every /j exchange is in the lab chat history, tagged with the bot it went through."""
        self.assertTrue(hl.take_relay("HEAD-TOKEN", self._job(), enqueue=lambda *a: None))
        rows = self._rows("tgj_shop_%_9001")
        self.assertEqual(len(rows), 1)
        role, x = rows[0]
        self.assertEqual(role, "user")
        self.assertEqual((x["origin"], x["bot"], x["via"], x["message_id"], x["chat_id"]), ("shop", "Shop", "/j", 9001, OWNER))
        self.assertFalse(cl.record_in({"message_id": 9001, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}}, "again", "shop"))  # once
        m = {"message_id": 9001, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "_relay": {"origin": "shop"}}
        real = pl._api; pl._api = lambda tok, method, **kw: {"ok": True, "result": {"message_id": 4711}}
        try:
            ids = hl._IO("SHOP-TOKEN", m).answer("Done.")
        finally:
            pl._api = real
        self.assertEqual(ids, [4711])
        out = [x for r, x in self._rows("tgout_%") if x.get("message_id") in ids]
        self.assertTrue(out)
        self.assertEqual((out[-1]["origin"], out[-1]["bot"], out[-1]["reply_to"]), ("shop", "Shop", 9001))

    def test_ledger_failure_never_drops_the_turn(self):
        real = cl.record_in; cl.record_in = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
        got = []
        try:
            self.assertTrue(hl.take_relay("HEAD-TOKEN", self._job(), enqueue=lambda *a: got.append(a)))
        finally:
            cl.record_in = real
        self.assertEqual(len(got), 1)

    def test_take_relay_refuses_a_forged_job(self):
        p = os.path.join(self.spool, "x_shop_1.json")
        with open(p, "w") as f:
            json.dump({"origin": "shop", "uid": USER_B, "chat_id": USER_B, "chat_type": "private",
                       "message_id": 1, "text": "hi"}, f)
        got = []
        self.assertFalse(hl.take_relay("HEAD-TOKEN", p, enqueue=lambda *a: got.append(a)))
        self.assertEqual(got, [])
        self.assertFalse(os.path.exists(p))

    def test_relayed_answer_is_prefixed_and_sent_with_the_origin_token(self):
        del API[:]
        m = {"message_id": 9001, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "_relay": {"origin": "shop"}}
        io = hl._IO("SHOP-TOKEN", m)
        io.answer("Done. Shop now does the thing.")
        rich = [kw for meth, kw in API if meth == "sendRichMessage"]
        self.assertEqual(len(rich), 1)
        self.assertTrue(rich[0]["rich_message"]["markdown"].startswith("Atlas says: Done."), rich[0]["rich_message"]["markdown"][:60])
        self.assertEqual(rich[0]["chat_id"], OWNER)
        self.assertEqual(rich[0]["reply_parameters"]["message_id"], 9001)
        plain = hl._IO("SHOP-TOKEN", {"message_id": 1, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}})
        del API[:]
        plain.answer("Hi")
        rich = [kw for meth, kw in API if meth == "sendRichMessage"]
        self.assertFalse(rich[-1]["rich_message"]["markdown"].startswith("Atlas says:"))

    # ---- "/j stop" (TIK-1083, 2026-10-29): a control job, not a queued turn
    def _stop_setup(self):
        calls, replies, marks = [], [], []
        self._old = (hl.mark_stopped, list(hl._q))
        hl.mark_stopped = lambda *a, **k: marks.append(1)
        hl._q.clear()
        mine = {"_relay": {"origin": "shop"}, "chat": {"id": OWNER, "type": "private"}}
        other = {"_relay": {"origin": "crm"}, "chat": {"id": OWNER, "type": "private"}}
        hl._q.extend([(mine, "a"), ({"chat": {"id": OWNER, "type": "private"}}, "plain"), (other, "b"), (mine, "c")])
        return calls, replies, marks

    def _stop_teardown(self):
        hl.mark_stopped = self._old[0]
        hl._q.clear(); hl._q.extend(self._old[1])

    def _stop_job(self, text="/stop"):
        m = {"message_id": 9002, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}}
        return self.jr.submit("shop", m, text, spool=self.spool)

    def test_j_stop_interrupts_clears_this_origin_and_says_how_many(self):
        calls, replies, marks = self._stop_setup()
        try:
            later = self.jr.submit("shop", {"message_id": 9003, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}},
                                   "a later message", spool=self.spool)
            p = self._stop_job("STOP")
            got = []
            ok = hl.take_relay("HEAD-TOKEN", p, enqueue=lambda *a: got.append(a), call=lambda f: calls.append(f) or {})
            self.assertTrue(ok)
            self.assertEqual(got, [])                                               # never queued as a turn
            self.assertEqual(calls, [{"t": "key", "key": "Escape"}])
            self.assertEqual(marks, [1])
            left = [t for _, t in hl._q]
            self.assertEqual(left, ["plain", "b"])                                  # other origins untouched
            self.assertFalse(os.path.exists(later))                                 # waiting spool file dropped
        finally:
            self._stop_teardown()

    def test_relay_stop_reply_counts_and_uses_the_origin_token(self):
        calls, replies, marks = self._stop_setup()
        try:
            job = {"origin": "shop", "uid": OWNER, "chat_id": OWNER, "chat_type": "private", "message_id": 9002, "text": "stop"}
            hl.relay_stop(job, call=lambda f: {}, spool=self.spool, reply=lambda tok, m, t: replies.append((tok, m, t)))
            tok, m, t = replies[0]
            self.assertEqual(tok, "SHOP-TOKEN")
            self.assertEqual((m["chat"]["id"], m["message_id"]), (OWNER, 9002))
            self.assertEqual(t, "⏹ Stopped Atlas (Shop) — 2 queued message(s) cleared.")
        finally:
            self._stop_teardown()

    def test_stop_the_music_is_a_normal_turn(self):
        calls, replies, marks = self._stop_setup()
        try:
            got = []
            hl.take_relay("HEAD-TOKEN", self._stop_job("stop the music"), enqueue=lambda *a: got.append(a),
                          call=lambda f: calls.append(f) or {})
            self.assertEqual(len(got), 1)
            self.assertEqual(calls, [])
            self.assertEqual(marks, [])
        finally:
            self._stop_teardown()

    def test_a_cleared_spool_file_is_skipped(self):
        self.assertFalse(hl.take_relay("HEAD-TOKEN", os.path.join(self.spool, "gone_shop_1.json"), enqueue=lambda *a: 1/0))


class RelayButton(unittest.TestCase):
    """alex 2026-11-05: the Desk "Yes" button did nothing. A relayed press is handled like a topic press."""
    def job(self, data="hd:p:0123456789ab:a"):
        return {"origin": "desk", "uid": OWNER, "chat_id": OWNER, "chat_type": "private", "message_id": 9, "text": "",
                "button": {"id": "77", "data": data, "message_id": 9, "text": "Proceed?",
                           "markup": [[{"text": "Yes", "callback_data": data}]]}}

    def test_press_reaches_on_button_with_the_lane_token(self):
        got = []
        orig = hl.jrelay.token_for
        hl.jrelay.token_for = lambda o: "LC-TOKEN"
        try:
            self.assertTrue(hl.relay_button(self.job(), press=lambda cq, d, tok, **k: got.append((cq, d, tok))))
        finally:
            hl.jrelay.token_for = orig
        cq, d, tok = got[0]
        self.assertEqual((d, tok, cq["from"]["id"], cq["id"]), ("hd:p:0123456789ab:a", "LC-TOKEN", OWNER, "77"))
        self.assertEqual(cq["message"]["_relay"], {"origin": "desk"})
        self.assertEqual(cq["message"]["reply_markup"]["inline_keyboard"][0][0]["text"], "Yes")

    def test_failed_press_is_said(self):
        said = []
        o1, o2 = hl.jrelay.token_for, hl.tgapi._reply
        hl.jrelay.token_for = lambda o: "T"
        hl.tgapi._reply = lambda tok, m, text: said.append(text)
        try:
            def boom(*a, **k):
                raise RuntimeError("bridge down")
            hl.relay_button(self.job(), press=boom)
        finally:
            hl.jrelay.token_for, hl.tgapi._reply = o1, o2
        self.assertTrue(said and "bridge down" in said[0])

    def test_take_relay_routes_a_button_job(self):
        seen = []
        o1, o2 = hl.jrelay.take, hl.relay_button
        hl.jrelay.take = lambda p: self.job()
        hl.relay_button = lambda job, call=None: seen.append(job) or True
        try:
            p = os.path.join(tempfile.mkdtemp(), "x.json"); open(p, "w").write("{}")
            self.assertTrue(hl.take_relay("t", p, enqueue=lambda *a: seen.append("queued")))
        finally:
            hl.jrelay.take, hl.relay_button = o1, o2
        self.assertEqual(len(seen), 1); self.assertIn("button", seen[0])


class RenameHead(unittest.TestCase):
    """TIK-1454: a topic rename updates the title; /renamehead moves the lane; the gateway swaps its loaded module."""

    class Lane:
        def __init__(self, name, tid):
            self._LANE, self.HEAD_THREADS = name, {OWNER: tid}

    class NL:
        MADE_BY = "m.json"
        def __init__(self, why=None, busy=None, title="Old"):
            self.why, self.b, self.title = why, busy, title
        def removable(self, lane, tid=None): return self.why
        def busy(self, lane): return self.b
        def slug(self, t, width=16): return t.lower().replace(" ", "-")[:width]
        def _jload(self, p, d): return {"title": self.title}

    def _m(self, text="/renamehead Net DNS", uid=OWNER, tid=777001, **kw):
        return dict({"message_id": 9, "from": {"id": uid}, "chat": {"id": uid, "type": "private"},
                     "message_thread_id": tid, "text": text}, **kw)

    def setUp(self):
        self.lane = self.Lane("garden", 777001)
        self.before = list(hl.OTHERS); hl.OTHERS.append(self.lane)
        self.started = []; self.start = lambda f, *a: self.started.append((f, a))
        SENT.clear(); hl._RENAMING.clear()

    def tearDown(self):
        hl.OTHERS[:] = self.before; hl._RENAMING.clear()

    def test_command_starts_a_worker_and_blocks_the_thread(self):
        self.assertTrue(hl.rename_head_cmd(self._m(), "tok", nl=self.NL(), start=self.start))
        self.assertEqual(self.started[0][1][3:], ("net-dns", "Net DNS"))
        self.assertIn(777001, hl._RENAMING)

    def test_refused_when_busy_or_not_allowed(self):
        for nl, word in ((self.NL(busy="in the middle of a turn"), "middle"), (self.NL(why="built by hand"), "by hand")):
            self.assertTrue(hl.rename_head_cmd(self._m(), "tok", nl=nl, start=self.start))
            self.assertIn(word, SENT[-1])
        self.assertEqual(self.started, []); self.assertEqual(hl._RENAMING, set())

    def test_no_name_asks(self):
        self.assertTrue(hl.rename_head_cmd(self._m("/renamehead"), "tok", nl=self.NL(title="Garden"), start=self.start))
        self.assertEqual(self.started, []); self.assertIn("new name", SENT[-1])

    def test_uses_stored_title_with_no_argument(self):
        self.assertTrue(hl.rename_head_cmd(self._m("/renamehead"), "tok", nl=self.NL(title="Fresh One"), start=self.start))
        self.assertEqual(self.started[0][1][3:], ("fresh-one", "Fresh One"))

    def test_not_rav_or_not_the_command(self):
        self.assertTrue(hl.rename_head_cmd(self._m(uid=999), "tok", nl=self.NL(), start=self.start))
        self.assertEqual(self.started, [])
        self.assertFalse(hl.rename_head_cmd(self._m("/renameheads x"), "tok", nl=self.NL(), start=self.start))
        self.assertFalse(hl.rename_head_cmd(self._m("rename the head"), "tok", nl=self.NL(), start=self.start))

    def test_success_swaps_the_lane(self):
        class R: stdout = '{"ok": true, "lane": "net-dns"}\n'
        loaded = []; real = hl.add_lane; hl.add_lane = lambda n: loaded.append(n)
        hl._RENAMING.add(777001)
        try:
            res = hl._rename_head("tok", self._m(), self.lane, "net-dns", "Net DNS", run=lambda a, **k: R())
        finally:
            hl.add_lane = real
        self.assertTrue(res["ok"]); self.assertEqual(loaded, ["net-dns"])
        self.assertNotIn(self.lane, hl.OTHERS); self.assertEqual(hl._RENAMING, set()); self.assertIn("Renamed", SENT[-1])

    def test_restored_failure_reloads_old_lane(self):
        class R: stdout = '{"ok": false, "step": "bridge", "why": "x", "restored": true}\n'
        hl._RENAMING.add(777001)
        hl._rename_head("tok", self._m(), self.lane, "n", "N", run=lambda a, **k: R())
        self.assertIn(self.lane, hl.OTHERS); self.assertEqual(hl._RENAMING, set()); self.assertIn("put everything back", SENT[-1])

    def test_unfinished_undo_is_loud(self):
        class R: stdout = '{"ok": false, "step": "bridge", "why": "x", "restored": false}\n'
        hl._rename_head("tok", self._m(), self.lane, "n", "N", run=lambda a, **k: R())
        self.assertNotIn(self.lane, hl.OTHERS); self.assertIn("no working head", SENT[-1])

    def test_message_during_rename_is_told_to_resend(self):
        hl._RENAMING.add(777001)
        self.assertTrue(hl.handle(self._m("hello"), "tok"))
        self.assertIn("send it again", SENT[-1])

    def test_handle_routes_a_topic_edit(self):
        real = hl.topic_edited; seen = []
        hl.topic_edited = lambda m, tok: seen.append(m) or True
        try:
            m = self._m(text=None, forum_topic_edited={"name": "X"})
            self.assertTrue(hl.handle(m, "tok")); self.assertEqual(seen, [m])
        finally:
            hl.topic_edited = real

    def test_rename_asks_for_the_wide_slug(self):
        self.assertTrue(hl.rename_head_cmd(self._m("/renamehead Network DNS Firewall"), "tok", nl=self.NL(), start=self.start))
        self.assertEqual(self.started[0][1][3], "network-dns-firewall")

    def test_topic_edit_retitles(self):
        argv = []
        class R: stdout = '{"ok": true}\n'
        m = self._m(text=None, forum_topic_edited={"name": "Net DNS"})
        self.assertTrue(hl.topic_edited(m, "tok", run=lambda a, **k: argv.append(a) or R()))
        self.assertEqual(argv[0][-5:], ["retitle", "--lane", "garden", "--title", "Net DNS"]); self.assertIn("Net DNS", SENT[-1])

    def test_topic_edit_failure_said_and_icon_only_ignored(self):
        class R: stdout = '{"ok": false, "why": "bad"}\n'
        hl.topic_edited(self._m(text=None, forum_topic_edited={"name": "X"}), "tok", run=lambda a, **k: R())
        self.assertIn("couldn't", SENT[-1])
        n = []
        self.assertTrue(hl.topic_edited(self._m(text=None, forum_topic_edited={"icon_custom_emoji_id": "1"}), "tok", run=lambda a, **k: n.append(a)))
        self.assertEqual(n, [])
        self.assertFalse(hl.topic_edited(self._m(), "tok"))


class DeleteThreadTest(unittest.TestCase):
    """alex 2026-10-30: /deletethread removes a new_lane-made topic's head (archived) and the topic itself."""

    class Lane:
        def __init__(self, name, tid):
            self._LANE, self.HEAD_THREADS = name, {OWNER: tid}

    class NL:
        def __init__(self, why=None):
            self.why, self.asked = why, []
        def removable(self, lane, tid=None):
            self.asked.append((lane, tid)); return self.why

    def _m(self, text="/deletethread", uid=OWNER, tid=777001, chat=None):
        return {"message_id": 9, "from": {"id": uid}, "chat": chat or {"id": uid, "type": "private"},
                "message_thread_id": tid, "text": text}

    def _cq(self, data, uid=OWNER):
        return {"id": "q", "from": {"id": uid}, "data": data,
                "message": {"message_id": 50, "chat": {"id": uid, "type": "private"}, "text": "Delete?", "message_thread_id": 777001}}

    def setUp(self):
        self.lane = self.Lane("garden", 777001)
        self.before = list(hl.OTHERS); hl.OTHERS.append(self.lane)
        self.old_threads = dict(hl.HEAD_THREADS); hl.HEAD_THREADS[OWNER] = 9100
        self.cards, self.api, self.started = [], [], []
        SENT.clear()
        self.card = lambda tok, m, text, kb: self.cards.append((text, kb))
        self.apif = lambda tok, meth, **kw: self.api.append((meth, kw)) or {"ok": True}
        self.start = lambda f, *a: self.started.append((f, a))

    def tearDown(self):
        hl.OTHERS[:] = self.before
        hl.HEAD_THREADS.clear(); hl.HEAD_THREADS.update(self.old_threads)

    def test_asks_with_buttons_for_a_new_lane_topic(self):
        nl = self.NL()
        self.assertTrue(hl.delete_thread_cmd(self._m(), "tok", nl=nl, card=self.card))
        self.assertEqual(nl.asked, [("garden", 777001)])
        datas = [b["callback_data"] for b in self.cards[0][1]["inline_keyboard"][0]]
        self.assertEqual(datas, ["hd:dt:y:garden:777001", "hd:dt:n:garden:777001"])

    def test_refuses_main_thread(self):
        nl = self.NL()
        self.assertTrue(hl.delete_thread_cmd(self._m(tid=9100), "tok", nl=nl, card=self.card))
        self.assertEqual(self.cards, []); self.assertIn("main Atlas", SENT[-1]); self.assertEqual(nl.asked, [])

    def test_refuses_hand_built_lane(self):
        self.assertTrue(hl.delete_thread_cmd(self._m(), "tok", nl=self.NL("lane garden was built by hand"), card=self.card))
        self.assertEqual(self.cards, []); self.assertIn("built by hand", SENT[-1])

    def test_topic_without_head(self):
        self.assertTrue(hl.delete_thread_cmd(self._m(tid=4242), "tok", nl=self.NL(), card=self.card))
        self.assertEqual(self.cards, []); self.assertIn("no head", SENT[-1])

    def test_not_rav_or_not_his_dm_is_not_a_delete(self):
        self.assertFalse(hl.delete_thread_cmd(self._m(uid=USER_B, chat={"id": USER_B, "type": "private"}), "tok",
                                              nl=self.NL(), card=self.card))
        self.assertFalse(hl.delete_thread_cmd(self._m(chat={"id": -100, "type": "supergroup"}), "tok", nl=self.NL(), card=self.card))
        self.assertEqual(self.cards, [])

    def test_other_text_untouched(self):
        for t in ("/deletethreads", "delete this thread", "/deletethread now"):
            self.assertFalse(hl.delete_thread_cmd(self._m(text=t), "tok", nl=self.NL(), card=self.card), t)

    def test_no_button_keeps_it(self):
        hl.delete_thread_button(self._cq("hd:dt:n:garden:777001"), "hd:dt:n:garden:777001", "tok", api=self.apif, start=self.start)
        self.assertEqual(self.started, []); self.assertIn(self.lane, hl.OTHERS)

    def test_yes_button_only_for_rav(self):
        hl.delete_thread_button(self._cq("hd:dt:y:garden:777001", uid=USER_B), "hd:dt:y:garden:777001", "tok",
                                api=self.apif, start=self.start)
        self.assertEqual(self.started, [])
        hl.delete_thread_button(self._cq("hd:dt:y:garden:777001"), "hd:dt:y:garden:777001", "tok", api=self.apif, start=self.start)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0][1][1:4], (OWNER, 777001, self.lane))

    def test_yes_for_wrong_thread_does_nothing(self):
        hl.delete_thread_button(self._cq("hd:dt:y:garden:999"), "hd:dt:y:garden:999", "tok", api=self.apif, start=self.start)
        self.assertEqual(self.started, [])

    def test_removed_then_unloaded_then_topic_deleted(self):
        class R: stdout = '{"ok": true, "lane": "garden", "thread": 777001, "archive": "/a/garden-1"}\n'
        argv = []
        res = hl._delete_thread("tok", OWNER, 777001, self.lane, run=lambda a, **k: argv.append(a) or R(), api=self.apif)
        self.assertTrue(res["ok"])
        self.assertEqual(argv[0][-5:], ["--lane", "garden", "--thread", "777001", "--json"])
        self.assertNotIn(self.lane, hl.OTHERS)
        meths = [m for m, _ in self.api]
        self.assertIn("deleteForumTopic", meths)
        self.assertEqual(dict(self.api)["deleteForumTopic"]["message_thread_id"], 777001)
        said = [kw for m, kw in self.api if m == "sendMessage"]
        self.assertEqual(said[-1]["message_thread_id"], 9100); self.assertIn("/a/garden-1", said[-1]["text"])

    def test_remove_failure_keeps_topic_and_lane(self):
        class R: stdout = '{"ok": false, "lane": "garden", "step": "unit", "why": "stop: boom"}\n'
        res = hl._delete_thread("tok", OWNER, 777001, self.lane, run=lambda a, **k: R(), api=self.apif)
        self.assertFalse(res["ok"]); self.assertIn(self.lane, hl.OTHERS)
        self.assertNotIn("deleteForumTopic", [m for m, _ in self.api])
        said = [kw for m, kw in self.api if m == "sendMessage"]
        self.assertEqual(said[-1]["message_thread_id"], 777001); self.assertIn("unit", said[-1]["text"])

    def test_script_crash_said(self):
        res = hl._delete_thread("tok", OWNER, 777001, self.lane, run=lambda a, **k: (_ for _ in ()).throw(OSError("boom")),
                                api=self.apif)
        self.assertFalse(res["ok"]); self.assertIn(self.lane, hl.OTHERS)

    def test_on_button_routes_dt(self):
        seen = []
        real = hl.delete_thread_button; hl.delete_thread_button = lambda cq, data, tok: seen.append(data)
        try:
            hl.on_button(self._cq("hd:dt:n:garden:777001"), "hd:dt:n:garden:777001", "tok")
        finally:
            hl.delete_thread_button = real
        self.assertEqual(seen, ["hd:dt:n:garden:777001"])

    def test_handle_runs_delete_before_lanes(self):
        real = hl.delete_thread_cmd; seen = []
        hl.delete_thread_cmd = lambda m, tok: seen.append(m) or True
        try:
            self.assertTrue(hl.handle(self._m(), "tok"))
        finally:
            hl.delete_thread_cmd = real
        self.assertEqual(len(seen), 1)

    def test_real_new_lane_refuses_hand_built(self):
        nl = hl._new_lane_mod()
        self.assertIsNotNone(nl.removable("techsup", None))
        self.assertIsNotNone(nl.removable("hb", 9100))



class RetryDrop(unittest.TestCase):
    """a dropped API connection is retried once by itself; a second drop within 60 s offers a button."""
    def setUp(self):
        if os.path.exists(hl.RETRIES): os.remove(hl.RETRIES)
        hl._state["auto_retry_ts"] = 0
        self.q, self.cards, self.said = [], [], []
        self._o = (hl._send_card, pl._api)
        hl._send_card = lambda tok, m, text, kb: self.cards.append((text, kb))
        pl._api = lambda *a, **k: {}
        class IO:
            tok = "t"
            def say(io, t): self.said.append(t)
        self.io = IO()
        self.m = {"chat": {"id": 111111111}, "from": {"id": 111111111}, "message_id": 5}
        self.enq = lambda tok, m, text: self.q.append((m, text))

    def tearDown(self):
        hl._send_card, pl._api = self._o

    def test_which_errors_retry(self):
        self.assertTrue(hl.retryable("server_error"))
        self.assertTrue(hl.retryable("API Error: Connection lost mid-response"))
        self.assertFalse(hl.retryable("the head stopped with an error"))
        self.assertFalse(hl.retryable(None))

    def test_first_drop_retries_once_and_says_so(self):
        self.assertEqual(hl.retry_or_offer(self.io, self.m, "server_error", now=lambda: 1000, enqueue=self.enq), "retried")
        self.assertEqual(len(self.q), 1); self.assertIn("cut off by a connection error", self.q[0][1])
        self.assertIn("Trying once more", self.said[0]); self.assertEqual(self.cards, [])

    def test_second_drop_within_a_minute_offers_a_button(self):
        hl.retry_or_offer(self.io, self.m, "server_error", now=lambda: 1000, enqueue=self.enq)
        self.assertEqual(hl.retry_or_offer(self.io, self.m, "server_error", now=lambda: 1030, enqueue=self.enq), "offered")
        self.assertEqual(len(self.q), 1)                                   # no second auto-retry
        data = self.cards[0][1]["inline_keyboard"][0][0]["callback_data"]
        self.assertTrue(data.startswith("hd:r:"))
        self.assertTrue(hl.jrelay.BUTTON_DATA.match(data) if hasattr(hl.jrelay, "BUTTON_DATA") else True)
        # later drop (>60 s) auto-retries again
        self.assertEqual(hl.retry_or_offer(self.io, self.m, "server_error", now=lambda: 1100, enqueue=self.enq), "retried")

    def test_button_is_owner_only_and_single_use(self):
        hl._state["auto_retry_ts"] = 1000
        hl.retry_or_offer(self.io, self.m, "server_error", now=lambda: 1010, enqueue=self.enq)
        data = self.cards[0][1]["inline_keyboard"][0][0]["callback_data"]
        ans = []
        cq = lambda uid: {"from": {"id": uid}, "message": {"chat": {"id": uid}, "message_id": 9, "text": "x"}}
        self.assertIsNone(hl.on_retry_button(cq(999), data, "t", ans.append, enqueue=self.enq))
        self.assertEqual(self.q, [])
        self.assertEqual(hl.on_retry_button(cq(111111111), data, "t", ans.append, enqueue=self.enq), "retried")
        self.assertEqual(len(self.q), 1); self.assertEqual(self.q[0][0]["message_id"], 5)
        self.assertEqual(hl._state["auto_retry_ts"], 0)
        self.assertIsNone(hl.on_retry_button(cq(111111111), data, "t", ans.append, enqueue=self.enq))
        self.assertEqual(len(self.q), 1)

    def test_on_button_routes_hd_r(self):
        seen = []
        o = hl.on_retry_button
        hl.on_retry_button = lambda cq, data, tok, answer: seen.append(data)
        try:
            hl.on_button({"from": {"id": 111111111}, "message": {}}, "hd:r:abc", "t", call=lambda c: {})
        finally:
            hl.on_retry_button = o
        self.assertEqual(seen, ["hd:r:abc"])

    def test_real_turn_with_server_error_retries(self):
        t = Turn("run"); t.setUp()
        hl._state["auto_retry_ts"] = -1e9
        got, o = [], hl._enqueue
        hl._enqueue = lambda tok, m, text: got.append(text)
        try:
            t.run_turn("hi", [[ev("StopFailure", error="server_error")]])
        finally:
            hl._enqueue = o
        self.assertEqual(len(got), 1); self.assertIn("connection error", got[0])


if __name__ == "__main__":
    unittest.main()


class UploadIndicatorTest(unittest.TestCase):
    """alex 2026-10-26: a photo that lagged on mobile looked missing. While a file uploads, the chat shows
    'sending photo...' (upload_photo) or 'sending file...' (upload_document)."""

    def _run(self, path):
        import head_lane as hl
        import tgapi as pl
        calls = []
        orig = (pl._api, pl._send_photo, pl._send_document)
        def slow(*a, **k):
            import time; time.sleep(0.15)
        pl._api = lambda tok, method, **kw: calls.append((method, kw.get("action")))
        pl._send_photo = slow; pl._send_document = slow
        try:
            io = hl._IO.__new__(hl._IO)
            io.tok, io.m = "t", {"chat": {"id": 1, "type": "private"}, "message_thread_id": 7}
            io.say = lambda *a: calls.append(("say",) + a)
            io.send_file(path)
        finally:
            pl._api, pl._send_photo, pl._send_document = orig
        return calls

    def test_photo_shows_upload_photo(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            calls = self._run(f.name)
        self.assertIn(("sendChatAction", "upload_photo"), calls)

    def test_file_shows_upload_document(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            calls = self._run(f.name)
        self.assertIn(("sendChatAction", "upload_document"), calls)


class ImageWorkIndicatorTest(unittest.TestCase):
    """alex 2026-10-26: show 'sending photo...' while the head edits/processes an image, not only during upload."""

    def test_classifier(self):
        import head_lane as hl
        f = hl.chat_action_for
        self.assertEqual(f("Bash", {"command": "python3 jedit.py /d develop --layer base"}), "upload_photo")
        self.assertEqual(f("Bash", {"command": "python3 layout.py ad.html out.jpg"}), "upload_photo")
        self.assertEqual(f("Read", {"file_path": "/x/shot.png"}), "upload_photo")
        self.assertEqual(f("mcp__playwright__browser_take_screenshot", {}), "upload_photo")
        self.assertEqual(f("Bash", {"command": "git status"}), "typing")
        self.assertEqual(f("Read", {"file_path": "/x/notes.md"}), "typing")
        self.assertEqual(f("Edit", {"file_path": "/x/head_lane.py"}), "typing")

    def test_action_holds_through_image_steps_and_drops_on_other_work(self):
        import head_lane as hl
        io = hl._IO.__new__(hl._IO)
        self.assertEqual(io.chat_action(), "typing")
        io.note_tool("Bash", {"command": "python3 jedit.py d retouch --layer base"})
        self.assertEqual(io.chat_action(), "upload_photo")
        io.note_tool("Bash", {"command": "git commit -m x"})
        self.assertEqual(io.chat_action(), "typing")

    def test_typing_thread_reads_the_live_action(self):
        import head_lane as hl, tgapi as pl, time
        sent = []
        orig = pl._api
        pl._api = lambda tok, method, **kw: sent.append(kw.get("action"))
        state = {"a": "typing"}
        try:
            with hl._Typing("t", {"chat": {"id": 1, "type": "private"}}, lambda: state["a"]) as t:
                time.sleep(0.05); state["a"] = "upload_photo"; t.stop.wait(4.3)
        finally:
            pl._api = orig
        self.assertEqual(sent[0], "typing"); self.assertIn("upload_photo", sent)


class VideoWorkIndicatorTest(unittest.TestCase):
    """alex 2026-10-26: 'sending video...' while the head works on video."""

    def test_classifier_video_wins(self):
        import head_lane as hl
        f = hl.chat_action_for
        self.assertEqual(f("Bash", {"command": "ffmpeg -i a.mp4 -vf scale=640:-2 b.mp4"}), "upload_video")
        self.assertEqual(f("Bash", {"command": "python3 motion.py scenes.html out.mp4 --dur 60"}), "upload_video")
        self.assertEqual(f("Bash", {"command": "ffmpeg -ss 3 -i clip.mp4 -frames:v 1 f.png"}), "upload_video")
        self.assertEqual(f("Bash", {"command": "python3 jedit.py d develop --layer base"}), "upload_photo")
        self.assertEqual(f("Bash", {"command": "git log"}), "typing")

    def test_io_holds_video_action(self):
        import head_lane as hl
        io = hl._IO.__new__(hl._IO)
        io.note_tool("Bash", {"command": "ffmpeg -i x.mp4 y.mp4"})
        self.assertEqual(io.chat_action(), "upload_video")
        io.note_tool("Bash", {"command": "python3 jedit.py d export o.png"})
        self.assertEqual(io.chat_action(), "upload_photo")
        io.note_tool("Bash", {"command": "ls"})
        self.assertEqual(io.chat_action(), "typing")


class ReplyToOwnMediaTest(unittest.TestCase):
    """alex 2026-10-26: 'This is great BTW!' as a reply to the agent's own 22 MB video became a re-download (HTTP 400,
    over the 20 MB bot limit) and his words were dropped."""

    def test_failed_download_still_delivers_his_words(self):
        import head_lane as hl, tgapi as pl
        got, replies = [], []
        orig = (pl._reply, hl._react, hl.note_undelivered)
        pl._reply = lambda tok, m, t: replies.append(t); hl._react = lambda *a: None
        hl.note_undelivered = lambda **k: got.append(("refused", k))
        try:
            def boom(tok, m):
                raise RuntimeError("HTTP Error 400: Bad Request")
            hl._prep_media("t", {"video": {"file_id": "x"}, "caption": "This is great BTW!", "chat": {"id": 1, "type": "private"}},
                           to_text=boom, enqueue=lambda tok, m, text: got.append(("queued", text)))
        finally:
            pl._reply, hl._react, hl.note_undelivered = orig
        self.assertEqual(got[0][0], "queued"); self.assertIn("This is great BTW!", got[0][1])
        self.assertIn("could not be downloaded", got[0][1])


# TIK-1087: /usage opened Claude Code's interactive panel in the REPL and wedged the head for an hour.
PANEL = ["   Current session", "   ████    8% used", "   Resets 10:20pm (America/Chicago)", "",
         "   Current week (all models)", "   ██████  20% used", "   Resets Nov 1, 7am (America/Chicago)", "",
         "   Current week (Fable)", "   ██  4% used", "   Resets Nov 1, 7am (America/Chicago)", "   f to fix 2 of 4"]


class UsageTest(unittest.TestCase):
    def fake(self, state="idle", panel_after_escape=False):
        self.calls, self.open = [], False

        def call(req, wait=5):
            self.calls.append(req)
            if req.get("t") == "send" and req.get("text") == "/usage":
                self.open = True
            if req.get("t") == "key" and req.get("key") == "Escape" and not panel_after_escape:
                self.open = False
            if req.get("t") == "tail":
                return {"lines": PANEL if self.open else ["❯ "]}
            return {"state": state, "ok": True}
        return call

    def test_parse_usage(self):
        self.assertEqual(hl.parse_usage(PANEL), [("Current session", 8, "10:20pm (America/Chicago)"),
                                                 ("Current week (all models)", 20, "Nov 1, 7am (America/Chicago)"),
                                                 ("Current week (Fable)", 4, "Nov 1, 7am (America/Chicago)")])
        self.assertEqual(hl.parse_usage(["❯ ", "Current session"]), [])

    def test_usage_reads_and_always_closes_the_panel(self):
        txt = hl.usage(call=self.fake(), sleep=lambda s: None)
        self.assertIn("Current session: 8% used · resets 10:20pm", txt)
        self.assertIn("Current week (all models): 20% used", txt)
        self.assertIn({"t": "key", "key": "Escape"}, self.calls)
        self.assertFalse(self.open)
        self.assertNotIn("still be open", txt)

    def test_a_panel_still_open_is_said_and_escaped_again(self):
        txt = hl.usage(call=self.fake(panel_after_escape=True), sleep=lambda s: None)
        self.assertIn("may still be open", txt)
        self.assertEqual(self.calls.count({"t": "key", "key": "Escape"}), 2)

    def test_busy_head_is_not_typed_into(self):
        txt = hl.usage(call=self.fake(state="busy"), sleep=lambda s: None)
        self.assertNotIn({"t": "send", "text": "/usage"}, self.calls)
        self.assertIn("busy", txt)

    def test_escape_sent_even_if_reading_fails(self):
        base = self.fake()

        def call(req, wait=5):
            if req.get("t") == "tail" and self.open:
                raise OSError("socket gone")
            return base(req, wait)
        with self.assertRaises(OSError):
            hl.usage(call=call, sleep=lambda s: None)
        self.assertIn({"t": "key", "key": "Escape"}, self.calls)

    def test_interactive_commands_are_never_typed_raw(self):
        self.assertEqual(hl.command_of("/usage"), "/usage")
        self.assertEqual(hl.command_of("/config"), "/config")
        calls = []
        real = hl.bridge
        hl.bridge = lambda req, wait=5: calls.append(req) or {"state": "idle", "ok": True}
        try:
            uid = next(iter(hl.HEAD_UIDS)); tid = hl.HEAD_THREADS[uid]
            m = {"message_id": 9, "chat": {"id": uid, "type": "private"}, "from": {"id": uid}, "message_thread_id": tid, "text": "/config"}
            self.assertTrue(hl.handle(m, "t"))
        finally:
            hl.bridge = real
        self.assertFalse(any(c.get("t") == "send" for c in calls))
        self.assertTrue(any("/config opens an interactive screen" in s for s in SENT))
        self.assertEqual(len(hl._q), 0)


class TopicCreatedTest(unittest.TestCase):
    """TIK-1133: alex makes a new topic in his DM -> new_lane.py builds its head; anything else is left alone."""

    def _m(self, uid=OWNER, chat=None, tid=777001, name="Garden"):
        return {"message_id": 5, "from": {"id": uid}, "chat": chat or {"id": uid, "type": "private"},
                "message_thread_id": tid, "forum_topic_created": {"name": name}}

    def setUp(self):
        self.started = []
        self.start = lambda f, *a: self.started.append(a)

    def test_rav_new_topic_builds_a_lane(self):
        self.assertTrue(hl.topic_created(self._m(), "tok", start=self.start))
        self.assertEqual(self.started, [("tok", OWNER, 777001, "Garden")])

    def test_someone_else_never_builds(self):
        self.assertFalse(hl.topic_created(self._m(uid=USER_B, chat={"id": USER_B, "type": "private"}), "tok", start=self.start))
        self.assertEqual(self.started, [])

    def test_group_topic_never_builds(self):
        self.assertFalse(hl.topic_created(self._m(chat={"id": -100123, "type": "supergroup"}), "tok", start=self.start))
        self.assertEqual(self.started, [])

    def test_existing_head_thread_not_rebuilt(self):
        old = dict(hl.HEAD_THREADS); hl.HEAD_THREADS[OWNER] = 777002
        try:
            self.assertTrue(hl.topic_created(self._m(tid=777002), "tok", start=self.start))
        finally:
            hl.HEAD_THREADS.clear(); hl.HEAD_THREADS.update(old)
        self.assertEqual(self.started, [])

    def test_plain_message_is_not_a_topic(self):
        m = self._m(); m.pop("forum_topic_created"); m["text"] = "hi"
        self.assertFalse(hl.topic_created(m, "tok", start=self.start))

    def test_build_ok_loads_lane_and_says_ready(self):
        class R: stdout = '{"ok": true, "lane": "garden", "thread": 777001}\n'
        posts, loaded, argv = [], [], []
        real = hl.add_lane; hl.add_lane = lambda n: loaded.append(n) or True
        try:
            res = hl.build_lane("tok", OWNER, 777001, "Garden", run=lambda a, **k: argv.append(a) or R(),
                                post=lambda tok, meth, **kw: posts.append(kw))
        finally:
            hl.add_lane = real
        self.assertTrue(res["ok"]); self.assertEqual(loaded, ["garden"])
        self.assertIn("--thread", argv[0]); self.assertEqual(argv[0][argv[0].index("--thread") + 1], "777001")
        self.assertEqual(posts[0]["message_thread_id"], 777001); self.assertIn("own head", posts[0]["text"])

    def test_build_fail_says_so_in_the_topic(self):
        class R: stdout = '{"ok": false, "lane": "garden", "step": "bridge", "why": "no Stop event"}\n'
        posts, loaded = [], []
        real = hl.add_lane; hl.add_lane = lambda n: loaded.append(n) or True
        try:
            res = hl.build_lane("tok", OWNER, 777001, "Garden", run=lambda a, **k: R(), post=lambda tok, meth, **kw: posts.append(kw))
        finally:
            hl.add_lane = real
        self.assertFalse(res["ok"]); self.assertEqual(loaded, [])
        self.assertIn("couldn't set up", posts[0]["text"]); self.assertIn("bridge", posts[0]["text"])

    def test_script_crash_still_said(self):
        posts = []
        res = hl.build_lane("tok", OWNER, 777001, "Garden", run=lambda a, **k: (_ for _ in ()).throw(OSError("boom")),
                            post=lambda tok, meth, **kw: posts.append(kw))
        self.assertFalse(res["ok"]); self.assertIn("boom", posts[0]["text"])

    def test_add_lane_once(self):
        before = list(hl.OTHERS)
        try:
            self.assertTrue(hl.add_lane("topic_test"))
            self.assertFalse(hl.add_lane("topic_test"))
            self.assertEqual(sum(o._LANE == "topic_test" for o in hl.OTHERS), 1)
        finally:
            hl.OTHERS[:] = before

    def test_handle_routes_topic_first(self):
        real = hl.topic_created; seen = []
        hl.topic_created = lambda m, tok: seen.append(m) or True
        try:
            self.assertTrue(hl.handle(self._m(), "tok"))
        finally:
            hl.topic_created = real
        self.assertEqual(len(seen), 1)


import io as _io  # noqa: E402
import time  # noqa: E402
import urllib.error as _uerr  # noqa: E402


def _http(code, desc):
    return _uerr.HTTPError("u", code, "x", {}, _io.BytesIO(('{"ok":false,"error_code":%d,"description":"%s"}' % (code, desc)).encode()))


GONE_BODY = "Bad Request: message to react not found"


class GoneCheck(unittest.TestCase):
    """TIK-1100: a message alex deleted while it was queued never reaches the head. Fail open on anything else. No hold."""
    def setUp(self):
        hl._q.clear(); hl._state["worker"] = object(); hl._state["running"] = False
        hl.HEAD_THREADS[OWNER] = 777
        SENT.clear(); API.clear()
        self._off = hl.GONE_OFF
        hl.GONE_OFF = os.path.join(_TMP, "gone_check.off")
        if os.path.exists(hl.GONE_OFF):
            os.unlink(hl.GONE_OFF)
        with sqlite3.connect(cl.CHATDB) as c:
            c.execute("DELETE FROM chat_log")
        self.log = []
        self._log = pl._log
        pl._log = lambda ev, **kw: self.log.append((ev, kw))

    def tearDown(self):
        pl._log = self._log
        hl.GONE_OFF = self._off

    def m(self, mid=50, age=10.0, mark=True):
        m = {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": mid, "message_thread_id": 777, "text": "Done"}
        if mark:
            m["_gone_check"] = True
            m["_q_ts"] = 1000.0 - age
        return m

    def api_raising(self, exc):
        def f(tok, method, **kw):
            API.append((method, kw)); raise exc
        return f

    def test_the_bot_api_answer_for_a_deleted_message_means_gone(self):
        self.assertTrue(hl.message_gone("t", self.m(), api=self.api_raising(_http(400, GONE_BODY))))

    def test_a_live_message_is_not_gone_and_keeps_the_same_reaction(self):
        calls = []
        self.assertFalse(hl.message_gone("t", self.m(), api=lambda tok, method, **kw: calls.append((method, kw)) or {"ok": True}))
        self.assertEqual(calls[0][0], "setMessageReaction")
        self.assertEqual(calls[0][1]["reaction"], [{"type": "emoji", "emoji": hl.REACT["queued"]}])

    def test_every_other_failure_fails_open_and_is_logged(self):
        for exc in (_http(400, "Bad Request: REACTION_INVALID"), _http(429, "Too Many Requests: retry after 5"),
                    _http(500, "Internal"), _uerr.URLError("down"), TimeoutError("slow"), RuntimeError("x")):
            self.log.clear()
            self.assertFalse(hl.message_gone("t", self.m(), api=self.api_raising(exc)), exc)
            ev = [kw for e, kw in self.log if kw.get("event2") == "gone_check_failed"]
            self.assertEqual(len(ev), 1, exc); self.assertTrue(ev[0]["delivered"])

    def test_a_400_that_merely_mentions_other_words_is_not_gone(self):
        self.assertFalse(hl.message_gone("t", self.m(), api=self.api_raising(_http(403, GONE_BODY))))   # wrong code
        self.assertFalse(hl.message_gone("t", self.m(), api=self.api_raising(_http(400, "Bad Request: chat not found"))))

    def test_drop_logs_to_the_print_log_and_marks_the_ledger_row(self):
        m = self.m(mid=51, age=7.0)
        with sqlite3.connect(cl.CHATDB) as c:
            c.execute("INSERT INTO chat_log (session_id, source, role, content, ts, extra) VALUES (?,?,?,?,?,?)",
                      ("tg_x", "telegram", "user", "Done", 1.0, json.dumps({"chat_id": OWNER, "tg_thread": 777, "message_id": 51})))
        self.assertTrue(hl.drop_if_deleted("t", m, now=lambda: 1000.0, gone=lambda tok, mm: True))
        ev = [kw for e, kw in self.log if kw.get("event2") == "dropped_deleted"]
        self.assertEqual(len(ev), 1); self.assertEqual((ev[0]["message_id"], ev[0]["age_s"]), (51, 7.0))
        with sqlite3.connect(cl.CHATDB) as c:
            x = json.loads(c.execute("SELECT extra FROM chat_log WHERE session_id='tg_x'").fetchone()[0])
        self.assertEqual(x["dropped_deleted"]["head"], hl._LANE); self.assertEqual(x["dropped_deleted"]["age_s"], 7.0)

    def test_a_message_that_is_not_marked_or_is_too_fresh_or_switched_off_is_never_checked(self):
        asked = []
        gone = lambda tok, mm: asked.append(1) or True
        unmarked_but_old = self.m(); del unmarked_but_old["_gone_check"]                                   # old enough, but not plain inbound text
        self.assertFalse(hl.drop_if_deleted("t", unmarked_but_old, now=lambda: 1000.0, gone=gone))          # media, relay, live: out of scope
        self.assertFalse(hl.drop_if_deleted("t", self.m(age=0.5), now=lambda: 1000.0, gone=gone))         # not queued behind anything
        open(hl.GONE_OFF, "w").close()
        self.assertFalse(hl.drop_if_deleted("t", self.m(age=30), now=lambda: 1000.0, gone=gone))
        self.assertEqual(asked, [])

    def test_a_live_message_is_not_dropped(self):
        self.assertFalse(hl.drop_if_deleted("t", self.m(), now=lambda: 1000.0, gone=lambda tok, mm: False))
        self.assertEqual([kw for e, kw in self.log if kw.get("event2") == "dropped_deleted"], [])

    def test_plain_inbound_text_is_marked_and_timestamped_when_queued(self):
        m = {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 60, "message_thread_id": 777, "text": "hello"}
        self.assertTrue(hl.handle(m, "t"))
        qm, qt = hl._q[0]
        self.assertTrue(qm["_gone_check"]); self.assertGreater(qm["_q_ts"], 0)
        self.assertNotIn("_gone_check", m, "the caller's dict is not changed")

    def test_serve_drops_the_deleted_one_and_delivers_the_next_with_no_sleep(self):
        delivered, slept = [], []
        real = (hl._turn, hl.message_gone, hl._wake.wait, time.sleep, hl.GONE_MIN_AGE_S)
        def stop_after_one_pass(timeout=None):
            if stop_after_one_pass.n:
                raise KeyboardInterrupt
            stop_after_one_pass.n += 1
            return True
        stop_after_one_pass.n = 0
        a, b = self.m(mid=70, age=0), self.m(mid=71, age=0)
        a["_q_ts"] = b["_q_ts"] = time.time() - 30
        hl._q.extend([(a, "typo"), (b, "retyped")])
        hl._turn = lambda tok, m, text, **kw: delivered.append((m["message_id"], text))
        hl.message_gone = lambda tok, m, api=None: m["message_id"] == 70
        hl._wake.wait = stop_after_one_pass
        time.sleep = lambda s: slept.append(s)
        try:
            with self.assertRaises(KeyboardInterrupt):
                hl._serve("t")
        finally:
            hl._turn, hl.message_gone, hl._wake.wait, time.sleep, hl.GONE_MIN_AGE_S = real
        self.assertEqual(delivered, [(71, "retyped")])
        self.assertEqual(slept, [], "the check must never delay delivery (Rav: no hold)")
        self.assertEqual([kw["message_id"] for e, kw in self.log if kw.get("event2") == "dropped_deleted"], [70])


class Burst(GoneCheck):
    """alex 2026-11-01: a lone message goes at once; messages <=1 s apart (a forwarded batch) become ONE turn."""
    def clock(self, t=1000.0):
        st = {"t": t}
        def now():
            return st["t"]
        def sleep(s):
            st["t"] += s
            self.slept.append(s)
            for at, item in list(self.arrivals):        # messages that land while the worker waits
                if at <= st["t"]:
                    self.arrivals.remove((at, item)); hl._q.append(item)
        self.slept, self.arrivals = [], []
        return now, sleep

    def msg(self, mid, ts, text=None):
        m = self.m(mid=mid)
        m["_q_ts"] = ts
        return (m, text or f"part {mid}")

    def test_a_lone_message_waits_at_most_half_a_second_from_arrival(self):
        now, sleep = self.clock(1000.0)
        m, t = self.msg(1, 1000.0)
        out = hl._burst("t", m, t, sleep=sleep, now=now, gone=lambda *a, **k: False)
        self.assertEqual(out[1], "part 1")
        self.assertLessEqual(sum(self.slept), hl.BURST_GAP_S + 1e-9)

    def test_an_old_lone_message_waits_not_at_all(self):
        now, sleep = self.clock(1005.0)
        m, t = self.msg(1, 1000.0)
        self.assertEqual(hl._burst("t", m, t, sleep=sleep, now=now)[1], "part 1")
        self.assertEqual(self.slept, [])

    def test_parts_that_keep_coming_are_one_turn(self):
        now, sleep = self.clock(1000.0)
        first = self.msg(1, 1000.0)
        self.arrivals = [(1000.3, self.msg(2, 1000.3)), (1001.1, self.msg(3, 1001.1)), (1001.9, self.msg(4, 1001.9))]
        m, text = hl._burst("t", *first, sleep=sleep, now=now, gone=lambda *a, **k: False)
        self.assertEqual(m["message_id"], 1)
        for i in (1, 2, 3, 4):
            self.assertIn(f"({i}/4) part {i}", text)
        self.assertEqual(list(hl._q), [])
        self.assertGreaterEqual(now(), 1002.9, "waited a full second of quiet after the last part")
        self.assertEqual([kw["n"] for e, kw in self.log if kw.get("event2") == "burst_joined"], [4])

    def test_a_part_after_a_second_of_quiet_is_its_own_turn(self):
        now, sleep = self.clock(1000.0)
        self.arrivals = [(1000.2, self.msg(2, 1000.2)), (1001.5, self.msg(3, 1001.5))]
        m, text = hl._burst("t", *self.msg(1, 1000.0), sleep=sleep, now=now, gone=lambda *a, **k: False)
        self.assertIn("2 messages", text)
        while self.arrivals:
            sleep(0.1)
        self.assertEqual([q[0]["message_id"] for q in hl._q], [3])

    def test_a_backlog_behind_a_running_turn_is_joined_by_arrival_times(self):
        now, sleep = self.clock(1100.0)
        hl._q.extend([self.msg(2, 1000.4), self.msg(3, 1000.9), self.msg(9, 1050.0)])
        m, text = hl._burst("t", *self.msg(1, 1000.0), sleep=sleep, now=now, gone=lambda *a, **k: False)
        self.assertIn("3 messages", text)
        self.assertEqual([q[0]["message_id"] for q in hl._q], [9])
        self.assertEqual(self.slept, [])

    def test_media_relays_and_other_people_are_never_joined(self):
        now, sleep = self.clock(1000.0)
        rel = self.msg(2, 1000.1); rel[0]["_relay"] = {"origin": "x"}
        hl._q.append(rel)
        self.assertEqual(hl._burst("t", *self.msg(1, 1000.0), sleep=sleep, now=now)[1], "part 1")
        self.assertEqual(len(hl._q), 1)
        hl._q.clear()
        other = self.msg(3, 1000.1); other[0]["from"] = {"id": 5}
        hl._q.append(other)
        self.assertEqual(hl._burst("t", *self.msg(1, 1000.0), sleep=sleep, now=now)[1], "part 1")
        media = self.msg(4, 1000.0)
        self.assertIs(hl._burst("t", media[0], hl.MEDIA_MARK, sleep=sleep, now=now)[1], hl.MEDIA_MARK)

    def test_a_deleted_part_is_dropped_and_the_rest_still_go(self):
        now, sleep = self.clock(1100.0)
        hl._q.extend([self.msg(2, 1000.3), self.msg(3, 1000.6)])
        m, text = hl._burst("t", *self.msg(1, 1000.0), sleep=sleep, now=now, gone=lambda tok, m: m["message_id"] == 2)
        self.assertIn("2 messages", text); self.assertNotIn("part 2", text)
        hl._q.extend([self.msg(5, 1000.3)])
        self.assertEqual(hl._burst("t", *self.msg(4, 1000.0), sleep=sleep, now=now, gone=lambda *a, **k: True), (None, None))

class MidturnOnce(GoneCheck):
    """TIK-1421: a message sent mid-turn reaches that turn and is not run again as its own turn."""
    def setUp(self):
        super().setUp()
        self._mt = (hl.MIDTURN, hl.MIDTURN_DONE)
        hl.MIDTURN, hl.MIDTURN_DONE = os.path.join(_TMP, "mt.jsonl"), os.path.join(_TMP, "mt_done.jsonl")
        for p in self._mt_paths():
            if os.path.exists(p):
                os.unlink(p)

    def _mt_paths(self):
        return (hl.MIDTURN, hl.MIDTURN_DONE)

    def tearDown(self):
        hl._state["running"] = False
        hl.MIDTURN, hl.MIDTURN_DONE = self._mt
        super().tearDown()

    def deliver_all(self):
        with open(hl.MIDTURN) as f, open(hl.MIDTURN_DONE, "a") as d:
            for line in f:
                d.write(json.dumps({"k": json.loads(line)["k"]}) + "\n")

    def test_message_mid_turn_is_keyed_and_its_queued_copy_skipped_once_delivered(self):
        hl._state["running"] = True
        m = {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 91, "message_thread_id": 777, "text": "also do X"}
        self.assertTrue(hl.handle(m, "t"))
        qm, qt = hl._q[0]
        self.assertTrue(qm.get("_mt"))
        self.assertIn(qm["_mt"], open(hl.MIDTURN).read())
        self.assertFalse(hl.skip_if_delivered("t", qm), "not delivered yet = it still runs as a turn")
        self.deliver_all()
        reacts, real = [], hl._react
        hl._react = lambda tok, mm, st: reacts.append(st)
        try:
            self.assertTrue(hl.skip_if_delivered("t", qm))
        finally:
            hl._react = real
        self.assertEqual(reacts, ["seen"], "👌 = taken mid-turn, not 👍 answered")

    def test_idle_message_gets_no_key(self):
        m = {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 92, "message_thread_id": 777, "text": "hi"}
        hl.handle(m, "t")
        self.assertNotIn("_mt", hl._q[0][0])
        self.assertFalse(os.path.exists(hl.MIDTURN))

    def test_serve_never_runs_a_delivered_message_as_a_turn(self):
        delivered = []
        real = (hl._turn, hl._wake.wait, hl.message_gone)
        def once(timeout=None):
            if once.n:
                raise KeyboardInterrupt
            once.n += 1
            return True
        once.n = 0
        a, b = self.m(mid=80), self.m(mid=81)
        a["_q_ts"], b["_q_ts"] = time.time() - 60, time.time() - 30
        a["_mt"] = "ka"
        with open(hl.MIDTURN_DONE, "w") as f:
            f.write(json.dumps({"k": "ka"}) + "\n")
        hl._q.extend([(a, "seen"), (b, "new")])
        hl._turn = lambda tok, m, text, **kw: delivered.append(m["message_id"])
        hl._wake.wait = once
        hl.message_gone = lambda *x, **k: False
        try:
            with self.assertRaises(KeyboardInterrupt):
                hl._serve("t")
        finally:
            hl._turn, hl._wake.wait, hl.message_gone = real
        self.assertEqual(delivered, [81])

    def test_a_message_run_as_its_own_turn_is_claimed_for_the_hook(self):
        hl.claim_midturn({"_mt": "a", "_mt_parts": ["b"]}, "x")
        self.assertTrue(hl.midturn_delivered("a")); self.assertTrue(hl.midturn_delivered("b"))
        hl.claim_midturn({}, "x")
        self.assertFalse(hl.midturn_delivered("c"))

    def test_the_done_file_is_cut_back_to_what_readers_use(self):
        with open(hl.MIDTURN_DONE, "w") as f:
            f.writelines(json.dumps({"k": str(i)}) + "\n" for i in range(9))
        self.assertEqual(hl.trim_midturn_done(keep=3), 6)
        self.assertEqual(len(open(hl.MIDTURN_DONE).readlines()), 3)
        self.assertTrue(hl.midturn_delivered("8")); self.assertFalse(hl.midturn_delivered("0"))
        self.assertEqual(hl.trim_midturn_done(keep=3), 0)

    def _serve_once(self, items, gone=lambda *x, **k: False):
        delivered = []
        real = (hl._turn, hl._wake.wait, hl.message_gone)
        def once(timeout=None):
            if once.n:
                raise KeyboardInterrupt
            once.n += 1
            return True
        once.n = 0
        hl._q.extend(items)
        hl._turn = lambda tok, m, text, **kw: delivered.append((m.get("message_id"), text))
        hl._wake.wait, hl.message_gone = once, gone
        try:
            with self.assertRaises(KeyboardInterrupt):
                hl._serve("t")
        finally:
            hl._turn, hl._wake.wait, hl.message_gone = real
        return delivered

    def test_serve_asks_whether_a_lone_queued_message_was_deleted(self):
        a = self.m(mid=85); a["_q_ts"] = time.time() - 60
        self.assertEqual(self._serve_once([(a, "gone")], gone=lambda tok, m, api=None: True), [])

    def test_serve_claims_a_message_it_runs_as_its_own_turn(self):
        a = self.m(mid=86); a["_q_ts"] = time.time() - 60; a["_mt"] = "k86"
        self.assertEqual(self._serve_once([(a, "late")]), [(86, "late")])
        self.assertTrue(hl.midturn_delivered("k86"), "the next turn's hook must not re-tell it")

    def test_a_delivered_part_is_left_out_of_a_burst(self):
        a, b, c = self.m(mid=1), self.m(mid=2), self.m(mid=3)
        a["_q_ts"], b["_q_ts"], c["_q_ts"] = 1000.0, 1000.3, 1000.6
        b["_mt"] = "kb"
        with open(hl.MIDTURN_DONE, "w") as f:
            f.write(json.dumps({"k": "kb"}) + "\n")
        hl._q.extend([(b, "two"), (c, "three")])
        m, text = hl._burst("t", a, "one", sleep=lambda s: None, now=lambda: 1100.0, gone=lambda *x, **k: False)
        self.assertIn("2 messages", text); self.assertNotIn("two", text)

class AlbumOneTurn(GoneCheck):
    """alex 2026-11-02: an album to the head is ONE turn."""
    def test_album_items_prepared_in_any_order_are_one_turn(self):
        timers, got = [], []
        class T:
            def __init__(self, s, f, args=()): self.f, self.a = f, args; timers.append(self)
            def start(self): pass
            def cancel(self): self.f = None
        enq = lambda tok, m, t: got.append((m["message_id"], t))
        for mid, txt in ((12, "[alex sent a photo ... b.jpg]"), (11, "look / [alex sent a photo ... a.jpg]")):
            m = self.m(mid=mid); m["media_group_id"] = "G"
            hl._album_add("t", m, txt, enq, timer=T)
        live = [t for t in timers if t.f]
        self.assertEqual(len(live), 1, "each new item restarts the wait")
        live[0].f(*live[0].a)
        self.assertEqual(len(got), 1)
        mid, text = got[0]
        self.assertEqual(mid, 11)
        self.assertIn("album of 2", text); self.assertLess(text.index("a.jpg"), text.index("b.jpg"))

    def test_prepared_album_item_is_held_for_the_album_not_enqueued(self):
        """The dispatch branch itself (ship gate TIK-1416): a prepared item with media_group_id goes to the
        album gatherer, never straight to enqueue. (The gatherer's own test calls _album_add directly.)"""
        got, held = [], []
        enq = lambda tok, m, t: got.append(t)
        orig = hl._album_add
        hl._album_add = lambda tok, m, text, enqueue, timer=None: held.append((m["message_id"], text))
        try:
            m = self.m(mid=21); m["media_group_id"] = "G2"
            hl._prep_media("t", m, to_text=lambda tok, m: "[alex sent a photo ... c.jpg]", enqueue=enq)
        finally:
            hl._album_add = orig
        self.assertEqual(got, [], "an album item must not be a turn of its own")
        self.assertEqual(held, [(21, "[alex sent a photo ... c.jpg]")])

    def test_a_lone_photo_is_not_held(self):
        got = []
        hl._prep_media("t", self.m(mid=13), to_text=lambda tok, m: "[photo]", enqueue=lambda tok, m, t: got.append(t))
        self.assertEqual(got, ["[photo]"])

class FollowUps(unittest.TestCase):
    """TIK-1028: a turn the head starts itself (job finished, reminder) still reaches the thread, marked."""
    def setUp(self):
        hl._q.clear(); hl._state["running"] = False; hl._state["busy_n"] = 0
        hl._state.pop("fu_off", None)
        hl.HEAD_THREADS[OWNER] = 777
        self._off = hl.FOLLOWUP_OFF; hl.FOLLOWUP_OFF = os.path.join(_TMP, "followup.off")
        if os.path.exists(hl.FOLLOWUP_OFF):
            os.unlink(hl.FOLLOWUP_OFF)
        self.posts = []
        self.post = lambda tok, m, text: self.posts.append((m, text))
        open(hl.EVENTS, "w").close()

    def tearDown(self):
        hl.FOLLOWUP_OFF = self._off

    def ev(self, name, msg=None, **extra):
        with open(hl.EVENTS, "a") as f:
            f.write(json.dumps({"event": name, "data": ({"last_assistant_message": msg} if msg is not None else {}), **extra}) + "\n")

    def test_the_first_look_starts_at_the_end_history_is_never_replayed(self):
        self.ev("Stop", "old answer")
        self.assertEqual(hl.relay_followups("t", post=self.post), 0)
        self.assertEqual(self.posts, [])
        self.ev("Stop", "grok finished")
        self.assertEqual(hl.relay_followups("t", post=self.post), 1)
        self.assertEqual(self.posts[0][0]["message_thread_id"], 777)
        self.assertTrue(self.posts[0][1].startswith(hl.FOLLOWUP_TAG)); self.assertIn("grok finished", self.posts[0][1])

    def test_each_stop_is_relayed_once(self):
        hl.relay_followups("t", post=self.post)
        self.ev("Stop", "one"); self.ev("Stop", "two")
        self.assertEqual(hl.relay_followups("t", post=self.post), 2)
        self.assertEqual(hl.relay_followups("t", post=self.post), 0)
        self.assertEqual(len(self.posts), 2)

    def test_only_a_stop_with_words_counts(self):
        hl.relay_followups("t", post=self.post)
        for name in ("PreToolUse", "UserPromptSubmit", "LaneStop", "PermissionRequest", "StopFailure"):
            self.ev(name, "x")
        self.ev("Stop"); self.ev("Stop", "   ")
        self.assertEqual(hl.relay_followups("t", post=self.post), 0)

    def test_nothing_is_read_while_a_turn_runs_or_work_is_queued(self):
        hl.relay_followups("t", post=self.post)
        self.ev("Stop", "belongs to the running turn")
        for setter in (lambda: hl._state.update(running=True), lambda: hl._state.update(busy_n=1), lambda: hl._q.append((object(), "x"))):
            hl._state.update(running=False, busy_n=0); hl._q.clear(); setter()
            self.assertEqual(hl.relay_followups("t", post=self.post), 0)
        self.assertEqual(self.posts, [])

    def test_a_finished_lane_turn_settles_its_own_events_so_its_reply_is_not_relayed_twice(self):
        real = hl._turn_run
        hl._turn_run = lambda *a, **k: self.ev("Stop", "the answer follow() already delivered")
        try:
            hl.relay_followups("t", post=self.post)
            hl._turn("t", {}, "hi")
        finally:
            hl._turn_run = real
        self.assertEqual(hl._state["busy_n"], 0)
        self.assertEqual(hl.relay_followups("t", post=self.post), 0)
        self.assertEqual(self.posts, [])

    def test_the_busy_count_is_released_even_when_the_turn_raises(self):
        real = hl._turn_run
        def boom(*a, **k): raise RuntimeError("x")
        hl._turn_run = boom
        try:
            with self.assertRaises(RuntimeError):
                hl._turn("t", {}, "hi")
        finally:
            hl._turn_run = real
        self.assertEqual(hl._state["busy_n"], 0)

    def test_the_off_switch_and_a_lane_without_a_thread_are_respected(self):
        hl.relay_followups("t", post=self.post)
        self.ev("Stop", "x")
        open(hl.FOLLOWUP_OFF, "w").close()
        self.assertEqual(hl.relay_followups("t", post=self.post), 0)
        os.unlink(hl.FOLLOWUP_OFF)
        hl.HEAD_THREADS[OWNER] = None
        try:
            self.assertEqual(hl.relay_followups("t", post=self.post), 0)
        finally:
            hl.HEAD_THREADS[OWNER] = 777
        self.assertEqual(self.posts, [])

    def test_a_failed_send_is_logged_not_raised_and_not_retried_forever(self):
        logged = []
        real = pl._log; pl._log = lambda ev, **kw: logged.append(kw.get("event2"))
        def bad(tok, m, text): raise RuntimeError("telegram down")
        try:
            hl.relay_followups("t", post=bad)
            self.ev("Stop", "x")
            self.assertEqual(hl.relay_followups("t", post=bad), 0)
            self.assertEqual(hl.relay_followups("t", post=bad), 0)
        finally:
            pl._log = real
        self.assertEqual(logged.count("followup_fail"), 1)

    def test_default_post_goes_out_as_a_message_in_the_thread_with_no_reply_target(self):
        API.clear()
        hl.relay_followups("t")
        self.ev("Stop", "background job done")
        self.assertEqual(hl.relay_followups("t"), 1)
        sends = [kw for method, kw in API if method in ("sendMessage", "sendRichMessage")]
        self.assertTrue(sends and all(kw.get("message_thread_id") == 777 for kw in sends), API)
        self.assertTrue(all("reply_parameters" not in kw and "reply_to_message_id" not in kw for kw in sends))

    def test_a_headsafe_probe_reply_is_not_relayed_as_a_follow_up(self):
        hl.relay_followups("t", post=self.post)
        self.ev("Stop", "ALIVE headsafe-probe-1a2b3c4d")
        self.ev("Stop", "real follow-up")
        self.assertEqual(hl.relay_followups("t", post=self.post), 1)
        self.assertIn("real follow-up", self.posts[0][1])

    def test_the_live_loop_runs_the_relay_for_every_lane(self):
        import inspect
        self.assertIn("relay_followups", inspect.getsource(hl._live_loop))


def test_parse_permission_skips_numbered_list_in_reply():
    """TIK-1416: a numbered list in the head's reply above the box was posted as the permission question."""
    pane = ["● Before I post, three things to confirm:",
            "  1. Channel: Skywolf Chronicles, unless you want it somewhere else.",
            "  3. Shorts: it's vertical and under 3 minutes.",
            "─" * 40, " Bash command", "   scp -q a.mp4 laptop:x", " Do you want to proceed?",
            " ❯ 1. Yes", "   2. Yes, and don't ask again", "   3. No, and tell Claude what to do differently (esc)",
            " Esc to cancel"]
    p = hl.parse_permission(pane)
    assert [n for n, _ in p["options"]] == ["1", "2", "3"] and p["options"][0][1] == "Yes"
    assert p["title"] == "Bash command" and "Channel" not in p["detail"]
    assert hl.parse_permission(pane[:3]) is None


class OwnerDmOnly(unittest.TestCase):
    """Security review 2026-11-12: the owner's message in a GROUP topic with the same number as a head's topic was
    taken as a turn and answered in that group."""

    def test_group_topic_with_same_number_is_refused(self):
        tid = next(iter(hl.HEAD_THREADS.values()))
        m = {"from": {"id": OWNER}, "chat": {"id": -100123, "type": "supergroup"}, "message_id": 1,
             "message_thread_id": tid, "text": "hi"}
        self.assertFalse(hl._owner_dm(m, OWNER))
        self.assertTrue(hl._owner_dm(dict(m, chat={"id": OWNER, "type": "private"}), OWNER))
