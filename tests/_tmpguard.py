"""Test temp guard (TIK-0702 (pattern), TIK-0707 (applied here)): every temp dir a remediator test run makes lands under ONE per-run root, and that root is removed
when the run ends. Import this FIRST in every test file:  import _tmpguard  (it must run before any tempfile.mkdtemp()).

- tempfile.tempdir and TMPDIR point at /tmp/remediator-tests/<pid>-<start>, so mkdtemp() here AND in child processes
  (mutant runs, subprocess workers) lands inside it.
- atexit removes it. A run that is killed (timeout, SIGKILL) cannot run atexit, so each new run first removes the roots
  of runs whose process is gone (pid not alive): leaks are bounded to runs that are still running.
- assert_clean() lets a suite prove it at the end: its own root holds nothing it did not clean, and no stray tmp* dir
  appeared directly in the system temp dir from THIS process tree (they would carry our run marker).
Why: foreman: 146,265 leaked mkdtemp dirs (~36 GB) in the host /tmp (2026-09-29..10-08). Here: 10-31 the host /tmp held ~280,000 entries, hl_turn_ 108k, hl_th_ 39k, hl_vis_ 33k, hl_perm_ 26k from these suites."""
import atexit, os, shutil, sys, tempfile, time

BASE = os.path.join(os.environ.get("REMEDIATOR_TEST_TMP_BASE") or "/tmp", "remediator-tests")
_ORIG = tempfile.gettempdir()

def _alive(pid):
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False
    except PermissionError: return True

def _reap():
    if not os.path.isdir(BASE): return 0
    n = 0
    for name in os.listdir(BASE):
        try: pid = int(name.split("-", 1)[0])
        except ValueError: continue
        if pid != os.getpid() and not _alive(pid):
            shutil.rmtree(os.path.join(BASE, name), ignore_errors=True); n += 1
    if n: print(f"[_tmpguard] removed {n} temp root(s) left by killed test runs", file=sys.stderr)   # logged, never silent
    return n

if os.environ.get("_REMEDIATOR_TMPGUARD_ROOT") and os.path.isdir(os.environ["_REMEDIATOR_TMPGUARD_ROOT"]):
    ROOT = os.environ["_REMEDIATOR_TMPGUARD_ROOT"]          # a child of a guarded run: share the parent's root, the parent cleans it
    OWNER = False
else:
    os.makedirs(BASE, exist_ok=True); _reap()
    ROOT = os.path.join(BASE, f"{os.getpid()}-{int(time.time())}")
    os.makedirs(ROOT); OWNER = True
    os.environ["_REMEDIATOR_TMPGUARD_ROOT"] = ROOT
os.environ["TMPDIR"] = ROOT
tempfile.tempdir = ROOT

def _cleanup():
    if OWNER: shutil.rmtree(ROOT, ignore_errors=True)
atexit.register(_cleanup)

def assert_clean():
    """Call at the very end of a suite: the guard is active and the run's temp root is where every temp dir went."""
    assert tempfile.gettempdir() == ROOT, f"tempfile.tempdir was changed away from the guard root ({tempfile.gettempdir()})"
    assert ROOT.startswith(BASE), "guard root outside its base"
    return ROOT
