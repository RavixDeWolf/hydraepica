"""TIK-1005: headsafe snapshot / delayed restart / liveness / finalize / revert. Everything runs in temp dirs with a fake clock,
fake systemctl and a fake head; the real head, its units and Rav's chat are never touched.   python3 tests/test_headsafe.py"""
import json, os, pathlib, shutil, socket, subprocess, sys, tempfile, threading, time, unittest

HERE = pathlib.Path(os.environ.get("TOOL_MODULE_DIR", pathlib.Path(__file__).resolve().parent))
TMP = pathlib.Path(tempfile.mkdtemp(prefix="headsafe_test_"))
os.environ["HEADSAFE_HOME"] = str(TMP / "store")
sys.path.insert(0, str(HERE))
import headsafe as hs  # noqa: E402


class Clock:
    def __init__(self): self.t = 1000.0; self.hooks = []
    def now(self): return self.t
    def sleep(self, s):
        self.t += s
        for h in list(self.hooks): h(self)


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP / "store", ignore_errors=True)
        self.w = TMP / "work"; shutil.rmtree(self.w, ignore_errors=True); self.w.mkdir()
        (self.w / "a.py").write_text("print('v1')\n"); (self.w / "b.txt").write_text("keep\n")
        os.chmod(self.w / "a.py", 0o750)
        cfg = {"git_repos": [], "files": [str(self.w / "a.py"), str(self.w / "b.txt"), str(self.w / "missing.txt")], "globs": [], "units": []}
        self.cfgp = TMP / "cfg.json"; self.cfgp.write_text(json.dumps(cfg)); os.environ["HEADSAFE_CONFIG"] = str(self.cfgp)
        self.clock = Clock(); self.ran = []; self.told = []
        self.rc = {}

    def run_cmd(self, cmd, timeout=60):
        self.ran.append(cmd); return self.rc.get(cmd, 0), "out"

    def watch(self, probe, notify=None):
        return hs.watch(run=self.run_cmd, sleep=self.clock.sleep, now=self.clock.now, probe=probe,
                        notify_fn=notify or (lambda t, run=None: self.told.append(t) or True))

    def armed(self, restart=("systemctl --user restart tg-gateway",), **kw):
        sid = hs.snapshot("t", now=self.clock.now)
        hs.arm(sid, restart, spawn=lambda s: None, now=self.clock.now, **kw)
        return sid

    def edit(self): (self.w / "a.py").write_text("print('v2 BROKEN')\n")


