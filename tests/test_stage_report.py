"""stage_report: stage html pages report their errors to the head that published them."""
import _harness_env  # noqa: F401  throwaway config + state (must be first)
import base64
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "harness", "stage"))
import stage_report as s  # noqa: E402
s.DIR = tempfile.mkdtemp()


class T(unittest.TestCase):
    def setUp(self):
        s._last.clear()
        self.calls = []

    def send(self, lane, text):
        self.calls.append((lane, text))
        return {"ok": True}

    def test_inject_after_head_never_before_doctype(self):
        h = s.inject("<!doctype html><html><head><title>x</title></head></html>")
        self.assertTrue(h.startswith("<!doctype html><html><head><script>"))
        self.assertEqual(s.inject(h), h)
        self.assertTrue(s.inject("<!DOCTYPE html><p>x").startswith("<!DOCTYPE html><script>"))
        self.assertTrue(s.inject("<p>x").startswith("<script>"))

    def test_errors_go_to_publishing_lane_once(self):
        it = {"id": "42", "title": "Chart", "lane": "links"}
        body = {"problems": [{"k": "error", "m": "NaN everywhere"}, {"k": "bogus", "m": "x"}]}
        r = s.report("alex", it, body, now=lambda: 1000, send=self.send)
        self.assertTrue(r["sent"])
        self.assertEqual(self.calls[0][0], "links")
        self.assertIn("NaN everywhere", self.calls[0][1])
        self.assertNotIn("bogus", self.calls[0][1])
        self.assertFalse(s.report("alex", it, body, now=lambda: 1100, send=self.send)["sent"])
        self.assertTrue(s.report("alex", it, body, now=lambda: 1000 + s.GAP_S, send=self.send)["sent"])

    def test_nothing_to_report_sends_nothing(self):
        self.assertFalse(s.report("alex", {"id": "1"}, {"problems": []}, send=self.send)["sent"])
        self.assertEqual(self.calls, [])

    def test_bad_lane_falls_back_to_main(self):
        self.assertEqual(s.lane_of({"lane": "links; rm -rf ~"}), "main")
        self.assertEqual(s.lane_of({}), "main")

    def test_snapshot_saved_and_named(self):
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()
        s.report("alex", {"id": "7", "lane": "main"}, {"problems": [{"k": "blank", "m": "drew nothing"}], "snap": png},
                 send=self.send)
        self.assertIn("Snapshot: ", self.calls[0][1])
        path = self.calls[0][1].split("Snapshot: ")[1].strip()
        self.assertEqual(open(path, "rb").read(), b"\x89PNG fake")

    def test_failed_handoff_is_not_ok(self):
        r = s.report("alex", {"id": "8"}, {"problems": [{"k": "error", "m": "x"}]},
                     send=lambda lane, text: {"ok": False, "why": "--to must be one of ('main',)"})
        self.assertFalse(r["ok"]) ; self.assertIn("must be one of", r["why"])

    def test_real_handoff_reaches_the_spool(self):
        import handoff
        r = s.report("alex", {"id": "9", "title": "P"}, {"problems": [{"k": "console", "m": "boom"}]})
        self.assertTrue(r["sent"], r)
        d = handoff.spool_for("main")
        self.assertTrue(any("boom" in open(os.path.join(d, f)).read() for f in os.listdir(d)))


if __name__ == "__main__":
    unittest.main()
