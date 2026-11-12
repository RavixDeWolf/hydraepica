#!/usr/bin/env python3
"""ship_check.py - mechanical pre-ship checks for anything that CATCHES, SURFACES or GATES.

WHY THIS EXISTS (2026-10-02, TIK-0604 AIR pager). I shipped a pager whose first live test sent
nothing, whose first regression test could not see its own bug, whose tests wrote into live
state, and whose running process still held the old code. Every one of those already had a
written memory. Memories are advisory and I did not consult them while building; a script that
exits non-zero cannot be skimmed past. This is that script.

THE FIVE CHECKS (each is a function; `main()` runs a JSON manifest of them):
  1 run_mutants      break the code on purpose; the suite MUST go red. The suite must be GREEN
                     first (a red baseline makes every mutant look "caught"). A mutant whose anchor
                     is not found exactly once is NOT_APPLIED - a silent no-op mutant is a lie
                     about coverage.
  2 live_unchanged   run the suites; the LIVE state they must never touch is byte-identical.
  3 run_as_hub       run code with the PRODUCTION cwd/sys.path, not the test's. The AIR pager bug
                     lived exactly in that difference (bare import worked in the test, not the hub).
  4 process_predates a running process older than the file it should be running = stale code.
  5 live_proof       a real end-to-end artifact exists and is NEWER than the code it proves.

CRASH SAFETY. run_mutants edits the target IN PLACE (production paths are usually hardcoded), so:
a sidecar copy is written first, every mutant is restored in a `finally`, SIGTERM/SIGHUP restore
before exiting, the result is verified byte-for-byte, and the next run (or `ship_check.py recover
FILE`) restores from the sidecar if a previous run was killed with -9. Do not restart the process
under test while this runs.

USAGE
  ship_check.py MANIFEST.json [--only a,b] [--skip a,b]
  ship_check.py recover FILE
Exit 0 only when every selected check passes. See SKILL_ship_a_watcher.md for the manifest schema.
"""
from __future__ import annotations
import calendar, datetime, hashlib, json, os, re, runpy, shlex, shutil, signal, subprocess, sys, time
from pathlib import Path

SIDE = ".shipcheck-orig"


# ---------------------------------------------------------------- helpers
def _sha(path):
    h = hashlib.sha256()
    n = 0
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
                n += len(chunk)
    except FileNotFoundError:
        return None, 0
    return h.hexdigest(), n


def _prefix_sha(path, n):
    h = hashlib.sha256()
    left = n
    with open(path, "rb") as f:
        while left > 0:
            b = f.read(min(1 << 20, left))
            if not b:
                break
            h.update(b)
            left -= len(b)
    return h.hexdigest()


def _run(cmd, cwd=None, env=None, timeout=240):
    """(returncode, combined output), or None on timeout. Kills the whole process group."""
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.communicate()
        return None
    return p.returncode, out


def _why(out):
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    for l in lines:
        if "FAIL" in l or "Error" in l or "Traceback" in l:
            return l[:110]
    return lines[-1][:110] if lines else ""


# ---------------------------------------------------------------- 1 mutants
def recover(target):
    """Restore `target` from its sidecar if a previous run died mid-mutation."""
    side = str(target) + SIDE
    if os.path.exists(side):
        shutil.copy2(side, str(target))   # copy2: content AND mtime
        os.remove(side)
        return True
    return False


NEVER_MUTATE = ("TESTMODE", "DATADIR", "_sink")


