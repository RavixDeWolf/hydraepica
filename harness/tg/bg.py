"""bg.py - run a long job so Rav can see it, without anyone remembering to (Rav 2026-10-27: "make that more automatic").

  python3 bg.py --kind video --name spider-avatar -- COMMAND ARGS...
  python3 bg.py --watch TASK.output --name "..." --kind video     follow a job already running (the head hook does this
                                                                   for every background task that runs past 60 s)

While COMMAND runs:
  - Telegram shows the matching indicator ("sending video..." / "sending photo..." / "recording voice..." / typing),
    renewed every 4 s;
  - one pinned progress bar (tg.py progress), edited at most every 20 s. Progress comes from the job's OWN output
    when it says (a "PROGRESS 42" or "PROGRESS 12/300" line, "42%", or "frame 120/300"), otherwise from elapsed time against how long
    the same --name took before (median of past runs). With neither, the bar shows elapsed time only: no made-up ETA.
When it ends: the bar is removed and ONE short line is sent: done (with the time) or FAILED (exit code + last line).
Every run is appended to <state_dir>/bg_runs.jsonl (Iron Rule 12). The job's own output passes through.
Exit code = the job's exit code."""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tg  # noqa: E402

RUNS = os.path.expanduser(os.environ.get("BG_RUNS", os.path.join(os.environ.get("HEAD_STATE_DIR", "~/.local/state/harness"), "bg_runs.jsonl")))
ACTION = {"video": "upload_video", "photo": "upload_photo", "audio": "record_voice", "work": "typing"}
PCT = [re.compile(r"PROGRESS\s+(\d+)\s*/\s*(\d+)"), re.compile(r"PROGRESS\s+(\d+(?:\.\d+)?)"), re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s?%"),
       re.compile(r"frame\s+(\d+)\s*/\s*(\d+)", re.I)]


def redirect_logs(cmd, cwd=None):
    """files a shell command sends its output to (`> f`, `>> f`, `| tee f`), so a watcher can read their progress"""
    import shlex
    out = []
    for m in re.finditer(r"(?:(?<![0-9&])>>?|\btee(?:\s+-a)?)\s*([^\s;&|<>]+)", cmd):
        f = m.group(1).strip("'\"")
        if f.startswith("/dev/") or f.startswith("&"):
            continue
        f = os.path.expanduser(f)
        if not os.path.isabs(f):
            cd = re.findall(r"\bcd\s+([^\s;&|]+)", cmd[:m.start()])
            base = os.path.expanduser(cd[-1].strip("'\"")) if cd else (cwd or os.getcwd())
            f = os.path.join(base, f)
        if f not in out:
            out.append(f)
    return out


def parse_pct(line):
    """a percentage the job printed about itself, or None"""
    for rx in PCT:
        m = rx.search(line)
        if m:
            v = float(m.group(1)) / float(m.group(2)) * 100 if rx.groups == 2 else float(m.group(1))
            if 0 <= v <= 100:
                return v
    return None


def typical(name):
    """median duration of past successful runs of this name, or None"""
    try:
        rows = [json.loads(ln[ln.index("{"):]) for ln in open(RUNS) if "{" in ln]   # "TS {json}" (ship_check proof)
        ds = [r["secs"] for r in rows if r.get("name") == name and r.get("rc") == 0]
    except OSError:
        return None
    if ds:
        return statistics.median(ds[-9:])
    # no run with this exact name (the hook names a job by its free-text description): use the time the name itself
    # gives ("~55 min", "about 50 minutes", "(~2h)") (Rav 2026-10-30: "We've done several of these by now... No eta?")
    m = re.search(r"(?:~|about |approx\.? )\s*(\d+(?:\.\d+)?)\s*(h|hours?|m|min|mins|minutes?)\b", name, re.I)
    if m:
        return float(m.group(1)) * (3600 if m.group(2).lower().startswith("h") else 60)
    return None


def fmt(s):
    s = int(s)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def run(kind, name, cmd, call=None, tick=20.0, beat=4.0):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    def events():
        for line in p.stdout:
            sys.stdout.write(line); sys.stdout.flush()
            yield ("line", line)
        yield ("rc", p.wait())
    return _monitor(kind, name, events(), call, tick, beat, cmd=cmd)


