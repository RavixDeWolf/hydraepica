"""Tests for tg.py (TIK-1027). No real Telegram, no live chat history. Run: python3 test_tg.py"""
import _harness_env  # noqa: F401  throwaway config + state (must be first)
import json
import os
import subprocess
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness", "tg")
sys.path.insert(0, HERE)
import tg  # noqa: E402

TMP = tempfile.mkdtemp(prefix="tgtool_")
tg.tgapi.LOG_PATH = os.path.join(TMP, "log")
tg.THREAD_FILE = os.path.join(TMP, "threads.json")
json.dump({str(tg.OWNER): 777}, open(tg.THREAD_FILE, "w"))
tg.chat_ledger.CHATDB = os.path.join(TMP, "chat.db")
tg.BUTTONS = os.path.join(TMP, "buttons.json")        # never the lane's live buttons file
with sqlite3.connect(tg.chat_ledger.CHATDB) as c:
    c.execute("CREATE TABLE chat_log (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, source TEXT NOT NULL,"
              " mode TEXT, role TEXT NOT NULL, content TEXT NOT NULL, model TEXT, ts REAL, thread_id TEXT, extra TEXT, user_id TEXT)")
CALLS = []


def call(method, kw):
    CALLS.append((method, kw))
    return {"ok": True, "result": {"message_id": 900 + len(CALLS)}}


def run(*argv):
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = tg.main(list(argv), call=call)
    return rc, json.loads(buf.getvalue())


class Tool(unittest.TestCase):
    def setUp(self):
        CALLS.clear()

    def test_only_ravs_chat_ever(self):
        run("send", "hi")
        run("forward", "5", "--thread", "1018")
        self.assertTrue(all(kw.get("chat_id") == tg.OWNER for _, kw in CALLS))
        self.assertEqual(CALLS[0][1]["message_thread_id"], 777)       # the main thread by default
        self.assertEqual(CALLS[1][1]["message_thread_id"], 1018)
        self.assertEqual(CALLS[1][1]["from_chat_id"], tg.OWNER)

    def test_sends_become_history_rows(self):
        rc, out = run("send", "remember", "me")
        self.assertEqual(rc, 0)
        r = tg.chat_ledger.find(tg.OWNER, out["message_id"], 777)
        self.assertEqual((r["role"], r["text"]), ("assistant", "remember me"))

    def test_edit_and_delete_only_own_messages(self):
        rc, out = run("delete", "12345")
        self.assertEqual(rc, 1); self.assertIn("not one of the bot's own", out["error"])
        rc, out = run("edit", "12345", "x")
        self.assertEqual(rc, 1)
        self.assertEqual(CALLS, [])
        _, sent = run("send", "mine")
        rc, _ = run("delete", str(sent["message_id"]))
        self.assertEqual((rc, CALLS[-1][0]), (0, "deleteMessage"))

    def test_media_needs_a_real_file_and_round_video_has_no_caption(self):
        rc, out = run("photo", "/nope.jpg")
        self.assertEqual(rc, 1)
        self.assertIn("no such file", json.dumps(out))          # the DECLARED refusal, not whatever the next line happens to raise (TIK-1512)
        self.assertEqual(CALLS, [])                              # and nothing was sent
        p = os.path.join(TMP, "v.mp4")              # a real 1 s clip: tg.py refuses to send a file that does not probe
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64", p], check=True)
        run("videonote", p, "a caption")
        self.assertNotIn("caption", CALLS[-1][1])
        run("video", p, "a caption")
        self.assertEqual((CALLS[-1][0], CALLS[-1][1]["caption"]), ("sendVideo", "a caption"))

    def test_context_reads_the_history(self):
        tg.chat_ledger.record_out(tg.OWNER, 777, 50, "before", now=1)
        tg.chat_ledger.record_out(tg.OWNER, 777, 51, "target", now=2)
        rc, out = run("context", "51")
        self.assertIn("target", out["message"]); self.assertIn("before", out["before"][0])

    def test_buttons_are_stored_for_the_lane_and_keyed_per_choice(self):
        rc, out = run("buttons", "Which pumpkin?", "Left", "Right")
        self.assertEqual(rc, 0)
        kb = CALLS[-1][1]["reply_markup"]["inline_keyboard"]
        self.assertEqual([b[0]["text"] for b in kb], ["Left", "Right"])
        key = kb[0][0]["callback_data"].split(":")[2]
        self.assertEqual([b[0]["callback_data"] for b in kb], [f"hd:u:{key}:0", f"hd:u:{key}:1"])
        self.assertEqual(json.load(open(tg.BUTTONS))[key], {"text": "Which pumpkin?", "labels": ["Left", "Right"]})
        self.assertEqual(run("buttons", "only one")[0], 1)          # a question needs at least two choices

    def test_sticker_and_poll(self):
        run("sticker", "CAACAgIAAxk")
        self.assertEqual((CALLS[-1][0], CALLS[-1][1]["sticker"]), ("sendSticker", "CAACAgIAAxk"))
        run("poll", "Tea?", "Yes", "No")
        self.assertEqual((CALLS[-1][0], [o["text"] for o in CALLS[-1][1]["options"]]), ("sendPoll", ["Yes", "No"]))

    def test_failures_are_logged_and_nonzero(self):
        rc, out = run("bogus")
        self.assertEqual(rc, 1)
        self.assertIn('"head_tg"', open(tg.tgapi.LOG_PATH).read())



