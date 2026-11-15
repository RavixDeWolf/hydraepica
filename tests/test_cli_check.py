"""cli_check: a head about to start the wrong Claude Code is said once per boot, never blocked."""
import _harness_env  # noqa: F401  throwaway config + state (must be first)
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "harness"))
import cli_check as c  # noqa: E402


def _bin(d, name="claude"):
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    open(p, "w").write("#!/bin/sh\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return p


def setup(monkey_env):
    root = tempfile.mkdtemp()
    os.environ["HEAD_STATE_DIR"] = os.path.join(root, "state")
    good = _bin(os.path.join(root, "home", ".local", "bin"))
    old = _bin(os.path.join(root, "usr", "bin"))
    return root, good, old


def test_match_is_logged_and_silent():
    root, good, old = setup(None)
    said = []
    assert c.run("main", os.path.dirname(good) + ":" + os.path.dirname(old), good, send=lambda t: said.append(t) or (True, ""))
    assert said == []
    assert "OK lane=main" in open(os.path.join(root, "state", "cli_check.log")).read()


def test_mismatch_said_once_per_boot():
    root, good, old = setup(None)
    said = []
    send = lambda t: said.append(t) or (True, "ok")
    path = os.path.dirname(old) + ":" + os.path.dirname(good)       # the old one wins on PATH
    assert not c.run("main", path, good, send=send, boot=lambda: "b1")
    assert not c.run("main", path, good, send=send, boot=lambda: "b1")
    assert len(said) == 1 and old in said[0]
    assert os.path.exists(os.path.join(root, "state", "CLI_MISMATCH"))
    c.run("main", path, good, send=send, boot=lambda: "b2")           # a new boot says it again
    assert len(said) == 2


def test_no_claude_at_all_and_failed_send_marked():
    root, good, old = setup(None)
    assert not c.run("x", "/nonexistent", good, send=lambda t: (False, "network down"), boot=lambda: "b")
    m = open(os.path.join(root, "state", "CLI_MISMATCH")).read()
    assert "resolved=None" in m and "TG SEND FAILED: network down" in m


def test_main_never_raises():
    os.environ["HEAD_STATE_DIR"] = "/proc/forbidden/x"
    assert c.main([]) == 0


def test_unit_runs_it_before_the_head():
    u = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deploy", "harness-head@.service")).read()
    assert "ExecStartPre=-/usr/bin/python3 -B %h/harness/harness/cli_check.py" in u
