#!/usr/bin/env python3
"""Tests for ship_check.py. Sealed: every fixture lives in a fresh temp dir; nothing touches live files."""
import json, os, signal, subprocess, sys, tempfile, time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ship_check as S

TMP = Path(tempfile.mkdtemp(prefix="shipcheck_t_"))
import atexit, shutil
def _cleanup_tmp():   # TIK-0702 family: shipcheck_t_ dirs piled up in /tmp. Keep the fixtures only when a test FAILED (to debug), and say so.
    if FAILED: print(f"[test_ship_check] {len(FAILED)} failure(s): fixtures kept at {TMP}", file=sys.stderr)
    else: shutil.rmtree(TMP, ignore_errors=True)
atexit.register(_cleanup_tmp)
CLEAN = {k: v for k, v in os.environ.items() if k not in ("PYTHONDONTWRITEBYTECODE", "SHIP_CHECK_MUTATING")}   # the latter: when ship_check checks ITSELF the outer run sets it, which hid the "not told" mutant
PY = sys.executable
FAILED = []


NCHK = 0


def check(name, cond):
    global NCHK
    NCHK += 1
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        FAILED.append(name)


def w(rel, text):
    p = TMP / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return str(p)


# fixture: a tiny "gate" and two suites, one that can see a bug and one that is blind to it
THING = w("thing.py", "def should_page(n):\n    return n >= 3\n")
GOOD = w("suite_good.py",
         "import sys\nsys.path.insert(0, %r)\nfrom thing import should_page\n"
         "ok = (not should_page(2)) and should_page(3)\n"
         "print('ok' if ok else 'FAIL boundary')\nsys.exit(0 if ok else 1)\n" % str(TMP))
BLIND = w("suite_blind.py",
          "import sys\nsys.path.insert(0, %r)\nfrom thing import should_page\n"
          "ok = should_page(5)\nprint('ok' if ok else 'FAIL')\nsys.exit(0 if ok else 1)\n" % str(TMP))
MUT = [("threshold loosened", "n >= 3", "n >= 1")]
ORIG = Path(THING).read_bytes()

TOTAL = 56
print("ship_check tests")

# --- 1 mutants
r = S.run_mutants(THING, MUT, [PY, GOOD])
check("a suite that can see the bug CATCHES the mutant", r["ok"] and r["results"][0]["status"] == "CAUGHT")
r = S.run_mutants(THING, MUT, [PY, BLIND])
check("a blind suite is reported SURVIVED", (not r["ok"]) and r["results"][0]["status"] == "SURVIVED")
r = S.run_mutants(THING, [("no such anchor", "n >= 99", "n >= 1")], [PY, GOOD])
check("a mutant whose anchor is missing is NOT_APPLIED, not a pass", r["results"][0]["status"] == "NOT_APPLIED" and not r["ok"])
r = S.run_mutants(THING, [("ambiguous", "n", "m")], [PY, GOOD])
check("a mutant whose anchor matches many times is NOT_APPLIED", r["results"][0]["status"] == "NOT_APPLIED")
RED = w("suite_red.py", "import sys\nprint('FAIL always')\nsys.exit(1)\n")
r = S.run_mutants(THING, MUT, [PY, RED])
check("a RED baseline aborts instead of calling every mutant caught", r["baseline"] is False and not r["ok"])
HANG = w("suite_hang.py", "import time\ntime.sleep(60)\n")
r = S.run_mutants(THING, MUT, [PY, HANG], timeout=2)
check("a hanging baseline times out cleanly", r["baseline"] is False and "timed out" in r["detail"])
check("target is byte-identical after every run above", Path(THING).read_bytes() == ORIG)
check("no sidecar left behind", not os.path.exists(THING + S.SIDE))

# --- 1b the two bugs the first run of THIS suite exposed
# (a) same-length mutant inside the baseline's second must still EXECUTE, not run cached bytecode
CACHE_THING = w("cache_thing.py", "def f(n):\n    return n >= 3\n")
CACHE_SUITE = w("cache_suite.py", "import sys\nsys.path.insert(0, %r)\nfrom cache_thing import f\n"
                "ok = (not f(2)) and f(3)\nprint('ok' if ok else 'FAIL boundary')\nsys.exit(0 if ok else 1)\n" % str(TMP))