def _monitor(kind, name, events, call=None, tick=20.0, beat=4.0, cmd=None, t_start=None):
    base = {"chat_id": tg.OWNER, "message_thread_id": tg.default_thread()}
    key = "bg-" + re.sub(r"[^a-z0-9]+", "-", name.lower())[:30]
    est, t0 = typical(name), t_start or time.time()
    state = {"pct": None, "last": ""}
    stop = threading.Event()

    def heartbeat():                               # the indicator dies after ~5 s, so it is renewed
        while not stop.is_set():
            try:
                tg.api("sendChatAction", call, action=ACTION[kind], **base)
            except Exception as e:
                print(f"bg.py: chat action failed ({e})", file=sys.stderr)
            stop.wait(beat)

    def bar():
        el = time.time() - t0
        if state["pct"] is not None:
            pct = state["pct"]; left = el * (100 - pct) / pct if pct > 1 else None
        elif est:
            pct = min(97, el / est * 100); left = max(est - el, 0)
        else:
            pct, left = "busy", None                # no progress, no history: say so, never a frozen 0%
        eta = fmt(left) if left is not None else ""   # countdown, re-measured every redraw (Rav 2026-11-02)
        label = f"{fmt(el)} · {name}" if left is None else name   # elapsed first: the pin preview cuts the end off
        try:
            tg.run(["progress", key, pct if pct == "busy" else str(int(pct)), label, eta], call=call)
        except Exception as e:
            print(f"bg.py: progress bar failed ({e})", file=sys.stderr)

    def ticker():                                  # 2026-10-28: a quiet job's bar froze (it only redrew on a new line)
        while not stop.wait(tick):
            bar()

    hb = threading.Thread(target=heartbeat, daemon=True); hb.start()
    bar()
    tk = threading.Thread(target=ticker, daemon=True); tk.start()
    rc = None
    # 2026-11-18: a watcher that is itself stopped (SIGTERM/SIGHUP: a restart, a logout) must still take its card
    # down. The signal becomes SystemExit, the loop exits, and the bar is removed below with an honest result line.
    import signal
    def _killed(signum, _frame):
        raise SystemExit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            signal.signal(sig, _killed)
        except ValueError:                                 # not the main thread (tests): nothing to install
            pass
    try:
        for ev, val in events:
            if ev == "rc":
                rc = val; break
            line = val
            if line.strip():
                state["last"] = line.strip()
            v = parse_pct(line)
            if v is not None:
                state["pct"] = v
    except SystemExit as e:
        rc = "killed"
        print(f"bg.py: stopped by signal (exit {e.code}); removing the bar", file=sys.stderr)
    stop.set(); hb.join(1); tk.join(1)
    secs = time.time() - t0
    try:
        tg.run(["progress", key, "done"], call=call)
    except Exception as e:
        print(f"bg.py: could not remove the bar ({e})", file=sys.stderr)
    msg = (f"✅ {name} done in {fmt(secs)}" if rc == 0 else
           f"⏹ {name}: stopped after {fmt(secs)} (the job ended without an exit code)" if rc == "stopped" else
           f"⏹ {name}: my progress watcher was stopped after {fmt(secs)}; the job itself may still be running" if rc == "killed" else
           f"⚠️ {name}: lost track after {fmt(secs)} (still running?)" if rc is None else
           f"❌ {name} FAILED (exit {rc}) after {fmt(secs)}: {state['last'][:160]}")
    try:
        tg.run(["send", msg], call=call)
    except Exception as e:
        print(f"bg.py: could not post the result ({e})", file=sys.stderr)
    os.makedirs(os.path.dirname(RUNS), exist_ok=True)
    with open(RUNS, "a") as f:
        f.write(time.strftime("%FT%T ") + json.dumps({"name": name, "kind": kind, "rc": rc, "secs": round(secs, 1),
                            "cmd": (cmd or [])[:6]}) + "\n")
    return rc


KIND_WORDS = [("video", ("video", "mp4", "motion.py", "avatar", "itk_", "ffmpeg", "render.sh", "assemble")),
              ("photo", ("jedit", "image", "photo", ".jpg", ".png", "layout.py", "genfill")),
              ("audio", ("audio", ".wav", "piper", "voice", "loudnorm"))]
EXIT = re.compile(r"^\[exited with code (-?\d+)\]\s*$", re.M)


def writer_alive(path):
    """True when some process still has `path` open (the job writing it). Unknown (no /proc) counts as alive."""
    try:
        target = os.path.realpath(path)
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            d = f"/proc/{pid}/fd"
            try:
                for fd in os.listdir(d):
                    if os.path.realpath(os.path.join(d, fd)) == target:
                        return True
            except OSError:
                continue
        return False
    except OSError:
        return True


def kind_of(text):
    t = text.lower()
    for k, words in KIND_WORDS:
        if any(w in t for w in words):
            return k
    return "work"


def watch(path, name, kind, call=None, quiet_s=60.0, tick=20.0, beat=4.0, poll=2.0, max_s=6 * 3600, also=(), stop_s=30.0):
    """follow a job someone else started (a background task writing to `path`, ending with '[exited with code N]').
    Silent if it finishes within quiet_s: short jobs need no bar. Then the same indicator + bar + one result line."""
    t0 = time.time()
    def read():
        try:
            return open(path, errors="replace").read()
        except OSError:
            return ""
    while time.time() - t0 < quiet_s:
        if EXIT.search(read()):
            return None
        time.sleep(poll)
    seen = 0; gone = {"n": 0}
    side = {f: 0 for f in also}                   # the job's own log (`> render.log`): progress only, never the exit
    def follow():
        nonlocal seen
        while time.time() - t0 < max_s:
            for f in side:
                try:
                    t = open(f, errors="replace").read()
                except OSError:
                    continue
                for ln in t[side[f]:].splitlines(True):
                    yield ("line", ln)
                side[f] = len(t)
            txt = read(); new = txt[seen:]; seen = len(txt)
            if not new and not EXIT.search(txt) and time.time() - t0 > quiet_s and not writer_alive(path):
                gone["n"] += 1
                if gone["n"] * poll >= stop_s:          # stop_s with nobody holding the file open: the job was stopped
                    yield ("rc", "stopped"); return
            else:
                gone["n"] = 0
            for ln in new.splitlines(True):
                m = EXIT.match(ln.strip())
                if m:
                    yield ("rc", int(m.group(1))); return
                yield ("line", ln)
            time.sleep(poll)
        yield ("rc", None)
    return _monitor(kind, name, follow(), call, tick, beat, t_start=t0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", choices=sorted(ACTION), default="work")
    ap.add_argument("--name", required=True, help="same name = same kind of job: its past runs give the ETA")
    ap.add_argument("--watch", metavar="OUTPUT_FILE", help="follow an already-running background task's output file")
    ap.add_argument("--also", action="append", default=[], metavar="LOG",
                    help="with --watch: also read progress from this file (a job that writes to its own log)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    if a.watch:
        watch(a.watch, a.name, a.kind, also=a.also); return
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("give the command after --")
    sys.exit(run(a.kind, a.name, cmd))


if __name__ == "__main__":
    main()
