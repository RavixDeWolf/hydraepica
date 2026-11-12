import _harness_env  # noqa: F401  throwaway config + state (must be first)
import os, sys, tempfile, unittest, uuid
os.environ["HEAD_STATE_DIR"] = tempfile.mkdtemp()                    # sealed: never the live events log
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness", "hooks"))
import head_hook as H


class WatchBackgroundTest(unittest.TestCase):
    def setUp(self):
        self.sid = uuid.uuid4().hex
        self.dir = f"/tmp/claude-test-{os.getpid()}/proj/{self.sid}/tasks"; os.makedirs(self.dir)
        open(os.path.join(self.dir, "abc123.output"), "w").write("")

    def ev(self, cmd, tid="abc123"):
        return {"tool_name": "Bash", "session_id": self.sid, "tool_input": {"command": cmd, "description": "render it"},
                "tool_response": {"backgroundTaskId": tid}}

    def test_background_job_gets_a_watcher(self):
        got = []
        argv = H.watch_background(self.ev("python3 motion.py s.html o.mp4"), spawn=got.append)
        self.assertEqual(got, [argv]); self.assertIn("--watch", argv); self.assertEqual(argv[argv.index("--kind") + 1], "video")

    def test_watcher_also_reads_the_jobs_own_log(self):
        got = []
        argv = H.watch_background(self.ev("cd /x && python3 flame.py a.wav o.mp4 > render.log 2>&1"), spawn=got.append)
        self.assertEqual(argv[argv.index("--also") + 1], "/x/render.log")

    def test_skips_foreground_and_self_reporting(self):
        got = []
        self.assertIsNone(H.watch_background(self.ev("python3 bg.py --name x -- sleep 9"), spawn=got.append))
        d = self.ev("ls"); d["tool_response"] = {"stdout": "x"}
        self.assertIsNone(H.watch_background(d, spawn=got.append)); self.assertEqual(got, [])

    def test_missing_output_file_is_logged_not_fatal(self):
        self.assertIsNone(H.watch_background(self.ev("sleep 99", tid="nope"), spawn=lambda a: 1 / 0))


if __name__ == "__main__":
    unittest.main()


class MidturnTest(unittest.TestCase):
    """TIK-1421: a keyed mid-turn message is handed to the running turn once and recorded as delivered."""
    def setUp(self):
        for p in (H.MIDTURN, H.MIDTURN_OFF, H.MIDTURN_DONE):
            if os.path.exists(p):
                os.remove(p)

    def write(self, **row):
        import json
        with open(H.MIDTURN, "a") as f:
            f.write(json.dumps(row) + "\n")

    def test_keyed_message_is_delivered_once_and_recorded(self):
        self.write(ts=1, text="also check the logs", k="abc")
        note = H.midturn_note()
        self.assertIn("[Rav, mid-turn]", note); self.assertIn("also check the logs", note); self.assertIn("NOT come again", note)
        self.assertIn('"abc"', open(H.MIDTURN_DONE).read())
        self.assertEqual(H.midturn_note(), "", "told once")

    def test_unkeyed_note_still_says_it_is_queued(self):
        self.write(ts=1, text="[your photo is processed]")
        note = H.midturn_note()
        self.assertIn("also queued", note); self.assertNotIn("NOT come again", note)
        self.assertFalse(os.path.exists(H.MIDTURN_DONE))

    def test_half_written_line_waits(self):
        with open(H.MIDTURN, "a") as f:
            f.write('{"ts": 1, "text": "x", "k": "q"')
        self.assertEqual(H.midturn_note(), "")
        self.assertFalse(os.path.exists(H.MIDTURN_DONE))

    def test_the_lane_skips_what_the_hook_delivered_end_to_end(self):
        import head_lane as hl, tgapi as pl
        real = (hl.MIDTURN, hl.MIDTURN_DONE, hl._react, pl._log)
        hl.MIDTURN, hl.MIDTURN_DONE = H.MIDTURN, H.MIDTURN_DONE
        reacts, logs = [], []
        hl._react = lambda tok, m, st: reacts.append((m.get("message_id"), st))
        pl._log = lambda ev, **kw: logs.append(kw)
        try:
            self.assertTrue(hl.note_midturn("steer left", key="k1"))
            self.assertTrue(hl.note_midturn("steer right", key="k2"))
            H.midturn_note()
            self.assertTrue(hl.skip_if_delivered("t", {"message_id": 1, "_mt": "k1"}))
            self.assertFalse(hl.skip_if_delivered("t", {"message_id": 3, "_mt": "never-delivered"}))
            self.assertFalse(hl.skip_if_delivered("t", {"message_id": 4}))
        finally:
            hl.MIDTURN, hl.MIDTURN_DONE, hl._react, pl._log = real
        self.assertEqual(reacts, [(1, "seen")])
        self.assertEqual([k.get("key") for k in logs if k.get("event2") == "midturn_consumed"], ["k1"])


class MidturnStaleTest(MidturnTest):
    def test_a_message_the_queue_already_ran_is_not_retold(self):
        import json
        self.write(ts=1, text="Should", k="s1")
        with open(H.MIDTURN_DONE, "w") as f:
            f.write(json.dumps({"k": "s1", "by": "queue"}) + "\n")
        self.assertEqual(H.midturn_note(), "")