class Snap(Base):
    def test_roundtrip_restores_content_mode_and_leaves_unchanged_files_alone(self):
        sid = hs.snapshot("first", now=self.clock.now)
        man = hs.manifest(sid)
        self.assertEqual(sorted(os.path.basename(f["path"]) for f in man["files"]), ["a.py", "b.txt"])   # the missing file is skipped
        self.edit(); os.chmod(self.w / "a.py", 0o600)
        restored, same = hs.restore(sid)
        self.assertEqual((self.w / "a.py").read_text(), "print('v1')\n"); self.assertEqual(oct((self.w / "a.py").stat().st_mode & 0o777), "0o750")
        self.assertEqual([os.path.basename(p) for p in restored], ["a.py"]); self.assertEqual(same, 1)

    def test_a_damaged_snapshot_is_refused_not_applied(self):
        sid = hs.snapshot("x", now=self.clock.now)
        p = pathlib.Path(hs.SNAPS) / (sid + ".json"); m = json.loads(p.read_text()); m["files"][0]["sha256"] = "0" * 64; p.write_text(json.dumps(m))
        self.edit()
        with self.assertRaises(RuntimeError): hs.restore(sid)
        self.assertIn("BROKEN", (self.w / "a.py").read_text())

    def test_a_tar_member_that_is_not_in_the_manifest_is_refused(self):
        import tarfile
        sid = hs.snapshot("x", now=self.clock.now)
        tp = pathlib.Path(hs.SNAPS) / (sid + ".tar.gz"); evil = TMP / "evil.txt"; evil.write_text("x")
        with tarfile.open(str(tp) + ".new", "w:gz") as out:
            with tarfile.open(tp, "r:gz") as src:
                for m in src.getmembers():
                    out.addfile(m, src.extractfile(m))
            out.add(str(evil), arcname=str(self.w / "extra.txt").lstrip("/"), recursive=False)
        os.replace(str(tp) + ".new", tp)
        with self.assertRaises(RuntimeError): hs.restore(sid)
        self.assertFalse((self.w / "extra.txt").exists())

    def test_big_files_are_skipped_and_an_empty_watch_list_is_an_error(self):
        (self.w / "big.bin").write_bytes(b"x" * (hs.MAX_FILE + 1))
        cfg = json.loads(self.cfgp.read_text()); cfg["files"].append(str(self.w / "big.bin")); self.cfgp.write_text(json.dumps(cfg))
        self.assertNotIn("big.bin", [os.path.basename(p) for p in hs.watched_files(hs.load_config())])
        self.cfgp.write_text(json.dumps({"git_repos": [], "files": [str(self.w / "nope")], "globs": []}))
        with self.assertRaises(RuntimeError): hs.snapshot("none")

    def test_only_the_newest_20_snapshots_are_kept(self):
        for i in range(23):
            self.clock.t += 1; hs.snapshot("n%d" % i, now=self.clock.now)
        self.assertEqual(len([f for f in os.listdir(hs.SNAPS) if f.endswith(".json")]), hs.KEEP_SNAPS)

    def test_git_repos_snapshot_tracked_and_untracked_but_not_ignored_files(self):
        r = self.w / "repo"; r.mkdir()
        subprocess.run(["git", "init", "-q", str(r)], check=True)
        (r / ".gitignore").write_text(".venv/\n"); (r / "t.py").write_text("t"); (r / "u.py").write_text("u")
        (r / ".venv").mkdir(); (r / ".venv" / "huge.py").write_text("no")
        self.cfgp.write_text(json.dumps({"git_repos": [str(r)], "files": [], "globs": []}))
        names = sorted(os.path.relpath(p, os.path.realpath(r)) for p in hs.watched_files(hs.load_config()))
        self.assertEqual(names, [".gitignore", "t.py", "u.py"])


