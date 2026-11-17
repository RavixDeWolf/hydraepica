#!/usr/bin/env python3
"""claude-head PTY bridge (TIK-0362): a Claude Code interactive REPL lives in a tmux session; this daemon is the typed-frame
front of it on a unix socket. tmux owns the PTY, so the REPL SURVIVES this daemon, its clients and a client disconnect; the
daemon is stateless apart from an output offset and can be restarted without touching the session.

Protocol: newline-delimited JSON over a unix socket (mode 0600, dir 0700). One request per line, any number of replies.
  {"t":"status"}                       -> {"t":"state","state":S,"session":N,"alive":bool,"tail":[...last lines...],"since":ts}
  {"t":"send","text":"..."}            -> {"t":"ack","ok":true} (typed into the REPL, then Enter) | {"t":"error","error":...}
  {"t":"key","key":"Enter|Escape|C-c|Up|Down|y|n"} -> {"t":"ack"}   (allow-listed keys only)
  {"t":"tail","lines":N}               -> {"t":"tail","lines":[...]}
  {"t":"subscribe"}                    -> {"t":"ack"} then {"t":"output","lines":[...],"state":S} frames as the pane changes
  {"t":"ensure"}                       -> start the session if it is not running -> {"t":"ack"}
STATES (parsed from the visible pane, see classify_pane): dead | login_required | setup_prompt | trust_prompt | permission_prompt | busy |
idle | unknown. `login_required` is a HUMAN step (claude.ai OAuth on Rav's own plan): the bridge never types credentials.
NOT done here (needs the logged-in REPL): typed frames for tool calls/turn boundaries beyond the pane heuristics, and the
Telegram "status" round trip acceptance test.
"""
import json, os, re, socket, socketserver, subprocess, sys, threading, time

SESSION = os.environ.get("HEAD_SESSION", "claude-head")
CMD = os.environ.get("HEAD_CMD", "claude")
CWD = os.environ.get("HEAD_CWD", os.path.expanduser("~"))
SOCK = os.environ.get("HEAD_SOCKET", os.path.expanduser("~/.local/state/harness/head.sock"))
LOG = os.environ.get("HEAD_LOG", os.path.expanduser("~/.local/state/harness/bridge.log"))
TMUX = os.environ.get("HEAD_TMUX", "tmux")
TMUX_L = os.environ.get("HEAD_TMUX_L", "")        # tmux -L <name>: a private tmux server (tests; never touches the real one)
POLL_S = float(os.environ.get("HEAD_POLL_S", "0.5"))
ALLOWED_KEYS = {"Enter", "Escape", "C-c", "Up", "Down", "Left", "Right", "Tab", "y", "n", "1", "2", "3"}
MAX_SEND = 8000
# Idle sleep (2026-11-15): an idle REPL is put to SLEEP (its tmux session ended, ~0.3 GB RAM freed) after IDLE_SLEEP_S with no
# send/key and an unchanged idle screen. The marker file says it is asleep (not dead): status reports "asleep", and the
# lane wakes it with "ensure" (claude --continue = same conversation) before typing the message. 0 = never sleep.
IDLE_SLEEP_S = float(os.environ.get("HEAD_IDLE_SLEEP_S", "0"))
SLEEP_CHECK_S = float(os.environ.get("HEAD_SLEEP_CHECK_S", "60"))
ASLEEP = os.path.join(os.path.dirname(SOCK), "ASLEEP")
# The idle clock survives a bridge restart. The last activity time is kept in ACTIVE (written at most once a minute);
# a new bridge starts from it (or, the first time, from the head's last hook event), not from "now".
ACTIVE = os.path.join(os.path.dirname(SOCK), "last_active")
ACT_WRITE_S = 60


def _load_act(now=None):
    """-> the last activity time to start the idle clock from: ACTIVE, else events.jsonl mtime, else now. Never in the future."""
    now = time.time() if now is None else now
    d = os.path.dirname(ACTIVE)
    for read in (lambda: float(open(ACTIVE).read().strip()), lambda: os.path.getmtime(os.path.join(d, "events.jsonl"))):
        try:
            return min(read(), now)
        except (OSError, ValueError):
            pass
    return now


