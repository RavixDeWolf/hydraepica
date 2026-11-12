import _harness_env  # noqa: F401  throwaway config + state (must be first)
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness", "stage"))
import cast  # noqa: E402

CARD = {"id": "c1", "kind": "card", "title": "Hi", "body": "there"}
GAL = {"id": "g1", "kind": "gallery", "images": [{"src": "data:image/png;base64,AA"}, {"src": "data:image/png;base64,BB"}]}
VID = {"id": "v1", "kind": "video", "src": "data:video/mp4;base64,AA"}


class Receiver(unittest.TestCase):
    def test_screens_from_config(self):
        self.assertEqual(sorted(cast.screens()), ["den", "tv", "wall"])
        self.assertEqual(cast.screen_key("tv"), "tv-key-0123456789abcdef0123456789abcdef")
        self.assertEqual(cast.screen_key("wall"), "")          # only a receiver has a key

    def test_cast_writes_the_page_and_now(self):
        r = cast.cast(CARD, "tv", "alex")
        self.assertTrue((cast.SCREENS_DIR / "tv" / (r["page"] + ".html")).is_file())
        self.assertEqual(cast.now_on("tv")["id"], "c1")
        cast.stop("tv", "alex")
        self.assertIsNone(cast.now_on("tv"))

    def test_gallery_pick_and_video_control_write_the_state_file(self):
        cast.cast(GAL, "tv", "alex")
        cast.pick(GAL, "tv", 1, "alex")
        self.assertEqual((cast.SCREENS_DIR / "tv" / "cast-g1.js").read_text(), "gal(1)")
        cast.cast(VID, "tv", "alex")
        t = [1000.0]
        cast.control("tv", "pause", "alex", clock=lambda: t[0])
        self.assertTrue(cast.control("tv", "status", "alex", clock=lambda: t[0])["paused"])
        cast.control("tv", "seek", "alex", t=42, clock=lambda: t[0])
        self.assertIn("t:42.000", (cast.SCREENS_DIR / "tv" / "cast-v1.js").read_text())
        cast.control("tv", "play", "alex", clock=lambda: t[0]); t[0] += 3
        self.assertAlmostEqual(cast.control("tv", "status", "alex", clock=lambda: t[0])["t"], 45, places=1)

    def test_refusals_are_loud(self):
        with self.assertRaises(cast.CastError):
            cast.cast(CARD, "attic", "alex")
        cast.cast(CARD, "tv", "alex")
        with self.assertRaises(cast.CastError):
            cast.control("tv", "play", "alex")                # a card is not a video
        with self.assertRaises(cast.CastError):
            cast.pick(GAL, "tv", 0, "alex")                   # that gallery is not on the screen
        log = [json.loads(l)["event"] for l in open(cast.LOG)]
        self.assertIn("cast_failed", log); self.assertIn("control_failed", log); self.assertIn("pick_failed", log)


class Remote(unittest.TestCase):
    def test_http_screen_gets_the_page_then_the_state(self):
        posts = []
        post = lambda url, body, timeout=10: posts.append((url, body)) or (200, {"ok": True})
        cast.cast(GAL, "wall", "alex", post=post)
        self.assertEqual(posts[0][0], "http://wall.test/api/cast"); self.assertIn("<html", posts[0][1]["html"])
        self.assertEqual(posts[1][1], {"name": "cast-g1", "state": "gal(0)"})

    def test_http_refusal_is_an_error(self):
        with self.assertRaises(cast.CastError):
            cast.cast(CARD, "wall", "alex", post=lambda *a, **k: (500, {}))

    def test_ssh_screen_copies_the_page(self):
        calls = []
        ssh = lambda host, args, data=None: calls.append((host, args, data)) or ""
        cast.cast(CARD, "den", "alex", ssh=ssh)
        self.assertEqual(calls[0][0], "den"); self.assertIn(b"Hi", calls[0][2])

    def test_stored_video_goes_to_receivers_only(self):
        with self.assertRaises(cast.CastError):
            cast.cast({"id": "v2", "kind": "video", "src": "media:alex/big.mp4"}, "wall", "alex",
                      post=lambda *a, **k: (200, {}))


if __name__ == "__main__":
    unittest.main()


class ShortKey(unittest.TestCase):
    def test_short_key_disables_the_screen(self):
        os.environ["TEST_TV_KEY"], old = "short", os.environ["TEST_TV_KEY"]
        try:
            self.assertEqual(cast.screen_key("tv"), "")
        finally:
            os.environ["TEST_TV_KEY"] = old
