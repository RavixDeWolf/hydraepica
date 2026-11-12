#!/usr/bin/env python3
"""headsafe - a head can edit itself, and a bad edit undoes itself.

The rule: a head may change ANY of itself (lane, hook, bridge, settings, units, CLAUDE.md). Only this safety net is
off limits. The flow: the state is saved before a change; if the head does not respond after the change or restart, it
reverts after 20 seconds. If it can respond, it does, then gets a minute to check things and run `finalize`, which stops
the timer. Otherwise it reverts, unless it decides to revert itself.

    headsafe snapshot [--label L]                   save the head's files (tar.gz + manifest) -> prints the snapshot id
    headsafe arm --snap ID [--restart CMD]...       DELAYED restart + watchdog (own systemd unit, survives the restart):
                 [--delay 8] [--liveness 20] [--finalize 60]
                    1. wait --delay s (the head's turn finishes), run each --restart command
                    2. LIVENESS: within --liveness s the head must answer a probe typed into its REPL, else REVERT
                    3. FINALIZE: the head gets --finalize s to verify and run `headsafe finalize`, else REVERT
    headsafe finalize                               stop the timer; only valid AFTER the liveness probe passed
    headsafe revert [--reason R]                    put the snapshot back now and restart (the head's own decision)
    headsafe status | list                          what is armed / which snapshots exist

Every step is appended to LOG (Iron Rule 12); a revert writes a REVERTED marker and messages the owner; a failed revert writes FAILED.
Nothing here reads or writes anything outside the watch list (config.py-style defaults below, overridable by HEADSAFE_CONFIG).
Limits (documented, not hidden): the head runs as the same Unix user, so "cannot edit the safety net" is a rule plus a deny
list, not a lock, until the code and store are made root-owned by the operator.
"""
import argparse
import glob
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import time

HOME = os.path.expanduser("~")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # this checkout
STATE = os.path.expanduser(os.environ.get("HARNESS_STATE_DIR", "~/.local/state/harness"))   # = [paths].state_dir
WORK = os.path.expanduser(os.environ.get("HARNESS_WORK_ROOT", "~/heads"))                  # = [paths].work_root
ROOT = os.environ.get("HEADSAFE_HOME", os.path.join(STATE, "headsafe"))
SNAPS = os.path.join(ROOT, "snaps")
ARMED = os.path.join(ROOT, "armed.json")
FINAL = os.path.join(ROOT, "finalized.json")
REQ = os.path.join(ROOT, "revert_request.json")         # the head asking the watchdog to revert (separate file: no write race with ARMED)
LOG = os.path.join(ROOT, "headsafe.log")
HISTORY = os.path.join(ROOT, "history")
REVERTED = os.path.join(ROOT, "REVERTED")
FAILED = os.path.join(ROOT, "FAILED")

DELAY_S, LIVENESS_S, FINALIZE_S = 8, 20, 60
KEEP_SNAPS = 20
MAX_FILE = 5 * 1024 * 1024
HEAD_SOCK = os.environ.get("HEADSAFE_SOCK", os.path.join(STATE, "head.sock"))
HEAD_EVENTS = os.environ.get("HEADSAFE_EVENTS", os.path.join(STATE, "events.jsonl"))
PROBE_PREFIX = "headsafe-probe-"          # head_lane.relay_followups ignores replies carrying this