def run_mutants(target, mutants, test_cmd, cwd=None, timeout=240, env=None, never_mutate=NEVER_MUTATE):
    """mutants = [(name, old, new)]. Returns dict(ok, baseline, results, recovered, detail).
    status per mutant: CAUGHT | SURVIVED | NOT_APPLIED | TIMEOUT | REFUSED.

    REFUSED: the mutant's anchor contains a `never_mutate` substring, i.e. it deletes the TEST SEAL
    (the env-driven switch that keeps a suite away from the real world). With the seal gone the
    suite can ACT: on 2026-10-02 one such hand-run mutant sent ~20 real Telegram pages to Rav.
    Prove the seal with a TEST (assert the sender IS the sink), never by deleting it.
    Belt and braces: every suite run here gets SHIP_CHECK_MUTATING=1, and a watcher's real
    side-effect function must refuse to run while that is set."""
    target = str(target)
    recovered = recover(target)
    orig = Path(target).read_bytes()
    text = orig.decode("utf-8")
    side = target + SIDE
    st = os.stat(target)
    orig_ns = (st.st_atime_ns, st.st_mtime_ns)
    shutil.copy2(target, side)
    prev = {}
    # No stray .pyc from a mutant, and (below) every mutant gets a DISTINCT mtime second.
    env = dict(os.environ if env is None else env)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["SHIP_CHECK_MUTATING"] = "1"

    def _restore():
        # Content AND mtime. The mtime matters twice: it re-validates the original .pyc, and it
        # stops process_predates() from calling a file we merely tested "modified after start".
        try:
            Path(target).write_bytes(orig)
            os.utime(target, ns=orig_ns)
        except Exception:
            pass

    def _on_signal(signum, frame):
        _restore()
        raise SystemExit(128 + signum)

    for s in (signal.SIGTERM, signal.SIGHUP):
        try:
            prev[s] = signal.signal(s, _on_signal)
        except (ValueError, OSError):
            pass  # not the main thread

    results = []
    try:
        base = _run(test_cmd, cwd, env, timeout)
        if base is None or base[0] != 0:
            why = "baseline timed out" if base is None else "baseline suite is RED: " + _why(base[1])
            return dict(ok=False, baseline=False, results=[], recovered=recovered, detail=why)
        for i, (name, a, b) in enumerate(mutants):
            hit = [w for w in never_mutate if w in a]
            if hit:
                results.append(dict(name=name, status="REFUSED",
                                    detail="touches the test seal (%s): the suite could then reach the real world" % ", ".join(hit)))
                continue
            n = text.count(a)
            if n != 1:
                results.append(dict(name=name, status="NOT_APPLIED",
                                    detail="anchor found %d times (need exactly 1)" % n))
                continue
            Path(target).write_bytes(text.replace(a, b, 1).encode("utf-8"))
            os.utime(target, ns=(orig_ns[0], orig_ns[1] + (i + 1) * 3 * 10**9))
            try:
                r = _run(test_cmd, cwd, env, timeout)
            finally:
                _restore()
            if r is None:
                results.append(dict(name=name, status="TIMEOUT", detail="suite hung on this mutant"))
            elif r[0] != 0:
                results.append(dict(name=name, status="CAUGHT", detail=_why(r[1])))
            else:
                results.append(dict(name=name, status="SURVIVED",
                                    detail="the suite stayed GREEN - it cannot see this bug"))
    finally:
        _restore()
        for s, h in prev.items():
            try:
                signal.signal(s, h)
            except (ValueError, OSError):
                pass
        if Path(target).read_bytes() != orig or os.stat(target).st_mtime_ns != orig_ns[1]:
            raise RuntimeError("TARGET NOT RESTORED: %s (sidecar kept at %s)" % (target, side))
        if os.path.exists(side):
            os.remove(side)
    bad = [r for r in results if r["status"] != "CAUGHT"]
    return dict(ok=not bad, baseline=True, results=results, recovered=recovered, detail="")


# ---------------------------------------------------------------- 2 live state
def live_unchanged(strict, append_ok, run):
    """Snapshot live files, call run(), compare. `strict` must be byte-identical.
    `append_ok` may GROW (a live log the real system keeps writing) but never be rewritten.
    A change can be live traffic rather than a test leak: re-run in a quiet window to tell."""
    snap = {p: _sha(p) for p in list(strict) + list(append_ok)}
    run()
    problems = []
    for p in strict:
        before, now = snap[p], _sha(p)
        if now[0] != before[0]:
            kind = "CREATED" if before[0] is None else ("DELETED" if now[0] is None else "CHANGED")
            problems.append((p, kind))
    for p in append_ok:
        (bh, bn), (nh, nn) = snap[p], _sha(p)
        if nh == bh or bh is None:
            continue
        if nn >= bn and _prefix_sha(p, bn) == bh:
            continue  # grew, prefix intact
        problems.append((p, "REWRITTEN (not append-only)"))
    return dict(ok=not problems, problems=problems)


