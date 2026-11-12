"""Tests for the agent Mini App server (TIK-1004). Run: python3 test_server.py"""
import _harness_env  # noqa: F401  throwaway config + state (must be first)
import _tmpguard  # noqa: F401  TIK-0707: all temp dirs of this suite land under ONE per-run root removed at exit (must stay the first import)
import json
import os
import sys
import tempfile
import time
import unittest
from urllib.parse import urlencode

_TMP = tempfile.mkdtemp(prefix="miniapp_test_")
os.environ["STAGE_STATE"] = _TMP
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness", "stage"))
import tg_webapp_auth as auth  # noqa: E402
import server as s             # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TOK = "42:TEST"
s.bot_token = lambda: TOK
# 20:47-20:55: a reloaded turn re-ran the REAL speech-to-text on fake test audio and wrote stt_fail lines into the
# LIVE tg_print.log. No test may reach real STT or the live Live folders: stub it, point everything at temp.
import live as _live  # noqa: E402
from pathlib import Path as _P  # noqa: E402
_live._stt = lambda p: ""
_live.LIVE_DIR, _live.SPOOL = _P(_TMP) / "live", _P(_TMP) / "spool"
KEY = "k" * 40
open(os.path.join(_TMP, "push.key"), "w").write(KEY)
OWNER, USER_B, USER_C, STRANGER = 111111111, 333333333, 222222222, 999
c = TestClient(s.app)


def init(uid, age=10, token=TOK):
    f = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": uid, "first_name": "x"})}
    f["hash"] = auth.sign(f, token)
    return urlencode(f)


def H(uid, **kw):
    return {"X-Init-Data": init(uid, **kw)}


class Auth(unittest.TestCase):
    def test_family_only(self):
        self.assertEqual(c.get("/api/me", headers=H(OWNER)).json()["person"], "alex")
        self.assertEqual(c.get("/api/me", headers=H(USER_B)).json()["person"], "jo")
        self.assertEqual(c.get("/api/me", headers=H(STRANGER)).status_code, 403)
        self.assertEqual(c.get("/api/me").status_code, 401)
        self.assertEqual(c.get("/api/me", headers=H(OWNER, token="9:OTHER")).status_code, 401)
        self.assertEqual(c.get("/api/me", headers=H(OWNER, age=99999999)).status_code, 401)

    def test_settings_admin_is_rav(self):
        self.assertTrue(c.get("/api/me", headers=H(OWNER)).json()["settings_admin"])
        self.assertFalse(c.get("/api/me", headers=H(USER_C)).json()["settings_admin"])

    def test_push_needs_the_push_key(self):
        item = {"kind": "card", "title": "t", "body": "b"}
        self.assertEqual(c.post("/api/push/alex", json=item).status_code, 401)
        self.assertEqual(c.post("/api/push/alex", json=item, headers={"X-Push-Key": "wrong"}).status_code, 401)
        self.assertEqual(c.post("/api/push/alex", json=item, headers=H(OWNER)).status_code, 401)   # a person can't push
        self.assertEqual(c.post("/api/push/alex", json=item, headers={"X-Push-Key": KEY}).status_code, 200)

    def test_empty_key_file_locks_push(self):
        p = os.path.join(_TMP, "push.key")
        open(p, "w").write("")
        try:
            item = {"kind": "card", "title": "t"}
            self.assertEqual(c.post("/api/push/alex", json=item).status_code, 401)
            self.assertEqual(c.post("/api/push/alex", json=item, headers={"X-Push-Key": ""}).status_code, 401)
        finally:
            open(p, "w").write(KEY)