PRIME = subprocess.run([PY, CACHE_SUITE], capture_output=True, env=CLEAN)   # compiles + caches the ORIGINAL right now
caught = 0
for _ in range(3):
    rr = S.run_mutants(CACHE_THING, [("same length", "n >= 3", "n >= 1")], [PY, CACHE_SUITE], env=CLEAN)
    caught += rr["results"][0]["status"] == "CAUGHT"
check("a same-length mutant is CAUGHT every time, never hidden by cached bytecode", caught == 3)
# (b) the original mtime must come back, or process_predates() would flag a file we only tested
os.utime(CACHE_THING, (1700000000, 1700000000))
S.run_mutants(CACHE_THING, [("same length", "n >= 3", "n >= 1")], [PY, CACHE_SUITE], env=CLEAN)
check("the target's mtime is restored exactly (a tested file is not 'modified')",
      int(os.stat(CACHE_THING).st_mtime) == 1700000000)

# (c) a mutant run must not leave bytecode behind (a stale mutant .pyc is a trap for the next run)
NC = TMP / "nocache"
NC.mkdir()
(NC / "nc_thing.py").write_text("def g(n):\n    return n >= 3\n")
NCS = w("nocache/nc_suite.py", "import sys\nsys.path.insert(0, %r)\nfrom nc_thing import g\n"
        "ok = (not g(2)) and g(3)\nprint('ok' if ok else 'FAIL boundary')\nsys.exit(0 if ok else 1)\n" % str(NC))
rr = S.run_mutants(str(NC / "nc_thing.py"), [("loosen", "n >= 3", "n >= 1")], [PY, NCS], env=CLEAN)
left = list((NC / "__pycache__").glob("*.pyc")) if (NC / "__pycache__").exists() else []
check("a mutant run leaves no bytecode behind", rr["results"][0]["status"] == "CAUGHT" and not left)

# (d) the seal guard: a mutant that deletes the test seal is REFUSED and never run
CNT = TMP / "count.txt"
CNT.write_text("")
CSUITE = w("count_suite.py", "import sys\nopen(%r, 'a').write('x\\n')\nsys.exit(0)\n" % str(CNT))
SEALT = w("seal_thing.py", "SEND = _sink if _TESTMODE else _send_real\nOTHER = 1\n")
rr = S.run_mutants(SEALT, [("delete the seal", "SEND = _sink if _TESTMODE else _send_real", "SEND = _send_real")],
                   [PY, CSUITE], env=CLEAN)
check("a mutant that removes the TEST SEAL is REFUSED", rr["results"][0]["status"] == "REFUSED" and not rr["ok"])
check("...and the suite was never run with it (only the baseline ran)", CNT.read_text().count("x") == 1)
check("...and the file is untouched", "_sink if _TESTMODE" in Path(SEALT).read_text())
# (e) every suite run under ship_check carries SHIP_CHECK_MUTATING=1
ENVS = w("env_suite.py", "import os, sys\nsys.exit(0 if os.environ.get('SHIP_CHECK_MUTATING') == '1' else 1)\n")
rr = S.run_mutants(THING, [], [PY, ENVS], env=CLEAN)
check("suites run under ship_check see SHIP_CHECK_MUTATING=1 (a real sender can refuse on it)", rr["baseline"] is True)

# --- 2 crash safety
try:
    S.run_mutants(THING, MUT, ["/no/such/binary"])
    raised = False
except FileNotFoundError:
    raised = True
check("an exception while running propagates", raised)
check("...and the target is STILL restored", Path(THING).read_bytes() == ORIG)

Path(THING + S.SIDE).write_bytes(ORIG)
Path(THING).write_bytes(ORIG.replace(b"n >= 3", b"n >= 1"))
check("simulated kill -9: file is left mutated", b"n >= 1" in Path(THING).read_bytes())
check("recover() restores from the sidecar", S.recover(THING) and Path(THING).read_bytes() == ORIG)
check("recover() with no sidecar is a no-op", S.recover(THING) is False)

Path(THING + S.SIDE).write_bytes(ORIG)
Path(THING).write_bytes(ORIG.replace(b"n >= 3", b"n >= 1"))
S.run_mutants(THING, MUT, [PY, GOOD])
check("the NEXT run self-heals a previously killed run", Path(THING).read_bytes() == ORIG)

