"""head_bridge idle sleep, wake-on-send, Always-on list, idle clock across restarts, and lane_env.
Every tmux call, screen and process list is faked; nothing touches a real tmux server or Claude Code."""
import _harness_env  # noqa: F401  throwaway config + state (must be first)
import os
import sys
import tempfile

_T = tempfile.mkdtemp(prefix="bridge-test-")
os.environ.setdefault("HEAD_SOCKET", os.path.join(_T, "head.sock"))
os.environ.setdefault("HEAD_LOG", os.path.join(_T, "bridge.log"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "harness"))
import head_bridge as hb  # noqa: E402

hb.LOG = os.path.join(_T, "inproc.log")


def _sleepy(monkeypatch, tmp_path, screen, state="idle", kids=()):
    """Sleeper harness: a fake live session whose screen/state/children we control; tmux calls recorded."""
    calls, live = [], {"on": True}
    monkeypatch.setattr(hb, "ASLEEP", str(tmp_path / "ASLEEP"))
    monkeypatch.setattr(hb, "ACTIVE", str(tmp_path / "last_active"))
    monkeypatch.setattr(hb, "_act", {"t": 0, "screen": None, "w": 0.0})
    monkeypatch.setattr(hb, "IDLE_SLEEP_S", 3 * 3600)
    monkeypatch.setattr(hb, "ALWAYS_ON", str(tmp_path / "always_on.json"))
    monkeypatch.setattr(hb, "LANE", "rl")
    monkeypatch.setattr(hb, "alive", lambda: live["on"])
    monkeypatch.setattr(hb, "capture", lambda n=60, ansi=False: list(screen))
    monkeypatch.setattr(hb, "classify_pane", lambda lines, a=True: state if a else ("asleep" if os.path.exists(hb.ASLEEP) else "dead"))
    monkeypatch.setattr(hb, "working_children", lambda: list(kids))
    def tmux(*a, timeout=8):
        calls.append(a)
        if a[0] == "kill-session": live["on"] = False
        if a[0] == "new-session": live["on"] = True
        return 0, "", ""
    monkeypatch.setattr(hb, "tmux", tmux)
    hb._act.update(t=0, screen=None)
    return calls, live


def test_idle_session_sleeps_after_3h_and_marks_asleep(monkeypatch, tmp_path):
    calls, live = _sleepy(monkeypatch, tmp_path, ["❯ "])
    assert hb.sleep_check(now=1000).startswith("idle")             # first sight after a start is not activity
    assert hb.sleep_check(now=3 * 3600 - 5).startswith("idle")     # clock runs from the saved last activity (t=0)
    assert hb.sleep_check(now=3 * 3600 + 1) == "slept"
    assert any(a[0] == "kill-session" for a in calls)
    assert os.path.exists(hb.ASLEEP) and live["on"] is False


def test_sleep_blocked_by_activity_busy_or_background_work(monkeypatch, tmp_path):
    calls, _ = _sleepy(monkeypatch, tmp_path, ["❯ "], state="busy")
    hb.sleep_check(now=0); assert hb.sleep_check(now=4 * 3600) == "state busy"
    calls, _ = _sleepy(monkeypatch, tmp_path, ["❯ "], kids=["ffmpeg"])
    hb.sleep_check(now=0); assert hb.sleep_check(now=4 * 3600).startswith("background work")
    calls, _ = _sleepy(monkeypatch, tmp_path, ["❯ "])
    hb.sleep_check(now=0); hb._act["t"] = 3 * 3600                  # a send/key moves the clock
    assert hb.sleep_check(now=4 * 3600).startswith("idle")
    monkeypatch.setattr(hb, "IDLE_SLEEP_S", 0)
    assert hb.sleep_check(now=99 * 3600) == "disabled"
    assert not any(a[0] == "kill-session" for a in calls)


def test_asleep_status_and_ensure_wakes_and_clears_marker(monkeypatch, tmp_path):
    calls, live = _sleepy(monkeypatch, tmp_path, ["❯ "])
    hb.sleep_check(now=0); hb.sleep_check(now=4 * 3600)
    assert hb.status()["state"] == "asleep"
    started, detail = hb.ensure()
    assert detail == "started" and not os.path.exists(hb.ASLEEP)
    assert any(a[0] == "new-session" for a in calls)


