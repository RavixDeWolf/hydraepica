"""A long or summarised head turn says it is still working; the reply of a long turn is still delivered;
a dropped final reply is retried once, then said at once."""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import test_head_lane as base   # noqa: E402  all live paths of head_lane are redirected to a temp dir there
hl, pl = base.hl, base.pl


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class IO:
    def __init__(self):
        self.said, self.tools, self.span = [], [], None

    def say(self, t): self.said.append(t)
    def step(self, line): self.tools.append(line)
    def perm_card(self, *a): pass
    def react(self, *a): pass


def script(clock, events):
    """events: [(at_seconds_after_start, record)] -> a read(off) that hands each record out once its time has come."""
    t0 = clock.t
    todo = list(events)

    def read(off):
        out = [r for at, r in todo if clock.t - t0 >= at]
        for x in [e for e in todo if clock.t - t0 >= e[0]]:
            todo.remove(x)
        return out, off + len(out)
    return read


def status(state):
    return lambda req, **k: {"state": state, "tail": []}


COMPACT = {"event": "SessionStart", "data": {"source": "compact"}}
STOP = {"event": "Stop", "data": {"last_assistant_message": "the real answer"}}


class Compaction(unittest.TestCase):
    def test_one_line_when_context_is_summarised_then_the_reply(self):
        c, io = Clock(), IO()
        io.tools.append("Bash: make video")
        read = script(c, [(60, COMPACT), (70, COMPACT), (120, STOP)])
        out, payload, _ = hl.follow(0, io, call=status("busy"), read=read, sleep=c.sleep, now=c.now)
        self.assertEqual((out, payload), ("done", "the real answer"))
        notes = [s for s in io.said if "summarised" in s]
        self.assertEqual(len(notes), 1, io.said)
        self.assertIn("Still working", notes[0])
        self.assertIn("make video", notes[0])

    def test_a_startup_session_start_is_not_a_compaction(self):
        c, io = Clock(), IO()
        read = script(c, [(5, {"event": "SessionStart", "data": {"source": "startup"}}), (10, STOP)])
        hl.follow(0, io, call=status("busy"), read=read, sleep=c.sleep, now=c.now)
        self.assertEqual(io.said, [])

    def test_a_second_compaction_much_later_gets_its_own_line(self):
        c, io = Clock(), IO()
        read = script(c, [(60, COMPACT), (60 + hl.COMPACT_GAP_S + 5, COMPACT), (hl.COMPACT_GAP_S + 100, STOP)])
        hl.follow(0, io, call=status("busy"), read=read, sleep=c.sleep, now=c.now)
        self.assertEqual(len([s for s in io.said if "summarised" in s]), 2)


class LongTurn(unittest.TestCase):
    def test_still_working_at_10_min_then_every_15_never_a_stream(self):
        c, io = Clock(), IO()
        read = script(c, [(41 * 60, STOP)])
        out, payload, _ = hl.follow(0, io, call=status("busy"), read=read, sleep=c.sleep, now=c.now)
        self.assertEqual(out, "done")                       # past 30 min the reply is still delivered as the answer
        still = [s for s in io.said if s.startswith("⏳ Still working,")]
        self.assertEqual(len(still), 3, io.said)            # 10, 25, 40 min
        self.assertIn("10 min", still[0])
        self.assertIn("25 min", still[1])

    def test_a_short_turn_says_nothing_extra(self):
        c, io = Clock(), IO()
        hl.follow(0, io, call=status("busy"), read=script(c, [(9 * 60, STOP)]), sleep=c.sleep, now=c.now)
        self.assertEqual(io.said, [])

    def test_silent_and_not_busy_gives_up_after_30_min(self):
        c, io = Clock(), IO()
        out, why, _ = hl.follow(0, io, call=status("idle"), read=script(c, []), sleep=c.sleep, now=c.now)
        self.assertEqual(out, "timeout")
        self.assertIn("30 min", why)
        self.assertLess(c.t - 1000, 31 * 60)

    def test_hook_events_keep_a_long_turn_alive(self):
        c, io = Clock(), IO()
        ev = [(m * 60, {"event": "PreToolUse", "data": {"tool_name": "Bash", "tool_input": {}}}) for m in range(5, 60, 5)]
        out, _, _ = hl.follow(0, io, call=status("idle"), read=script(c, ev + [(62 * 60, STOP)]), sleep=c.sleep, now=c.now)
        self.assertEqual(out, "done")

    def test_hard_cap(self):
        c, io = Clock(), IO()

        def sleep(s):                                       # a missing cap must FAIL, not spin forever
            c.sleep(s)
            if c.t - 1000 > 3 * 3600:
                raise AssertionError("no hard cap: still following after 3 h")
        out, why, _ = hl.follow(0, io, call=status("busy"), read=script(c, []), sleep=sleep, now=c.now,
                                hard_cap=2 * 3600)
        self.assertEqual(out, "timeout")
        self.assertIn("2 h", why)
        self.assertGreaterEqual(c.t - 1000, 2 * 3600)


