"""The optional re-check holds a false done/can't once, and lets checked replies through."""
import _harness_env  # noqa: F401  throwaway config + state (must be first)
import io, json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "harness", "hooks"))
import recheck

def tx(*tools, prompt="do it"):
    rows = [{"type": "user", "message": {"content": prompt}}]
    for name, inp in tools:
        rows.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})
        rows.append({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}})
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    f.write("\n".join(json.dumps(r) for r in rows)); f.close()
    return f.name

def run(msg, path, active=False):
    d = {"last_assistant_message": msg, "stop_hook_active": active, "transcript_path": path}
    return recheck.decide(d, recheck.turn_tools(path))

class T(unittest.TestCase):
    def test_false_done_after_edit_is_held(self):
        self.assertIsNotNone(run("Fixed, it's live now.", tx(("Edit", {"file_path": "/x"}))))
    def test_done_with_no_tool_is_held(self):
        self.assertIsNotNone(run("Done.", tx()))
    def test_done_after_restart_is_held(self):
        self.assertIsNotNone(run("Restarted, all set.", tx(("Bash", {"command": "systemctl --user restart x"}))))
    def test_done_after_a_look_passes(self):
        self.assertIsNone(run("Fixed and verified.", tx(("Edit", {"file_path": "/x"}),
                                                        ("Bash", {"command": "pytest -q tests"}))))
    def test_cant_without_search_is_held(self):
        self.assertIsNotNone(run("I can't reach that box.", tx()))
    def test_cant_after_search_passes(self):
        self.assertIsNone(run("There's no such file.", tx(("Bash", {"command": "ls /etc/foo"}))))
    def test_second_stop_always_passes(self):
        self.assertIsNone(run("Done.", tx(), active=True))
    def test_plain_reply_passes(self):
        self.assertIsNone(run("Here is the plan for tomorrow.", tx()))
    def test_unreadable_transcript_never_blocks(self):
        self.assertIsNone(run("Done.", "/nonexistent/t.jsonl"))
    def test_turn_resets_at_new_prompt(self):
        p = tx(("Bash", {"command": "ls"}))
        with open(p, "a") as f:
            f.write("\n" + json.dumps({"type": "user", "message": {"content": "next"}}))
        self.assertEqual(recheck.turn_tools(p), [])

class Hook(unittest.TestCase):
    def test_hook_blocks_and_logs_stopheld_not_stop(self):
        d = tempfile.mkdtemp(); open(os.path.join(d, "recheck_on"), "w").close()
        import head_hook
        old = head_hook.D, head_hook.EV
        head_hook.D, head_hook.EV = d, os.path.join(d, "events.jsonl")
        self.addCleanup(lambda: setattr(head_hook, "D", old[0]) or setattr(head_hook, "EV", old[1]))
        out = io.StringIO()
        head_hook.handle({"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "Done.",
                          "transcript_path": tx(("Write", {"file_path": "/y"}))}, out=out)
        self.assertEqual(json.loads(out.getvalue())["decision"], "block")
        evs = [json.loads(l)["event"] for l in open(os.path.join(d, "events.jsonl"))]
        self.assertIn("StopHeld", evs); self.assertNotIn("Stop", evs)
        out2 = io.StringIO()
        head_hook.handle({"hook_event_name": "Stop", "stop_hook_active": True, "last_assistant_message": "Done.",
                          "transcript_path": tx()}, out=out2)
        self.assertEqual(out2.getvalue(), "")
        evs = [json.loads(l)["event"] for l in open(os.path.join(d, "events.jsonl"))]
        self.assertIn("Stop", evs)


class Plugin(unittest.TestCase):
    def test_off_by_default_on_per_lane(self):
        import head_hook
        d = tempfile.mkdtemp()
        old = head_hook.D, head_hook.append
        seen = []
        head_hook.D, head_hook.append = d, seen.append
        try:
            msg = {"hook_event_name": "Stop", "last_assistant_message": "Done, it is fixed.", "transcript_path": tx(("Edit", {}))}
            out = io.StringIO(); head_hook.handle(dict(msg), out=out)
            self.assertNotIn("block", out.getvalue())
            self.assertFalse(recheck.enabled(d))
            open(os.path.join(d, "recheck_on"), "w").close()
            out = io.StringIO(); head_hook.handle(dict(msg), out=out)
            self.assertEqual(json.loads(out.getvalue())["decision"], "block")
            self.assertEqual(seen[-1]["event"], "StopHeld")
            out = io.StringIO(); head_hook.handle(dict(msg, stop_hook_active=True), out=out)
            self.assertNotIn("block", out.getvalue())                  # one hold per turn, never a loop
            self.assertEqual(seen[-1]["event"], "Stop")
        finally:
            head_hook.D, head_hook.append = old

if __name__ == "__main__":
    unittest.main()