class Stage(unittest.TestCase):
    def setUp(self):
        for p in ("alex", "jo", "sam"):
            s.save(p, {"history": [], "live": None, "saved": [], "inbox": [], "archive": []})

    def push(self, person, item):
        return c.post(f"/api/push/{person}", json=item, headers={"X-Push-Key": KEY})

    def test_each_person_sees_only_their_own(self):
        self.push("alex", {"kind": "card", "title": "for rav", "body": "x"})
        self.push("jo", {"kind": "card", "title": "for jo", "body": "y"})
        r = c.get("/api/stage", headers=H(OWNER)).json()["history"]
        w = c.get("/api/stage", headers=H(USER_B)).json()["history"]
        self.assertEqual([i["title"] for i in r], ["for rav"])
        self.assertEqual([i["title"] for i in w], ["for jo"])

    def test_newest_first_and_capped(self):
        for n in range(s.MAX_ITEMS + 5):
            s.add_item("alex", {"kind": "card", "title": str(n)}, now=1000 + n)
        v = c.get("/api/stage", headers=H(OWNER)).json()
        self.assertEqual(len(v["history"]), s.MAX_ITEMS)
        self.assertEqual(v["history"][0]["title"], str(s.MAX_ITEMS + 4))
        self.assertEqual(v["live"]["title"], str(s.MAX_ITEMS + 4))
        # overflow is ARCHIVED, never dropped (Rav 2026-10-24)
        self.assertEqual([i["title"] for i in v["archive"]], ["4", "3", "2", "1", "0"])

    def test_gallery_rating_lands_on_that_picture_and_stays_in_range(self):
        iid = self.push("alex", {"kind": "gallery", "images": [{"src": "data:image/png;base64,AA=="}, {"src": "data:image/png;base64,AA=="}]}).json()["id"]
        for stars, code in ((6, 200), (7, 400), (-1, 400)):
            r = c.post("/api/act", headers=H(OWNER), json={"where": "history", "id": iid, "action": "rate", "i": 1, "title": str(stars)})
            self.assertEqual(r.status_code, code)
        ims = c.get(f"/api/item/history/{iid}", headers=H(OWNER)).json()["images"]
        self.assertEqual([im.get("stars") for im in ims], [None, 6])

    def test_gallery_note_lands_on_that_picture(self):
        g = {"kind": "gallery", "images": [{"src": "data:image/png;base64,AA==", "caption": f"p{n}"} for n in range(3)]}
        iid = self.push("alex", g).json()["id"]
        r = c.post("/api/act", headers=H(OWNER), json={"where": "history", "id": iid, "action": "note", "i": 1, "title": " love it "})
        self.assertEqual(r.json()["note"], "love it")
        ims = c.get("/api/stage", headers=H(OWNER)).json()["live"]["images"]
        self.assertEqual([im["note"] for im in ims], ["", "love it", ""])
        bad = c.post("/api/act", headers=H(OWNER), json={"where": "history", "id": iid, "action": "note", "i": 9, "title": "x"})
        self.assertEqual(bad.status_code, 400)

    def test_big_video_streams_from_disk_for_its_person_only(self):
        d = s.STATE / "media" / "alex"; d.mkdir(parents=True, exist_ok=True)
        (d / "big.mp4").write_bytes(b"0123456789")
        self.assertEqual(self.push("alex", {"kind": "video", "src": "media:alex/big.mp4"}).status_code, 200)
        self.assertEqual(self.push("alex", {"kind": "video", "src": "media:alex/missing.mp4"}).status_code, 400)
        self.assertEqual(self.push("alex", {"kind": "video", "src": "media:alex/../x.mp4"}).status_code, 400)
        r = c.get("/api/media/big.mp4", headers={**H(OWNER), "range": "bytes=2-4"})
        self.assertEqual((r.status_code, r.content), (206, b"234"))
        self.assertEqual(c.get("/api/media/big.mp4", headers=H(USER_B)).status_code, 404)   # not jo's file
        self.assertIn(c.get("/api/media/big.mp4").status_code, (401, 403))

    def test_validation(self):
        self.assertEqual(self.push("alex", {"kind": "script"}).status_code, 400)
        self.assertEqual(self.push("alex", {"kind": "image", "src": "https://evil/x.png"}).status_code, 400)
        self.assertEqual(self.push("alex", {"kind": "video", "src": "https://evil/x.mp4"}).status_code, 400)
        self.assertEqual(self.push("alex", {"kind": "video", "src": "data:video/mp4;base64,AAAA"}).status_code, 200)
        self.assertEqual(self.push("alex", {"kind": "url", "url": "javascript:alert(1)"}).status_code, 400)
        self.assertEqual(self.push("nobody", {"kind": "card"}).status_code, 404)
        self.assertEqual(self.push("alex", {"kind": "card", "body": "x" * (s.MAX_ITEM_BYTES + 1)}).status_code, 413)

    def test_unknown_fields_dropped(self):
        self.push("alex", {"kind": "card", "title": "t", "onload": "evil()"})
        it = c.get("/api/stage", headers=H(OWNER)).json()["live"]
        self.assertNotIn("onload", it)

    def test_clear_is_per_person(self):
        self.push("alex", {"kind": "card", "title": "a"})
        self.push("jo", {"kind": "card", "title": "b"})
        c.post("/api/clear", headers=H(OWNER))
        r = c.get("/api/stage", headers=H(OWNER)).json()
        self.assertIsNone(r["live"])
        self.assertEqual(len(r["history"]), 1)                         # clear blanks the stage, keeps history
        self.assertEqual(c.get("/api/stage", headers=H(USER_B)).json()["live"]["title"], "b")

    def test_live_push_reaches_only_that_person(self):
        import asyncio
        qr, qw = asyncio.Queue(), asyncio.Queue()
        s._subs.clear()
        s._subs["alex"] = {qr}; s._subs["jo"] = {qw}
        self.push("alex", {"kind": "card", "title": "live"})
        self.assertEqual(qr.get_nowait()["type"], "state")
        self.assertTrue(qw.empty())
        s._subs.clear()

    def a(self, uid, **b):  # noqa
        return c.post("/api/act", json=b, headers=H(uid))

    def ids(self, person, where):
        return [i["id"] for i in s.load(person)[where]]

    def test_lists_are_metadata_and_item_is_full(self):
        self.push("alex", {"kind": "html", "title": "p", "html": "<b>x</b>"})
        self.push("alex", {"kind": "card", "title": "q", "body": "B"})
        v = c.get("/api/stage", headers=H(OWNER)).json()
        self.assertNotIn("html", v["history"][1])
        self.assertEqual(v["live"]["body"], "B")
        full = c.get(f"/api/item/history/{v['history'][1]['id']}", headers=H(OWNER)).json()
        self.assertEqual(full["html"], "<b>x</b>")
        self.assertEqual(c.get(f"/api/item/history/{v['history'][1]['id']}", headers=H(USER_B)).status_code, 404)

    def test_show_from_history_sets_live(self):
        self.push("alex", {"kind": "card", "title": "old"}); self.push("alex", {"kind": "card", "title": "new"})
        old = self.ids("alex", "history")[1]
        self.assertEqual(self.a(OWNER, where="history", id=old, action="show").status_code, 200)
        self.assertEqual(s.load("alex")["live"], old)

    def test_save_is_a_permanent_copy_and_fav(self):
        self.push("alex", {"kind": "card", "title": "tpl"})
        h = self.ids("alex", "history")[0]
        sid = self.a(OWNER, where="history", id=h, action="save").json()["saved_id"]
        self.a(OWNER, where="history", id=h, action="delete")
        st = s.load("alex")
        self.assertEqual([i["title"] for i in st["saved"]], ["tpl"])
        self.assertIsNone(st["live"])
        self.a(OWNER, where="saved", id=sid, action="fav")
        self.assertTrue(s.load("alex")["saved"][0]["fav"])
        self.assertEqual(self.a(OWNER, where="history", id="nope", action="fav").status_code, 404)
        # showing a saved stage puts THE SAME stage live (same id); it stays saved
        self.a(OWNER, where="saved", id=sid, action="show")
        st = s.load("alex")
        self.assertEqual(len(st["saved"]), 1)
        self.assertEqual(st["history"][0]["title"], "tpl")
        self.assertEqual(st["live"], sid)
        self.assertEqual(sid, h)
        self.a(OWNER, where="saved", id=sid, action="show")      # again: no second copy in Recent
        self.assertEqual(len(s.load("alex")["history"]), 1)

    def test_one_stage_one_identity(self):
        """Rav 2026-10-24: saving twice made two stages. It is one stage."""
        self.push("alex", {"kind": "card", "title": "p"})
        h = self.ids("alex", "history")[0]
        self.a(OWNER, where="history", id=h, action="save")
        self.assertTrue(self.a(OWNER, where="history", id=h, action="save").json()["already"])
        self.assertEqual(self.ids("alex", "saved"), [h])
        self.a(OWNER, where="history", id=h, action="send", to="jo")
        self.a(OWNER, where="history", id=h, action="send", to="jo")
        self.assertEqual(self.ids("jo", "inbox"), [h])

    def test_rename_everywhere(self):
        self.push("alex", {"kind": "card", "title": "old"})
        h = self.ids("alex", "history")[0]
        self.a(OWNER, where="history", id=h, action="save")
        self.assertEqual(self.a(OWNER, where="saved", id=h, action="rename", title="  New name ").status_code, 200)
        st = s.load("alex")
        self.assertEqual(st["history"][0]["title"], "New name"); self.assertEqual(st["saved"][0]["title"], "New name")
        self.assertEqual(self.a(OWNER, where="saved", id=h, action="rename", title=" ").status_code, 400)
        self.assertEqual(self.a(OWNER, where="saved", id=h, action="rename", title="x" * 121).status_code, 400)

    def test_old_duplicate_saves_fold_back(self):
        s.save("alex", {"history": [{"id": "5", "kind": "card", "ts": 1}], "live": "5", "inbox": [], "archive": [],
                       "saved": [{"id": "8", "kind": "card", "ts": 1, "origin": "history:5"},
                                 {"id": "7", "kind": "card", "ts": 1, "origin": "history:5"}]})
        self.assertEqual(self.ids("alex", "saved"), ["5"])

    def test_saved_never_expires_archive_expires_after_90_days(self):
        old = time.time() - (s.ARCHIVE_DAYS + 1) * 86400
        s.save("alex", {"history": [], "live": None, "inbox": [],
                       "saved": [{"id": "1", "kind": "card", "ts": old, "saved_ts": old}],
                       "archive": [{"id": "2", "kind": "card", "ts": old, "archived_ts": old},
                                   {"id": "3", "kind": "card", "ts": old, "archived_ts": time.time()}]})
        st = s.load("alex")
        self.assertEqual([i["id"] for i in st["archive"]], ["3"])
        self.assertEqual([i["id"] for i in st["saved"]], ["1"])

    def test_archive_and_restore(self):
        self.push("alex", {"kind": "card", "title": "x"})
        h = self.ids("alex", "history")[0]
        self.a(OWNER, where="history", id=h, action="archive")
        st = s.load("alex")
        self.assertEqual(st["history"], []); self.assertIsNone(st["live"])
        self.a(OWNER, where="archive", id=h, action="show")
        st = s.load("alex")
        self.assertEqual(st["archive"], []); self.assertEqual(st["live"], h)

    def test_send_lands_in_the_other_inbox_only(self):
        import asyncio
        self.push("alex", {"kind": "card", "title": "hi sam"})
        h = self.ids("alex", "history")[0]
        qs = asyncio.Queue(); s._subs.clear(); s._subs["sam"] = {qs}
        self.assertEqual(self.a(OWNER, where="history", id=h, action="send", to="sam").status_code, 200)
        self.assertEqual(qs.get_nowait()["type"], "inbox")
        s._subs.clear()
        su = s.load("sam")
        self.assertEqual(su["inbox"][0]["from"], "alex"); self.assertEqual(su["history"], [])
        self.assertEqual(s.load("jo")["inbox"], [])
        self.assertEqual(self.a(OWNER, where="history", id=h, action="send", to="alex").status_code, 400)
        self.assertEqual(self.a(OWNER, where="history", id=h, action="send", to="pat").status_code, 400)

    def test_old_shape_migrates(self):
        s._file("alex").write_text(json.dumps({"items": [{"id": "9", "kind": "card", "ts": 1}]}))
        st = s.load("alex")
        self.assertEqual(st["live"], "9"); self.assertEqual(len(st["history"]), 1)

    def test_act_needs_a_person(self):
        self.assertEqual(c.post("/api/act", json={}).status_code, 401)


    def test_events_need_a_person(self):
        self.assertEqual(c.get("/api/events").status_code, 401)

    def test_page_served(self):
        r = c.get("/app")
        self.assertEqual(r.status_code, 200)
        self.assertIn('sandbox", "allow-scripts"', r.text)          # html items never get same-origin
        self.assertNotIn("allow-same-origin", r.text)