# ---------------------------------------------------------------- 3 production path
def run_as_hub(code, cwd, drop_path_substrings=(), env=None, timeout=60, ssh=None, python=None):
    """Run `code` with the PRODUCTION cwd and a sys.path stripped of the dirs the test relies on.
    With `ssh`, run it on that node under its own interpreter (`python`) - the library versions
    that serve production, not this machine's. Returns (returncode, stdout, stderr)."""
    drop = list(drop_path_substrings)
    prelude = "import sys\nsys.path[:] = [p for p in sys.path if not any(s in p for s in %r)]\n" % (drop,)
    if ssh:
        return run_remote(ssh, prelude + code, python or "python3", cwd=cwd, timeout=timeout)
    r = subprocess.run([sys.executable, "-c", prelude + code], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


# ---------------------------------------------------------------- remote nodes (TIK-1184)
# "Works from the production cwd" is not "works in production" when production is ANOTHER MACHINE
# (the ticket system passed here on FastAPI 0.129 and crash-looped on another node's 0.141). A manifest entry may name
# `"ssh": "build-box"` (+ optional `"python"`, the node's venv interpreter): the check then runs on that host.
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def run_remote(host, src, python="python3", cwd=None, timeout=60):
    """Pipe python source to `python -` on `host` (in `cwd`). Returns (rc, stdout, stderr). rc 255 is
    ssh itself failing - callers must say so rather than call it a verdict on the code."""
    inner = "exec %s -" % shlex.quote(python)
    if cwd:
        inner = "cd %s && %s" % (shlex.quote(cwd), inner)
    try:
        r = subprocess.run(["ssh", *SSH_OPTS, host, inner], input=src, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return 255, "", "ssh %s: timed out after %ss" % (host, timeout)
    return r.returncode, r.stdout, r.stderr


def _remote_note(host, rc, err):
    if rc == 255:
        return "ssh to %s FAILED (not a verdict on the code): %s" % (host, (err.strip().splitlines() or ["?"])[-1][:90])
    return None


def remote_call(host, func, python="python3", timeout=60, **kw):
    """Run one of THIS module's check functions on `host` and return its JSON result.
    The module source is shipped over stdin, so the node needs nothing installed."""
    src = ("import json, types\nm = types.ModuleType('ship_check')\n"
           "exec(compile(%r, 'ship_check', 'exec'), m.__dict__)\n"
           "print(json.dumps(m.%s(**json.loads(%r))))\n") % (Path(__file__).read_text(), func, json.dumps(kw))
    rc, out, err = run_remote(host, src, python, timeout=timeout)
    note = _remote_note(host, rc, err)
    if note or rc != 0:
        return dict(ok=False, error=note or "remote %s failed: %s" % (func, (err.strip().splitlines() or ["?"])[-1][:100]))
    try:
        return json.loads(out.strip().splitlines()[-1])
    except Exception:
        return dict(ok=False, error="remote %s returned no JSON: %r" % (func, out[-80:]))


# ---------------------------------------------------------------- 4 stale process
def _pid_for_port(port):
    r = subprocess.run(["ss", "-ltnp", "sport = :%d" % port], capture_output=True, text=True)
    m = re.search(r"pid=(\d+)", r.stdout)
    return int(m.group(1)) if m else None


def _fmt(t):
    """An instant, unambiguously: local clock WITH its zone abbreviation, UTC, and epoch.
    A bare %H:%M:%S is the same string twice on the fall-back night (2026-11-01 01:30 CDT / CST)."""
    return "%s (%s UTC, epoch %d)" % (time.strftime("%m-%d %H:%M:%S %Z", time.localtime(t)),
                                       time.strftime("%H:%M:%S", time.gmtime(t)), int(t))


def _started_epoch(pid, proc="/proc"):
    """Process start as a UTC epoch: /proc/<pid>/stat starttime (clock ticks since boot) + btime.
    Never ps lstart text and never a local clock, so a repeated or skipped DST hour cannot move it.
    Falls back to `ps etimes` (elapsed seconds, also zone-free) where /proc is unavailable."""
    try:
        raw = Path(proc, str(pid), "stat").read_text()
        ticks = int(raw[raw.rindex(")") + 2:].split()[19])   # field 22; fields 1-2 (pid, comm) are before the last ')'
        btime = None
        for line in Path(proc, "stat").read_text().splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
        if btime is not None:
            return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        pass
    r = subprocess.run(["ps", "-o", "etimes=", "-p", str(pid)], capture_output=True, text=True)
    s_ = r.stdout.strip()
    return time.time() - int(s_) if s_.isdigit() else None


def _pid_for_unit(unit):
    """systemd unit (user scope first, then system) -> MainPID, or None."""
    for scope in (["--user"], []):
        try:
            out = subprocess.run(["systemctl", *scope, "show", "-p", "MainPID", "--value", unit],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            continue
        if out.isdigit() and int(out) > 0:
            return int(out)
    return None


def process_predates(files, pid=None, port=None, unit=None):
    """Files modified AFTER the process started: it is running an older copy of them."""
    if pid is None and unit is not None:
        pid = _pid_for_unit(unit)
    if pid is None and port is not None:
        pid = _pid_for_port(port)
    if pid is None:
        return dict(ok=False, error="no process found", stale=[])
    t0 = _started_epoch(pid)
    if t0 is None:
        return dict(ok=False, error="cannot read start time of pid %s" % pid, stale=[])
    stale = []
    for f in files:
        try:
            m = os.path.getmtime(f)
        except FileNotFoundError:
            stale.append((f, "MISSING"))
            continue
        if m > t0 + 1:
            stale.append((f, "modified %s, process started %s" % (_fmt(m), _fmt(t0))))
    return dict(ok=not stale, pid=pid, started=t0, stale=stale, error="")


# ---------------------------------------------------------------- 5 live proof
_ZONES = {"Z": 0, "UTC": 0, "GMT": 0, "EDT": -4, "EST": -5, "CDT": -5, "CST": -6,
          "MDT": -6, "MST": -7, "PDT": -7, "PST": -8}
_TS_RE = re.compile(r"\s*(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:[.,]\d+)?\s*"
                    r"(Z|UTC|GMT|[ECMP][SD]T|[+-]\d\d:\d\d|[+-]\d{4})?(?!\w)")


def _parse_ts(line, file_mtime=None):
    """Timestamp at the start of a log line -> sorted list of UTC-epoch candidates, or None.
    One candidate when the instant is known: an explicit offset/zone (Z, +05:00, -0600, CDT...), or a
    naive local time that is unambiguous, or one the file's own mtime settles (a line cannot be newer
    than the file that holds it). TWO candidates when a naive local time falls in a repeated (fall-back)
    or skipped (spring-forward) hour and nothing settles it. Never guessed with time.mktime."""
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        y, mo, d, H, M, S = (int(x) for x in m.groups()[:6])
        z = m.group(7)
        if z:
            if z in _ZONES:
                off = _ZONES[z] * 3600
            else:
                off = (-1 if z[0] == "-" else 1) * (int(z[1:3]) * 3600 + int(z[-2:]) * 60)
            return [calendar.timegm((y, mo, d, H, M, S)) - off]
        naive = datetime.datetime(y, mo, d, H, M, S)
    except (ValueError, OverflowError):
        return None
    cands = sorted({naive.replace(fold=0).timestamp(), naive.replace(fold=1).timestamp()})
    if len(cands) == 2 and file_mtime is not None and file_mtime + 1 < cands[1]:
        return [cands[0]]
    return cands


def live_proof(path, pattern, newer_than=()):
    """A line matching `pattern` exists in `path` and (if newer_than given) carries a timestamp at
    least as new as the newest of those code files. Everything is compared as UTC epoch. A naive local
    time that is ambiguous (DST repeated/skipped hour) and not settled by the file mtime is reported
    as AMBIGUOUS and FAILS unless even its earlier reading is newer than the code."""
    try:
        text = Path(path).read_text(errors="replace")
        fmt_ = os.path.getmtime(path)
    except FileNotFoundError:
        return dict(ok=False, detail="proof file missing: %s" % path)
    hits = [l for l in text.splitlines() if re.search(pattern, l)]
    if not hits:
        return dict(ok=False, detail="no line in %s matches %r" % (os.path.basename(path), pattern))
    if not newer_than:
        return dict(ok=True, detail="%d matching line(s)" % len(hits))
    code_m = max(os.path.getmtime(f) for f in newer_than)
    sure, amb = None, []
    for l in hits:
        c = _parse_ts(l, fmt_)
        if not c:
            continue
        if len(c) == 1:
            sure = c[0] if sure is None else max(sure, c[0])
        else:
            amb.append(c)
    if sure is None and not amb:
        return dict(ok=False, detail="proof lines carry no timestamp, so they cannot be tied to this build")
    if sure is not None and sure + 1 >= code_m:
        return dict(ok=True, detail="proof at %s is newer than the code" % _fmt(sure))
    worst = max((c[0] for c in amb), default=None)    # every ambiguous line read its EARLIER way
    best = max((c[1] for c in amb), default=None)     # ... or its LATER way
    if worst is not None and worst + 1 >= code_m:
        return dict(ok=True, detail="proof at %s (earlier of two readings of an ambiguous local time) "
                                    "is still newer than the code" % _fmt(worst))
    if best is not None and best + 1 >= code_m:
        return dict(ok=False, detail="AMBIGUOUS: newest proof is a local time in a repeated/skipped DST hour, "
                                     "%s or %s; the code changed %s. Not guessing - prove it again, or log with a "
                                     "UTC offset" % (_fmt(worst), _fmt(best), _fmt(code_m)))
    newest = max(x for x in (sure, best) if x is not None)
    return dict(ok=False, detail="newest proof %s is OLDER than the last code change %s - prove it again" % (
        _fmt(newest), _fmt(code_m)))


# ---------------------------------------------------------------- manifest runner
def _suite_cmd(s):
    inner = s["cmd"] if not s.get("cwd") else "cd %s && %s" % (shlex.quote(s["cwd"]), s["cmd"])
    return ["ssh", *SSH_OPTS, s["ssh"], inner]


ORDER = ["suites", "hub_path", "mutants", "process", "live_proof"]


def _as_list(v):
    return v if isinstance(v, list) else ([v] if v else [])


def check_manifest(m, only=None, skip=None):
    want = [c for c in ORDER if (not only or c in only) and not (skip and c in skip)]
    rows = []
    cwd = m.get("cwd")

    def add(name, ok, detail=""):
        rows.append((name, ok, detail))

    if "suites" in want and m.get("suites"):
        lf = m.get("live_files", {})
        suite_fail = []

        def run_all():
            for s in m["suites"]:
                if isinstance(s, dict):   # {"cmd": ..., "ssh": "build-box", "cwd": ...}: a suite that must run on its node
                    s = _suite_cmd(s)
                r = _run(s, cwd, None, m.get("suite_timeout", 240))
                if r is None:
                    suite_fail.append("%s: TIMEOUT" % s)
                elif r[0] != 0:
                    suite_fail.append("%s: %s" % (s, _why(r[1])))

        res = live_unchanged(lf.get("strict", []), lf.get("append_ok", []), run_all)
        add("suites", not suite_fail, "%d suite(s) green" % len(m["suites"]) if not suite_fail else "; ".join(suite_fail))
        n = len(lf.get("strict", [])) + len(lf.get("append_ok", []))
        add("live state untouched", res["ok"],
            ("%d live file(s) byte-identical across the full suite" % n) if res["ok"]
            else "; ".join("%s %s" % (os.path.basename(p), k) for p, k in res["problems"]))

    if "hub_path" in want:
        for h in _as_list(m.get("hub_path")):
            node = h.get("ssh")
            rc, out, err = run_as_hub(h["code"], h.get("cwd", cwd), h.get("drop", []),
                                      ssh=node, python=h.get("python"), timeout=h.get("timeout", 60))
            exp = h.get("expect_stdout", "")
            ok = rc == 0 and exp in out
            where = " on %s" % node if node else ""
            add("production path%s: %s" % (where, h.get("name", "?")), ok,
                ("ran with the production cwd/sys.path" + where) if ok else
                (_remote_note(node, rc, err) if node else None) or (err.strip().splitlines() or [out.strip()])[-1][:110])

    if "mutants" in want:
        for mm in _as_list(m.get("mutants")):
            muts = runpy.run_path(mm["file"])["MUTANTS"]
            res = run_mutants(mm["target"], muts, mm["test"], cwd=mm.get("cwd", cwd), timeout=mm.get("timeout", 240),
                              never_mutate=tuple(mm.get("never_mutate", NEVER_MUTATE)))
            label = "mutants: " + os.path.basename(mm["target"])
            if not res["baseline"]:
                add(label, False, res["detail"])
                continue
            c = sum(1 for r in res["results"] if r["status"] == "CAUGHT")
            bad = [r for r in res["results"] if r["status"] != "CAUGHT"]
            add(label, res["ok"], "%d/%d caught" % (c, len(res["results"])) +
                ("" if res["ok"] else " | " + "; ".join("%s: %s" % (r["status"], r["name"]) for r in bad)))

    if "process" in want and m.get("process"):
        for pr in _as_list(m["process"]):
            if pr.get("ssh"):
                res = remote_call(pr["ssh"], "process_predates", pr.get("python", "python3"),
                                  files=pr["files"], port=pr.get("port"), pid=pr.get("pid"), unit=pr.get("unit"))
                res.setdefault("stale", [])
            else:
                res = process_predates(pr["files"], port=pr.get("port"), pid=pr.get("pid"), unit=pr.get("unit"))
            add("running process is current" + (" on %s" % pr["ssh"] if pr.get("ssh") else ""), res["ok"],
                ("pid %s started after every listed file changed" % res.get("pid")) if res["ok"]
                else (res.get("error") or "; ".join("%s (%s)" % (os.path.basename(f), w) for f, w in res["stale"])
                      + " -> RESTART REQUIRED"))

    if "live_proof" in want and m.get("live_proof"):
        for lp in _as_list(m["live_proof"]):
            if lp.get("ssh"):
                res = remote_call(lp["ssh"], "live_proof", lp.get("python", "python3"),
                                  path=lp["file"], pattern=lp["pattern"], newer_than=lp.get("newer_than", []))
                res.setdefault("detail", res.get("error", ""))
            else:
                res = live_proof(lp["file"], lp["pattern"], lp.get("newer_than", []))
            add("live proof" + (" on %s" % lp["ssh"] if lp.get("ssh") else ""), res["ok"], res["detail"])

    return rows


def _print(name, rows):
    print("SHIP CHECK - %s" % name)
    for n, ok, d in rows:
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", n, ("  - " + d) if d else ""))
    bad = [r for r in rows if not r[1]]
    print("RESULT: %s" % ("READY TO SHIP" if not bad else "NOT READY - %d failing" % len(bad)))
    return 0 if not bad else 1


def main(argv):
    if len(argv) >= 3 and argv[1] == "recover":
        print("restored from sidecar" if recover(argv[2]) else "no sidecar - nothing to restore")
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2
    only = skip = None
    for i, a in enumerate(argv):
        if a == "--only":
            only = argv[i + 1].split(",")
        if a == "--skip":
            skip = argv[i + 1].split(",")
    m = json.load(open(argv[1]))
    return _print(m.get("name", argv[1]), check_manifest(m, only, skip))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