class UploadActionTest(unittest.TestCase):
    """Rav 2026-10-26: a lagging photo looked missing. Media sends show 'sending photo...' while uploading."""

    def test_photo_send_shows_upload_photo_first(self):
        import time
        calls = []

        def call(m, kw):
            calls.append((m, kw.get("action")))
            if m == "sendPhoto":
                time.sleep(0.1)
            return {"ok": True, "result": {"message_id": 1}}
        orig = tg._record
        tg._record = lambda *a, **k: 1
        try:
            from PIL import Image
            img = os.path.join(TMP, "noise.png"); Image.effect_noise((64, 64), 40).save(img)
            tg.run(["photo", img, "cap"], call=call)
        finally:
            tg._record = orig
        self.assertEqual(calls[0], ("sendChatAction", "upload_photo"))
        self.assertIn(("sendPhoto", None), calls)


class ProgressTest(unittest.TestCase):
    """Rav 2026-10-26: one small progress message, edited in place, deleted when done, never wider than a phone."""

    def setUp(self):
        import tempfile
        self.f = tempfile.mktemp(); self.orig = (tg.PROGRESS_FILE, tg._record)
        tg.PROGRESS_FILE = self.f; tg._record = lambda res, *a, **k: res["result"]["message_id"]

    def tearDown(self):
        tg.PROGRESS_FILE, tg._record = self.orig

    def test_send_edit_skip_done(self):
        calls = []
        def call(m, kw):
            calls.append((m, kw.get("text"))); return {"ok": True, "result": {"message_id": 77}}
        tg.run(["progress", "job", "10", "rendering"], call=call)
        tg.run(["progress", "job", "50", "rendering"], call=call)
        tg.run(["progress", "job", "50", "rendering"], call=call)          # unchanged: no API call
        tg.run(["progress", "job", "done"], call=call)
        self.assertEqual([c[0] for c in calls], ["sendMessage", "pinChatMessage", "editMessageText", "deleteMessage"])

    def test_pin_names_the_thread(self):
        """a pin without message_thread_id is chat-wide and never shows in the thread (Rav saw no pin, 2026-10-27)"""
        calls = []
        def call(m, kw):
            calls.append((m, kw)); return {"ok": True, "result": {"message_id": 78}}
        r = tg.run(["progress", "job2", "10", "x"], call=call)
        pin = [kw for m, kw in calls if m == "pinChatMessage"][0]
        self.assertIsNotNone(pin.get("message_thread_id")); self.assertIs(r["pinned"], True)

    def test_text_stays_narrow(self):
        t = tg.progress_text(42, "a very long label that would push the bubble sideways on a phone", "22:52")
        self.assertTrue(all(len(line) <= 30 for line in t.split("\n")), t)
        self.assertTrue(t.startswith("🟩🟩🟩🟩🟨🟨🟨🟨🟨🟨 42%"))