class Cli(unittest.TestCase):
    def test_items(self):
        import stage
        ns = lambda **k: type("A", (), dict({"caption": None, "title": None, "b": None}, **k))
        self.assertEqual(stage.item_from(ns(kind="card", a="T", b="B")), {"kind": "card", "title": "T", "body": "B"})
        p = os.path.join(_TMP, "x.png"); open(p, "wb").write(b"\x89PNG")
        self.assertTrue(stage.item_from(ns(kind="image", a=p))["src"].startswith("data:image/png;base64,"))
        t = os.path.join(_TMP, "x.txt"); open(t, "w").write("hi")
        with self.assertRaises(SystemExit):
            stage.item_from(ns(kind="image", a=t))
        v = os.path.join(_TMP, "x.mp4"); open(v, "wb").write(b"\0\0\0\x18ftyp")
        self.assertTrue(stage.item_from(ns(kind="video", a=v))["src"].startswith("data:video/mp4;base64,"))
        with self.assertRaises(SystemExit):
            stage.item_from(ns(kind="video", a=p))
        big = os.path.join(_TMP, "big.mp4"); open(big, "wb").write(b"\0" * (stage.VIDEO_MAX_BYTES + 1))
        src = stage.item_from(ns(kind="video", a=big, to="alex"))["src"]     # too big to inline: kept on disk
        self.assertRegex(src, r"^media:alex/\d+-big\.mp4$")
        self.assertEqual(os.path.getsize(os.path.join(os.environ["STAGE_STATE"], "media", src[6:])), stage.VIDEO_MAX_BYTES + 1)