class FakeAnswer(IO):
    def __init__(self, fails):
        super().__init__()
        self.fails, self.calls = fails, []

    def answer(self, md):
        self.calls.append(md)
        if self.fails:
            self.fails -= 1
            self.lost = getattr(self, "lost", []) + [md]
            return []
        return [len(self.calls)]


class DroppedReply(unittest.TestCase):
    def setUp(self):
        if os.path.exists(hl.UNDELIVERED):
            os.remove(hl.UNDELIVERED)

    def test_retry_once_after_a_short_wait(self):
        c, io = Clock(), FakeAnswer(fails=1)
        ids = hl.deliver(io, "hello", sleep=c.sleep)
        self.assertEqual(io.calls, ["hello", "hello"])
        self.assertEqual(c.slept, [hl.SEND_RETRY_WAIT_S])
        self.assertTrue(ids)
        self.assertEqual(io.said, [])
        self.assertEqual(hl.pending_notice(commit=False), "")

    def test_second_failure_is_said_at_once_and_noted(self):
        c, io = Clock(), FakeAnswer(fails=2)
        hl.deliver(io, "hello", sleep=c.sleep)
        self.assertEqual(len(io.calls), 2)                  # one retry, not more
        self.assertEqual(len(io.said), 1)
        self.assertIn("didn't send", io.said[0])
        self.assertIn("not delivered", hl.pending_notice())

    def test_no_failure_no_wait(self):
        c, io = Clock(), FakeAnswer(fails=0)
        hl.deliver(io, "hello", sleep=c.sleep)
        self.assertEqual(c.slept, [])

    def test_answer_records_a_part_telegram_refused(self):
        o = pl._api

        def api(tok, method, **kw):
            raise RuntimeError("HTTP Error 429")
        pl._api = api
        try:
            io = hl._IO("t", {"chat": {"id": 111111111}, "message_id": 1})
            io.lost = []
            self.assertEqual(io._answer("x" * (hl.RICH_LIMIT + 10)), [])
            self.assertTrue(io.lost)
        finally:
            pl._api = o


class RateLimit(unittest.TestCase):
    def setUp(self):
        if os.path.exists(hl.RETRIES):
            os.remove(hl.RETRIES)
        hl._state["auto_retry_ts"] = 0
        self.q, self.cards = [], []
        self._o = hl._send_card
        hl._send_card = lambda tok, m, text, kb: self.cards.append(text)
        self.m = {"chat": {"id": 111111111}, "from": {"id": 111111111}, "message_id": 5}

    def tearDown(self):
        hl._send_card = self._o

    def test_rate_limit_waits_then_retries_once_then_tells_owner(self):
        c, io = Clock(), IO()
        io.tok = "t"
        enq = lambda tok, m, text: self.q.append(text)   # noqa: E731
        self.assertTrue(hl.retryable("rate_limit"))
        self.assertEqual(hl.retry_or_offer(io, self.m, "rate_limit", now=c.now, enqueue=enq, sleep=c.sleep), "retried")
        self.assertEqual(c.slept, [hl.RATE_LIMIT_WAIT_S])
        self.assertEqual(len(self.q), 1)
        self.assertIn("30 s", io.said[0])
        c.t += 10
        self.assertEqual(hl.retry_or_offer(io, self.m, "rate_limit", now=c.now, enqueue=enq, sleep=c.sleep), "offered")
        self.assertEqual(len(self.q), 1)
        self.assertIn("didn't send", self.cards[0])

    def test_connection_drop_does_not_wait(self):
        c, io = Clock(), IO()
        io.tok = "t"
        hl.retry_or_offer(io, self.m, "server_error", now=c.now, enqueue=lambda *a: None, sleep=c.sleep)
        self.assertEqual(c.slept, [])


if __name__ == "__main__":
    unittest.main()


class Denied(unittest.TestCase):
    """Auto mode's refusals reach the thread (they were silent)."""
    def test_refusal_is_said_and_the_turn_goes_on(self):
        c, io = Clock(), IO()
        read = script(c, [(5, {"event": "PermissionDenied", "data": {"tool_name": "Bash",
                                "tool_input": {"command": "rm -rf ~/demo-scratch"}, "reason": "bulk delete"}}),
                          (10, {"event": "Stop", "data": {"last_assistant_message": "ok"}})])
        out = hl.follow(0, io, call=lambda c_: {"state": "idle"}, read=read, sleep=c.sleep, now=c.now)
        self.assertEqual(out[0], "done")
        self.assertEqual(len(io.said), 1)
        self.assertIn("refused a Bash call (bulk delete)", io.said[0])
        self.assertIn("rm -rf ~/demo-scratch", io.said[0])

    def test_settings_template_registers_the_hook(self):
        import json
        p = os.path.join(_HERE, "..", "harness", "templates", "head_settings.json")
        self.assertIn("PermissionDenied", json.load(open(p))["hooks"])