class Flow(Base):
    def test_a_head_that_answers_and_finalizes_keeps_its_change(self):
        sid = self.armed(); self.edit()
        def finalize_when_waiting(c):
            if (hs._read(hs.ARMED) or {}).get("phase") == "awaiting-finalize" and not os.path.exists(hs.FINAL):
                hs.finalize()
        self.clock.hooks.append(finalize_when_waiting)
        self.assertEqual(self.watch(lambda tok, t: (True, "answered")), 0)
        self.assertIn("BROKEN", (self.w / "a.py").read_text(), "a finalized change stays")
        self.assertEqual(self.ran, ["systemctl --user restart tg-gateway"])
        self.assertEqual(hs._read(hs.ARMED)["outcome"], "finalized"); self.assertEqual(self.told, [])

    def test_no_liveness_reverts_restores_restarts_again_and_tells_rav(self):
        self.armed(); self.edit()
        self.assertEqual(self.watch(lambda tok, t: (False, "no answer to the probe within 20s")), 0)
        self.assertEqual((self.w / "a.py").read_text(), "print('v1')\n")
        self.assertEqual(self.ran, ["systemctl --user restart tg-gateway"] * 2, "restarted for the change AND again after the revert")
        self.assertTrue(os.path.exists(hs.REVERTED)); self.assertEqual(hs._read(hs.ARMED)["outcome"], "reverted")
        self.assertEqual(len(self.told), 1); self.assertIn("REVERTED", self.told[0]); self.assertIn("no liveness", self.told[0])

    def test_the_probe_window_and_the_finalize_window_are_the_ones_asked_for(self):
        seen = []
        self.armed(liveness=20, finalize=60); self.edit()
        t0 = self.clock.t
        self.watch(lambda tok, t: seen.append(t) or (True, "ok"))
        self.assertEqual(seen, [20]); waited = self.clock.t - t0
        self.assertTrue(60 <= waited - 8 <= 62, waited)      # delay 8, then the full 60 s finalize window before it reverted
        self.assertEqual((self.w / "a.py").read_text(), "print('v1')\n"); self.assertIn("not finalized within 60s", self.told[0])

    def test_a_failing_restart_command_reverts(self):
        self.armed(("false-cmd",)); self.rc["false-cmd"] = 1; self.edit()
        self.watch(lambda tok, t: (True, "x"))
        self.assertEqual((self.w / "a.py").read_text(), "print('v1')\n"); self.assertIn("restart command failed", self.told[0])

    def test_the_head_can_revert_itself_while_it_checks(self):
        self.armed(); self.edit()
        def ask(c):
            if (hs._read(hs.ARMED) or {}).get("phase") == "awaiting-finalize" and not os.path.exists(hs.REQ):
                self.assertEqual(hs.request_revert("I broke the thread names"), "watchdog told to revert")
        self.clock.hooks.append(ask)
        self.watch(lambda tok, t: (True, "ok"))
        self.assertEqual((self.w / "a.py").read_text(), "print('v1')\n"); self.assertIn("I broke the thread names", self.told[0])

    def test_a_watchdog_crash_reverts_instead_of_leaving_a_half_state(self):
        self.armed(); self.edit()
        def boom(tok, t): raise OSError("probe exploded")
        self.assertEqual(self.watch(boom), 1)
        self.assertEqual((self.w / "a.py").read_text(), "print('v1')\n"); self.assertIn("watchdog hit an error", self.told[0])

    def test_a_failed_revert_is_loud_not_silent(self):
        sid = self.armed(); self.edit()
        p = pathlib.Path(hs.SNAPS) / (sid + ".tar.gz"); p.write_bytes(b"not a tarball")
        self.assertEqual(self.watch(lambda tok, t: (False, "dead")), 1)
        self.assertTrue(os.path.exists(hs.FAILED)); self.assertIn("FAILED", self.told[0]); self.assertEqual(hs._read(hs.ARMED)["outcome"], "revert-failed")
        self.assertIn("BROKEN", (self.w / "a.py").read_text())

    def test_finalize_before_the_probe_passed_is_refused(self):
        self.armed()
        with self.assertRaises(RuntimeError): hs.finalize()

    def test_arming_twice_or_from_a_missing_snapshot_is_refused(self):
        sid = self.armed()
        with self.assertRaises(RuntimeError): hs.arm(sid, spawn=lambda s: None)
        shutil.rmtree(TMP / "store" / "snaps")
        with self.assertRaises(Exception): hs.arm("nope", spawn=lambda s: None)

    def test_the_watchdog_is_spawned_as_its_own_unit(self):
        got = []
        sid = hs.snapshot("s", now=self.clock.now); hs.arm(sid, spawn=got.append, now=self.clock.now)
        self.assertEqual(got, [sid])

    def test_the_spawned_watchdog_gets_the_headsafe_environment(self):
        seen = []
        real = hs._sh; hs._sh = lambda cmd, timeout=60: seen.append(cmd) or (0, "Running as unit")
        try:
            hs._spawn_watch("20261031-000000-x")
        finally:
            hs._sh = real
        cmd = seen[0]
        self.assertEqual(cmd[:3], ["systemd-run", "--user", "--collect"]); self.assertEqual(cmd[-1], "watch")
        self.assertIn("--setenv=HEADSAFE_HOME=" + os.environ["HEADSAFE_HOME"], cmd)
        self.assertIn("--setenv=HEADSAFE_CONFIG=" + os.environ["HEADSAFE_CONFIG"], cmd)

    def test_every_phase_is_logged(self):
        self.armed(); self.edit(); self.watch(lambda tok, t: (False, "dead"))
        ev = [json.loads(l)["event"] for l in open(hs.LOG)]
        for need in ("snapshot", "armed", "phase", "restart", "probe", "reverting", "reverted", "done"):
            self.assertIn(need, ev)