class Live(unittest.TestCase):
    """TIK-1025 step 2: frames + push-to-talk, Rav only (the main head is his)."""
    def setUp(self):
        import live
        self.live = live
        d = tempfile.mkdtemp(dir=_TMP)
        live.LIVE_DIR, live.SPOOL = s.Path(d) / "live", s.Path(d) / "spool"

    JPG = b"\xff\xd8\xff" + b"x" * 500

    def test_head_picker(self):
        """Rav 2026-10-30: Live goes to the head he picks; never a stranger's lane, never a silent fall back to hb."""
        L = self.live
        base = L.SPOOL.parent
        json.dump(["notes", "rg", "gone"], open(base / "lanes.json", "w"))
        for lane, th in (("notes", {str(OWNER): 1337510}), ("rg", {})):
            (base / lane).mkdir()
            json.dump(th, open(base / lane / "tg_threads.json", "w"))
        json.dump({"title": "Notes"}, open(base / "notes" / "made_by_new_lane.json", "w"))
        self.assertEqual(L.lanes(OWNER), [{"lane": "main", "name": "Atlas"}, {"lane": "notes", "name": "Notes"}])
        self.assertEqual(L.spool_for(OWNER, "notes"), base / "notes" / "live_spool")
        self.assertEqual(L.spool_for(OWNER, None), L.SPOOL)
        for bad in ("rg", "gone", "../x"):
            with self.assertRaises(L.LiveError):
                L.spool_for(OWNER, bad)
        r = c.get("/api/live/lanes", headers=H(OWNER))
        self.assertEqual([x["lane"] for x in r.json()["lanes"]], ["main", "notes"])
        self.assertEqual(c.get("/api/live/lanes", headers=H(USER_B)).status_code, 403)
        L._segs.clear()
        L.add_segment(OWNER, b"a" * 5000, "audio/webm", 2, stt=lambda p: "hello notes", notify=lambda e: None,
                      run=lambda f: f())
        r = c.post("/api/live/end", headers=dict(H(OWNER), **{"X-Live-Lane": "rg"}))
        self.assertEqual(r.status_code, 400)
        self.assertTrue(L._segs)                                                  # a bad pick keeps the recording
        old = L.contact_sheet
        L.contact_sheet = lambda fr, out, log=print: out
        try:
            r = c.post("/api/live/end", headers=dict(H(OWNER), **{"X-Live-Lane": "notes"}))
        finally:
            L.contact_sheet = old
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue((base / "notes" / "live_spool" / r.json()["job"]).exists())
        self.assertTrue(L.say(OWNER, b"a" * 5000, "audio/webm", lane="notes").startswith(str(base / "notes" / "live_spool")))

    def test_rav_only(self):
        for uid in (USER_B, USER_C):
            self.assertEqual(c.post("/api/live/frame", content=self.JPG, headers=H(uid)).status_code, 403)
            self.assertEqual(c.post("/api/live/say", content=b"a" * 5000, headers=H(uid)).status_code, 403)
        self.assertEqual(c.post("/api/live/frame", content=self.JPG).status_code, 401)
        self.assertFalse(self.live.SPOOL.exists())

    def test_frame_kept_and_ring_capped(self):
        for i in range(self.live.KEEP + 5):
            self.live.save_frame(self.JPG, now=1000 + i)
        self.assertEqual(len(list((self.live.LIVE_DIR / "frames").glob("*.jpg"))), self.live.KEEP)
        self.assertTrue((self.live.LIVE_DIR / "latest.jpg").exists())

    def test_not_a_jpeg_refused(self):
        r = c.post("/api/live/frame", content=b"<html>", headers=H(OWNER))
        self.assertEqual(r.status_code, 400)

    def test_frame_over_http(self):
        self.assertEqual(c.post("/api/live/frame", content=self.JPG, headers=H(OWNER)).status_code, 200)
        self.assertEqual(c.get("/api/live/status", headers=H(OWNER)).json()["frames"], 1)

    def test_say_spools_audio_with_recent_frames(self):
        now = time.time()
        self.live.save_frame(self.JPG, now=now - 60)        # too old to belong to this clip
        for i in range(10):
            self.live.save_frame(self.JPG, now=now - 10 + i)
        hdr = dict(H(OWNER)); hdr["Content-Type"] = "audio/mp4"
        r = c.post("/api/live/say", content=b"a" * 5000, headers=hdr)
        self.assertEqual(r.status_code, 200)
        jobs = list(self.live.SPOOL.glob("*.json"))
        self.assertEqual(len(jobs), 1)
        j = json.loads(jobs[0].read_text())
        self.assertEqual(j["uid"], OWNER)
        self.assertTrue(j["audio"].endswith(".m4a"))
        self.assertEqual(len(j["frames"]), self.live.RECENT_N)
        self.assertTrue(j["frames"][-1].endswith(f"{int((now - 1) * 1000)}.jpg"))   # the newest is always included
        self.assertEqual(list(self.live.SPOOL.glob("*.part")), [])

    def test_a_long_hold_covers_the_whole_hold(self):
        """TIK-1025 house tour: a minute-long hold got only its last 4 frames."""
        now = 10_000.0
        for i in range(60):                                     # 90 s of frames, one every 1.5 s
            self.live.save_frame(self.JPG, now=now - 90 + i * 1.5)
        held = self.live.hold_frames(now - 60, now)
        self.assertEqual(len(held), self.live.HOLD_CAP)
        self.assertLess(held[0][0], 2.5)                         # starts at the START of the hold...
        self.assertGreater(held[-1][0], 57)                      # ...and ends at its end
        self.assertTrue(all(t >= 0 for t, _ in held))
        made = []
        old = self.live.contact_sheet
        self.live.contact_sheet = lambda fr, out, log=print: made.append((fr, out)) or out
        try:
            job = json.loads(open(self.live.say(OWNER, b"a" * 5000, "audio/webm", now=now, hold_s=60)).read())
        finally:
            self.live.contact_sheet = old
        self.assertEqual(len(job["frames"]), self.live.HOLD_CAP)
        self.assertEqual(job["sheet"], made[0][1]); self.assertEqual(job["hold_s"], 60)
        self.assertEqual(len(made[0][0]), self.live.HOLD_CAP)

    def test_hold_length_reaches_say(self):
        seen = {}
        old = self.live.say
        self.live.say = lambda uid, audio, mime, hold_s=None, lane=None: seen.update(hold_s=hold_s) or str(self.live.SPOOL / "x.json")
        try:
            hdr = dict(H(OWNER)); hdr["X-Hold-Ms"] = "61400"
            c.post("/api/live/say", content=b"a" * 5000, headers=hdr)
        finally:
            self.live.say = old
        self.assertAlmostEqual(seen["hold_s"], 61.4)

    def test_sheet_failure_is_logged_not_fatal(self):
        logs = []
        self.assertIsNone(self.live.contact_sheet([(0, "/nope.jpg")], "/tmp/x.jpg", log=logs.append))
        self.assertIn("contact sheet failed", logs[0])
        self.assertIsNone(self.live.contact_sheet([], "/tmp/x.jpg", log=logs.append))

    def test_many_holds_one_turn_heard_as_they_come(self):
        """Rav 20:12: hold to talk several times, each heard at once; the agent's turn sends it all."""
        L = self.live
        L._segs.clear()
        heard = iter(["here's the kitchen", "and this is the dojo"])
        seen = []
        now = 50_000.0
        for i in range(30):
            L.save_frame(self.JPG, now=now - 30 + i)
        old = L.contact_sheet
        L.contact_sheet = lambda fr, out, log=print: out
        try:
            a = L.add_segment(OWNER, b"a" * 5000, "audio/webm", 5, now=now - 20, stt=lambda p: next(heard),
                              notify=seen.append, run=lambda f: f())
            b = L.add_segment(OWNER, b"b" * 5000, "audio/webm", 4, now=now - 5, stt=lambda p: next(heard),
                              notify=seen.append, run=lambda f: f())
            self.assertEqual([s["state"] for s in seen], ["heard", "heard"])    # heard before the turn ends
            job = json.loads(open(L.end_turn(OWNER, now=now)).read())
        finally:
            L.contact_sheet = old
        self.assertEqual(job["transcript"], "[0:00] here's the kitchen\n[0:16] and this is the dojo")
        self.assertEqual((job["segments"], job["hold_s"]), (2, 25.0))          # from the FIRST hold's start
        self.assertTrue(job["sheet"]); self.assertEqual(len(job["frames"]), L.HOLD_CAP)
        self.assertEqual(L._segs, {})                                           # the turn is closed

    def test_end_turn_waits_for_a_clip_still_being_heard(self):
        L = self.live
        L._segs.clear()
        seg = L.add_segment(OWNER, b"a" * 5000, "audio/webm", 2, stt=lambda p: "x", run=lambda f: None)  # still running
        def sleep(_):
            seg["text"], seg["state"] = "late words", "heard"
        old = L.contact_sheet
        L.contact_sheet = lambda fr, out, log=print: None
        try:
            job = json.loads(open(L.end_turn(OWNER, sleep=sleep)).read())
        finally:
            L.contact_sheet = old
        self.assertIn("late words", job["transcript"])

    def test_turn_refusals_and_limits(self):
        L = self.live
        L._segs.clear()
        with self.assertRaises(L.LiveError):
            L.end_turn(OWNER)                                                     # nothing recorded
        with self.assertRaises(L.LiveError):
            L.add_segment(OWNER, b"", "audio/webm", 2, run=lambda f: None)        # an empty clip is refused
        self.assertEqual(L._segs, {})
        L.add_segment(OWNER, b"a" * 5000, "audio/webm", 2, stt=lambda p: "", run=lambda f: f())
        with self.assertRaises(L.LiveError):
            L.end_turn(OWNER)                                                     # nothing heard
        L.add_segment(OWNER, b"a" * 5000, "audio/webm", 2, now=1000, stt=lambda p: "a", run=lambda f: f())
        L.add_segment(OWNER, b"a" * 5000, "audio/webm", 2, now=1000 + L.SEG_TTL_S + 1, stt=lambda p: "b", run=lambda f: f())
        self.assertEqual(len(L._segs[OWNER]["items"]), 1)                         # an abandoned turn starts fresh
        self.assertEqual(L.clear_turn(OWNER), 1); self.assertEqual(L._segs, {})
        for i in range(L.SEG_MAX):
            L.add_segment(OWNER, b"a" * 5000, "audio/webm", 1, stt=lambda p: "a", run=lambda f: None)
        with self.assertRaises(L.LiveError):
            L.add_segment(OWNER, b"a" * 5000, "audio/webm", 1, stt=lambda p: "a", run=lambda f: None)
        L._segs.clear()

    def test_reopened_page_sees_the_waiting_clips(self):
        L = self.live
        L._segs.clear()
        self.assertEqual(c.get("/api/live/turn", headers=H(OWNER)).json(), {"segments": []})
        L.add_segment(OWNER, b"a" * 5000, "audio/webm", 2, stt=lambda p: "still here", run=lambda f: f())
        self.assertEqual(c.get("/api/live/turn", headers=H(OWNER)).json()["segments"],
                         [{"i": 1, "state": "heard", "text": "still here"}])
        self.assertEqual(c.get("/api/live/turn", headers=H(USER_B)).status_code, 403)
        self.assertEqual(L.open_turn(OWNER, now=time.time() + L.SEG_TTL_S + 1), [])   # expired turns are not shown
        L._segs.clear()

    def test_waiting_clips_survive_a_restart(self):
        """A deploy restarts the Mini App: Rav's unsent clips must still be there, a half-heard one heard again."""
        L = self.live
        L._segs.clear(); L._loaded["dir"] = None
        L.add_segment(OWNER, b"a" * 5000, "audio/webm", 2, stt=lambda p: "first", run=lambda f: f())
        L.add_segment(OWNER, b"b" * 5000, "audio/webm", 2, stt=lambda p: "x", run=lambda f: None)   # dies mid-STT
        L._segs.clear(); L._loaded["dir"] = None                                # the process restarts
        old = L._stt
        L._stt = lambda p: "second, heard again"
        try:
            segs = L.open_turn(OWNER)
            for _ in range(50):
                if L.open_turn(OWNER)[1]["state"] != "transcribing":
                    break
                time.sleep(0.05)
        finally:
            L._stt = old
        self.assertEqual([s["text"] for s in L.open_turn(OWNER)], ["first", "second, heard again"])
        self.assertEqual(len(segs), 2)
        L.clear_turn(OWNER)
        L._segs.clear(); L._loaded["dir"] = None
        self.assertEqual(L.open_turn(OWNER), [])                                  # cleared stays cleared

    def test_turn_endpoints_are_ravs_and_refuse_an_empty_turn(self):
        self.live._segs.clear()
        self.assertEqual(c.post("/api/live/segment", content=b"a" * 5000, headers=H(USER_B)).status_code, 403)
        self.assertEqual(c.post("/api/live/end", headers=H(USER_C)).status_code, 403)
        r = c.post("/api/live/end", headers=H(OWNER))
        self.assertEqual(r.status_code, 400); self.assertIn("nothing recorded", r.json()["detail"])
        self.assertEqual(c.post("/api/live/clear", headers=H(OWNER)).json()["dropped"], 0)

    def test_old_frames_left_out(self):
        now = time.time()
        self.live.save_frame(self.JPG, now=now - 60)
        self.live.save_frame(self.JPG, now=now - 2)
        self.assertEqual(len(self.live.recent(now)), 1)

    def test_speak_needs_the_push_key_and_reaches_only_an_open_live_tab(self):
        self.assertEqual(c.post("/api/push-speak/alex", json={"text": "hi"}).status_code, 401)
        self.assertEqual(c.post("/api/push-speak/alex", json={"text": "hi"}, headers=H(OWNER)).status_code, 401)  # a person is not the agent
        k = {"X-Push-Key": KEY}
        r = c.post("/api/push-speak/alex", json={"text": "hi"}, headers=k).json()
        self.assertFalse(r["live"])                                   # no page open, no fresh frames
        got = []
        class Q:
            def put_nowait(self, e): got.append(e)
        s._subs["alex"] = {Q()}
        self.live.beat(on=False)
        try:
            self.live.save_frame(self.JPG)                            # fresh frames alone do NOT mean Live is on
            r = c.post("/api/push-speak/alex", json={"text": "hi"}, headers=k).json()
            self.assertFalse(r["live"]); self.assertEqual(got, [])    # app open but the Live session is off
            self.assertEqual(c.post("/api/live/beat", headers=H(OWNER)).status_code, 200)   # Start: the page beats
            r = c.post("/api/push-speak/alex", json={"text": "hello there"}, headers=k).json()
            self.assertTrue(r["live"]); self.assertEqual(got, [{"type": "speak", "id": r["id"]}])
            c.post("/api/live/beat?off=1", headers=H(OWNER))           # Stop: answers go to Telegram again
            self.assertFalse(c.post("/api/push-speak/alex", json={"text": "x"}, headers=k).json()["live"])
            self.assertEqual(c.post("/api/live/beat", headers=H(USER_B)).status_code, 403)
        finally:
            s._subs.pop("alex", None)
        return r["id"]

    def test_paused_camera_still_hears_the_agent(self):
        """20:45: Rav paused the camera to keep talking; the answer must still come out of the phone."""
        L = self.live
        L.beat(now=1000)
        self.assertTrue(L.session_on(now=1005))                       # no frames at all, session still on
        self.assertFalse(L.session_on(now=1000 + L.BEAT_FRESH_S + 1))  # the page went away: beats stopped
        L.beat(on=False)
        self.assertFalse(L.session_on())

    def test_live_status_needs_the_key_and_a_known_state(self):
        got = []
        class Q:
            def put_nowait(self, e): got.append(e)
        s._subs["alex"] = {Q()}
        try:
            self.assertEqual(c.post("/api/push-live/alex", json={"state": "heard"}, headers=H(OWNER)).status_code, 401)
            self.assertEqual(c.post("/api/push-live/alex", json={"state": "rm -rf"}, headers={"X-Push-Key": KEY}).status_code, 400)
            r = c.post("/api/push-live/alex", json={"state": "heard", "text": "what is this"}, headers={"X-Push-Key": KEY})
            self.assertEqual(r.status_code, 200)
        finally:
            s._subs.pop("alex", None)
        self.assertEqual(got, [{"type": "live", "state": "heard", "text": "what is this"}])

    def test_voice_stream_is_that_persons_only(self):
        sid = self.test_speak_needs_the_push_key_and_reaches_only_an_open_live_tab()
        self.assertEqual(c.get(f"/api/live/voice/{sid}", headers=H(USER_B)).status_code, 404)
        self.assertEqual(c.get("/api/live/voice/nope", headers=H(OWNER)).status_code, 404)
        old = self.live.speak_pcm
        self.live.speak_pcm = lambda text: iter([b"\x01\x00" * 4, text.encode()])
        try:
            r = c.get(f"/api/live/voice/{sid}", headers=H(OWNER))
        finally:
            self.live.speak_pcm = old
        self.assertEqual((r.status_code, r.headers["x-sample-rate"]), (200, "22050"))
        self.assertTrue(r.content.endswith(b"hello there"))

    def test_speak_pcm_streams_and_logs_a_failure(self):
        class P:
            def __init__(self, chunks, rc):
                import io
                self.stdin, self.stdout, self.stderr, self.rc = io.BytesIO(), io.BytesIO(b"".join(chunks)), io.BytesIO(b"boom"), rc
                self.stdin.close = lambda: None
            def wait(self, timeout=None): return self.rc
        logs = []
        out = list(self.live.speak_pcm("Hi there.", popen=lambda *a, **k: P([b"a" * 5000], 0), log=logs.append))
        self.assertEqual(sum(map(len, out)), 5000); self.assertGreater(len(out), 1)    # chunked, not one blob
        self.assertEqual(logs, [])
        list(self.live.speak_pcm("Hi.", popen=lambda *a, **k: P([], 1), log=logs.append))
        self.assertIn("piper rc=1", logs[0])
        # the page closes mid-answer: Piper is killed at once, never waited on
        killed = []
        p = P([b"a" * 9000], 0); p.kill = lambda: killed.append("kill")
        p.wait = lambda timeout=None: killed.append(("reap", timeout))
        g = self.live.speak_pcm("Hi.", popen=lambda *a, **k: p, log=logs.append)
        next(g); g.close()
        self.assertEqual(killed, ["kill", ("reap", 2)])                     # killed first, then reaped (no zombie)
        self.assertIn("abandoned", logs[-1])

    def test_voice_is_warmed_once_and_used_by_the_next_answer(self):
        import io
        made = []
        class P:
            def __init__(self, *a, **k):
                self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(b"pcm" * 2000), io.BytesIO()
                self.stdin.close = lambda: None; self.dead = False; made.append(self)
            def poll(self): return 1 if self.dead else None
            def kill(self): self.dead = True
            def wait(self, timeout=None): return 0
        self.live._warm.update(p=None, ts=0)
        self.assertEqual(self.live.warm(popen=P, now=1000), "started")
        self.assertEqual(self.live.warm(popen=P, now=1001), "warm")          # a second press does not pile up Pipers
        self.assertEqual(len(made), 1)
        self.assertEqual(self.live.warm(popen=P, now=1000 + self.live.WARM_MAX_S + 1), "started")
        self.assertTrue(made[0].dead)                                        # the stale one is killed, never left
        warm = made[1]
        self.live._warm["ts"] = s.time.time()
        out = b"".join(self.live.speak_pcm("Hi."))                           # no popen: the warm one is taken
        self.assertEqual(warm.stdin.getvalue(), b"Hi.")
        self.assertEqual(len(out), 6000)
        self.assertIsNone(self.live._warm["p"])
        self.assertEqual(c.post("/api/live/warm", headers=H(USER_B)).status_code, 403)

    def test_empty_clip_refused(self):
        self.assertEqual(c.post("/api/live/say", content=b"", headers=H(OWNER)).status_code, 400)