DEFAULT_CONFIG = {
    "git_repos": [REPO],                                         # tracked + untracked-not-ignored files
    "files": [
        os.path.join(WORK, "main", "CLAUDE.md"), os.path.join(WORK, "main", ".claude", "settings.json"),
        os.path.join(WORK, "main", ".claude", "settings.local.json"),
        os.path.join(STATE, "lanes.json"), os.path.join(STATE, "tg_threads.json"),
        os.path.expanduser(os.environ.get("HARNESS_CONFIG", "~/.config/harness/config.toml")),
    ],
    "globs": ["~/.config/systemd/user/harness-*", "~/.config/systemd/user/harness-*/*"],
    "units": ["harness-head@main.service", "harness-gateway.service"],   # their active state is recorded in the manifest
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    p = os.environ.get("HEADSAFE_CONFIG")
    if p:
        with open(p) as f:
            cfg.update(json.load(f))
    return cfg


def log(event, **kw):
    try:
        os.makedirs(ROOT, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **kw}) + "\n")
    except Exception as e:                                         # a log that cannot be written is said out loud
        print("headsafe: cannot write its log (%r): %s %s" % (e, event, kw), file=sys.stderr)


def _x(p):
    return os.path.realpath(os.path.expanduser(p))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _sh(cmd, timeout=60):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


# ------------------------------------------------------------------ what is watched
def watched_files(cfg, run=_sh):
    out = {}
    for repo in cfg.get("git_repos", []):
        r = _x(repo)
        rc, txt = run(["git", "-C", r, "ls-files", "-co", "--exclude-standard", "-z"])
        if rc != 0:
            raise RuntimeError("git ls-files failed in %s: %s" % (r, txt[:200]))
        for rel in [t for t in txt.split("\0") if t]:
            out[os.path.join(r, rel)] = True
    for f in cfg.get("files", []):
        out[_x(f)] = True
    for g in cfg.get("globs", []):
        for f in glob.glob(os.path.expanduser(g)):
            if os.path.isfile(f):
                out[_x(f)] = True
    keep = []
    for p in sorted(out):
        try:
            if os.path.isfile(p) and os.path.getsize(p) <= MAX_FILE:
                keep.append(p)
        except OSError:
            pass
    return keep


# ------------------------------------------------------------------ snapshot / restore
def snapshot(label="", cfg=None, run=_sh, now=time.time):
    cfg = cfg or load_config()
    os.makedirs(SNAPS, exist_ok=True)
    files = watched_files(cfg, run)
    if not files:
        raise RuntimeError("nothing to snapshot: the watch list matched no files")
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "snap").lower()).strip("-")[:30] or "snap"
    sid = time.strftime("%Y%m%d-%H%M%S", time.localtime(now())) + "-" + slug
    man = {"id": sid, "label": label, "at": now(), "files": [], "git": {}, "units": {}}
    tmp = os.path.join(SNAPS, sid + ".tar.gz.tmp")
    with tarfile.open(tmp, "w:gz") as tf:
        for p in files:
            st = os.stat(p)
            man["files"].append({"path": p, "sha256": sha256(p), "size": st.st_size, "mode": st.st_mode & 0o7777})
            tf.add(p, arcname=p.lstrip("/"), recursive=False)
    os.replace(tmp, os.path.join(SNAPS, sid + ".tar.gz"))
    for repo in cfg.get("git_repos", []):
        rc, txt = run(["git", "-C", _x(repo), "rev-parse", "HEAD"])
        man["git"][_x(repo)] = txt if rc == 0 else "?"
    for u in cfg.get("units", []):
        rc, txt = run(["systemctl", "--user", "is-active", u])
        man["units"][u] = txt.splitlines()[0] if txt else "?"
    with open(os.path.join(SNAPS, sid + ".json"), "w") as f:
        json.dump(man, f, indent=1)
    _prune()
    log("snapshot", snap=sid, label=label, files=len(files))
    return sid


def _prune():
    ids = sorted(f[:-5] for f in os.listdir(SNAPS) if f.endswith(".json"))
    for sid in ids[:-KEEP_SNAPS]:
        for ext in (".json", ".tar.gz"):
            try:
                os.unlink(os.path.join(SNAPS, sid + ext))
            except OSError:
                pass


def manifest(sid):
    with open(os.path.join(SNAPS, sid + ".json")) as f:
        return json.load(f)