_act = {"t": _load_act(), "screen": None, "w": 0.0}


def _mark(now=None):
    """Activity now: reset the idle clock and keep it on disk (at most once per ACT_WRITE_S) so a restart does not reset it."""
    now = time.time() if now is None else now
    _act["t"] = now
    if now - _act.get("w", 0.0) >= ACT_WRITE_S:
        try:
            with open(ACTIVE + ".tmp", "w") as f:
                f.write("%.0f\n" % now)
            os.replace(ACTIVE + ".tmp", ACTIVE)
            _act["w"] = now
        except OSError as e:
            log("could not save the idle clock: %r" % (e,))


# The owner's per-head "Always on" list, set from the stage's Settings > Heads page. A listed head never sleeps.
ALWAYS_ON = os.environ.get("HEAD_ALWAYS_ON") or os.path.join(
    os.path.expanduser(os.environ.get("HARNESS_STATE_DIR", "~/.local/state/harness")), "always_on.json")
LANE = os.environ.get("HEAD_LANE") or "main"
WAKE_WAIT_S = float(os.environ.get("HEAD_WAKE_WAIT_S", "40"))


def always_on():
    """-> True when this lane is on the Always-on list. Unreadable list = True (never sleep on a guess), logged."""
    try:
        with open(ALWAYS_ON) as f:
            return LANE in (json.load(f).get("lanes") or [])
    except FileNotFoundError:
        return False
    except Exception as e:
        log("always_on list unreadable (%r): staying awake" % (e,))
        return True


def log(m):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (time.strftime("%F %T"), m))
    except Exception:
        pass


