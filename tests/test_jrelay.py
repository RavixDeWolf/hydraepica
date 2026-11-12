"""Tests for shared/jrelay.py (TIK-1083). Run: python3 test_jrelay.py"""
import _harness_env  # noqa: F401  throwaway config + state (must be first)
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jrelay as jr  # noqa: E402
_REAL_SEND = jr._send_rav

_TMP = tempfile.mkdtemp(prefix="jrelay_test_")
jr.LOG = os.path.join(_TMP, "relay.log")
jr.BASE_DIR = os.path.join(_TMP, "base")           # every lane spool the module derives lives under the temp dir
jr.SPOOL = os.path.join(_TMP, "relay_spool")       # (never the live <state_dir>/<lane>/relay_spool)
jr.ALERT_STATE = os.path.join(_TMP, "alerts.json")  # never the live throttle state
SENT = []
jr._send_rav = lambda origin, text, **k: SENT.append(text)   # never a real Telegram message from a test
import urllib.request  # noqa: E402
def _no_net(*a, **k):
    raise RuntimeError("test tried a real network call")
urllib.request.urlopen = _no_net   # _REAL_SEND under a mutant (e.g. "alert skips the bot DM") once sent Rav a real
                                   # "hi" through @ropegremlinbot (2026-10-29 22:57); the network is closed to tests
OWNER, USER_B, USER_C, STRANGER = 111111111, 222222222, 333333333, 99999999


def msg(uid=OWNER, chat_type="private", chat_id=None, mid=77, text="/j hello"):
    return {"message_id": mid, "from": {"id": uid, "first_name": "x"},
            "chat": {"id": chat_id if chat_id is not None else uid, "type": chat_type}, "text": text}


def events():
    with open(jr.LOG) as f:
        return [json.loads(l) for l in f if l.strip()]


class Gate(unittest.TestCase):
    def test_only_rav_as_int(self):
        self.assertTrue(jr.allowed(OWNER))
        for bad in (USER_B, USER_C, STRANGER, 0, None, str(OWNER), float(OWNER), True, -OWNER):
            self.assertFalse(jr.allowed(bad), bad)

    def test_submit_refuses_household_and_strangers(self):
        for uid in (USER_B, USER_C, STRANGER):
            with self.assertRaises(jr.RelayRefused):
                jr.submit("shop", msg(uid=uid), "hi")
        self.assertEqual(os.listdir(jr.SPOOL) if os.path.isdir(jr.SPOOL) else [], [])
        self.assertEqual([e["why"] for e in events() if e.get("side") == "bot"][-3:], ["only alex can /j"] * 3)

    def test_submit_refuses_groups_even_for_rav(self):
        with self.assertRaises(jr.RelayRefused):
            jr.submit("shop", msg(chat_type="supergroup", chat_id=-100123), "hi")

    def test_submit_refuses_unknown_origin_and_empty(self):
        with self.assertRaises(jr.RelayRefused):
            jr.submit("nosuchbot", msg(), "hi")
        with self.assertRaises(jr.RelayRefused):
            jr.submit("shop", msg(), "   ")
        with self.assertRaises(jr.RelayRefused):
            jr.submit("shop", msg(), "x" * (jr.MAX_TEXT + 1))