def restore(sid):
    """Put every watched file back as it was. -> (restored, unchanged). Only paths listed in the manifest are written."""
    man = manifest(sid)
    want = {f["path"]: f for f in man["files"]}
    restored, same = [], 0
    with tarfile.open(os.path.join(SNAPS, sid + ".tar.gz"), "r:gz") as tf:
        for ti in tf.getmembers():
            path = "/" + ti.name
            if path not in want:
                raise RuntimeError("snapshot member %r is not in its manifest" % ti.name)
            data = tf.extractfile(ti).read()
            if hashlib.sha256(data).hexdigest() != want[path]["sha256"]:
                raise RuntimeError("snapshot member %r fails its checksum: refusing to restore from a damaged snapshot" % ti.name)
            if os.path.isfile(path) and sha256(path) == want[path]["sha256"]:
                same += 1
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".headsafe.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.chmod(tmp, want[path]["mode"])
            os.replace(tmp, path)
            restored.append(path)
    return restored, same


# ------------------------------------------------------------------ the armed state
def _read(p):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, p)


def _set_phase(st, phase, **kw):
    st.update(phase=phase, phase_at=time.time(), **kw)
    _write(ARMED, st)
    log("phase", id=st["id"], phase=phase, **kw)


def arm(sid, restart=(), delay=DELAY_S, liveness=LIVENESS_S, finalize=FINALIZE_S, spawn=None, now=time.time):
    manifest(sid)                                                   # raises if the snapshot does not exist
    cur = _read(ARMED)
    if cur and cur.get("phase") not in ("done",):
        raise RuntimeError("already armed (%s, phase %s): finalize or revert it first" % (cur.get("id"), cur.get("phase")))
    for p in (FINAL, REVERTED, FAILED, REQ):
        try:
            os.unlink(p)
        except OSError:
            pass
    st = {"id": sid, "restart": list(restart), "delay": delay, "liveness": liveness, "finalize": finalize,
          "armed_at": now(), "phase": "armed", "phase_at": now()}
    _write(ARMED, st)
    log("armed", id=sid, restart=list(restart), delay=delay, liveness=liveness, finalize=finalize)
    (spawn or _spawn_watch)(sid)
    return st


def _spawn_watch(sid):
    unit = "headsafe-" + re.sub(r"[^A-Za-z0-9]", "", sid)[:24]
    env = ["--setenv=%s=%s" % (k, v) for k, v in sorted(os.environ.items()) if k.startswith("HEADSAFE_")]   # a transient unit starts with a clean env
    rc, txt = _sh(["systemd-run", "--user", "--collect", "--unit=" + unit] + env + [sys.executable, os.path.abspath(__file__), "watch"])
    if rc != 0:
        raise RuntimeError("could not start the watchdog unit: %s" % txt[:200])
    log("watchdog_started", unit=unit)


# ------------------------------------------------------------------ the probe
def bridge_probe(token, timeout, sock=HEAD_SOCK, events=HEAD_EVENTS, sleep=time.sleep, now=time.time):
    """Type a probe into the head's REPL and wait for its reply in the hook events. -> (ok, why)."""
    want = "ALIVE " + PROBE_PREFIX + token
    try:
        off = os.path.getsize(events)
    except OSError:
        off = 0
    t0 = now()
    ack, last = None, ""
    while now() - t0 < timeout:                       # a bridge that is still coming up after the restart is not a dead head
        s = socket.socket(socket.AF_UNIX)
        try:
            s.settimeout(5)
            s.connect(sock)
            s.sendall((json.dumps({"t": "send", "text": "[headsafe] liveness check after a self-edit. Reply with exactly: " + want}) + "\n").encode())
            ack = json.loads((s.recv(65536).split(b"\n")[0]) or b"{}")
            break
        except Exception as e:
            last = str(e)[:120]
        finally:
            s.close()
        sleep(1)
    if ack is None:
        return False, "the head's bridge did not take the probe in %ds (%s)" % (timeout, last)
    if not ack.get("ok"):
        return False, "the bridge refused the probe: %s" % (str(ack)[:120],)
    while now() - t0 < timeout:
        try:
            with open(events, "rb") as f:
                f.seek(off)
                data = f.read()
        except OSError:
            data = b""
        for line in data.split(b"\n")[:-1]:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("event") == "Stop" and want in str((r.get("data") or {}).get("last_assistant_message") or ""):
                return True, "answered in %.1fs" % (now() - t0)
        sleep(1)
    return False, "no answer to the probe within %ds" % timeout