class BrowserDoor(unittest.TestCase):
    """TIK-1509 slices 1+4: push a URL into a NEW tab (Rav only, http/https only) and honest 503s. CDP is faked."""

    def setUp(self):
        import io
        from unittest import mock
        self.mock, self.io = mock, io
        self.calls = []
        try:
            os.remove(s.BROWSER_AUDIT)
        except OSError:
            pass

    def _urlopen(self, resp=None, exc=None):
        def f(req, timeout=None):
            self.calls.append((req.get_method() if hasattr(req, "get_method") else "GET",
                               req.full_url if hasattr(req, "full_url") else req, timeout))
            if exc is not None:
                raise exc
            return self.io.BytesIO(json.dumps(resp).encode())
        return f

    def open_(self, uid, url, **kw):
        return c.post("/api/browser/open", json={"url": url}, headers=H(uid, **kw))

    def test_only_rav_and_only_with_initdata(self):
        with self.mock.patch("urllib.request.urlopen", self._urlopen({"id": "T1"})):
            self.assertEqual(c.post("/api/browser/open", json={"url": "https://example.com/"}).status_code, 401)
            self.assertEqual(self.open_(USER_B, "https://example.com/").status_code, 403)
            self.assertEqual(self.open_(USER_C, "https://example.com/").status_code, 403)
            self.assertEqual(self.open_(STRANGER, "https://example.com/").status_code, 403)
            self.assertEqual(c.post("/api/browser/open", json={"url": "https://example.com/"}, headers={"X-Push-Key": KEY}).status_code, 401)
        self.assertEqual(self.calls, [], "a refused caller must never reach CDP")

    def test_only_http_and_https(self):
        with self.mock.patch("urllib.request.urlopen", self._urlopen({"id": "T1"})):
            for bad in ("file:///etc/passwd", "chrome://settings", "javascript:alert(1)", "data:text/html,<b>x</b>", "ftp://h/x",
                        "about:blank", "//example.com/x", "example.com", "", "   ", "https://u:p@example.com/", "https://",
                        "http://exa mple.com/", "https://example.com/\nx", "https://example.com/" + "a" * 2100, None, 42):
                r = self.open_(OWNER, bad)
                self.assertEqual(r.status_code, 400, repr(bad)[:60])
            self.assertEqual(c.post("/api/browser/open", content=b"not json", headers=H(OWNER)).status_code, 400)
        self.assertEqual(self.calls, [], "a refused URL must never reach CDP")

    def test_rav_opens_a_new_tab_and_it_is_audited(self):
        with self.mock.patch("urllib.request.urlopen", self._urlopen({"id": "T9", "type": "page"})):
            r = self.open_(OWNER, "https://example.com/a?b=c&d=e")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "url": "https://example.com/a?b=c&d=e", "tab": "T9"})
        method, url, timeout = self.calls[0]
        self.assertEqual(method, "PUT")                                   # /json/new: a NEW tab, not Page.navigate on the working one
        self.assertTrue(url.startswith("http://127.0.0.1:9222/json/new?https%3A%2F%2Fexample.com"), url)
        self.assertEqual(len(self.calls), 1)
        rows = [json.loads(l) for l in open(s.BROWSER_AUDIT)]
        self.assertEqual([x["event"] for x in rows], ["open"])
        self.assertEqual((rows[0]["by"], rows[0]["url"], rows[0]["tab"]), ("alex", "https://example.com/a?b=c&d=e", "T9"))

    def test_cdp_down_or_hung_is_an_honest_503_and_logged(self):
        import urllib.error
        for exc, word in ((urllib.error.URLError(ConnectionRefusedError(111, "refused")), "unavailable"),
                          (TimeoutError("timed out"), "not answering"),
                          (urllib.error.URLError(TimeoutError("timed out")), "not answering")):
            with self.mock.patch("urllib.request.urlopen", self._urlopen(exc=exc)):
                r = self.open_(OWNER, "https://example.com/")
            self.assertEqual(r.status_code, 503, repr(exc))
            self.assertIn(word, r.json()["detail"], repr(exc))
        rows = [json.loads(l) for l in open(s.BROWSER_AUDIT)]
        self.assertEqual([x["event"] for x in rows], ["fail"] * 3)
        self.assertEqual([x["hang"] for x in rows], [False, True, True])

    def test_view_hang_is_a_503_not_a_frozen_image(self):
        with self.mock.patch("urllib.request.urlopen", self._urlopen(exc=TimeoutError("timed out"))):
            r = c.get("/api/browser", headers=H(USER_B))
        self.assertEqual(r.status_code, 503)
        self.assertIn("not answering", r.json()["detail"])
        self.assertNotIn("src", r.json())
        with self.mock.patch("urllib.request.urlopen", self._urlopen([{"type": "browser_ui"}])):
            self.assertEqual(c.get("/api/browser", headers=H(USER_B)).json()["note"], "no page open")   # unchanged
        self.assertEqual(c.get("/api/browser").status_code, 401)
        rows = [json.loads(l) for l in open(s.BROWSER_AUDIT)]
        self.assertEqual(rows[-1]["what"], "view")

    def test_url_cleaner_unit(self):
        self.assertEqual(s._clean_browser_url("  https://Example.com/x  "), "https://Example.com/x")
        self.assertIsNone(s._clean_browser_url("HTTP://"))
        self.assertEqual(s._clean_browser_url("http://127.0.0.1:8080/x"), "http://127.0.0.1:8080/x")

