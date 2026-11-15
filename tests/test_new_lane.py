"""Tests for new_lane.py (TIK-1133). Everything under a temp NEW_LANE_HOME; tmux/systemctl/bridge are fakes."""
import _harness_env  # noqa: F401  throwaway config + state (must be first)
import importlib, json, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "harness"))


def fresh():
    home = tempfile.mkdtemp(prefix="newlane_")
    os.environ["NEW_LANE_HOME"] = home
    os.environ["NEW_LANE_TEMPLATES"] = os.path.join(home, "templates")
    os.makedirs(os.path.join(home, "templates"))
    with open(os.path.join(home, "templates", "head_settings.json"), "w") as f:
        f.write('{"hooks": {"Stop": []}}')
    with open(os.path.join(home, "templates", "head_settings.local.json"), "w") as f:
        f.write('{"permissions": {"allow": []}}')
    os.makedirs(os.path.join(home, "state"))
    with open(os.path.join(home, "state", "lanes.json"), "w") as f:
        json.dump(["techsup", "mail"], f)
    with open(os.path.join(home, "state", "tg_threads.json"), "w") as f:
        json.dump({"111111111": 9100}, f)
    with open(os.path.join(home, ".claude.json"), "w") as f:
        json.dump({"projects": {"/x": {"keep": 1}}, "other": 2}, f)
    import new_lane
    return importlib.reload(new_lane), home


def _untmuxL(cmd):
    """TIK-1310: every head has its own tmux server. Assert the -L head-<lane> pair is present, then hand the fakes the old shape."""
    if cmd[0] == "tmux":
        assert cmd[1] == "-L" and cmd[2].startswith("head-"), "tmux call without a private socket: %r" % (cmd,)
        return (cmd[0],) + tuple(cmd[3:])
    return cmd


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Head:
    """A fake head: tmux screens in order, systemctl ok, a bridge that writes a Stop event."""
    def __init__(self, nl, screens, stop=True, unit_rc=0):
        self.nl, self.screens, self.stop, self.unit_rc, self.keys, self.sent = nl, list(screens), stop, unit_rc, [], []

    def sh(self, *cmd, timeout=60):
        cmd = _untmuxL(cmd)
        if cmd[0] == "systemctl":
            return R(self.unit_rc, err="unit failed" if self.unit_rc else "")
        if cmd[1] == "capture-pane":
            s = self.screens[0] if len(self.screens) == 1 else self.screens.pop(0)
            return R(out=s)
        if cmd[1] == "send-keys":
            self.keys.append(cmd[-1]); return R()
        if cmd[1] == "has-session":
            return R(1)                                # a stopped head's session is gone
        return R()

    def call(self, lane, req):
        self.sent.append(req)
        if self.stop:
            with open(os.path.join(self.nl.BASE, lane, "events.jsonl"), "a") as f:
                f.write(json.dumps({"event": "Stop"}) + "\n")
        return {"t": "ack", "ok": True}


READY = "❯ \n  ⏵⏵ auto mode on (shift+tab to cycle)"
CLOCK = iter(range(0, 10 ** 7, 1))


def now():
    return next(CLOCK)