# SIGTERM mid-run must restore before dying
SLOW = w("suite_slow.py", "import time, sys\ntime.sleep(3)\nsys.exit(0)\n")
child = (f"import sys; sys.path.insert(0, {HERE!r})\nimport ship_check as S\n"
         "S.run_mutants(%r, [('x','n >= 3','n >= 1')], [%r, %r], timeout=30)\n" % (THING, PY, SLOW))
cp = subprocess.Popen([PY, "-c", child])
time.sleep(1.2)  # inside the baseline run
cp.send_signal(signal.SIGTERM)
cp.wait(timeout=20)
time.sleep(0.3)
check("SIGTERM during a run restores the target", Path(THING).read_bytes() == ORIG)
check("SIGTERM leaves no sidecar", not os.path.exists(THING + S.SIDE))

# --- 3 live state
LIVE = w("live/state.json", '{"a": 1}')
LOG = w("live/audit.log", "line1\nline2\n")
res = S.live_unchanged([LIVE], [LOG], lambda: None)
check("untouched live files pass", res["ok"])
res = S.live_unchanged([LIVE], [LOG], lambda: Path(LIVE).write_text('{"a": 2}'))
check("a rewritten strict file is flagged CHANGED", (not res["ok"]) and res["problems"][0][1] == "CHANGED")
Path(LIVE).write_text('{"a": 1}')
res = S.live_unchanged([LIVE], [LOG], lambda: open(LOG, "a").write("line3\n"))
check("an append-only file may GROW", res["ok"])
res = S.live_unchanged([LIVE], [LOG], lambda: Path(LOG).write_text("tampered\n"))
check("an append-only file that is REWRITTEN is flagged", (not res["ok"]) and "REWRITTEN" in res["problems"][0][1])
Path(LOG).write_text("line1\nline2\nline3\n")
res = S.live_unchanged([LIVE], [LOG], lambda: Path(LOG).write_text("XXXXX1\nXXXXX2\nXXXXX3\nXXXXX4\nXXXXX5\n"))
check("a LONGER rewrite is still flagged (growth is not proof of append-only)",
      (not res["ok"]) and "REWRITTEN" in res["problems"][0][1])
GHOST = str(TMP / "live" / "not_yet.json")
res = S.live_unchanged([GHOST], [], lambda: Path(GHOST).write_text("x"))
check("a strict file CREATED by the suite is flagged", (not res["ok"]) and res["problems"][0][1] == "CREATED")

# --- 4 production path: the exact AIR-pager bug class
MODDIR = TMP / "gatedir"
MODDIR.mkdir()
(MODDIR / "mymod.py").write_text("X = 41\n")
WORK = TMP / "work"
WORK.mkdir()
rc, out, err = S.run_as_hub("import mymod\nprint(mymod.X)", str(WORK), drop_path_substrings=["gatedir"],
                           env=dict(os.environ, PYTHONPATH=str(MODDIR)))
check("a bare import FAILS under the production path (the bug class)", rc != 0 and "ModuleNotFoundError" in err)
rc, out, err = S.run_as_hub("import mymod\nprint(mymod.X)", str(WORK), env=dict(os.environ, PYTHONPATH=str(MODDIR)))
check("the same import PASSES when the test's path leaks in (why the first test was blind)", rc == 0 and "41" in out)

# --- 5 stale process
sleeper = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
time.sleep(1.5)
OLD = w("old_code.py", "x = 1\n")
os.utime(OLD, (time.time() - 3600, time.time() - 3600))
res = S.process_predates([OLD], pid=sleeper.pid)
check("a file OLDER than the process is current", res["ok"])
NEW = w("new_code.py", "x = 2\n")
os.utime(NEW, (time.time() + 5, time.time() + 5))
res = S.process_predates([NEW], pid=sleeper.pid)
check("a file NEWER than the process is flagged stale", (not res["ok"]) and res["stale"])
res = S.process_predates([str(TMP / "gone.py")], pid=sleeper.pid)
check("a missing file is flagged, not skipped", not res["ok"])
sleeper.kill()
sleeper.wait()
res = S.process_predates([OLD], pid=sleeper.pid)
check("a dead process is an error, not a pass", not res["ok"] and res.get("error"))