def test_working_children_ignores_helpers(monkeypatch):
    monkeypatch.setattr(hb, "tmux", lambda *a, timeout=8: (0, "100\n", ""))
    ps = "100 1 claude --continue\n101 100 npm exec @playwright/mcp@0.0.83\n102 101 node /x/node_modules/.bin/playwright-mcp\n103 100 /bin/bash -c source x; ffmpeg -i a b\n104 103 ffmpeg -i a b\n"
    monkeypatch.setattr(hb.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": ps})())
    got = hb.working_children()
    assert "ffmpeg" in got and not any("playwright" in g or g in ("npm", "node", "claude") for g in got), got


def test_classify_dead_vs_asleep(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "ASLEEP", str(tmp_path / "ASLEEP"))
    assert hb.classify_pane([], is_alive=False) == "dead"
    open(hb.ASLEEP, "w").write("x")
    assert hb.classify_pane([], is_alive=False) == "asleep"


def test_always_on_head_never_sleeps_and_bad_list_stays_awake(monkeypatch, tmp_path):
    calls, live = _sleepy(monkeypatch, tmp_path, ["❯ "])
    open(hb.ALWAYS_ON, "w").write('{"lanes": ["rl"]}')
    hb.sleep_check(now=0)
    assert hb.sleep_check(now=9 * 3600) == "always on" and live["on"]
    open(hb.ALWAYS_ON, "w").write('{"lanes": ["other"]}')
    hb.sleep_check(now=0)
    assert hb.sleep_check(now=9 * 3600) == "slept"
    calls, live = _sleepy(monkeypatch, tmp_path, ["❯ "])
    open(hb.ALWAYS_ON, "w").write("{garbled")
    hb.sleep_check(now=0)
    assert hb.sleep_check(now=9 * 3600) == "always on" and live["on"]


def test_send_from_any_route_wakes_an_asleep_head_first(monkeypatch, tmp_path):
    calls, live = _sleepy(monkeypatch, tmp_path, ["❯ "])
    hb.sleep_check(now=0); assert hb.sleep_check(now=4 * 3600) == "slept"
    sent = []
    monkeypatch.setattr(hb, "send_text", lambda text: (sent.append((live["on"], text)), (True, "sent"))[1])
    out = hb.handle({"t": "send", "text": "ticket poke"})
    assert out[0]["ok"] and sent == [(True, "ticket poke")]          # typed only after the wake
    assert not os.path.exists(hb.ASLEEP) and any(a[0] == "new-session" for a in calls)


def test_wake_that_never_gets_ready_is_an_error_not_a_send(monkeypatch, tmp_path):
    calls, live = _sleepy(monkeypatch, tmp_path, ["❯ "], state="unknown")
    open(hb.ASLEEP, "w").write("x"); live["on"] = False
    t = {"v": 0}
    monkeypatch.setattr(hb.time, "sleep", lambda s: t.update(v=t["v"] + s))
    monkeypatch.setattr(hb, "send_text", lambda text: (_ for _ in ()).throw(AssertionError("typed into a head that is not ready")))
    why = hb.wake_for("send", sleep=lambda s: t.update(v=t["v"] + s), now=lambda: t["v"])
    assert why and "not ready" in why
    open(hb.ASLEEP, "w").write("x"); live["on"] = False
    monkeypatch.setattr(hb, "WAKE_WAIT_S", 0)
    out = hb.handle({"t": "send", "text": "hi"})                         # through the socket path: an error, nothing typed
    assert out[0]["t"] == "error" and "not ready" in out[0]["error"]


def test_awake_head_send_is_untouched(monkeypatch, tmp_path):
    calls, live = _sleepy(monkeypatch, tmp_path, ["❯ "])
    assert hb.wake_for("send") is None and not any(a[0] == "new-session" for a in calls)


def test_lane_env_routes_headsafe_probe_to_the_lane():
    e = {"HEAD_LANE": "den", "HEAD_STATE_DIR": "/s/den", "HEAD_SOCKET": "/s/den/head.sock", "PATH": "x"}
    out = hb.lane_env(e)
    assert out == {"HEAD_LANE": "den", "HEAD_STATE_DIR": "/s/den",
                   "HEADSAFE_SOCK": "/s/den/head.sock", "HEADSAFE_EVENTS": "/s/den/events.jsonl"}


def test_lane_env_main_head_keeps_headsafe_defaults():
    assert hb.lane_env({"HEAD_SOCKET": "/s/head.sock"}) == {}
    out = hb.lane_env({"HEAD_LANE": "main", "HEAD_STATE_DIR": "/s", "HEAD_SOCKET": "/s/head.sock"})
    assert "HEADSAFE_SOCK" not in out and "HEADSAFE_EVENTS" not in out


def test_idle_clock_survives_restart(monkeypatch, tmp_path):
    """A bridge restart must not reset the 3 h idle clock."""
    calls, _ = _sleepy(monkeypatch, tmp_path, ["❯ "])
    hb._mark(now=1000)                                  # last real activity
    assert open(hb.ACTIVE).read().strip() == "1000"
    assert hb._load_act(now=5000) == 1000               # a new bridge starts from the saved time, not now
    hb._act.update({"t": hb._load_act(now=5000), "screen": None})
    assert hb.sleep_check(now=5000) != "screen changed"  # first look after restart is not activity
    assert hb.sleep_check(now=1000 + 3 * 3600 + 1) == "slept"


def test_load_act_falls_back_to_events_then_now(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "ACTIVE", str(tmp_path / "last_active"))
    assert hb._load_act(now=777) == 777
    ev = tmp_path / "events.jsonl"; ev.write_text("x\n"); os.utime(ev, (500, 500))
    assert hb._load_act(now=777) == 500
    (tmp_path / "last_active").write_text("9999999999\n")
    assert hb._load_act(now=777) == 777                 # never in the future