class RenameTest(unittest.TestCase):
    def _lane(self):
        nl, home = fresh()
        h = Head(nl, [READY])
        self.assertTrue(nl.create("New Chat", 777001, sh=h.sh, call=h.call, sleep=lambda s: None, now=now)["ok"])
        pd = nl._project_dir("new-chat")
        os.makedirs(pd)
        with open(os.path.join(pd, "s.jsonl"), "w") as f:
            f.write("{}")
        return nl, home, h, pd

    def _go(self, nl, h, new="network", title="Network DNS", tid=777001):
        return nl.rename("new-chat", new, title, tid, sh=h.sh, call=h.call, sleep=lambda s: None, now=now)

    def test_moves_everything_and_keeps_the_transcript(self):
        nl, home, h, pd = self._lane()
        r = self._go(nl, h)
        self.assertTrue(r["ok"], r)
        self.assertEqual(json.load(open(nl.LANES)), ["techsup", "mail", "network"])
        self.assertFalse(os.path.exists(os.path.join(home, "heads", "new-chat")))
        self.assertFalse(os.path.exists(os.path.join(nl.BASE, "new-chat")))
        self.assertTrue(os.path.isfile(os.path.join(nl._project_dir("network"), "s.jsonl")))
        self.assertFalse(os.path.exists(pd))
        md = open(os.path.join(home, "heads", "network", "CLAUDE.md")).read()
        self.assertIn('"Network DNS"', md); self.assertNotIn('"New Chat"', md)
        self.assertIn("lane `network`", md); self.assertIn("/network)", md)
        self.assertEqual(json.load(open(os.path.join(nl.BASE, "network", "tg_threads.json"))), {"111111111": 777001})
        made = json.load(open(os.path.join(nl.BASE, "network", nl.MADE_BY)))
        self.assertEqual((made["renamed_from"], made["title"]), ("new-chat", "Network DNS"))
        ps = json.load(open(os.path.join(home, ".claude.json")))["projects"]
        self.assertTrue(ps[os.path.join(home, "heads", "network")]["hasTrustDialogAccepted"])
        self.assertNotIn(os.path.join(home, "heads", "new-chat"), ps)

    def _assert_restored(self, nl, home, pd, r):
        self.assertFalse(r["ok"]); self.assertTrue(r["restored"], r)
        self.assertIn("new-chat", json.load(open(nl.LANES)))
        self.assertNotIn("network", json.load(open(nl.LANES)))
        self.assertTrue(os.path.isfile(os.path.join(home, "heads", "new-chat", "CLAUDE.md")))
        self.assertTrue(os.path.isdir(os.path.join(nl.BASE, "new-chat")))
        self.assertTrue(os.path.isfile(os.path.join(pd, "s.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(home, "heads", "network")))
        self.assertIn("lane `new-chat`", open(os.path.join(home, "heads", "new-chat", "CLAUDE.md")).read())
        ps = json.load(open(os.path.join(home, ".claude.json")))["projects"]
        self.assertIn(os.path.join(home, "heads", "new-chat"), ps)
        self.assertNotIn(os.path.join(home, "heads", "network"), ps)

    def test_no_stop_after_restart_puts_it_all_back(self):
        nl, home, h, pd = self._lane()
        h.stop = False
        r = self._go(nl, h)
        self.assertEqual(r["step"], "bridge")
        self._assert_restored(nl, home, pd, r)
        self.assertTrue(any(x["event"] == "rename_failed" and x["restored"] for x in nl.log_rows()))

    def test_undo_starts_the_old_unit_and_stops_the_new(self):
        nl, home, h, pd = self._lane()
        h.stop = False
        calls = []
        real = h.sh
        def sh(*c, timeout=60):
            if c[0] == "systemctl":
                calls.append(c[2:4] + c[-1:])
            return real(*c, timeout=timeout)
        nl.rename("new-chat", "network", "N", 777001, sh=sh, call=h.call, sleep=lambda s: None, now=now)
        self.assertEqual(calls[-1], ("enable", "--now", "harness-head@new-chat.service"))
        self.assertIn(("disable", "--now", "harness-head@network.service"), calls)

    def test_a_failed_undo_is_reported_not_restored(self):
        nl, home, h, pd = self._lane()
        h.stop = False
        real = h.sh
        def sh(*c, timeout=60):
            if c[0] == "systemctl" and c[-1].endswith("@network.service") and c[2] == "disable":
                return R(1, err="nope")
            return real(*c, timeout=timeout)
        r = nl.rename("new-chat", "network", "N", 777001, sh=sh, call=h.call, sleep=lambda s: None, now=now)
        self.assertFalse(r["ok"]); self.assertFalse(r["restored"])
        self.assertTrue(any(x["event"] == "rename_undo_failed" for x in nl.log_rows()))

    def test_refusals_touch_nothing(self):
        nl, home, h, pd = self._lane()
        self.assertEqual(self._go(nl, h, new="mail")["step"], "name")
        self.assertEqual(self._go(nl, h, new="BAD NAME")["step"], "name")
        self.assertEqual(self._go(nl, h, tid=5)["step"], "check")
        os.makedirs(nl._project_dir("taken1"))
        self.assertEqual(self._go(nl, h, new="taken1")["step"], "name")
        with open(os.path.join(nl.BASE, "new-chat", "events.jsonl"), "a") as f:
            f.write(json.dumps({"event": "PreToolUse"}) + "\n")
        r = self._go(nl, h)
        self.assertEqual(r["step"], "busy")
        self.assertIn("new-chat", json.load(open(nl.LANES)))
        with open(os.path.join(nl.BASE, "new-chat", "events.jsonl"), "a") as f:
            f.write(json.dumps({"event": "Stop"}) + "\n")
        q = os.path.join(nl.BASE, "new-chat", "task_spool"); os.makedirs(q)
        open(os.path.join(q, "j.json"), "w").close()
        self.assertEqual(self._go(nl, h)["step"], "busy")
        self.assertFalse(any(x["event"] == "rename_start" for x in nl.log_rows()))

    def test_hand_built_lane_refused(self):
        nl, home = fresh()
        h = Head(nl, [READY])
        r = nl.rename("techsup", "tech", "T", sh=h.sh, call=h.call, sleep=lambda s: None, now=now)
        self.assertEqual(r["step"], "check")

    def test_retitle_only_changes_the_title(self):
        nl, home, h, pd = self._lane()
        r = nl.retitle("new-chat", 'Net "DNS"')
        self.assertTrue(r["ok"], r)
        md = open(os.path.join(home, "heads", "new-chat", "CLAUDE.md")).read()
        self.assertEqual(md.count('"Net \'DNS\'"'), 3); self.assertNotIn("New Chat", md)
        self.assertIn("lane `new-chat`", md)
        self.assertEqual(json.load(open(os.path.join(nl.BASE, "new-chat", nl.MADE_BY)))["title"], 'Net "DNS"')
        self.assertFalse(nl.retitle("techsup", "x")["ok"])

    def test_retitle_when_file_already_has_the_title(self):
        # Rav 2026-11-04: CLAUDE.md hand-corrected first, marker still old; the rename failed and undid itself
        nl, home, h, pd = self._lane()
        p = os.path.join(home, "heads", "new-chat", "CLAUDE.md")
        text = open(p).read().replace("New Chat", "Net")
        open(p, "w").write(text)
        r = nl.retitle("new-chat", "Net")
        self.assertTrue(r["ok"], r)
        self.assertEqual(json.load(open(os.path.join(nl.BASE, "new-chat", nl.MADE_BY)))["title"], "Net")

    def test_retitle_refuses_a_file_with_no_title_slot(self):
        nl, home, h, pd = self._lane()
        open(os.path.join(home, "heads", "new-chat", "CLAUDE.md"), "w").write("no titles here")
        self.assertFalse(nl.retitle("new-chat", "Net")["ok"])

    def test_wide_slug(self):
        nl, home = fresh()
        self.assertEqual(nl.slug("Network DNS Firewall", width=20), "network-dns-firewall")


class SlugTest(unittest.TestCase):
    def test_slug_and_collisions(self):
        nl, home = fresh()
        self.assertEqual(nl.slug("Garden Plans!"), "garden-plans")
        self.assertEqual(nl.slug("Mail"), "mail-2")              # lane exists
        os.makedirs(os.path.join(home, "heads", "recipes"))
        self.assertEqual(nl.slug("Recipes"), "recipes-2")        # folder exists
        self.assertEqual(nl.slug("2027 plans"), "t-2027-plans")  # must start with a letter
        self.assertEqual(nl.slug("🎉🎉"), "topic")
        self.assertEqual(nl.slug("Main"), "main-2")                  # reserved
        self.assertRegex(nl.slug("a" * 80), r"^[a-z][a-z0-9_-]{1,20}$")


class CreateTest(unittest.TestCase):
    def test_happy_path(self):
        nl, home = fresh()
        h = Head(nl, [READY])
        r = nl.create("Garden", 777001, sh=h.sh, call=h.call, sleep=lambda s: None, now=now)
        self.assertTrue(r["ok"], r); self.assertEqual(r["lane"], "garden")
        d = os.path.join(home, "heads", "garden")
        self.assertEqual(open(os.path.join(d, ".claude", "settings.json")).read(), '{"hooks": {"Stop": []}}')
        self.assertEqual(open(os.path.join(d, ".claude", "settings.local.json")).read(), '{"permissions": {"allow": []}}')
        self.assertIn('"Garden"', open(os.path.join(d, "CLAUDE.md")).read())
        sk = os.path.join(d, ".claude", "skills")          # every harness skill is linked into the new head
        self.assertEqual(sorted(os.listdir(sk)), sorted(os.listdir(nl.SKILLS)))
        self.assertTrue(all(os.path.islink(os.path.join(sk, n)) for n in os.listdir(sk)))
        self.assertIn(os.path.join(home, "heads", "main", "CLAUDE.md"), open(os.path.join(d, "CLAUDE.md")).read())
        self.assertEqual(json.load(open(os.path.join(nl.BASE, "garden", "tg_threads.json"))), {"111111111": 777001})
        cj = json.load(open(os.path.join(home, ".claude.json")))
        p = cj["projects"][d]
        self.assertTrue(p["hasTrustDialogAccepted"] and p["hasClaudeMdExternalIncludesApproved"])
        self.assertEqual(cj["projects"]["/x"], {"keep": 1}); self.assertEqual(cj["other"], 2)   # nothing else touched
        self.assertEqual(json.load(open(nl.LANES)), ["techsup", "mail", "garden"])

    def test_thread_already_owned(self):
        nl, home = fresh()
        h = Head(nl, [READY])
        r = nl.create("Again", 9100, sh=h.sh, call=h.call, sleep=lambda s: None, now=now)
        self.assertFalse(r["ok"]); self.assertEqual(r["step"], "thread")
        self.assertFalse(os.path.exists(os.path.join(home, "heads", "again")))

    def test_no_stop_event_fails_and_never_registers(self):
        nl, home = fresh()
        h = Head(nl, [READY], stop=False)
        r = nl.create("Garden", 777001, sh=h.sh, call=h.call, sleep=lambda s: None, now=now)
        self.assertFalse(r["ok"]); self.assertEqual(r["step"], "bridge")
        self.assertNotIn("garden", json.load(open(nl.LANES)))   # the gateway never routes to a head that is not ready

    def test_unit_fail_never_registers(self):
        nl, home = fresh()
        h = Head(nl, [READY], unit_rc=1)
        r = nl.create("Garden", 777001, sh=h.sh, call=h.call, sleep=lambda s: None, now=now)
        self.assertFalse(r["ok"]); self.assertEqual(r["step"], "unit")
        self.assertNotIn("garden", json.load(open(nl.LANES)))

    def test_missing_template(self):
        nl, home = fresh()
        os.remove(nl.TEMPLATE)
        h = Head(nl, [READY])
        r = nl.create("Garden", 777001, sh=h.sh, call=h.call, sleep=lambda s: None, now=now)
        self.assertFalse(r["ok"]); self.assertEqual(r["step"], "settings")

    def test_missing_local_template(self):
        nl, home = fresh()
        os.remove(nl.TEMPLATE_LOCAL)
        h = Head(nl, [READY])
        r = nl.create("Garden", 777001, sh=h.sh, call=h.call, sleep=lambda s: None, now=now)
        self.assertFalse(r["ok"]); self.assertEqual(r["step"], "settings")

    def test_every_step_logged(self):
        nl, home = fresh()
        h = Head(nl, [READY])
        nl.create("Garden", 777001, sh=h.sh, call=h.call, sleep=lambda s: None, now=now)
        ev = [r["event"] for r in nl.log_rows()]
        for e in ("start", "files", "approved", "unit_started", "bridge_ok", "registered", "done"):
            self.assertIn(e, ev)


TRUST = """ Do you trust the files in this folder?
 ❯ 1. Yes, I trust this folder
   2. No, exit
 Enter to confirm · Esc to cancel"""
IMPORTS = """ Allow external CLAUDE.md file imports?
   /home/x/notes/SELF.md
 ❯ No, disable external imports
   Yes, allow external imports
 Enter to confirm · Esc to cancel"""
IMPORTS_ON_YES = IMPORTS.replace("❯ No", "  No").replace("   Yes, allow", " ❯ Yes, allow")
TEACH = """   Teach auto mode about your environment?
   ❯ Also scan shell history     true
     Continue
     Don't show again
   ←/→ to change · Enter to continue · Esc to cancel"""
NO_TARGET = """   Some new dialog?
   ❯ Maybe
   Enter to confirm · Esc to cancel"""


class DialogTest(unittest.TestCase):
    def test_trust_yes_is_already_selected(self):
        nl, _ = fresh()
        h = Head(nl, [TRUST, TRUST, READY])
        self.assertEqual(nl.answer_dialogs("g", h.sh, lambda s: None, now), ["trust"])
        self.assertEqual(h.keys, ["Enter"])

    def test_imports_moves_down_to_yes(self):
        nl, _ = fresh()
        h = Head(nl, [IMPORTS, IMPORTS_ON_YES, READY])
        self.assertEqual(nl.answer_dialogs("g", h.sh, lambda s: None, now), ["imports"])
        self.assertEqual(h.keys, ["Down", "Enter"])

    def test_cursor_did_not_move_escapes_not_guesses(self):
        nl, _ = fresh()
        h = Head(nl, [IMPORTS, IMPORTS, READY])
        nl.answer_dialogs("g", h.sh, lambda s: None, now)
        self.assertEqual(h.keys, ["Down", "Escape"])
        self.assertNotIn("Enter", h.keys)

    def test_teach_picks_dont_show_again(self):
        nl, _ = fresh()
        after = TEACH.replace("   ❯ Also", "     Also").replace("     Don't", "   ❯ Don't")
        h = Head(nl, [TEACH, after, READY])
        self.assertEqual(nl.answer_dialogs("g", h.sh, lambda s: None, now), ["teach"])
        self.assertEqual(h.keys, ["Down", "Down", "Enter"])

    def test_unknown_option_escapes(self):
        nl, _ = fresh()
        nl.DIALOGS.append(("new", __import__("re").compile("Some new dialog"), [r"Never there"]))
        try:
            h = Head(nl, [NO_TARGET, READY])
            nl.answer_dialogs("g", h.sh, lambda s: None, now)
        finally:
            nl.DIALOGS.pop()
        self.assertEqual(h.keys, ["Escape"])

    def test_never_ready_fails_loudly(self):
        nl, _ = fresh()
        h = Head(nl, ["loading..."])
        with self.assertRaises(nl.Fail) as c:
            nl.answer_dialogs("g", h.sh, lambda s: None, now, wait=10)
        self.assertEqual(c.exception.step, "dialogs")

    def test_late_dialog_eats_first_test_then_resent(self):
        nl, _ = fresh()
        os.makedirs(os.path.join(nl.BASE, "g"))
        h = Head(nl, [TEACH, TEACH.replace("   ❯ Also", "     Also").replace("     Don't", "   ❯ Don't"), READY])
        n = {"c": 0}
        real = h.call
        def call(lane, req):                               # the first send is eaten (no Stop); the second answers
            n["c"] += 1
            return real(lane, req) if n["c"] > 1 else {"t": "ack", "ok": True}
        self.assertTrue(nl.bridge_test("g", call, lambda s: None, now, wait=20, sh=h.sh))
        self.assertEqual(n["c"], 2); self.assertIn("Enter", h.keys)


class Unit:
    """systemctl + tmux fakes for remove: records calls; the session dies on kill-session unless told not to."""
    def __init__(self, unit_rc=0, dies=True):
        self.unit_rc, self.dies, self.calls, self.alive = unit_rc, dies, [], True

    def sh(self, *cmd, timeout=60):
        self.calls.append(cmd)
        cmd = _untmuxL(cmd)
        if cmd[0] == "systemctl":
            return R(self.unit_rc, err="no such unit" if self.unit_rc else "")
        if cmd[1] == "kill-session":
            self.alive = not self.dies; return R()
        if cmd[1] == "has-session":
            return R(0 if self.alive else 1)
        return R()


def built(nl, home, title="Garden", tid=777001):
    """A lane as create() leaves it, plus a Claude transcript and a memory file."""
    r = nl.create(title, tid, sh=Head(nl, [READY]).sh, call=Head(nl, [READY]).call, sleep=lambda s: None, now=now)
    assert r["ok"], r
    pj = nl.project_dir(r['lane'])
    os.makedirs(os.path.join(pj, "memory"))
    with open(os.path.join(pj, "s1.jsonl"), "w") as f:
        for row in ({"type": "user", "timestamp": "2026-10-30T05:00:00", "message": {"content": "what is this for?"}},
                    {"type": "assistant", "timestamp": "2026-10-30T05:00:05",
                     "message": {"content": [{"type": "text", "text": "Garden planning."}, {"type": "tool_use"}]}},
                    {"type": "summary"}):
            f.write(json.dumps(row) + "\n")
    with open(os.path.join(pj, "memory", "lesson.md"), "w") as f:
        f.write("tomatoes need sun")
    return r["lane"], pj


class RemoveTest(unittest.TestCase):
    def test_remove_archives_everything_and_unregisters(self):
        nl, home = fresh()
        lane, pj = built(nl, home)
        u = Unit()
        r = nl.remove(lane, 777001, sh=u.sh, now=lambda: 0)
        self.assertTrue(r["ok"], r)
        self.assertEqual(json.load(open(nl.LANES)), ["techsup", "mail"])
        self.assertIn(("systemctl", "--user", "disable", "--now", "harness-head@garden.service"), u.calls)
        self.assertIn(("tmux", "-L", "head-garden", "kill-session", "-t", "harness-head-garden"), u.calls)
        for gone in (os.path.join(home, "heads", "garden"), os.path.join(nl.BASE, "garden"), pj):
            self.assertFalse(os.path.exists(gone), gone)                   # moved, not left behind
        d = r["archive"]
        self.assertTrue(d.startswith(os.path.join(nl.BASE, "archive", "head_lanes", "garden-")))
        self.assertTrue(os.path.isfile(os.path.join(d, "folder", "CLAUDE.md")))
        self.assertTrue(os.path.isfile(os.path.join(d, "state", "tg_threads.json")))
        self.assertEqual(open(os.path.join(d, "claude_projects", "memory", "lesson.md")).read(), "tomatoes need sun")
        self.assertTrue(os.path.isfile(os.path.join(d, "claude_projects", "s1.jsonl")))      # raw kept too
        md = open(os.path.join(d, "transcript_garden_s1_1.md")).read()                     # the RAG copy
        self.assertIn("what is this for?", md); self.assertIn("Garden planning.", md)
        m = json.load(open(os.path.join(d, "manifest.json")))
        self.assertEqual(m["lane"], "garden"); self.assertEqual(m["transcript_md"], 1)
        self.assertEqual(len(m["moved"]), 3)
        self.assertIn("garden", nl.slug("Garden"))                          # name free again
        ev = [r["event"] for r in nl.log_rows()]
        for e in ("remove_start", "unregistered", "unit_stopped", "archived", "removed"):
            self.assertIn(e, ev)

    def test_refuses_hand_built_and_main_lanes(self):
        nl, home = fresh()
        os.makedirs(os.path.join(nl.BASE, "techsup"))
        for lane in ("main", "techsup", "mail", "../x", "head"):
            u = Unit()
            r = nl.remove(lane, sh=u.sh)
            self.assertFalse(r["ok"], lane); self.assertEqual(r["step"], "check")
            self.assertEqual(u.calls, [])                                     # nothing touched
        self.assertEqual(json.load(open(nl.LANES)), ["techsup", "mail"])

    def test_reserved_name_refused_even_with_a_marker(self):
        nl, home = fresh()
        for lane in ("main", "head"):
            os.makedirs(os.path.join(nl.BASE, lane), exist_ok=True)
            with open(os.path.join(nl.BASE, lane, nl.MADE_BY), "w") as f:
                f.write("{}")
            self.assertIn("main thread", nl.removable(lane))

    def test_refuses_another_lanes_thread(self):
        nl, home = fresh()
        lane, _ = built(nl, home)
        r = nl.remove(lane, 999, sh=Unit().sh)
        self.assertFalse(r["ok"]); self.assertEqual(r["step"], "check")
        self.assertIn(lane, json.load(open(nl.LANES)))

    def test_lane_built_before_the_marker_counts_by_its_log(self):
        nl, home = fresh()
        lane, _ = built(nl, home)
        os.remove(os.path.join(nl.BASE, lane, nl.MADE_BY))
        self.assertTrue(nl.made_here(lane))
        self.assertFalse(nl.made_here("techsup"))

    def test_unit_stop_failure_stops_before_archive(self):
        nl, home = fresh()
        lane, pj = built(nl, home)
        r = nl.remove(lane, 777001, sh=Unit(unit_rc=1).sh)
        self.assertFalse(r["ok"]); self.assertEqual(r["step"], "unit")
        self.assertTrue(os.path.isdir(os.path.join(home, "heads", "garden")))       # nothing archived
        self.assertTrue(os.path.isdir(pj))

    def test_tmux_survivor_fails_loudly(self):
        nl, home = fresh()
        lane, _ = built(nl, home)
        r = nl.remove(lane, 777001, sh=Unit(dies=False).sh)
        self.assertFalse(r["ok"]); self.assertEqual(r["step"], "tmux")
        self.assertTrue(os.path.isdir(os.path.join(home, "heads", "garden")))

    def test_big_transcript_split_under_ingest_limit(self):
        nl, home = fresh()
        lane, pj = built(nl, home)
        with open(os.path.join(pj, "big.jsonl"), "w") as f:
            for i in range(5):
                f.write(json.dumps({"type": "user", "message": {"content": "x" * 600_000}}) + "\n")
        r = nl.remove(lane, 777001, sh=Unit().sh, now=lambda: 0)
        parts = [x for x in os.listdir(r["archive"]) if x.startswith("transcript_garden_big_")]
        self.assertGreater(len(parts), 1)
        for x in parts:
            self.assertLess(os.path.getsize(os.path.join(r["archive"], x)), 2_000_000)


if __name__ == "__main__":
    unittest.main()