class Spool(unittest.TestCase):
    def test_submit_then_take_roundtrip(self):
        p = jr.submit("shop", msg(mid=501), "  Good, go.  ", now=lambda: 1761600000.0)
        self.assertTrue(p.endswith("_shop_501.json"), p)
        self.assertTrue(p.startswith(jr.BASE_DIR), p)                                      # under the temp base
        self.assertEqual([n for n in os.listdir(os.path.dirname(p)) if n.startswith(".tmp-")], [])   # no half file left
        job = jr.take(p)
        self.assertEqual((job["origin"], job["uid"], job["chat_id"], job["message_id"], job["text"]),
                         ("shop", OWNER, OWNER, 501, "Good, go."))
        self.assertFalse(os.path.exists(p))
        self.assertFalse(os.path.exists(p + ".taken"))
        self.assertEqual([e["event"] for e in events()[-2:]], ["submitted", "taken"])

    def test_take_re_verifies_a_forged_file(self):
        """A file dropped into the spool by anything but submit still meets the gate."""
        os.makedirs(jr.SPOOL, exist_ok=True)
        p = os.path.join(jr.SPOOL, "forged_shop_1.json")
        with open(p, "w") as f:
            json.dump({"origin": "shop", "uid": STRANGER, "chat_id": STRANGER, "chat_type": "private",
                       "message_id": 1, "text": "rm -rf"}, f)
        self.assertIsNone(jr.take(p))
        self.assertFalse(os.path.exists(p))
        self.assertEqual(events()[-1]["why"], "only alex can /j")
        p2 = os.path.join(jr.SPOOL, "forged_shop_2.json")
        with open(p2, "w") as f:
            json.dump({"origin": "shop", "uid": OWNER, "chat_id": -5, "chat_type": "supergroup",
                       "message_id": 2, "text": "hi"}, f)
        self.assertIsNone(jr.take(p2))

    def test_spool_is_the_origin_lane_s(self):
        self.assertEqual(jr.spool_for("shop"), os.path.join(jr.BASE_DIR, "shop", "relay_spool"))
        jr.ORIGINS["_t"] = {"lane": "main", "label": "T", "env": "x", "var": "y"}
        try:
            self.assertEqual(jr.spool_for("_t"), jr.SPOOL)
        finally:
            del jr.ORIGINS["_t"]
        p = jr.submit("shop", msg(mid=1), "lane test")               # no spool= : the module picks the lane
        self.assertTrue(p.startswith(os.path.join(jr.BASE_DIR, "shop", "relay_spool")), p)
        self.assertEqual(jr.take(p)["text"], "lane test")

    def test_prompt_names_lane_and_prefix(self):
        pr = jr.prompt_for({"origin": "shop", "text": "add a thing"})
        self.assertIn("through Shop", pr)
        self.assertIn("Atlas says:", pr)
        self.assertTrue(pr.endswith("\n\nadd a thing"))