# --- 6 live proof
now = time.time()
fresh = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
stale_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 7200))
CODE = w("proof_code.py", "x = 1\n")
os.utime(CODE, (now - 60, now - 60))
LP1 = w("proof_fresh.log", "%s PAGED ok\n" % fresh)
LP2 = w("proof_stale.log", "%s PAGED ok\n" % stale_ts)
LP3 = w("proof_undated.log", "PAGED ok\n")
check("a proof newer than the code passes", S.live_proof(LP1, "PAGED ok", [CODE])["ok"])
r = S.live_proof(LP2, "PAGED ok", [CODE])
check("a proof OLDER than the last code change fails", (not r["ok"]) and "OLDER" in r["detail"])
r = S.live_proof(LP3, "PAGED ok", [CODE])
check("an undated proof cannot be tied to a build, so it fails", (not r["ok"]) and "timestamp" in r["detail"])
check("a missing pattern fails", not S.live_proof(LP1, "NEVER APPEARS", [CODE])["ok"])
check("a missing proof file fails", not S.live_proof(str(TMP / "nope.log"), "x")["ok"])

# --- 6b DST: everything in UTC/epoch (TIK-1309). TZ=America/Chicago, instants fixed inside the repeated hour
# (2026-11-01 01:00-02:00, once CDT then once CST) and the skipped hour (2026-03-08 02:00-03:00).
import calendar as _cal
_TZ0 = os.environ.get("TZ")
def _set_tz(tz):
    os.environ["TZ"] = tz
    time.tzset()
_set_tz("America/Chicago")
def E(y, mo, d, H, M, S=0):   # a UTC instant
    return float(_cal.timegm((y, mo, d, H, M, S)))
HZ = os.sysconf("SC_CLK_TCK")
def fake_proc(pid, start_epoch, comm="(my (odd) name)"):
    root = TMP / ("fakeproc_%d" % int(start_epoch))
    boot = E(2026, 10, 1, 0, 0, 0)
    (root / str(pid)).mkdir(parents=True, exist_ok=True)
    f = ["S"] + ["0"] * 18 + [str(int((start_epoch - boot) * HZ))] + ["0"] * 10      # field 22 = starttime
    (root / str(pid) / "stat").write_text("%d %s %s\n" % (pid, comm, " ".join(f)))
    (root / "stat").write_text("cpu 1 2 3\nbtime %d\nprocesses 5\n" % int(boot))
    return str(root)
def code_at(name, t):
    f = w(name, "x = 1\n"); os.utime(f, (t, t)); return f
def log_at(name, text, t):
    f = w(name, text); os.utime(f, (t, t)); return f

# process start from /proc starttime + btime
fp = fake_proc(4242, E(2026, 11, 1, 7, 30))
check("process start is /proc starttime + btime, as a UTC epoch (comm with parens parsed)",
      abs(S._started_epoch(4242, proc=fp) - E(2026, 11, 1, 7, 30)) < 1)
check("process start of my own pid agrees with ps etimes within 2 s",
      abs(S._started_epoch(os.getpid()) - (time.time() - int(subprocess.run(["ps", "-o", "etimes=", "-p", str(os.getpid())], capture_output=True, text=True).stdout))) < 2)
check("a pid absent from /proc and from ps is None, not a guess", S._started_epoch(2**22 + 12345, proc=fp) is None)
# process_predates inside the repeated hour: the text of the clock is the same twice; the instant is not
_real = S._started_epoch
try:
    S._started_epoch = lambda pid, proc="/proc": _real(pid, proc=fp)
    f_old = code_at("dst_p_old.py", E(2026, 11, 1, 6, 45))      # 01:45 CDT (first pass)
    r = S.process_predates([f_old], pid=4242)
    check("DST: file saved 01:45 CDT is OLDER than a process started 01:30 CST (clock text says newer)", r["ok"] and not r["stale"])
    S._started_epoch = lambda pid, proc="/proc": _real(pid, proc=fake_proc(4243, E(2026, 11, 1, 6, 20)))
    f_new = code_at("dst_p_new.py", E(2026, 11, 1, 7, 10))      # 01:10 CST (second pass)
    r = S.process_predates([f_new], pid=4243)
    check("DST: file saved 01:10 CST is NEWER than a process started 01:20 CDT (clock text says older)", (not r["ok"]) and r["stale"])
    check("DST: the stale message names both zones, so the repeated hour is readable",
          "CST" in r["stale"][0][1] and "CDT" in r["stale"][0][1] and "UTC" in r["stale"][0][1])
    S._started_epoch = lambda pid, proc="/proc": _real(pid, proc=fake_proc(4244, E(2026, 3, 8, 7, 59, 30)))
    r = S.process_predates([code_at("gap_p_new.py", E(2026, 3, 8, 8, 0, 30))], pid=4244)
    check("spring gap: a file saved 03:00:30 CDT is newer than a process started 01:59:30 CST", not r["ok"] and r["stale"])
    r = S.process_predates([code_at("gap_p_old.py", E(2026, 3, 8, 7, 59, 0))], pid=4244)
    check("spring gap: a file saved 01:59:00 CST is older than a process started 01:59:30 CST", r["ok"])