if __name__ == "__main__":
    unittest.main()


class Screen(unittest.TestCase):
    def test_receiver_needs_its_key(self):
        self.assertEqual(c.get("/screen/tv").status_code, 401)
        self.assertEqual(c.get("/screen/tv?k=wrong").status_code, 401)
        self.assertEqual(c.get("/screen/tv?k=tv-key-123").status_code, 200)
        self.assertEqual(c.get("/screen/wall?k=").status_code, 401)            # not a receiver: no page at all
        self.assertEqual(c.get("/screen/tv/k/wrong/f/cast-x.html").status_code, 401)

    def test_cast_shows_on_the_receiver(self):
        it = s.add_item("alex", {"kind": "card", "title": "On TV", "body": "b"})
        r = c.post("/api/act", json={"where": "history", "id": it["id"], "action": "cast", "to": "tv"}, headers=H(OWNER))
        self.assertEqual(r.status_code, 200, r.text)
        page = c.get("/screen/tv/now?k=tv-key-123").json()["page"]
        self.assertIn("On TV", c.get(f"/screen/tv/k/tv-key-123/f/{page}.html").text)
        self.assertEqual(c.get("/screen/tv/k/tv-key-123/f/..%2Fsecret.html").status_code, 404)

    def test_me_lists_the_screens(self):
        self.assertEqual(c.get("/api/me", headers=H(OWNER)).json()["screens"], ["den", "tv", "wall"])