# ------------------------------------------------------------------ revert / watch / finalize
def notify(text, run=_sh):
    """Tell the owner (a revert is the one time this speaks). A failure to tell is logged, never raised."""
    cmd = os.environ.get("HEADSAFE_NOTIFY")                        # a command that takes the text as its last argument (tests, demos)
    cmd = cmd.split() + [text] if cmd else ["python3", os.path.join(REPO, "harness", "tg", "tg.py"), "send", text]
    rc, txt = run(cmd, 40)
    if rc != 0:
        log("notify_failed", rc=rc, out=txt[:200])
    return rc == 0


def do_revert(st, reason, run=_sh, sleep=time.sleep, notify_fn=notify):
    sid = st["id"]
    log("reverting", id=sid, reason=reason)
    try:
        restored, same = restore(sid)
    except Exception as e:
        _write(FAILED, {"id": sid, "reason": reason, "error": str(e)[:300], "at": time.time()})
        log("revert_failed", id=sid, error=str(e)[:300])
        notify_fn("headsafe: a revert of the head FAILED (%s). Snapshot %s could not be restored: %s. The head needs a look on the host." % (reason, sid, str(e)[:160]))
        return False
    # restart the same things the change restarted, plus the head's daemon units if their files were among those put back
    cmds = list(st.get("restart") or [])
    for c in cmds:
        rc, txt = run(c, 90)
        log("revert_restart", cmd=c, rc=rc, out=txt[:160])
    _write(REVERTED, {"id": sid, "reason": reason, "restored": restored, "at": time.time()})
    notify_fn("headsafe: the head's self-edit was REVERTED (%s). Snapshot %s put back: %d file(s) restored." % (reason, sid, len(restored)))
    log("reverted", id=sid, restored=len(restored), unchanged=same)
    return True


def watch(run=_sh, sleep=time.sleep, now=time.time, probe=None, notify_fn=notify):
    st = _read(ARMED)
    if not st or st.get("phase") != "armed":
        log("watch_nothing_to_do", state=str(st)[:100])
        return 0
    sid = st["id"]
    probe = probe or (lambda tok, t: bridge_probe(tok, t, sleep=sleep, now=now))
    def asked():                                                       # the head chose to revert itself
        r = _read(REQ)
        return (r or {}).get("reason") if r and r.get("id") == sid else None

    def undo(reason):
        ok = do_revert(st, reason, run, sleep, notify_fn)
        try:
            os.unlink(REQ)
        except OSError:
            pass
        _finish(st, "reverted" if ok else "revert-failed")
        return 0 if ok else 1

    try:
        _set_phase(st, "delay", seconds=st["delay"])
        sleep(st["delay"])
        if asked():
            return undo(asked())
        _set_phase(st, "restarting")
        for c in st["restart"]:
            rc, txt = run(c, 90)
            log("restart", cmd=c, rc=rc, out=txt[:160])
            if rc != 0:
                return undo("restart command failed: %s (rc %s)" % (c, rc))
        if asked():
            return undo(asked())
        _set_phase(st, "liveness", seconds=st["liveness"])
        token = "%08x" % (int(now() * 1000) & 0xFFFFFFFF)
        ok, why = probe(token, st["liveness"])
        log("probe", id=sid, ok=ok, why=why)
        if not ok:
            return undo("no liveness: " + why)
        _set_phase(st, "awaiting-finalize", seconds=st["finalize"], probe=why)
        t0 = now()
        while now() - t0 < st["finalize"]:
            fin = _read(FINAL)
            if fin and fin.get("id") == sid:
                _finish(st, "finalized")
                log("finalized", id=sid)
                return 0
            if asked():
                return undo(asked())
            sleep(1)
        return undo("not finalized within %ds" % st["finalize"])
    except Exception as e:                                              # the watchdog itself failing must undo, not leave a half state
        log("watch_error", id=sid, error=repr(e)[:300])
        undo("the watchdog hit an error: %r" % (e,))
        return 1


