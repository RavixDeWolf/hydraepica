import _harness_env  # noqa: F401  throwaway config + state (must be first)
import io
import json
import os
import unittest

import config
import llm


def resp(text):
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    return R(json.dumps({"choices": [{"message": {"content": text}}]}).encode())


class LLM(unittest.TestCase):
    def setUp(self):
        self.eps = [{"name": "local", "url": "http://a", "model": "m"},
                    {"name": "router", "url": "http://b", "model": "m", "key_env": "T_KEY"}]
        llm.endpoints = lambda: self.eps
        self.logs = []
        os.environ["T_KEY"] = "k"

    def test_first_endpoint_answers(self):
        self.assertEqual(llm.chat("hi", "s", log=self.logs.append, opener=lambda r, timeout: resp("hello")), "hello")
        self.assertEqual(self.logs, [])

    def test_fallback_is_logged_and_carries_the_key(self):
        seen = []
        def op(req, timeout):
            seen.append(req)
            if req.full_url == "http://a":
                raise OSError("refused")
            return resp("from b")
        self.assertEqual(llm.chat("hi", "s", log=self.logs.append, opener=op), "from b")
        self.assertEqual(seen[1].get_header("Authorization"), "Bearer k")
        self.assertTrue(any("local failed" in l for l in self.logs) and any("fallback router" in l for l in self.logs))

    def test_give_up_is_logged_and_returns_none(self):
        def op(req, timeout):
            raise OSError("down")
        self.assertIsNone(llm.chat("hi", "s", log=self.logs.append, opener=op))
        self.assertIn("gave up", self.logs[-1])

    def test_missing_key_is_a_failure_not_a_crash(self):
        del os.environ["T_KEY"]
        self.eps = self.eps[1:]
        self.assertIsNone(llm.chat("hi", "s", log=self.logs.append, opener=lambda r, timeout: resp("x")))
        self.assertIn("T_KEY", self.logs[0])


class Gateway(unittest.TestCase):
    def setUp(self):
        import gateway
        self.g = gateway
        self.sent, self.heads = [], []
        gateway.api = lambda method, http_timeout=35, **kw: self.sent.append((method, kw)) or {"ok": True}
        import head_lane
        self._h = head_lane.handle
        head_lane.handle = lambda m, tok: self.heads.append(m) or m.get("message_thread_id") == 7

    def tearDown(self):
        import head_lane
        head_lane.handle = self._h

    def msg(self, uid, text="hi", thread=None, chat_type="private"):
        m = {"message_id": 1, "from": {"id": uid}, "chat": {"id": uid, "type": chat_type}, "text": text, "date": 1}
        if thread is not None:
            m["message_thread_id"] = thread
        return {"update_id": 10, "message": m}

    def test_stranger_never_reaches_a_head(self):
        self.g.handle(self.msg(99))
        self.assertEqual(self.heads, [])
        self.assertEqual(self.sent[0][1]["text"], self.g.STRANGER_MSG)

    def test_stranger_in_a_group_is_not_answered(self):
        self.g.handle(self.msg(99, chat_type="supergroup"))
        self.assertEqual(self.sent, [])

    def test_user_in_a_head_thread_goes_to_the_head(self):
        self.g.handle(self.msg(config.OWNER_ID, thread=7))
        self.assertEqual(len(self.heads), 1)
        self.assertEqual(self.sent, [])

    def test_button_from_a_non_user_is_ignored(self):
        pressed = []
        self.g.CB_HANDLERS["hd:"] = lambda cq, d: pressed.append(d)
        self.g.handle({"update_id": 11, "callback_query": {"id": "c", "data": "hd:yes", "from": {"id": 99}}})
        self.assertEqual(pressed, [])
        self.g.handle({"update_id": 12, "callback_query": {"id": "c", "data": "hd:yes", "from": {"id": config.OWNER_ID}}})
        self.assertEqual(pressed, ["hd:yes"])


if __name__ == "__main__":
    unittest.main()