finally:
    S._started_epoch = _real

# live_proof: explicit offsets are honoured, whatever the machine zone says
def LP(text, log_t, code_t, name):
    return S.live_proof(log_at(name + ".log", text, log_t), "PAGED ok", [code_at(name + ".py", code_t)])
LATE = E(2026, 11, 2, 0, 0)    # the log was last written long after: settles nothing
r = LP("2026-11-01T01:30:00-06:00 PAGED ok\n", LATE, E(2026, 11, 1, 6, 45), "o1")
check("DST: 01:30-06:00 (07:30Z) is newer than code saved 06:45Z (01:45 CDT)", r["ok"])
r = LP("2026-11-01T01:30:00-05:00 PAGED ok\n", LATE, E(2026, 11, 1, 7, 10), "o2")
check("DST: 01:30-05:00 (06:30Z) is OLDER than code saved 07:10Z (01:10 CST)", (not r["ok"]) and "OLDER" in r["detail"])
r = LP("2026-11-01T07:30:00Z PAGED ok\n", LATE, E(2026, 11, 1, 7, 10), "o3")
check("a Z timestamp is read as UTC", r["ok"])
r = LP("2026-11-01T07:30:00.123456Z PAGED ok\n", LATE, E(2026, 11, 1, 7, 45), "o3b")
check("fractional seconds and a Z timestamp that is older fail", (not r["ok"]) and "OLDER" in r["detail"])
r = LP("2026-11-01 01:30:00 CDT PAGED ok\n", LATE, E(2026, 11, 1, 7, 0), "o4")
check("a CDT abbreviation is 06:30Z, older than code at 07:00Z", (not r["ok"]) and "OLDER" in r["detail"])
r = LP("2026-11-01 01:30:00 CST PAGED ok\n", LATE, E(2026, 11, 1, 7, 0), "o5")
check("a CST abbreviation is 07:30Z, newer than code at 07:00Z", r["ok"])
r = LP("2026-11-01T01:30:00-0600 PAGED ok\n", LATE, E(2026, 11, 1, 7, 0), "o6")
check("a compact -0600 offset is honoured", r["ok"])
_set_tz("Asia/Tokyo")
r = LP("2026-11-01T01:30:00-06:00 PAGED ok\n", LATE, E(2026, 11, 1, 7, 0), "o7")
check("an explicit offset gives the same answer under a different machine zone", r["ok"])
_set_tz("America/Chicago")

# naive local time in the repeated hour: never guessed with mktime
AMB = "2026-11-01 01:30:00 PAGED ok\n"      # 06:30Z (CDT) or 07:30Z (CST)
r = LP(AMB, LATE, E(2026, 11, 1, 7, 0), "a1")
check("DST: a naive 01:30 against code at 07:00Z is AMBIGUOUS and FAILS (not a silent pass or a silent OLDER)",
      (not r["ok"]) and "AMBIGUOUS" in r["detail"] and "CDT" in r["detail"] and "CST" in r["detail"])
r = LP(AMB, LATE, E(2026, 11, 1, 6, 0), "a2")
check("DST: a naive 01:30 passes when even its earlier reading is newer than the code", r["ok"])
r = LP(AMB, LATE, E(2026, 11, 1, 8, 0), "a3")
check("DST: a naive 01:30 is OLDER when even its later reading predates the code", (not r["ok"]) and "OLDER" in r["detail"] and "AMBIGUOUS" not in r["detail"])
r = LP(AMB, E(2026, 11, 1, 6, 45), E(2026, 11, 1, 6, 0), "a4")
check("DST: the log's own mtime (06:45Z) proves a naive 01:30 was the FIRST pass (06:30Z): newer than code 06:00Z", r["ok"])
r = LP(AMB, E(2026, 11, 1, 6, 45), E(2026, 11, 1, 6, 40), "a5")
check("DST: mtime-settled 01:30 (06:30Z) is OLDER than code 06:40Z, and says OLDER not AMBIGUOUS",
      (not r["ok"]) and "OLDER" in r["detail"] and "AMBIGUOUS" not in r["detail"])