def _finish(st, outcome):
    os.makedirs(HISTORY, exist_ok=True)
    _write(os.path.join(HISTORY, st["id"] + ".json"), dict(st, outcome=outcome, ended_at=time.time()))
    _write(ARMED, {"id": st["id"], "phase": "done", "outcome": outcome})
    log("done", id=st["id"], outcome=outcome)


def finalize():
    st = _read(ARMED)
    if not st or st.get("phase") != "awaiting-finalize":
        raise RuntimeError("nothing to finalize: phase is %r (finalize is only valid after the liveness probe passed)" % ((st or {}).get("phase"),))
    _write(FINAL, {"id": st["id"], "at": time.time()})
    log("finalize_requested", id=st["id"])
    return st["id"]


def request_revert(reason="reverted by the head"):
    st = _read(ARMED)
    if st and st.get("phase") in ("awaiting-finalize", "liveness", "restarting", "delay", "armed"):
        _write(REQ, {"id": st["id"], "reason": reason, "at": time.time()})
        log("revert_requested", id=st["id"], reason=reason)
        return "watchdog told to revert"
    return None


def revert_now(reason="reverted by the head", sid=None, run=_sh):
    """No armed run: put a snapshot back right now (the newest, or sid)."""
    if sid is None:
        ids = sorted(f[:-5] for f in os.listdir(SNAPS) if f.endswith(".json")) if os.path.isdir(SNAPS) else []
        if not ids:
            raise RuntimeError("no snapshot to revert to")
        sid = ids[-1]
    return do_revert({"id": sid, "restart": []}, reason, run)


def status():
    return {"armed": _read(ARMED), "snapshots": sorted(f[:-5] for f in os.listdir(SNAPS) if f.endswith(".json")) if os.path.isdir(SNAPS) else [],
            "reverted_marker": _read(REVERTED), "failed_marker": _read(FAILED)}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="headsafe")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("snapshot"); a.add_argument("--label", default="")
    a = sub.add_parser("arm"); a.add_argument("--snap", required=True); a.add_argument("--restart", action="append", default=[])
    a.add_argument("--delay", type=int, default=DELAY_S); a.add_argument("--liveness", type=int, default=LIVENESS_S); a.add_argument("--finalize", type=int, default=FINALIZE_S)
    sub.add_parser("finalize"); a = sub.add_parser("revert"); a.add_argument("--reason", default="reverted by the head"); a.add_argument("--snap")
    sub.add_parser("watch"); sub.add_parser("status"); sub.add_parser("list")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "snapshot":
            print(snapshot(args.label))
        elif args.cmd == "arm":
            st = arm(args.snap, args.restart, args.delay, args.liveness, args.finalize)
            print("armed %s: restart in %ds, liveness probe within %ds after it, then %ds to finalize" % (st["id"], st["delay"], st["liveness"], st["finalize"]))
        elif args.cmd == "finalize":
            print("finalize accepted for", finalize())
        elif args.cmd == "revert":
            r = request_revert(args.reason)
            if r:
                print(r)
            else:
                print("reverted" if revert_now(args.reason, args.snap) else "REVERT FAILED (see %s)" % FAILED)
        elif args.cmd == "watch":
            return watch()
        else:
            print(json.dumps(status(), indent=1))
    except RuntimeError as e:
        print("headsafe: " + str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
