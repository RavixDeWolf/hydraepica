"""Highlight-to-comment on html stage pages, and the Heads page's Always-on list (stage server routes)."""
import _harness_env  # noqa: F401  throwaway config + state (must be first)
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_stage_server import H, OWNER, USER_B, c, s  # noqa: E402  (one stage app + signed sign-ins)

import stage_comments as sc  # noqa: E402  (server.py put harness/stage on sys.path)


class Store(unittest.TestCase):
    def setUp(self):
        self.old = sc.DIR
        sc.DIR = tempfile.mkdtemp()

    def tearDown(self):
        sc.DIR = self.old

    def test_add_list_delete_per_person(self):
        lst = sc.add("sam", "1", {"quote": "calms", "note": "soften"}, now=lambda: 1.0)
        self.assertEqual(sc.listing("sam", "1")[0]["note"], "soften")
        self.assertEqual(sc.listing("alex", "1"), [])
        self.assertEqual(sc.delete("sam", "1", lst[0]["id"]), [])

    def test_add_needs_quote_and_note(self):
        for b in ({"quote": "x"}, {"note": "y"}, {"quote": " ", "note": "y"}):
            with self.assertRaises(ValueError):
                sc.add("sam", "1", b)

    def test_send_hands_only_new_notes_to_the_publishing_lane(self):
        sc.add("sam", "9", {"quote": "q1", "note": "n1"}, now=lambda: 1.0)
        calls = []
        ok = lambda lane, text: (calls.append((lane, text)), {"ok": True})[1]
        r = sc.send("sam", {"id": "9", "lane": "drafts", "title": "Herbs"}, handoff=ok)
        self.assertEqual((r["sent"], r["lane"]), (1, "drafts"))
        self.assertEqual(calls[0][0], "drafts")
        self.assertIn("n1", calls[0][1]); self.assertIn("q1", calls[0][1]); self.assertIn("Herbs", calls[0][1])
        self.assertEqual(sc.send("sam", {"id": "9", "lane": "drafts"}, handoff=ok)["sent"], 0)   # nothing new
        self.assertEqual(len(calls), 1)
        self.assertEqual(sc.listing("sam", "9")[0]["sent_to"], "drafts")

    def test_bad_lane_goes_to_main(self):
        sc.add("sam", "9", {"quote": "q", "note": "n"})
        got = []
        sc.send("sam", {"id": "9", "lane": "../x"}, handoff=lambda lane, text: (got.append(lane), {"ok": True})[1])
        self.assertEqual(got, ["main"])

    def test_failed_handoff_is_said_and_keeps_notes_unsent(self):
        sc.add("sam", "9", {"quote": "q", "note": "n"})
        r = sc.send("sam", {"id": "9", "lane": "drafts"}, handoff=lambda lane, text: {"ok": False, "why": "no such lane"})
        self.assertFalse(r["ok"]); self.assertIn("no such lane", r["why"])
        self.assertIsNone(sc.listing("sam", "9")[0]["sent_ts"])

    def test_commenter_script_talks_only_to_its_parent(self):
        self.assertIn("parent.postMessage", sc.COMMENTER)
        self.assertIn("e.source===parent", sc.COMMENTER)
        self.assertIn("hydraComment:1", sc.COMMENTER)


class Routes(unittest.TestCase):
    def setUp(self):
        self.old = sc.DIR
        sc.DIR = tempfile.mkdtemp()
        self.html = s.add_item("jo", {"kind": "html", "title": "Draft", "html": "<p>calms the mind</p>"})
        self.card = s.add_item("jo", {"kind": "card", "title": "c", "body": "b"})

    def tearDown(self):
        sc.DIR = self.old

    def test_comment_on_own_html_stage_only(self):
        jo = H(USER_B)
        r = c.post("/api/comments", headers=jo, json={"id": self.html["id"], "op": "add", "quote": "calms", "note": "softer"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/api/comments", params={"id": self.html["id"]}, headers=jo).json()["comments"][0]["note"], "softer")
        # someone else's stage, a card, an empty note, a bad op, no sign-in
        self.assertEqual(c.get("/api/comments", params={"id": self.html["id"]}, headers=H(OWNER)).status_code, 404)
        self.assertEqual(c.post("/api/comments", headers=jo, json={"id": self.card["id"], "op": "add", "quote": "b", "note": "n"}).status_code, 404)
        self.assertEqual(c.post("/api/comments", headers=jo, json={"id": self.html["id"], "op": "add", "quote": "x", "note": ""}).status_code, 400)
        self.assertEqual(c.post("/api/comments", headers=jo, json={"id": self.html["id"], "op": "zap"}).status_code, 400)
        self.assertEqual(c.get("/api/comments", params={"id": self.html["id"]}).status_code, 401)

    def test_commenter_script_served(self):
        self.assertIn("window.__jc", c.get("/api/commenter.js").json()["script"])


class Heads(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.old = (s.ALWAYS_ON_FILE, s.head_lanes, s.config.STATE_DIR)
        s.ALWAYS_ON_FILE, s.config.STATE_DIR = d / "always_on.json", str(d)
        s.head_lanes = lambda: ["main", "drafts", "support"]
        (d / "support").mkdir(); (d / "support" / "ASLEEP").write_text("x")

    def tearDown(self):
        s.ALWAYS_ON_FILE, s.head_lanes, s.config.STATE_DIR = self.old

    def test_admin_only_listing_and_switch(self):
        self.assertEqual(c.get("/api/heads", headers=H(USER_B)).status_code, 403)
        self.assertEqual(c.post("/api/heads/always_on", headers=H(USER_B), json={"lane": "main", "on": True}).status_code, 403)
        r = c.get("/api/heads", headers=H(OWNER)).json()["heads"]
        self.assertEqual([h["lane"] for h in r], ["main", "drafts", "support"])
        self.assertEqual([h["asleep"] for h in r], [False, False, True])
        self.assertFalse(any(h["always_on"] for h in r))
        self.assertEqual(c.post("/api/heads/always_on", headers=H(OWNER), json={"lane": "support", "on": True}).json(),
                         {"lane": "support", "always_on": True})
        self.assertEqual(json.loads(s.ALWAYS_ON_FILE.read_text())["lanes"], ["support"])
        c.post("/api/heads/always_on", headers=H(OWNER), json={"lane": "main", "on": True})
        c.post("/api/heads/always_on", headers=H(OWNER), json={"lane": "support", "on": False})
        self.assertEqual(json.loads(s.ALWAYS_ON_FILE.read_text())["lanes"], ["main"])
        self.assertEqual(c.post("/api/heads/always_on", headers=H(OWNER), json={"lane": "../x", "on": True}).status_code, 404)

    def test_head_lanes_parses_units(self):
        out = ("harness-head@support.service loaded active running X\nharness-head@main.service loaded active running Y\n"
               "harness-gateway.service loaded active running Z\nharness-head@drafts.service loaded inactive dead W\n")
        run = lambda *a, **k: type("R", (), {"stdout": out})()
        self.assertEqual(self.old[1](run=run), ["main", "drafts", "support"])


if __name__ == "__main__":
    unittest.main()
