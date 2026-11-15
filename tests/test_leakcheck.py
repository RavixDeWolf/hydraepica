"""Demo-safe leak check: what is flagged, what is not, and that the hook only acts in a demo-safe lane."""
import io
import json
import os
import re
import sys
import tempfile
import _harness_env  # noqa: F401  (throwaway config first)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "harness", "hooks"))
import leakcheck as L   # noqa: E402
import head_hook        # noqa: E402


def test_text_hits():
    t = "Server at 203.0.113.3 and file /srv/x/a.py and ~/work, localhost:4474, sk-abcdefghijklmnopqrstu"
    rules = {r for r, _ in L.find(t)}
    assert rules == {"IP address", "internal path", "local service port", "key or token"}


def test_clean_text():
    assert L.scan_reply("Rendered in 40 seconds, version 1.2.3 is out. See example.org", ocr_fn=lambda p: "") == []


def test_media_line_itself_ignored_but_image_scanned():
    r = L.scan_reply("Here it is\nMEDIA:/somewhere/x.png", ocr_fn=lambda p: "ok 203.0.113.5")
    assert r == ["image x.png: IP address '203.0.113.5'"]


def test_unreadable_image_blocks():
    def bad(p):
        raise RuntimeError("nope")
    assert "could not be checked" in L.scan_reply("MEDIA:/a/b.jpg", ocr_fn=bad)[0]


def test_non_image_media_skipped():
    assert L.scan_reply("MEDIA:/a/b.ogg", ocr_fn=lambda p: "203.0.113.1") == []


def test_config_aliases_words_domains():
    aliases, rules = L._build({"aliases": {"build-box": "Builder"}, "words": ["Project Falcon"],
                               "domains": ["corp.example"]})
    assert aliases == {"build-box": "Builder"}
    names = {n for n, _ in rules}
    assert {"private word", "internal site name"} <= names
    rx = dict(rules)
    assert rx["private word"].search("about project falcon today")
    assert rx["internal site name"].search("see git.corp.example") and not rx["internal site name"].search("www.corp.example")
    assert L._build({}) == ({}, [r for r in rules if r[0] == "account name"])


def test_aliases_flag_and_suggest(monkeypatch=None):
    old = (L.ALIASES, L._NAME)
    L.ALIASES = {"build-box": "Builder", "NAS": "Storage"}
    L._NAME = re.compile(r"(?<![\w.-])(build-box|NAS)(?![\w-])")
    try:
        hits = L.find_all("Ran it on build-box, then NAS")
        assert [m for _, m in hits] == ["build-box", "NAS"] and "Builder" in hits[0][0]
        assert L.find_all("Done on Builder. NASA and nas.txt are fine") == []
    finally:
        L.ALIASES, L._NAME = old


def test_label_bash_falls_back_to_command():
    assert L.scan_tool("Bash", {"command": "cat /etc/hosts"}) != []
    assert L.scan_tool("Bash", {"description": "Show hosts", "command": "cat /etc/hosts"}) == []


def test_midturn_send_text_and_image():
    cmd = 'python3 ~/harness/harness/tg/tg.py send "box at 203.0.113.9"'
    assert L.scan_tool("Bash", {"description": "Send note", "command": cmd}) == ["tg.py send: IP address '203.0.113.9'"]
    f = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
    cmd = f"python3 ~/harness/harness/stage/stage.py image {f}"
    assert "image" in L.scan_tool("Bash", {"description": "Stage it", "command": cmd}, ocr_fn=lambda p: "at 203.0.113.9")[0]
    assert L.scan_tool("Bash", {"description": "Stage it", "command": cmd}, ocr_fn=lambda p: "kitchen") == []


def test_bg_only_name_checked():
    cmd = "python3 ~/h/tg/bg.py --name Render -- python3 /srv/a/run.py"
    assert L.scan_tool("Bash", {"description": "Render", "command": cmd}) == []
    cmd = "python3 ~/h/tg/bg.py --name 'sync 203.0.113.2' -- true"
    assert L.scan_tool("Bash", {"description": "Render", "command": cmd}) != []


def test_hook_acts_only_in_a_demo_safe_lane():
    d = tempfile.mkdtemp()
    old = head_hook.D, head_hook.append
    head_hook.D, head_hook.append = d, (lambda rec: None)
    try:
        msg = {"hook_event_name": "Stop", "last_assistant_message": "see /etc/caddy"}
        out = io.StringIO(); head_hook.handle(dict(msg), out=out)
        assert '"block"' not in out.getvalue()
        open(os.path.join(d, "demo_safe"), "w").close()
        out = io.StringIO(); head_hook.handle(dict(msg), out=out)
        assert json.loads(out.getvalue().splitlines()[0])["decision"] == "block"
        assert "leakcheck.log" in os.listdir(d)
        pre = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"description": "Ping 203.0.113.1", "command": "x"}}
        out = io.StringIO(); head_hook.handle(dict(pre), out=out)
        assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"
        pre["tool_input"]["description"] = "Ping the router"
        out = io.StringIO(); head_hook.handle(dict(pre), out=out)
        assert "deny" not in out.getvalue()
    finally:
        head_hook.D, head_hook.append = old