class Probe(Base):
    def server(self, answer_ok=True):
        sock = str(TMP / "h.sock")
        if os.path.exists(sock): os.unlink(sock)
        srv = socket.socket(socket.AF_UNIX); srv.bind(sock); srv.listen(1)
        got = {}
        def serve():
            c, _ = srv.accept(); got["req"] = json.loads(c.recv(65536).split(b"\n")[0])
            c.sendall((json.dumps({"t": "ack", "ok": answer_ok}) + "\n").encode()); c.close()
        threading.Thread(target=serve, daemon=True).start()
        return sock, srv, got

    def test_a_reply_carrying_the_token_is_liveness(self):
        sock, srv, got = self.server(); ev = TMP / "ev.jsonl"; ev.write_text("")
        tok = "abc123"
        def head_replies(c):
            with open(ev, "a") as f:
                f.write(json.dumps({"event": "Stop", "data": {"last_assistant_message": "ALIVE " + hs.PROBE_PREFIX + tok}}) + "\n")
        self.clock.hooks.append(head_replies)
        ok, why = hs.bridge_probe(tok, 20, sock=sock, events=str(ev), sleep=self.clock.sleep, now=self.clock.now)
        srv.close(); self.assertTrue(ok, why); self.assertIn(hs.PROBE_PREFIX + tok, got["req"]["text"])

    def test_silence_a_wrong_token_and_a_missing_bridge_are_not_liveness(self):
        sock, srv, _ = self.server(); ev = TMP / "ev2.jsonl"; ev.write_text("")
        def other_answers(c):                       # an answer to SOMEONE ELSE's probe, arriving while ours waits
            with open(ev, "a") as f:
                f.write(json.dumps({"event": "Stop", "data": {"last_assistant_message": "ALIVE " + hs.PROBE_PREFIX + "other"}}) + "\n")
        self.clock.hooks.append(other_answers)
        ok, why = hs.bridge_probe("mine", 5, sock=sock, events=str(ev), sleep=self.clock.sleep, now=self.clock.now); srv.close()
        self.clock.hooks.clear()
        self.assertFalse(ok); self.assertIn("no answer", why)
        ok, why = hs.bridge_probe("mine", 5, sock=str(TMP / "none.sock"), events=str(ev), sleep=self.clock.sleep, now=self.clock.now)
        self.assertFalse(ok); self.assertIn("did not take the probe", why)

    def test_a_bridge_still_coming_up_after_the_restart_is_waited_for(self):
        sock = str(TMP / "late.sock"); ev = TMP / "ev4.jsonl"; ev.write_text("")
        if os.path.exists(sock): os.unlink(sock)
        state = {"n": 0, "srv": None}
        def come_up_then_answer(c):
            state["n"] += 1
            if state["n"] == 3 and state["srv"] is None:                     # the bridge starts 3 s into the window
                srv = socket.socket(socket.AF_UNIX); srv.bind(sock); srv.listen(1); state["srv"] = srv
                def serve():
                    cc, _ = srv.accept(); cc.recv(65536); cc.sendall((json.dumps({"t": "ack", "ok": True}) + "\n").encode()); cc.close()
                    with open(ev, "a") as f:
                        f.write(json.dumps({"event": "Stop", "data": {"last_assistant_message": "ALIVE " + hs.PROBE_PREFIX + "late1"}}) + "\n")
                threading.Thread(target=serve, daemon=True).start()
        self.clock.hooks.append(come_up_then_answer)
        ok, why = hs.bridge_probe("late1", 20, sock=sock, events=str(ev), sleep=self.clock.sleep, now=self.clock.now)
        if state["srv"]: state["srv"].close()
        self.assertTrue(ok, why)

    def test_a_bridge_that_refuses_is_not_liveness(self):
        sock, srv, _ = self.server(answer_ok=False)
        ok, why = hs.bridge_probe("t", 5, sock=sock, events=str(TMP / "ev3"), sleep=self.clock.sleep, now=self.clock.now); srv.close()
        self.assertFalse(ok); self.assertIn("refused", why)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