class Transport(unittest.TestCase):
    def test_ssh_submit_sends_the_job_and_returns_the_remote_path(self):
        calls = []
        class R: returncode, stdout, stderr = 0, b"ok /remote/spool/x.json\n", b""
        def run(argv, input=None, **kw):
            calls.append((argv, json.loads(input.decode())))
            return R()
        p = jr.submit("shop", msg(mid=7), "hi", ssh=["ssh", "hb-relay"], run=run)
        self.assertEqual(p, "/remote/spool/x.json")
        self.assertEqual(calls[0][0], ["ssh", "hb-relay"])
        self.assertEqual((calls[0][1]["uid"], calls[0][1]["text"], calls[0][1]["message_id"]), (OWNER, "hi", 7))
        self.assertNotIn("token", json.dumps(calls[0][1]).lower())
        self.assertEqual(events()[-1]["via"], "ssh")

    def test_ssh_submit_refusal_and_failure_are_errors_not_silence(self):
        class R: returncode, stdout, stderr = 2, b"refused only alex can /j\n", b""
        with self.assertRaises(jr.RelayRefused):
            jr.submit("shop", msg(), "hi", ssh=["ssh", "x"], run=lambda *a, **k: R())
        self.assertEqual(events()[-1]["event"], "transport_fail")
        def boom(*a, **k):
            raise OSError("no route")
        with self.assertRaises(jr.RelayRefused):
            jr.submit("shop", msg(), "hi", ssh=["ssh", "x"], run=boom)
        self.assertEqual(os.listdir(jr.SPOOL) if os.path.isdir(jr.SPOOL) else [], [])   # nothing spooled locally

    def test_ssh_submit_gate_runs_before_any_transport(self):
        with self.assertRaises(jr.RelayRefused):
            jr.submit("shop", msg(uid=STRANGER), "hi", ssh=["ssh", "x"], run=lambda *a, **k: self.fail("sent"))

    def test_media_rides_along(self):
        """Rav 2026-10-30: a photo captioned "/j ..." through Shop lost its picture. Only a file_id travels."""
        import io, contextlib
        m = msg(text=None); m["caption"] = "/j look"
        m["photo"] = [{"file_id": "small"}, {"file_id": "BIG", "width": 900}]
        self.assertEqual(jr.media_of(m), {"kind": "photo", "file_id": "BIG", "file_name": ""})
        d = dict(msg(), document={"file_id": "D", "file_name": "a.pdf"})
        self.assertEqual(jr.media_of(d)["file_name"], "a.pdf")
        self.assertIsNone(jr.media_of(msg()))
        self.assertIsNone(jr.media_of(dict(msg(), photo=[{"nope": 1}])))
        self.assertEqual(jr._job("shop", m, "look", lambda: 1.0)["media"]["file_id"], "BIG")
        self.assertNotIn("media", jr._job("shop", msg(), "hi", lambda: 1.0))
        sp = os.path.join(_TMP, "recv_media")
        job = {"origin": "shop", "uid": OWNER, "chat_id": OWNER, "chat_type": "private", "message_id": 4, "text": "look",
               "media": {"kind": "photo", "file_id": "BIG", "evil": "x"}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(jr.receive(io.BytesIO(json.dumps(job).encode()), spool=sp), 0)
        got = jr.take(out.getvalue()[3:].strip())
        self.assertEqual(got["media"], {"kind": "photo", "file_id": "BIG", "file_name": ""})   # extra keys dropped
        job["media"] = {"kind": "shell", "file_id": "x"}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            jr.receive(io.BytesIO(json.dumps(job).encode()), spool=sp)
        self.assertNotIn("media", jr.take(out.getvalue()[3:].strip()))

    def test_bare_j_needs_media(self):
        photo = dict(msg(text="/j"), photo=[{"file_id": "S"}, {"file_id": "BIG"}])
        self.assertEqual(jr._job("shop", photo, "", lambda: 1.0)["media"]["file_id"], "BIG")
        with self.assertRaises(jr.RelayRefused):
            jr._job("shop", msg(text="/j"), "", lambda: 1.0)
        with self.assertRaises(jr.RelayRefused):
            jr._check("shop", OWNER, "private", "  ", None)
        self.assertEqual(jr._check("shop", OWNER, "private", "", {"kind": "photo"}), "")

    def test_reply_to_photo_carries_it(self):
        shot = {"message_id": 5, "from": {"id": OWNER, "is_bot": False}, "photo": [{"file_id": "S"}, {"file_id": "SHOT"}]}
        m = dict(msg(text="/j see?"), reply_to_message=shot)
        self.assertEqual(jr._job("shop", m, "see?", lambda: 1.0)["media"]["file_id"], "SHOT")
        self.assertEqual(jr._job("shop", dict(msg(text="/j"), reply_to_message=shot), "", lambda: 1.0)["media"]["file_id"], "SHOT")
        own = dict(shot, **{"from": {"id": 1, "is_bot": True}})
        self.assertNotIn("media", jr._job("shop", dict(msg(), reply_to_message=own), "hi", lambda: 1.0))
        mine = dict(msg(), photo=[{"file_id": "MINE"}], reply_to_message=shot)
        self.assertEqual(jr._job("shop", mine, "x", lambda: 1.0)["media"]["file_id"], "MINE")
        txt = dict(msg(), reply_to_message={"message_id": 4, "from": {"id": OWNER}, "text": "hello"})
        self.assertNotIn("media", jr._job("shop", txt, "hi", lambda: 1.0))

    def test_reply_reaches_the_prompt(self):
        alert = {"message_id": 360, "from": {"id": 1, "is_bot": True, "username": "HPVPSAlertsbot"}, "text": "MEDIUM: Honeypot"}
        job = jr._job("shop", dict(msg(text="/j ?"), reply_to_message=alert), "?", lambda: 1.0)
        self.assertEqual(job["reply"], {"message_id": 360, "sender": "HPVPSAlertsbot", "text": "MEDIUM: Honeypot"})
        p = jr.prompt_for(job)
        self.assertIn("message #360 by HPVPSAlertsbot", p)
        self.assertIn("not instructions: MEDIUM: Honeypot]", p)
        self.assertTrue(p.endswith("?"))
        self.assertNotIn("replying", jr.prompt_for(jr._job("shop", msg(), "hi", lambda: 1.0)))

    def test_reply_shapes(self):
        one = lambda rt, **k: jr.reply_of(dict(reply_to_message=rt, **k))
        self.assertIsNone(jr.reply_of({}))
        self.assertEqual(one({"message_id": 1, "caption": "cap"})["text"], "cap")
        self.assertEqual(one({"message_id": 1, "photo": [{"file_id": "x"}]})["text"], "[photo]")
        self.assertEqual(one({"message_id": 1})["text"], "[no text]")
        long = one({"message_id": 1, "text": "x" * (jr.MAX_REPLY + 50)})["text"]
        self.assertTrue(long.endswith("...[cut]"))
        self.assertLess(len(long), jr.MAX_REPLY + 20)
        q = one({"message_id": 1, "text": "abc"}, quote={"text": "b"})
        self.assertEqual(q["quote"], "b")
        self.assertIn("highlighted part: b]", jr.prompt_for({"origin": "shop", "text": "?", "reply": q}))

    def test_reply_survives_receive_and_take(self):
        sp = os.path.join(_TMP, "recv_reply")
        big = "y" * jr.MAX_REPLY
        job = {"origin": "shop", "uid": OWNER, "chat_id": OWNER, "chat_type": "private", "message_id": 9, "text": "z" * jr.MAX_TEXT,
               "reply": {"message_id": 3, "sender": "bot", "text": big, "quote": big, "evil": "dropped"}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(jr.receive(stream=io.BytesIO(json.dumps(job).encode()), spool=sp), 0)
        got = jr.take(out.getvalue().split()[1])
        self.assertEqual(got["reply"], {"message_id": 3, "sender": "bot", "text": big, "quote": big})

    def test_bare_media_job_survives_receive_and_take(self):
        sp = os.path.join(_TMP, "recv_bare")
        job = {"origin": "shop", "uid": OWNER, "chat_id": OWNER, "chat_type": "private", "message_id": 9, "text": "",
               "media": {"kind": "photo", "file_id": "BIG"}}
        import io
        out = io.StringIO(); old = sys.stdout; sys.stdout = out
        try:
            rc = jr.receive(io.BytesIO(json.dumps(job).encode()), spool=sp)
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0, out.getvalue())
        got = jr.take(out.getvalue()[3:].strip())
        self.assertEqual((got["text"], got["media"]["file_id"]), ("", "BIG"))
        job["media"] = {"kind": "shell", "file_id": "x"}
        out = io.StringIO(); sys.stdout = out
        try:
            rc = jr.receive(io.BytesIO(json.dumps(job).encode()), spool=sp)
        finally:
            sys.stdout = old
        self.assertEqual(rc, 2)

    def test_receive_spools_a_good_job_and_refuses_a_bad_one(self):
        import io
        sp = os.path.join(_TMP, "recv_spool")
        good = json.dumps({"origin": "shop", "uid": OWNER, "chat_id": OWNER, "chat_type": "private", "message_id": 3,
                           "text": "go", "ts": 1761600000.0}).encode()
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = jr.receive(io.BytesIO(good), spool=sp)
        self.assertEqual(rc, 0)
        self.assertTrue(out.getvalue().startswith("ok " + sp), out.getvalue())
        self.assertEqual(jr.take(out.getvalue()[3:].strip())["text"], "go")
        for bad in (b"not json", b"[1]", json.dumps({"origin": "shop", "uid": USER_B, "chat_id": 1, "chat_type": "private", "text": "x"}).encode(),
                    json.dumps({"origin": "shop", "uid": OWNER, "chat_id": -9, "chat_type": "group", "text": "x"}).encode(),
                    json.dumps({"origin": "evil", "uid": OWNER, "chat_id": OWNER, "chat_type": "private", "text": "x"}).encode()):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(jr.receive(io.BytesIO(bad), spool=sp), 2, bad)
            self.assertTrue(out.getvalue().startswith("refused "))
        self.assertEqual(os.listdir(sp), [])


class Button(unittest.TestCase):
    """Rav 2026-11-05: the Desk "Yes" button did nothing; the bot dropped every press."""
    def setUp(self):                              # own log: the Gate tests read the shared one's tail
        self._log, jr.LOG = jr.LOG, os.path.join(tempfile.mkdtemp(), "relay.log")

    def tearDown(self):
        jr.LOG = self._log

    def cq(self, uid=OWNER, data="hd:p:0123456789ab:a", chat_type="private"):
        return {"id": "77", "from": {"id": uid}, "data": data,
                "message": {"message_id": 9, "chat": {"id": OWNER, "type": chat_type}, "text": "Do you want to proceed?",
                            "reply_markup": {"inline_keyboard": [[{"text": "Yes", "callback_data": data}]]}}}

    def test_press_roundtrip(self):
        sp = os.path.join(_TMP, "btn_spool")
        job = jr.take(jr.submit_button("desk", self.cq(), spool=sp, ssh=[]))
        self.assertEqual(job["button"]["data"], "hd:p:0123456789ab:a")
        self.assertEqual((job["uid"], job["text"], job["button"]["id"]), (OWNER, "", "77"))

    def test_press_refused(self):
        sp = os.path.join(_TMP, "btn_spool2")
        for c in (self.cq(uid=USER_B), self.cq(chat_type="group"), self.cq(data="sd:1:somesite"),
                  self.cq(data="hd:" + "x" * 80), {"data": "hd:1"}):
            with self.assertRaises(jr.RelayRefused):
                jr.submit_button("desk", c, spool=sp, ssh=[])
        self.assertFalse(os.path.exists(sp) and [n for n in os.listdir(sp) if n.endswith(".json")])

    def test_press_survives_receive_and_a_forged_one_does_not(self):
        import io, contextlib
        sp = os.path.join(_TMP, "btn_recv")
        b = jr.button_of(self.cq())
        good = dict(v=1, origin="desk", uid=OWNER, chat_id=OWNER, chat_type="private", message_id=9, text="", button=b, ts=1.0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(jr.receive(io.BytesIO(json.dumps(good).encode()), spool=sp), 0)
        self.assertEqual(jr.take(out.getvalue()[3:].strip())["button"]["data"], b["data"])
        for bad in (dict(good, button=dict(b, data="rm -rf /")), dict(good, button="hd:p"),
                    dict(good, text="hi", button="hd:p"), dict(good, text="hi", button=dict(b, data="evil"))):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertNotEqual(jr.receive(io.BytesIO(json.dumps(bad).encode()), spool=sp), 0)

    def test_take_rechecks_a_forged_button_file(self):
        sp = os.path.join(_TMP, "btn_forge"); os.makedirs(sp, exist_ok=True)
        p = os.path.join(sp, "x.json")
        json.dump(dict(origin="desk", uid=OWNER, chat_id=OWNER, chat_type="private", message_id=9, text="hi",
                       button={"id": "1", "data": "evil", "message_id": 9}), open(p, "w"))
        self.assertIsNone(jr.take(p))


class Token(unittest.TestCase):
    def test_token_from_env_file(self):
        p = os.path.join(_TMP, "a.env")
        with open(p, "w") as f:
            f.write("# c\nOTHER=1\nBOT_TOKEN=\"12:abc\"\n")
        self.assertEqual(jr.token_for("shop", env_path=p), "12:abc")
        with open(p, "w") as f:
            f.write("OTHER=1\n")
        with self.assertRaises(jr.RelayRefused):
            jr.token_for("shop", env_path=p)
        with self.assertRaises(jr.RelayRefused):
            jr.token_for("nobody", env_path=p)

    def test_token_from_bare_file(self):
        p = os.path.join(_TMP, "bare.tok")
        with open(p, "w") as f:
            f.write("34:xyz\n")
        self.assertEqual(jr.token_for("mall", env_path=p), "34:xyz")
        with open(p, "w") as f:
            f.write("\n")
        with self.assertRaises(jr.RelayRefused):
            jr.token_for("mall", env_path=p)

    def test_ops_origin_routes_to_its_own_lane(self):
        self.assertEqual(jr.ORIGINS["ops"]["lane"], "ops")
        self.assertTrue(jr.spool_for("ops").endswith("/ops/relay_spool"))
        self.assertIsNone(jr.ORIGINS["ops"]["var"])              # bare-token file

    def test_blog_origin_routes_to_its_own_lane(self):
        self.assertEqual(jr.ORIGINS["blog"]["lane"], "blog")
        self.assertTrue(jr.spool_for("blog").endswith("/blog/relay_spool"))
        self.assertEqual(jr.ORIGINS["mall"]["lane"], "mall")


class Stop(unittest.TestCase):
    def test_stop_forms(self):
        for x in ("stop", "STOP", "/stop", " Stop ", "/stop@RopeGremlinBot", "stop!", "/STOP."):
            self.assertTrue(jr.is_stop(x), x)

    def test_not_stop(self):
        for x in ("stop the music", "please stop", "stopwatch", "/stopp", "", None, "don't stop", "/j stop", "s top"):
            self.assertFalse(jr.is_stop(x), x)


class Strangers(unittest.TestCase):
    MEL = 444444444

    def setUp(self):
        del SENT[:]
        self.st = os.path.join(tempfile.mkdtemp(dir=_TMP), "a.json")
        self.t = [1000.0]

    def m(self, uid=None, ctype="supergroup", title="Crew", username=None, first="Pat"):
        f = {"id": uid or self.MEL, "first_name": first}
        if username:
            f["username"] = username
        c = {"id": -100123, "type": ctype}
        if title:
            c["title"] = title
        return {"message_id": 5, "from": f, "chat": c}

    def call(self, m, text="/j hello there", origin="crm"):
        import random
        return jr.stranger(origin, m, text, now=lambda: self.t[0], send=SENT.append, state_path=self.st, rng=random.Random(1))

    def test_alert_text_for_the_real_case(self):
        self.call(self.m(), "hello there")
        self.assertEqual(SENT, ["🚫 Pat (id 444444444) tried /j on CRM in Crew (supergroup): 'hello there'"])

    def test_alert_names_username_and_private_chat(self):
        self.call(self.m(uid=42, ctype="private", title=None, username="sam", first="Sam"), "x")
        self.assertEqual(SENT, ["🚫 Sam (@sam, id 42) tried /j on CRM in a private chat: 'x'"])

    def test_one_alert_per_uid_and_origin_per_day(self):
        self.call(self.m(uid=42)); self.t[0] += 3600; self.call(self.m(uid=42))
        self.assertEqual(len(SENT), 1)
        self.call(self.m(uid=42), origin="ops")                       # another bot: its own alert
        self.assertEqual(len(SENT), 2)
        self.t[0] += 24 * 3600; self.call(self.m(uid=42))
        self.assertEqual(len(SENT), 3)

    def test_log_carries_who_where_what(self):
        self.call(self.m(username="mel"), "y" * 300)
        e = [x for x in events() if x.get("event") == "refused" and x.get("side") == "crm"][-1]
        self.assertEqual((e["uid"], e["username"], e["first_name"], e["chat"], e["chat_type"], e["chat_title"]),
                         (self.MEL, "mel", "Pat", -100123, "supergroup", "Crew"))
        self.assertEqual(len(e["text"]), 200)

    def test_pat_is_teased_at_most_every_10_minutes(self):
        r1 = self.call(self.m())
        self.assertIn(r1, jr.TEASE[self.MEL]["lines"])
        self.t[0] += 300
        self.assertIsNone(self.call(self.m()))
        self.t[0] += 301
        self.assertIn(self.call(self.m()), jr.TEASE[self.MEL]["lines"])

    def test_other_strangers_get_nothing(self):
        self.assertIsNone(self.call(self.m(uid=42)))

    def test_failed_alert_is_retried_next_time(self):
        def boom(t):
            raise RuntimeError("tg down")
        jr.stranger("crm", self.m(uid=43), "a", now=lambda: self.t[0], send=boom, state_path=self.st)
        self.call(self.m(uid=43))
        self.assertEqual(len(SENT), 1)

    def test_submit_from_pat_carries_the_tease(self):
        with self.assertRaises(jr.RelayRefused) as cm:
            jr.submit("crm", self.m(ctype="private", title=None), "hi")
        self.assertIn(cm.exception.tease, jr.TEASE[self.MEL]["lines"])


class AlertRoute(unittest.TestCase):
    def test_alert_goes_through_the_origin_bot_to_rav(self):
        posted, fell = [], []
        _REAL_SEND("crm", "hi", post=lambda o, t: posted.append((o, t)), fallback=fell.append)
        self.assertEqual((posted, fell), ([("crm", "hi")], []))

    def test_bot_send_failure_falls_back_to_the_hb_thread_and_is_logged(self):
        fell = []
        def boom(o, t):
            raise RuntimeError("bot blocked")
        _REAL_SEND("blog", "hey", post=boom, fallback=fell.append)
        self.assertEqual(fell, ["hey"])
        self.assertEqual(events()[-1]["event"], "stranger_alert_fallback")


if __name__ == "__main__":
    unittest.main(verbosity=1)