r = LP(AMB, E(2026, 11, 1, 7, 45), E(2026, 11, 1, 7, 0), "a6")
check("DST: a log written later than the second 01:30 cannot settle a naive 01:30", (not r["ok"]) and "AMBIGUOUS" in r["detail"])
r = LP("2026-11-01 00:59:59 PAGED ok\n", LATE, E(2026, 11, 1, 5, 59, 0), "a7")
check("a naive time just BEFORE the repeated hour is unambiguous (05:59:59Z)", r["ok"])
r = LP("2026-11-01 02:00:00 PAGED ok\n", LATE, E(2026, 11, 1, 8, 0, 30), "a8")
check("a naive time just AFTER the repeated hour is unambiguous (08:00:00Z), older than 08:00:30Z", (not r["ok"]) and "OLDER" in r["detail"] and "AMBIGUOUS" not in r["detail"])
# skipped hour (spring forward): 02:30 never existed
GAP = "2026-03-08 02:30:00 PAGED ok\n"
r = LP(GAP, LATE, E(2026, 3, 8, 8, 0), "g1")
check("spring gap: a naive 02:30 (a time that never existed) against code at 08:00Z is AMBIGUOUS and fails", (not r["ok"]) and "AMBIGUOUS" in r["detail"])
r = LP(GAP, LATE, E(2026, 3, 8, 7, 0), "g2")
check("spring gap: a naive 02:30 passes when even its earlier reading is newer than the code", r["ok"])
r = LP("2026-03-08 03:30:00 PAGED ok\n", LATE, E(2026, 3, 8, 8, 0), "g3")
check("spring gap: 03:30 CDT (08:30Z) is unambiguous and newer than code at 08:00Z", r["ok"])
r = LP("2026-03-08 01:30:00 PAGED ok\n", LATE, E(2026, 3, 8, 7, 45), "g4")
check("spring gap: 01:30 CST (07:30Z) is unambiguous and older than code at 07:45Z", (not r["ok"]) and "OLDER" in r["detail"] and "AMBIGUOUS" not in r["detail"])
r = LP("2026-13-45 99:00:00 PAGED ok\n", LATE, E(2026, 3, 8, 7, 45), "g5")
check("an impossible date is no timestamp, not a crash", (not r["ok"]) and "timestamp" in r["detail"])
r = LP("2026-11-01 01:30:00 PAGED ok\n2026-11-01T08:30:00Z PAGED ok\n", LATE, E(2026, 11, 1, 8, 0), "g6")
check("an ambiguous line does not hide a later unambiguous proof", r["ok"])
_set_tz("UTC") if _TZ0 is None else None
if _TZ0 is None:
    os.environ.pop("TZ", None); time.tzset()
else:
    _set_tz(_TZ0)

# --- 7 manifest end to end
MFILE = w("mutants_thing.py", "MUTANTS = [('threshold loosened', 'n >= 3', 'n >= 1')]\n")
good = {"name": "good", "cwd": str(TMP), "suites": ["%s %s" % (PY, GOOD)],
        "live_files": {"strict": [LIVE], "append_ok": [LOG]},
        "mutants": {"target": THING, "test": "%s %s" % (PY, GOOD), "file": MFILE}}
mp = w("good.json", json.dumps(good))
r = subprocess.run([PY, os.path.join(HERE, "ship_check.py"), mp], capture_output=True, text=True)
check("CLI: a healthy manifest exits 0 and says READY", r.returncode == 0 and "READY TO SHIP" in r.stdout)

blind = dict(good, name="blind", suites=["%s %s" % (PY, BLIND)], mutants=dict(good["mutants"], test="%s %s" % (PY, BLIND)))
r = subprocess.run([PY, os.path.join(HERE, "ship_check.py"), w("blind.json", json.dumps(blind))],
                   capture_output=True, text=True)
check("CLI: a blind suite exits 1 and names the surviving mutant", r.returncode == 1 and "SURVIVED" in r.stdout)

LEAKER = w("suite_leaker.py", "import sys\nopen(%r, 'w').write('{\"a\": 99}')\nsys.exit(0)\n" % LIVE)
Path(LIVE).write_text('{"a": 1}')
leak = dict(good, name="leak", suites=["%s %s" % (PY, LEAKER)])
leak.pop("mutants")
r = subprocess.run([PY, os.path.join(HERE, "ship_check.py"), w("leak.json", json.dumps(leak))],
                   capture_output=True, text=True)