def tmux(*a, timeout=8):
    pre = ["-L", TMUX_L] if TMUX_L else []
    r = subprocess.run([TMUX, *pre, *a], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def alive():
    try:
        return tmux("has-session", "-t", SESSION)[0] == 0
    except Exception as e:
        log("has-session failed: %r" % (e,))
        return False


def ensure():
    """Start the detached session if absent. -> (started, detail). A start failure is returned, never swallowed."""
    if alive():
        return False, "already running"
    if os.path.exists(ASLEEP):
        try: os.unlink(ASLEEP)
        except OSError as e: log("WAKE could not remove the ASLEEP marker: %r" % (e,))
        log("WAKE: asleep session started again (claude --continue)")
    _mark(); _act["screen"] = None
    os.makedirs(CWD, exist_ok=True)
    # TIK-1082: a second head (HEAD_LANE=techsup) passes its lane and state dir into the REPL, so its hooks and
    # tg.py write to ITS folder and thread; tmux would otherwise hand it the tmux server's environment.
    env = [a for k, v in lane_env(os.environ).items() for a in ("-e", f"{k}={v}")]
    rc, out, err = tmux("new-session", "-d", "-s", SESSION, *env, "-x", "200", "-y", "50", "-c", CWD, CMD)
    if rc != 0:
        log("new-session FAILED rc=%d %s" % (rc, err.strip()[:200]))
        return False, "new-session failed: %s" % err.strip()[:200]
    log("session %s started: %s" % (SESSION, CMD))
    return True, "started"


def lane_env(environ):
    """Env passed into a head's REPL. A lane head also gets its OWN headsafe probe route (HEADSAFE_SOCK/HEADSAFE_EVENTS,
    read by safety/headsafe.py), so a lane's self-edit probe goes to that lane, not to the main head. (2026-11-16: a lane's
    probe landed on the main head, went unanswered, and its good deploy was reverted.) The main head keeps the defaults."""
    out = {k: environ[k] for k in ("HEAD_LANE", "HEAD_STATE_DIR") if environ.get(k)}
    sd, sock = environ.get("HEAD_STATE_DIR"), environ.get("HEAD_SOCKET")
    if sd and sock and (environ.get("HEAD_LANE") or "main") != "main":
        out["HEADSAFE_SOCK"] = sock
        out["HEADSAFE_EVENTS"] = os.path.join(sd, "events.jsonl")
    return out


def capture(lines=60, ansi=False):
    rc, out, err = tmux("capture-pane", "-p", *(["-e"] if ansi else []), "-t", SESSION, "-S", "-%d" % lines)
    return out.rstrip("\n").split("\n") if rc == 0 else []


_LOGIN = re.compile(r"(please run /login|invalid api key|not logged in|log in to continue|select login method|paste code here|open this url|oauth)", re.I)
_SETUP = re.compile(r"(choose the text style|syntax theme|light mode|dark mode|let's get started|select a theme)", re.I)
_TRUST = re.compile(r"(do you trust the files in this folder|trust this folder|yes, proceed|trust the files)", re.I)
# TIK-1416 (2026-11-01 19:01): the old pattern ran across the whole pane with re.S, so a REPLY that merely mentioned
# "1. Yes / 2. ..." (techsup explaining the bug) and the echo of a typed answer ("❯ 1. Yes / 2. Public / 3. Yes short.")
# both read as a live box, the state stuck on permission_prompt while idle, and the card was re-posted every check.
# A real box is line-shaped: a question line on its own, or "1. Yes" at a line start with "2." at the start of one of the
# next few lines. Anchored per line (re.M, no re.S); prose with those words mid-line no longer matches.
_PERM = re.compile(r"(^\s*[│|]?\s*(do you want to (proceed|make this edit|create|run)|allow this)[^\n]{0,80}$"
                   r"|^\s*[│|]?\s*[❯>]?\s*1\.\s*yes\b[^\n]*\n(?:[^\n]*\n){0,4}?\s*[│|]?\s*[❯>]?\s*2\.\s)", re.I | re.M)
# BUSY = something RUNNING right now: the footer's "esc to interrupt", or a spinner line still counting ("✶ Cooking… (1m 7s").
# NOT a bare glyph or word: a finished turn leaves "✻ Worked for 2m" on screen and the reply text can say "thinking",
# and that read as busy for 5 idle minutes on 2026-10-24 (TIK-0361) -- every wait-for-idle hung.
_BUSY = re.compile(r"(esc to interrupt|^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✻✶✳✢·*]\s*\w+…\s*\(\d)", re.I | re.M)
_IDLE = re.compile(r"(^\s*[>│❯]\s*$|^\s*[>❯]\s|\? for shortcuts|bypass permissions|accept edits|auto mode on|shift\+tab to cycle)", re.M)


def classify_pane(lines, is_alive=True):
    """Pure. -> state string. Order matters: a login or trust gate beats everything, a permission prompt beats busy."""
    if not is_alive:
        return "asleep" if os.path.exists(ASLEEP) else "dead"
    text = "\n".join(lines[-40:])
    if _LOGIN.search(text):
        return "login_required"
    if _TRUST.search(text):
        return "trust_prompt"
    if _SETUP.search(text):
        return "setup_prompt"            # first-run theme picker: observed on the real claude 2.1.195 REPL, no login involved
    if _PERM.search(text):
        return "permission_prompt"
    if _BUSY.search("\n".join(lines[-8:])):
        return "busy"
    if _IDLE.search("\n".join(lines[-8:])):
        return "idle"
    return "unknown"


def sleep_check(now=None):
    """One sleeper tick. -> "slept" | reason it stayed awake. Sleeps only an IDLE screen that has not changed and saw no
    send/key for IDLE_SLEEP_S; anything else (busy, a prompt, unknown) keeps it awake. Every sleep is logged."""
    now = time.time() if now is None else now
    if IDLE_SLEEP_S <= 0:
        return "disabled"
    if always_on():
        return "always on"
    if not alive():
        return "not running"
    lines = capture(60)
    screen = [l for l in lines if l.strip()]
    if _act["screen"] is None:                      # first look after a (re)start: not activity, keep the saved clock
        _act["screen"] = screen
    elif screen != _act["screen"]:
        _act["screen"] = screen; _mark(now)
        return "screen changed"
    st = classify_pane(lines, True)
    if st != "idle":
        return "state " + st
    idle = now - _act["t"]
    if idle < IDLE_SLEEP_S:
        return "idle %d s" % idle
    busy_kids = working_children()
    if busy_kids:
        return "background work: " + ", ".join(busy_kids[:3])
    try:
        with open(ASLEEP, "w") as f:
            f.write("%s asleep after %d s idle; the next message wakes it\n" % (time.strftime("%F %T"), idle))
    except OSError as e:
        log("SLEEP skipped: cannot write the ASLEEP marker (%r)" % (e,))
        return "marker failed"
    rc, _, err = tmux("kill-session", "-t", SESSION)
    if rc != 0:
        log("SLEEP FAILED: kill-session rc=%d %s" % (rc, err.strip()[:120]))
        try: os.unlink(ASLEEP)
        except OSError: pass
        return "kill failed"
    log("SLEEP: %s idle %d min -> session ended to free memory; the next message wakes it" % (SESSION, idle // 60))
    return "slept"


# Long-lived helpers every REPL keeps: MCP servers and the shell wrapper. Anything else under the REPL (a background
# job, a BOB, a render) is real work, and a session with real work is never put to sleep.
_HELPER = re.compile(r"(npm exec|npx|/node_modules/|mcp|desktop-commander|playwright|^node |^sh -c|^/bin/sh|^bash$|^-?bash|claude( |$))")


def working_children():
    """-> short names of non-helper processes under the REPL's pane (empty = nothing running but helpers)."""
    rc, out, _ = tmux("list-panes", "-t", SESSION, "-F", "#{pane_pid}")
    if rc != 0 or not out.strip():
        return []
    try:
        ps = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True, timeout=8).stdout
    except Exception as e:
        log("working_children: ps failed %r -> treating as busy" % (e,))
        return ["ps-failed"]
    kids, rows = {}, []
    for ln in ps.splitlines():
        parts = ln.split(None, 2)
        if len(parts) >= 2:
            rows.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    for pid, ppid, args in rows:
        kids.setdefault(ppid, []).append((pid, args))
    todo, found = [out.split()[0]], []
    while todo:
        for pid, args in kids.get(todo.pop(), []):
            todo.append(pid)
            if not _HELPER.search(args):
                found.append(args.split()[0].rsplit("/", 1)[-1][:30] if args else pid)
    return found


def _sleeper():
    while True:
        time.sleep(SLEEP_CHECK_S)
        try:
            sleep_check()
        except Exception as e:
            log("sleeper error: %r" % (e,))


def status(tail_n=12):
    a = alive()
    lines = capture(60) if a else []
    return {"t": "state", "state": classify_pane(lines, a), "session": SESSION, "alive": a,
            "tail": [l for l in lines if l.strip()][-tail_n:], "since": time.time()}


def send_text(text):
    _mark()
    if not alive():
        return False, "session not running (use ensure)"
    if not isinstance(text, str) or not text.strip():
        return False, "empty text"
    if len(text) > MAX_SEND:
        return False, "text longer than %d chars" % MAX_SEND
    # 2026-10-27 01:19 (Rav's "?"): a first character of ? ! or # is a REPL key, not text. A bare "?" opened the
    # shortcuts panel and the message was never sent; "!" would start shell mode. A leading space types them as text
    # (reproduced in a throwaway session: " ?" reached the model). "/" is left alone: slash commands are on purpose.
    if text[:1] in "?!#":
        text = " " + text
    # -l = literal: the text is typed as characters, never interpreted as a tmux key name.
    for chunk in text.split("\n"):
        rc, _, err = tmux("send-keys", "-t", SESSION, "-l", chunk)
        if rc != 0:
            return False, "send-keys failed: %s" % err.strip()[:120]
        rc, _, err = tmux("send-keys", "-t", SESSION, "Enter")
        if rc != 0:
            return False, "send-keys Enter failed: %s" % err.strip()[:120]
    return _confirm_submitted()


_RULE = re.compile(r"^\s*─{10,}\s*$")
_RULE_START = re.compile(r"^\s*─{10,}")          # a rule may end in a cut multibyte character at the pane edge


# Claude Code's own hints shown INSIDE an empty input box. 2026-10-25 (TIK-1044): while a turn runs, a typed message
# is QUEUED (shown above the box with "ctrl+x ctrl+s to send now") and the empty box reads "❯ Press up to edit queued
# messages". That hint was read as unsent text: 4 false "Couldn't reach the main head" + the lane re-sent the message as
# "refused", so Rav was told it failed and the head got it twice. Reproduced in a throwaway session; fixture in tests.
_PLACEHOLDERS = (re.compile(r"^Press up to edit queued messages\s*$"), re.compile(r'^Try "[^"]*"\s*$'))


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_DIM = re.compile(r"\x1b\[2m.*?(?=\x1b\[(?:0|22)?m|$)")


def _visible_text(line):
    """A line as typed text: Claude Code draws its suggestions and hints DIM inside the empty box, so with colour
    captured (capture(..., ansi=True)) the dim runs are dropped first. Plain lines pass through unchanged."""
    return _ANSI.sub("", _DIM.sub("", line))


def input_pending(lines):
    """True when the REPL's input box still holds text: the last "❯ ..." line framed by the box's rules.
    2026-10-24 16:35 (TIK-1026): a multi-line prompt typed fast is taken as a PASTE ("[Pasted text #4 +8 lines]"),
    its Enters become newlines inside it, and the last Enter was eaten too -- the video's prompt sat unsent for
    6 minutes while the lane waited. No box on screen (another REPL, a menu) = not pending.
    19:56 (TIK-1025): the box can hold SEVERAL lines (21 image chips wrapped to two) and its rules can end in a cut
    character; the old one-line check saw no box and called a stuck prompt sent. Now: the text between the last two
    rule lines, however many lines that is."""
    rules = [i for i, l in enumerate(lines) if _RULE_START.match(_ANSI.sub("", l))]
    if len(rules) < 2:
        return False
    a, b = rules[-2], rules[-1]
    box = [_visible_text(l).strip() for l in lines[a + 1:b] if _visible_text(l).strip()]
    if not (box and box[0].startswith("❯")):
        return False
    text = " ".join(box)[1:].strip()
    return bool(text) and not any(p.match(text) for p in _PLACEHOLDERS)


def _confirm_submitted(tries=3, wait=0.8, cap=None, sleep=time.sleep, key=None):
    cap = cap or (lambda: capture(15, ansi=True))
    key = key or (lambda: tmux("send-keys", "-t", SESSION, "Enter"))
    for n in range(tries + 1):
        sleep(wait)
        screen = cap()
        if not input_pending(screen):
            q = [l for l in screen if "to send now" in _ANSI.sub("", l)]
            if q:                                          # queued behind a running turn: that IS delivered
                log("send: queued behind a running turn (TIK-1044), counted as sent; screen rows=%d" % len(screen))
            if n:
                log("send: input was still in the box, Enter re-sent %d time(s), now submitted" % n)
            return True, "sent" if not n else "sent (Enter re-sent %dx)" % n
        if n < tries:
            key()
    log("send FAILED: input still in the box after %d extra Enter(s)" % tries)
    return False, "typed, but the REPL did not take it: input still in the box after %d Enter(s)" % tries


def send_key(key):
    if key not in ALLOWED_KEYS:
        return False, "key %r not allowed" % (key,)
    _mark()
    if not alive():
        return False, "session not running"
    rc, _, err = tmux("send-keys", "-t", SESSION, key)
    return rc == 0, "ok" if rc == 0 else err.strip()[:120]


def wake_for(t, sleep=time.sleep, now=time.time):
    """Anything that types into an ASLEEP head (the lane, a handoff, a ticket poke, standdown, a script on the
    socket) wakes it first, then waits for the REPL to be ready. -> None when ready/not asleep, else the reason."""
    if not os.path.exists(ASLEEP) or alive():
        return None
    log("WAKE requested by a %r on the socket" % t)
    started, detail = ensure()
    t0 = now()
    while now() - t0 < WAKE_WAIT_S:
        st = classify_pane(capture(60), alive())
        if st in ("idle", "busy"):
            log("WAKE ready (%s) after %d s" % (st, now() - t0))
            return None
        sleep(2)
    log("WAKE FAILED: not ready after %d s (ensure: %s)" % (WAKE_WAIT_S, detail))
    return "woke but not ready after %d s (%s)" % (WAKE_WAIT_S, detail)


def handle(req):
    t = req.get("t")
    if t in ("send", "key"):
        why = wake_for(t)
        if why:
            return [{"t": "error", "error": why}]
    if t == "status":
        return [status()]
    if t == "ensure":
        started, detail = ensure()
        return [{"t": "ack", "ok": detail in ("started", "already running"), "detail": detail, "started": started}]
    if t == "send":
        ok, d = send_text(req.get("text"))
        return [{"t": "ack", "ok": True, "detail": d}] if ok else [{"t": "error", "error": d}]
    if t == "key":
        ok, d = send_key(req.get("key"))
        return [{"t": "ack", "ok": True, "detail": d}] if ok else [{"t": "error", "error": d}]
    if t == "tail":
        try:
            n = max(1, min(200, int(req.get("lines", 40))))
        except (TypeError, ValueError):
            return [{"t": "error", "error": "lines must be an integer"}]
        return [{"t": "tail", "lines": capture(n)}]
    return [{"t": "error", "error": "unknown request type %r" % (t,)}]


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for raw in self.rfile:
            try:
                req = json.loads(raw.decode("utf-8", "replace"))
                if not isinstance(req, dict):
                    raise ValueError("request must be a JSON object")
            except Exception as e:
                self._w({"t": "error", "error": "bad request: %s" % e})
                continue
            if req.get("t") == "subscribe":
                self._w({"t": "ack", "ok": True})
                return self._stream()
            try:
                for fr in handle(req):
                    self._w(fr)
            except Exception as e:
                log("handler error: %r" % (e,))
                self._w({"t": "error", "error": "internal: %s" % type(e).__name__})

    def _w(self, obj):
        self.wfile.write((json.dumps(obj) + "\n").encode()); self.wfile.flush()

    def _stream(self):
        last = None
        try:
            while True:
                lines = [l for l in capture(60) if l.strip()]
                if lines != last:
                    last = lines
                    self._w({"t": "output", "lines": lines[-30:], "state": classify_pane(lines, alive())})
                time.sleep(POLL_S)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve():
    d = os.path.dirname(SOCK)
    os.makedirs(d, exist_ok=True); os.chmod(d, 0o700)
    if os.path.exists(SOCK):
        try:
            c = socket.socket(socket.AF_UNIX); c.settimeout(1); c.connect(SOCK); c.close()
            print("another bridge is already serving %s" % SOCK, file=sys.stderr); return 2
        except OSError:
            os.unlink(SOCK)
    import signal
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(SystemExit(0)))   # run the finally: remove the socket
    srv = Server(SOCK, Handler)
    os.chmod(SOCK, 0o600)
    log("bridge up on %s (session=%s cmd=%s idle_sleep=%ds)" % (SOCK, SESSION, CMD, IDLE_SLEEP_S))
    if os.path.exists(ASLEEP) and not alive():
        log("boot: session is ASLEEP; it stays asleep until a message wakes it")
    else:
        ensure_started, detail = ensure()
        log("ensure at boot: %s" % detail)
    threading.Thread(target=_sleeper, daemon=True).start()
    try:
        srv.serve_forever()
    finally:
        try: os.unlink(SOCK)
        except OSError: pass
    return 0


if __name__ == "__main__":
    sys.exit(serve())
