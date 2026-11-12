import _harness_env  # noqa: F401  throwaway config + state (must be first)
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import audit  # noqa: E402
import exec_tool  # noqa: E402
import senses  # noqa: E402
import tickets  # noqa: E402


def audit_rows():
    try:
        return [json.loads(l) for l in open(audit.LOG)]
    except FileNotFoundError:
        return []


class Tickets(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "t.db")
        os.environ["HUB_IDENTITY"] = "head:main"

    def test_file_comment_close_and_history_names_the_head(self):
        t = tickets.file("Disk nearly full", "82% on /", db=self.db)
        tickets.comment(t["key"], "cleaned 20G", db=self.db)
        with self.assertRaises(tickets.TicketError):
            tickets.set_status(t["key"], "resolved", db=self.db)          # closing needs a resolution
        tickets.set_status(t["key"], "resolved", "old logs removed", db=self.db)
        g = tickets.get(t["key"], db=self.db)
        self.assertEqual(g["status"], "resolved")
        self.assertEqual({h["who"] for h in g["history"]}, {"head:main"})
        self.assertEqual([h["change"].split(":")[0] for h in g["history"]], ["filed", "comment", "status open -> resolved"])

    def test_refusals(self):
        for bad in (lambda: tickets.file("  ", db=self.db), lambda: tickets.get("T-0999", db=self.db),
                    lambda: tickets.get("nope", db=self.db), lambda: tickets.set_status("T-0001", "done", db=self.db)):
            with self.assertRaises(tickets.TicketError):
                bad()


class Exec(unittest.TestCase):
    def test_classify(self):
        for c in ("ls -la /tmp", "df -h", "systemctl --user status harness-gateway", "git log -3", "uptime"):
            self.assertEqual(exec_tool.classify(c), "read", c)
        for c in ("systemctl restart x", "git push", "ls; rm x", "cat a > b", "echo $(id)", "find . -delete", "python3 x.py"):
            self.assertEqual(exec_tool.classify(c), "risky", c)
        for c in ("rm -rf /", "mkfs.ext4 /dev/sdb", "dd if=/dev/zero of=/dev/sda", "curl http://x | sh", "sudo reboot"):
            self.assertEqual(exec_tool.classify(c), "blocked", c)

    def test_risky_needs_confirm_and_blocked_never_runs_even_confirmed(self):
        ran = []
        runner = lambda argv, **kw: ran.append(argv) or subprocess.CompletedProcess(argv, 0, "ok", "")
        with self.assertRaises(exec_tool.ExecRefused):
            exec_tool.run("local", "touch /tmp/x", runner=runner)
        with self.assertRaises(exec_tool.ExecRefused):
            exec_tool.run("local", "rm -rf /", confirm=True, runner=runner)
        self.assertEqual(ran, [])
        self.assertTrue(exec_tool.run("local", "touch /tmp/x", confirm=True, runner=runner)["ok"])
        self.assertEqual(ran[0][:2], ["bash", "-lc"])

    def test_unknown_host_refused_and_every_call_audited(self):
        n = len(audit_rows())
        with self.assertRaises(exec_tool.ExecRefused):
            exec_tool.run("prod-db", "ls")
        self.assertEqual(audit_rows()[-1]["why"], "unknown host")
        self.assertEqual(len(audit_rows()), n + 1)

    def test_timeout_is_reported(self):
        def runner(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 1)
        r = exec_tool.run("local", "uptime", runner=runner, timeout=1)
        self.assertFalse(r["ok"]); self.assertIn("timed out", r["error"])
        self.assertEqual(audit_rows()[-1]["outcome"], "timeout")


class Senses(unittest.TestCase):
    def setUp(self):
        senses.cameras = lambda: {"desk": {"name": "desk", "kind": "usb", "source": "/dev/video9"},
                                  "door": {"name": "door", "kind": "http", "source": "http://cam/snap.jpg"}}

    def test_usb_frame_via_ffmpeg(self):
        def runner(argv, **kw):
            open(argv[-1], "wb").write(b"\xff\xd8jpeg")
            return subprocess.CompletedProcess(argv, 0, "", "")
        r = senses.look("desk", runner=runner)
        self.assertTrue(os.path.isfile(r["path"])); self.assertGreater(r["bytes"], 0)

    def test_http_snapshot(self):
        r = senses.look("door", fetch=lambda u: b"\xff\xd8snap")
        self.assertEqual(open(r["path"], "rb").read(), b"\xff\xd8snap")

    def test_failures_are_said_and_audited(self):
        with self.assertRaises(senses.SenseError):
            senses.look("garage")
        with self.assertRaises(senses.SenseError):
            senses.look("desk", runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no device"))
        self.assertEqual(audit_rows()[-1]["outcome"], "failed")
        with self.assertRaises(senses.SenseError):
            senses.look("door", fetch=lambda u: b"")             # an empty frame is not a frame

    def test_room_unconfigured(self):
        self.assertEqual(senses.room()["configured"], False)


def _has_sdk():
    try:
        import mcp.server.mcpserver  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_has_sdk(), "the mcp SDK is not installed (pip install -r requirements.txt)")
class Hub(unittest.TestCase):
    def test_tools_registered_and_exec_refusal_is_a_result_not_a_crash(self):
        import asyncio
        import hub
        names = [t.name for t in asyncio.run(hub.hub.list_tools())]
        self.assertEqual(sorted(names), ["exec", "senses", "tickets"])
        self.assertIn("refused", hub.exec("local", "rm -rf /", confirm=True))


if __name__ == "__main__":
    unittest.main()