check("CLI: a suite that writes live state exits 1 and names the file", r.returncode == 1 and "state.json" in r.stdout)
check("CLI: the target is intact after all CLI runs", Path(THING).read_bytes() == ORIG)

# --- 6 remote nodes (TIK-1184): a fake `ssh` on PATH runs the command locally and logs the host
SHIMDIR = TMP / "shim"
SHIMDIR.mkdir()
SSHLOG = TMP / "ssh.log"
(SHIMDIR / "ssh").write_text('#!/bin/bash\nwhile [ "$1" = "-o" ]; do shift 2; done\n'
                             'echo "$1" >> %s\nhost="$1"; shift\n'
                             '[ "$host" = "deadnode" ] && { echo "ssh: connect to host deadnode: Connection refused" >&2; exit 255; }\n'
                             'exec bash -c "$*"\n' % SSHLOG)
(SHIMDIR / "ssh").chmod(0o755)
os.environ["PATH"] = str(SHIMDIR) + ":" + os.environ["PATH"]
rc, out, err = S.run_remote("build-box", "print(6*7)", PY)
check("run_remote pipes the source to the node's interpreter", rc == 0 and out.strip() == "42")
check("run_remote really went through ssh to the named host", "build-box" in SSHLOG.read_text())
rc, out, err = S.run_as_hub("import mymod\nprint(mymod.X)", str(WORK), ssh="build-box", python=PY)
check("production path ON A NODE: a missing module fails there", rc != 0 and "ModuleNotFoundError" in err)
rc, out, err = S.run_as_hub("import os\nprint(os.getcwd())", str(WORK), ssh="build-box", python=PY)
check("production path on a node runs in the production cwd", rc == 0 and out.strip() == str(WORK))
m = {"cwd": str(TMP), "hub_path": {"name": "x", "code": "print(1)", "ssh": "deadnode", "python": PY, "expect_stdout": "1"}}
rows = S.check_manifest(m, only=["hub_path"])
check("an unreachable node says ssh FAILED, not a verdict on the code", (not rows[0][1]) and "ssh to deadnode FAILED" in rows[0][2])
m["hub_path"]["ssh"] = "build-box"
rows = S.check_manifest(m, only=["hub_path"])
check("a reachable node passes and the row names it", rows[0][1] and "on build-box" in rows[0][0])
m = {"cwd": str(TMP), "suites": [{"cmd": "%s -c 'import sys; sys.exit(3)'" % PY, "ssh": "suitenode"}]}
rows = S.check_manifest(m, only=["suites"])
check("a suite that must run on its node goes through ssh to it and its failure counts", (not rows[0][1]) and "suitenode" in SSHLOG.read_text())
m = {"cwd": str(TMP), "process": {"ssh": "procnode", "python": PY, "pid": os.getpid(), "files": [OLD]}}
rows = S.check_manifest(m, only=["process"])
check("a process check with ssh asks that node", rows[0][1] and "on procnode" in rows[0][0] and "procnode" in SSHLOG.read_text())
m = {"cwd": str(TMP), "live_proof": {"ssh": "proofnode", "python": PY, "file": str(TMP / "nofile"), "pattern": "x"}}
rows = S.check_manifest(m, only=["live_proof"])
check("a live proof with ssh reads the file on that node", (not rows[0][1]) and "on proofnode" in rows[0][0] and "proofnode" in SSHLOG.read_text())
res = S.remote_call("build-box", "live_proof", PY, path=str(TMP / "nofile"), pattern="x", newer_than=[])
check("remote live_proof reports a missing proof file", (not res["ok"]) and "missing" in res["detail"])
res = S.remote_call("build-box", "process_predates", PY, files=[OLD], pid=os.getpid(), port=None, unit=None)
check("remote process_predates answers for a pid on that node", res["ok"] is True and res["pid"] == os.getpid())
res = S.remote_call("deadnode", "process_predates", PY, files=[OLD], pid=1, port=None, unit=None)
check("remote_call surfaces an ssh failure as not-ok with the reason", (not res["ok"]) and "ssh to deadnode FAILED" in res["error"])

print("")
print("%d/%d passed" % (NCHK - len(FAILED), NCHK))
sys.exit(1 if FAILED else 0)