class MediaCheckTest(unittest.TestCase):
    def test_blank_and_broken_are_not_sent(self):
        import tempfile
        from PIL import Image
        d = tempfile.mkdtemp(); sent = []
        call = lambda m, kw: sent.append(m) or {"ok": True, "result": {"message_id": 5}}
        blank = os.path.join(d, "b.png"); Image.new("RGB", (100, 100), (0, 0, 0)).save(blank)
        junk = os.path.join(d, "j.jpg"); open(junk, "wb").write(b"not an image")
        for f in (blank, junk):
            with self.assertRaises(tg.TgError):
                tg.run(["photo", f], call=call)
        self.assertEqual([m for m in sent if m == "sendPhoto"], [])
        ok = os.path.join(d, "ok.png"); Image.effect_noise((64, 64), 40).save(ok)
        self.assertIsNone(tg.media_problem("photo", ok))




class BgTest(unittest.TestCase):
    """bg.py: indicator while it runs, bar from the job's own % or past runs, bar removed + one result line at the end"""
    def setUp(self):
        import tempfile, bg
        self.bg = bg; self.d = tempfile.mkdtemp(); bg.RUNS = os.path.join(self.d, "runs.jsonl")
        self.orig = (tg.PROGRESS_FILE, tg._record); tg.PROGRESS_FILE = os.path.join(self.d, "p.json")
        tg._record = lambda res, *a, **k: res["result"]["message_id"]

    def tearDown(self):
        tg.PROGRESS_FILE, tg._record = self.orig

    def go(self, cmd, **kw):
        calls = []
        def call(m, kw2):
            calls.append((m, kw2)); return {"ok": True, "result": {"message_id": 9}}
        rc = self.bg.run("video", "t-job", cmd, call=call, tick=0.0, beat=0.05, **kw)
        return rc, calls

    def test_parse_pct(self):
        P = self.bg.parse_pct
        self.assertAlmostEqual(P("PROGRESS 1950/5400"), 36.11, places=2)   # 2026-10-27: the bar sat at 0% on this
        self.assertEqual((P("PROGRESS 42"), P("render 37% done"), P("frame 30/120")), (42.0, 37.0, 25.0))
        self.assertIsNone(P("loaded 3 models")); self.assertIsNone(P("version 2.5.4"))

    def test_success_shows_video_action_bar_and_done_line(self):
        rc, calls = self.go([sys.executable, "-c", "import time;print('PROGRESS 50',flush=True);time.sleep(0.3)"])
        names = [m for m, _ in calls]
        self.assertEqual(rc, 0)
        self.assertIn("upload_video", [kw.get("action") for m, kw in calls if m == "sendChatAction"])
        self.assertIn("deleteMessage", names)                                   # the bar is removed
        self.assertTrue(any("50%" in (kw.get("text") or "") for m, kw in calls if m == "editMessageText"))
        self.assertIn("done", [kw for m, kw in calls if m == "sendMessage"][-1]["text"])
        last = open(self.bg.RUNS).read().splitlines()[-1]
        self.assertRegex(last, r"^\d{4}-\d\d-\d\dT"); self.assertEqual(json.loads(last[last.index("{"):])["rc"], 0)

    def test_failure_is_loud_and_keeps_exit_code(self):
        rc, calls = self.go([sys.executable, "-c", "import sys;print('boom: disk full');sys.exit(3)"])
        last = [kw for m, kw in calls if m == "sendMessage"][-1]["text"]
        self.assertEqual(rc, 3); self.assertIn("FAILED (exit 3)", last); self.assertIn("disk full", last)

    def test_eta_from_past_runs(self):
        open(self.bg.RUNS, "w").write(json.dumps({"name": "t-job", "rc": 0, "secs": 100}) + "\n")
        self.assertEqual(self.bg.typical("t-job"), 100); self.assertIsNone(self.bg.typical("other"))


    def test_watch_quiet_when_short_then_follows_long(self):
        f = os.path.join(self.d, "t.output")
        open(f, "w").write("quick\n\n[exited with code 0]\n")
        calls = []
        self.assertIsNone(self.bg.watch(f, "w", "video", call=lambda m, k: calls.append(m), quiet_s=1, poll=0.05))
        self.assertEqual(calls, [])                                               # short job: no bar, no message
        open(f, "w").write("PROGRESS 40\n")
        import threading
        threading.Timer(0.4, lambda: open(f, "a").write("boom\n\n[exited with code 2]\n")).start()
        calls = []
        def call(m, kw):
            calls.append((m, kw)); return {"ok": True, "result": {"message_id": 9}}
        rc = self.bg.watch(f, "w", "video", call=call, quiet_s=0.1, poll=0.05, tick=0.0, beat=0.05)
        self.assertEqual(rc, 2)
        self.assertIn("FAILED (exit 2)", [kw for m, kw in calls if m == "sendMessage"][-1]["text"])

    def test_watch_reads_progress_from_the_jobs_own_log_and_redraws_while_quiet(self):
        # 2026-10-28: a render wrote "PROGRESS n/total" to render.log (not the task output) and its bar sat at 0%
        f = os.path.join(self.d, "t2.output"); log = os.path.join(self.d, "render.log")
        open(f, "w").write(""); open(log, "w").write("PROGRESS 2700/5400\n")
        import threading
        threading.Timer(0.6, lambda: open(f, "a").write("[exited with code 0]\n")).start()
        bars = []
        def call(m, kw):
            if m in ("sendMessage", "editMessageText") and "%" in kw.get("text", ""):
                bars.append(kw["text"])
            return {"ok": True, "result": {"message_id": 9}}
        self.assertEqual(self.bg.watch(f, "w", "video", call=call, quiet_s=0.05, poll=0.05, tick=0.1, beat=5, also=[log]), 0)
        self.assertTrue(any("50%" in b for b in bars), bars)

    def test_watch_notices_a_job_stopped_from_outside(self):
        # 2026-10-28: a TaskStop'd render never wrote its exit line; the bar stayed pinned and 6 h later said "lost track"
        f = os.path.join(self.d, "t3.output"); open(f, "w").write("... 30s\n")
        sent = []
        def call(m, kw):
            if m == "sendMessage": sent.append(kw["text"])
            return {"ok": True, "result": {"message_id": 9}}
        rc = self.bg.watch(f, "w", "video", call=call, quiet_s=0.05, poll=0.05, tick=5, beat=5, stop_s=0.3, max_s=20)
        self.assertEqual(rc, "stopped")
        self.assertIn("stopped after", sent[-1])

    def test_watch_keeps_following_while_the_job_holds_its_file(self):
        f = os.path.join(self.d, "t4.output"); h = open(f, "w"); h.write("... 30s\n"); h.flush()
        import threading
        threading.Timer(0.8, lambda: (h.write("[exited with code 0]\n"), h.close())).start()
        rc = self.bg.watch(f, "w", "video", call=lambda m, kw: {"ok": True, "result": {"message_id": 9}},
                           quiet_s=0.05, poll=0.05, tick=5, beat=5, stop_s=0.3, max_s=20)
        self.assertEqual(rc, 0)

    def test_no_progress_no_history_shows_busy_not_zero(self):
        t = self.bg.tg.progress_text("busy", "3m20s · render", now=40)
        self.assertNotIn("0%", t); self.assertNotIn("🟨", t); self.assertIn("working", t)
        self.assertNotEqual(t, self.bg.tg.progress_text("busy", "3m20s · render", now=60))   # the hourglass flips

    def test_redirect_logs(self):
        self.assertEqual(self.bg.redirect_logs("cd /a && run x > r.log 2>&1; y 2>/dev/null | tee -a /b/t.log"),
                         ["/a/r.log", "/b/t.log"])

    def test_kind_of(self):
        self.assertEqual([self.bg.kind_of(x) for x in ("python3 motion.py s.html o.mp4", "python3 jedit.py d export o.jpg", "pytest -q")],
                         ["video", "photo", "work"])


if __name__ == "__main__":
    unittest.main()
