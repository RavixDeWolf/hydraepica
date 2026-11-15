#!/usr/bin/env python3
"""new_lane.py - build a Claude Code head lane for one of the owner's Telegram topics.

The one way a topic lane is made, by hand or by the gateway when Rav creates a new topic (head_lane.topic_created).
Every step the hand-built lanes taught us (TIK-1082/1126/1129/1131), in order:
  1. lane name: slug of the topic title, collision-safe (never an existing lane, folder or unit)
  2. <work_root>/<lane>/.claude/settings.json copied from the template BEFORE the head first starts
     (without it the head fires no hook events and its replies never reach Telegram - the sec lane, 2026-10-29)
  3. <work_root>/<lane>/CLAUDE.md: generic; the head asks the owner what the thread is for and writes it in
  4. state dir + tg_threads.json {uid: thread}
  5. ~/.claude.json: folder trusted + external imports approved (else the first message is eaten by a dialog)
  6. systemctl --user enable --now harness-head@<lane>
  7. any startup dialog still on screen is answered there (trust / imports: Yes; "Teach auto mode": don't show)
  8. bridge test: a prompt must come back with a Stop event in events.jsonl
  9. lanes.json gets the lane LAST, so the gateway never routes to a head that is not ready
Every step is logged to <state_dir>/new_lane.log; a failure stops and says which step.

  new_lane.py create --title "Topic name" --thread 1234567 [--uid <user id>] [--lane slug] [--json]
  new_lane.py slug "Topic name"            print the lane name it would get
  new_lane.py retitle --lane L --title T   only the title the head is told about
  new_lane.py rename --lane L --title T [--to new] [--thread N]   move the lane to a new name, conversation kept.
     Run it through /renamehead in the topic: the gateway has to swap its loaded lane too.
Output with --json: one line {"ok": bool, "lane": ..., "step": ..., "why": ...}. Exit 0 only when ok."""
import argparse, fcntl, json, os, re, shutil, socket, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

OWNER = config.OWNER_ID
HOME = os.path.expanduser(os.environ.get("NEW_LANE_HOME", "~"))
_SANDBOX = "NEW_LANE_HOME" in os.environ    # tests: every path under that one folder
BASE = os.path.join(HOME, "state") if _SANDBOX else config.STATE_DIR
WORK = os.path.join(HOME, "heads") if _SANDBOX else config.WORK_ROOT                   # each head's working folder is WORK/<lane>; the main head's is WORK/main
LANES = os.path.join(BASE, "lanes.json")
LOG = os.path.join(BASE, "new_lane.log")
TEMPLATES = os.environ.get("NEW_LANE_TEMPLATES") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")   # owned by the maker, not by any lane
TEMPLATE = os.path.join(TEMPLATES, "head_settings.json")
TEMPLATE_LOCAL = os.path.join(TEMPLATES, "head_settings.local.json")
SKILLS = os.environ.get("NEW_LANE_SKILLS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
RESERVED = {"main", "head", "claude", "base", "inbox", "perm"}
READY_S, ANSWER_S = 90, 120
MADE_BY = "made_by_new_lane.json"          # provenance: only a lane this script built may be removed by it
ARCHIVE = os.path.join(BASE, "archive", "head_lanes")   # a removed lane's files + transcripts as markdown
PROJECTS = os.path.join(HOME, ".claude", "projects")


def lane_dir(lane):
    return os.path.join(WORK, lane)


def project_dir(lane):
    """Claude Code keeps a folder's sessions under ~/.claude/projects/<the folder's path with / and . as ->."""
    return os.path.join(PROJECTS, re.sub(r"[/.]", "-", os.path.abspath(lane_dir(lane))))


def log(event, **kw):
    os.makedirs(BASE, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")                  # leading stamp: ship_check ties a proof line to a build
    with open(LOG, "a") as f:
        f.write(ts + " " + json.dumps({"ts": ts, "event": event, **kw}) + "\n")


def log_rows():
    """The log as dicts; lines are "TS {json}" (older lines are bare JSON)."""
    try:
        with open(LOG) as f:
            return [json.loads(l[l.index("{"):]) for l in f if "{" in l]
    except FileNotFoundError:
        return []


class Fail(Exception):
    def __init__(self, step, why):
        super().__init__(f"{step}: {why}"); self.step, self.why = step, why


def _jload(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _jsave(p, obj):
    tmp = p + ".tmp-new_lane"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, p)


def lanes():
    return [str(x) for x in _jload(LANES, [])]


def taken(name):
    """True when the name is already a lane, a head folder, a state dir or reserved."""
    return (name in RESERVED or name in lanes() or os.path.exists(lane_dir(name))
            or os.path.exists(os.path.join(BASE, name)))


def slug(title, is_taken=taken, width=16):
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:width].strip("-")
    if not s or not s[0].isalpha():
        s = ("t-" + s if s else "topic")[:16].strip("-")
    name, n = s, 2
    while is_taken(name):
        name = f"{s}-{n}"; n += 1
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,20}", name):      # the gateway's own check (head_lane OTHERS)
        raise Fail("name", f"no usable lane name from {title!r}")
    return name


def thread_owner(tid):
    """The lane already answering this thread, or None. 'main' = the main head itself."""
    for name in ["main"] + lanes():
        d = BASE if name == "main" else os.path.join(BASE, name)
        if tid in [int(v) for v in _jload(os.path.join(d, "tg_threads.json"), {}).values() if v is not None]:
            return name
    return None


CLAUDE_MD = """# CLAUDE.md — {agent}, "{title}" head (made by new_lane.py)
@{main_md}

---

## THIS head: "{title}" (overrides the thread details above)
You are {agent}, the same agent as the main head, running as a separate head behind the owner's Telegram topic
**"{title}"** (thread {thread}, lane `{lane}`, created {date}). Your replies, `tg.py` sends, buttons, pins and progress
bars go to THIS topic automatically (HEAD_STATE_DIR={state}). Memory is shared with the other heads; this
conversation is not.

**What this thread is for:** not known yet. The owner made the topic; its name is all there is. On your FIRST turn,
after answering what they asked, ask in one line what this thread is for (unless the message already says). Then write
the answer here, replacing this paragraph, with the date and their words, and keep to it from then on.

- Other heads may be working at the same time: never restart the gateway, other harness-head units or another
  head's tmux without saying so first; check `git status` before editing shared code.
- Hand work to another head: `python3 {handoff} --to <lane> "..."` (any lane in lanes.json; `main` = the main head).
"""


def link_skills(d, src=None):
    """Symlink every harness skill into <d>/.claude/skills/<name>, so a fix to a skill reaches every head.
    An existing entry of the same name is left alone (a head may override a skill). -> [names linked]."""
    src = src or SKILLS
    dst = os.path.join(d, ".claude", "skills")
    os.makedirs(dst, exist_ok=True)
    done = []
    for name in sorted(os.listdir(src)) if os.path.isdir(src) else []:
        if not os.path.isfile(os.path.join(src, name, "SKILL.md")):
            continue
        link = os.path.join(dst, name)
        if os.path.lexists(link):
            continue
        os.symlink(os.path.join(src, name), link)
        done.append(name)
    return done


def write_files(lane, title, tid, uid):
    for t in (TEMPLATE, TEMPLATE_LOCAL):
        if not os.path.isfile(t):
            raise Fail("settings", f"template {t} missing")
    d = lane_dir(lane)
    os.makedirs(os.path.join(d, ".claude"), exist_ok=True)
    shutil.copyfile(TEMPLATE, os.path.join(d, ".claude", "settings.json"))
    shutil.copyfile(TEMPLATE_LOCAL, os.path.join(d, ".claude", "settings.local.json"))
    link_skills(d)
    with open(os.path.join(d, "CLAUDE.md"), "w") as f:
        f.write(CLAUDE_MD.format(agent=config.AGENT_NAME, title=title.replace('"', "'"), thread=tid, lane=lane,
                                 date=time.strftime("%Y-%m-%d"), main_md=os.path.join(lane_dir("main"), "CLAUDE.md"),
                                 state=os.path.join(BASE, lane), handoff=os.path.join(os.path.dirname(os.path.abspath(__file__)), "tg", "handoff.py")))
    st = os.path.join(BASE, lane)
    os.makedirs(st, exist_ok=True)
    _jsave(os.path.join(st, "tg_threads.json"), {str(uid): tid})
    _jsave(os.path.join(st, MADE_BY), {"by": "new_lane.py", "title": title, "thread": tid, "uid": uid,
                                       "created": time.strftime("%Y-%m-%d %H:%M:%S")})
    log("files", lane=lane, folder=d)


def approve_folder(lane):
    """Trust + external imports for <work_root>/<lane>, set in ~/.claude.json before the head first starts.
    Locked and re-read at write time: running Claude Code sessions rewrite this file too."""
    path = lane_dir(lane)
    with open(CLAUDE_JSON + ".new_lane.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        d = _jload(CLAUDE_JSON, None)
        if d is None:
            raise Fail("approve", f"{CLAUDE_JSON} missing")
        p = d.setdefault("projects", {}).setdefault(path, {})
        p.update(hasTrustDialogAccepted=True, hasClaudeMdExternalIncludesApproved=True,
                 hasClaudeMdExternalIncludesWarningShown=True)
        _jsave(CLAUDE_JSON, d)
    log("approved", lane=lane)


def _sh(*cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def start_unit(lane, sh=_sh):
    r = sh("systemctl", "--user", "enable", "--now", f"harness-head@{lane}.service")
    if r.returncode:
        raise Fail("unit", (r.stderr or r.stdout).strip()[:200])
    log("unit_started", lane=lane)


# the startup dialogs we have met, what to pick, in order of preference
DIALOGS = [
    ("trust", re.compile(r"trust this folder|trust the files in this folder", re.I), [r"Yes, I trust", r"^\s*Yes\b"]),
    ("imports", re.compile(r"external imports", re.I), [r"Yes, allow external imports"]),
    ("teach", re.compile(r"Teach auto mode", re.I), [r"Don.t show again"]),
]
READY = re.compile(r"auto mode on|bypass permissions|\? for shortcuts", re.I)


def _screen(lane, sh=_sh):
    return sh("tmux", "-L", f"head-{lane}", "capture-pane", "-pt", f"harness-head-{lane}").stdout.splitlines()


def _key(lane, k, sh=_sh):
    sh("tmux", "-L", f"head-{lane}", "send-keys", "-t", f"harness-head-{lane}", k)


def pick(lines, wants):
    """-> (moves, found): how many lines the ❯ cursor must move (+down) to reach the first wanted option."""
    opts = [i for i, l in enumerate(lines) if re.match(r"^\s*(❯\s*)?\S", l)]
    cur = next((i for i, l in enumerate(lines) if l.lstrip().startswith("❯")), None)
    for w in wants:
        tgt = next((i for i, l in enumerate(lines) if re.search(w, l.lstrip("❯ ").strip(), re.I) and i in opts), None)
        if tgt is not None and cur is not None:
            between = [i for i in opts if min(cur, tgt) < i <= max(cur, tgt)]
            return (len(between) if tgt > cur else -len(between)), True
    return 0, False


def answer_dialogs(lane, sh=_sh, sleep=time.sleep, now=time.time, wait=ANSWER_S):
    """Watch the new head's screen until its prompt is ready, answering each startup dialog on screen.
    -> list of dialogs answered. Raises Fail when the screen never gets ready."""
    done, t0 = [], now()
    while now() - t0 < wait:
        lines = _screen(lane, sh)
        text = "\n".join(lines)
        hit = next(((n, w) for n, rx, w in DIALOGS if rx.search(text)), None)
        if hit:
            name, wants = hit
            moves, ok = pick(lines, wants)
            if not ok:
                _key(lane, "Escape", sh); log("dialog_escaped", lane=lane, dialog=name)
            else:
                for _ in range(abs(moves)):
                    _key(lane, "Down" if moves > 0 else "Up", sh)
                sleep(0.4)
                chk, _ = pick(_screen(lane, sh), wants)
                if chk != 0:                                 # the cursor is not on the wanted line: do not guess
                    _key(lane, "Escape", sh); log("dialog_escaped", lane=lane, dialog=name, why="cursor check")
                else:
                    _key(lane, "Enter", sh); log("dialog_answered", lane=lane, dialog=name)
            done.append(name)
            sleep(2)
            continue
        if READY.search(text):
            return done
        sleep(2)
    raise Fail("dialogs", "the head's screen never reached its prompt: " + " | ".join(l.strip() for l in _screen(lane, sh)[-4:] if l.strip())[:200])


def bridge(lane, req, wait=15):
    s = socket.socket(socket.AF_UNIX); s.settimeout(wait)
    s.connect(os.path.join(BASE, lane, "head.sock"))
    s.sendall((json.dumps(req) + "\n").encode())
    buf = b""
    try:
        while b"\n" not in buf:
            c = s.recv(65536)
            if not c:
                break
            buf += c
    finally:
        s.close()
    return json.loads(buf.split(b"\n")[0] or b"{}")


def _stopped(ev, off):
    if not os.path.exists(ev):
        return False
    with open(ev) as f:
        f.seek(off)
        return any(json.loads(l).get("event") == "Stop" for l in f if l.strip())


def bridge_test(lane, call=bridge, sleep=time.sleep, now=time.time, wait=READY_S, sh=_sh, tries=2):
    """A prompt in, a Stop event out: proves the hooks fire, so the lane's replies will reach Telegram.
    A dialog that shows up only once the prompt is ready (Howl 2026-10-29: "Teach auto mode" ate the ping) eats the
    test: it is answered on screen and the test sent again, once."""
    ev = os.path.join(BASE, lane, "events.jsonl")
    for attempt in range(1, tries + 1):
        off = os.path.getsize(ev) if os.path.exists(ev) else 0
        t0 = now()
        while now() - t0 < wait:                               # the bridge socket comes up a moment after the unit
            try:
                r = call(lane, {"t": "send", "text": "Setup check from new_lane.py: reply with just the word ready."})
                break
            except OSError:
                sleep(2)
        else:
            raise Fail("bridge", "head.sock never answered")
        if not r.get("ok"):
            raise Fail("bridge", str(r.get("error") or r)[:200])
        while now() - t0 < wait:
            if _stopped(ev, off):
                log("bridge_ok", lane=lane, attempt=attempt)
                return True
            sleep(2)
        log("bridge_no_stop", lane=lane, attempt=attempt)
        if attempt < tries:
            answer_dialogs(lane, sh, sleep, now, wait=30)
    raise Fail("bridge", "no Stop event in events.jsonl: the hooks are not firing, replies would never arrive")


def register(lane):
    with open(LANES + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        cur = lanes()
        if lane not in cur:
            _jsave(LANES, cur + [lane])
    log("registered", lane=lane)


# ---------------------------------------------------------------- remove: the inverse (Rav 2026-10-30, /deletethread)
# "the whole thing gets removed from TG. The history stays/gets archived. Whatever memories and lessons stay in RAG."
# Nothing is deleted on disk: the head's folder, state dir and Claude transcripts MOVE into the archive tier with a
# manifest, and its transcripts are written out as markdown there, which any search index
# picks up. chat_log rows are never touched. The Telegram topic itself is deleted by the gateway, after this succeeds.

def made_here(lane):
    """True only for a lane new_lane.py built: its marker, or (lanes built before the marker) a 'done' in the log."""
    if os.path.isfile(os.path.join(BASE, lane, MADE_BY)):
        return True
    return any(r.get("event") == "done" and r.get("lane") == lane for r in log_rows())


def removable(lane, tid=None):
    """-> None when the lane may be removed, else why not (said to Rav as is)."""
    if lane in RESERVED or lane == "main":
        return "that's the main thread; it can't be removed"
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,20}", lane or ""):
        return f"{lane!r} is not a lane name"
    if not made_here(lane):
        return f"lane {lane} was built by hand, not by the new-topic automation; remove it by hand (SKILL_head_lanes)"
    if tid is not None:
        own = [int(v) for v in _jload(os.path.join(BASE, lane, "tg_threads.json"), {}).values() if v is not None]
        if tid not in own:
            return f"thread {tid} is not lane {lane}'s"
    return None


def unregister(lane):
    with open(LANES + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        cur = lanes()
        if lane in cur:
            _jsave(LANES, [x for x in cur if x != lane])
    log("unregistered", lane=lane)


def stop_unit(lane, sh=_sh):
    r = sh("systemctl", "--user", "disable", "--now", f"harness-head@{lane}.service")
    if r.returncode != 0:
        raise Fail("unit", "stop: " + (r.stderr or r.stdout).strip()[:200])
    k = sh("tmux", "-L", f"head-{lane}", "kill-session", "-t", f"harness-head-{lane}")      # the REPL outlives the bridge unit
    alive = sh("tmux", "-L", f"head-{lane}", "has-session", "-t", f"harness-head-{lane}").returncode == 0
    if alive:
        raise Fail("tmux", f"claude-head-{lane} still running: {(k.stderr or '').strip()[:120]}")
    log("unit_stopped", lane=lane)


def _text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text":
            out.append(c.get("text") or "")
    return "\n".join(out)


def transcript_md(path, lane):
    """A Claude Code session .jsonl -> markdown of what was said (user + assistant text, tools left out)."""
    turns = []
    with open(path, errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("type") not in ("user", "assistant"):
                continue
            msg = r.get("message") or {}
            t = _text_of(msg.get("content")).strip()
            if t:
                turns.append(f"**{r['type'].upper()}** ({r.get('timestamp', '')[:19]}): {t}")
    return turns


def write_transcripts(lane, dest):
    """One markdown file per session (split under the RAG ingest's 2 MB limit). -> number of files written."""
    src = project_dir(lane)
    n = 0
    for name in sorted(os.listdir(src)) if os.path.isdir(src) else []:
        if not name.endswith(".jsonl"):
            continue
        turns = transcript_md(os.path.join(src, name), lane)
        part, size, k = [], 0, 1
        def flush():
            nonlocal n, k
            with open(os.path.join(dest, f"transcript_{lane}_{name[:-6]}_{k}.md"), "w") as f:
                f.write(f"# Head lane {lane}: transcript {name[:-6]} part {k} (archived by new_lane.py remove)\n\n"
                        + "\n\n---\n\n".join(part))
            n += 1; k += 1
        for t in turns:
            if size + len(t) > 1_500_000 and part:
                flush(); part, size = [], 0
            part.append(t); size += len(t)
        if part:
            flush()
    return n


def archive(lane, now=time.time):
    """Move the head's folder, state dir and Claude transcripts into the archive tier, with a manifest. -> dest."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now()))
    dest = os.path.join(ARCHIVE, f"{lane}-{stamp}")
    os.makedirs(dest)
    md = write_transcripts(lane, dest)                       # before the move: the RAG copy
    moved = []
    for src, as_ in ((lane_dir(lane), "folder"), (os.path.join(BASE, lane), "state"),
                     (project_dir(lane), "claude_projects")):
        if os.path.exists(src):
            shutil.move(src, os.path.join(dest, as_))
            moved.append({"from": src, "to": as_})
    files = []
    for root, _, fs in os.walk(dest):
        for x in fs:
            p = os.path.join(root, x)
            files.append({"path": os.path.relpath(p, dest), "bytes": os.path.getsize(p)})
    _jsave(os.path.join(dest, "manifest.json"), {"lane": lane, "archived": stamp, "moved": moved,
                                                 "transcript_md": md, "files": files,
                                                 "note": "markdown transcripts, ready for any search index"})
    log("archived", lane=lane, dest=dest, transcript_md=md, files=len(files))
    return dest


def remove(lane, tid=None, sh=_sh, now=time.time):
    """The inverse of create, in order: check -> lanes.json -> unit + tmux -> archive. -> {"ok", "lane", ...}."""
    step = "check"
    try:
        why = removable(lane, tid)
        if why:
            raise Fail("check", why)
        log("remove_start", lane=lane, thread=tid)
        step = "unregister"; unregister(lane)                  # first: the gateway stops routing to it
        step = "unit"; stop_unit(lane, sh)
        step = "archive"; dest = archive(lane, now)
        log("removed", lane=lane, thread=tid, archive=dest)
        return {"ok": True, "lane": lane, "thread": tid, "archive": dest}
    except Fail as e:
        log("remove_failed", lane=lane, step=e.step, why=e.why)
        return {"ok": False, "lane": lane, "step": e.step, "why": e.why}
    except Exception as e:
        log("remove_failed", lane=lane, step=step, why=f"{type(e).__name__}: {e}"[:300])
        return {"ok": False, "lane": lane, "step": step, "why": f"{type(e).__name__}: {e}"[:300]}


# ---------------------------------------------------------------- retitle + rename in place (Rav 2026-11-03, TIK-1454)
# retitle: only the title the head is told about. rename: the internal name too, keeping the head's conversation.

def retitle(lane, title):
    """Topic renamed in Telegram: CLAUDE.md and the provenance marker get the new title. -> {"ok", ...}."""
    why = removable(lane)
    if why:
        log("retitle_refused", lane=lane, why=why)
        return {"ok": False, "lane": lane, "step": "check", "why": why}
    mk = os.path.join(BASE, lane, MADE_BY)
    made = _jload(mk, {})
    old, clean = str(made.get("title") or ""), title.replace('"', "'")
    md = os.path.join(lane_dir(lane), "CLAUDE.md")
    with open(md) as f:
        text = f.read()
    # the title sits in quotes in the first heading, the THIS head heading, and the bold topic line
    # n counts places that hold a title, so a file already showing the new title is not a failure
    text2, n = re.subn(r'(, |THIS head: |\*\*)"[^"\n]*"', lambda m: f'{m.group(1)}"{clean}"', text)
    if not n:
        log("retitle_failed", lane=lane, why="no title found in CLAUDE.md")
        return {"ok": False, "lane": lane, "step": "claude_md", "why": "no title found in CLAUDE.md"}
    with open(md + ".tmp-new_lane", "w") as f:
        f.write(text2)
    os.replace(md + ".tmp-new_lane", md)
    _jsave(mk, dict(made, title=title))
    log("retitled", lane=lane, old=old, title=title)
    return {"ok": True, "lane": lane, "title": title}


BUSY_EVENTS = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart"}


def busy(lane):
    """-> why the head can't be stopped now, or None. A turn in flight or queued work would be cut off."""
    last = None
    try:
        with open(os.path.join(BASE, lane, "events.jsonl")) as f:
            for l in f:
                try:
                    last = json.loads(l).get("event") or last
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    if last in BUSY_EVENTS:
        return "the head is in the middle of a turn"
    for spool in ("task_spool", "relay_spool"):
        d = os.path.join(BASE, lane, spool)
        if os.path.isdir(d) and any(n.endswith(".json") for n in os.listdir(d)):
            return f"the head has queued work in {spool}"
    return None


def _project_dir(lane):
    return project_dir(lane)


def _move_project_entry(old, new):
    """~/.claude.json keeps trust per folder path; the entry follows the folder. Locked: sessions rewrite this file."""
    a, b = lane_dir(old), lane_dir(new)
    with open(CLAUDE_JSON + ".new_lane.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        d = _jload(CLAUDE_JSON, None)
        if d is None:
            raise Fail("approve", f"{CLAUDE_JSON} missing")
        ps = d.setdefault("projects", {})
        if a in ps:
            ps[b] = ps.pop(a)
        _jsave(CLAUDE_JSON, d)


def _names_in_claude_md(folder, old, new):
    md = os.path.join(lane_dir(folder), "CLAUDE.md")
    with open(md) as f:
        text = f.read()
    text = text.replace(f"lane `{old}`", f"lane `{new}`").replace(f"/{old})", f"/{new})")
    with open(md, "w") as f:
        f.write(text)


def rename(lane, new, title=None, tid=None, sh=_sh, call=bridge, sleep=time.sleep, now=time.time):
    """Move a lane to a new name, conversation included. Any failure undoes every step done so far and says so.
    -> {"ok", "lane", "old", ...}; on failure {"ok": False, "step", "why", "restored": bool}."""
    step, undo = "check", []
    try:
        why = removable(lane, tid)
        if why:
            raise Fail("check", why)
        if lane not in lanes():
            raise Fail("check", f"{lane} is not a registered lane")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,20}", new or ""):
            raise Fail("name", f"{new!r} is not a lane name")
        if taken(new) or os.path.exists(_project_dir(new)):
            raise Fail("name", f"{new} is already taken")
        why = busy(lane)
        if why:
            raise Fail("busy", why)
        log("rename_start", lane=lane, new=new, thread=tid)
        step = "unregister"; unregister(lane); undo.append(lambda: register(lane))
        step = "unit"; undo.append(lambda: start_unit(lane, sh)); stop_unit(lane, sh)
        step = "move"
        for a, b in ((lane_dir(lane), lane_dir(new)),
                     (os.path.join(BASE, lane), os.path.join(BASE, new)), (_project_dir(lane), _project_dir(new))):
            if os.path.exists(a):
                os.rename(a, b)
                undo.append(lambda a=a, b=b: os.rename(b, a))
        step = "approve"; _move_project_entry(lane, new); undo.append(lambda: _move_project_entry(new, lane))
        step = "claude_md"; _names_in_claude_md(new, lane, new); undo.append(lambda: _names_in_claude_md(new, new, lane))
        mk = os.path.join(BASE, new, MADE_BY)
        step = "marker"; _jsave(mk, dict(_jload(mk, {}), renamed_from=lane))
        step = "retitle"
        if title:
            r = retitle(new, title)
            if not r["ok"]:
                raise Fail("retitle", r["why"])
        step = "start"; undo.append(lambda: stop_unit(new, sh)); start_unit(new, sh)
        step = "dialogs"; answer_dialogs(new, sh, sleep, now)
        step = "bridge"; bridge_test(new, call, sleep, now, sh=sh)
        step = "register"; register(new)
        log("renamed", lane=new, old=lane, thread=tid)
        return {"ok": True, "lane": new, "old": lane, "thread": tid}
    except Exception as e:
        st, why = (e.step, e.why) if isinstance(e, Fail) else (step, f"{type(e).__name__}: {e}"[:300])
        restored = True
        for fn in reversed(undo):
            try:
                fn()
            except Exception as ue:                              # keep undoing; the partial state is said loudly
                restored = False
                log("rename_undo_failed", lane=lane, new=new, error=f"{type(ue).__name__}: {ue}"[:200])
        log("rename_failed", lane=lane, new=new, step=st, why=why, restored=restored, undone=len(undo))
        return {"ok": False, "lane": lane, "step": st, "why": why, "restored": restored}


def create(title, tid, uid=OWNER, lane=None, sh=_sh, call=bridge, sleep=time.sleep, now=time.time):
    step = "name"
    try:
        owner = thread_owner(tid)
        if owner:
            raise Fail("thread", f"thread {tid} already has a head (lane {owner})")
        lane = lane or slug(title)
        if lane in lanes():
            raise Fail("name", f"lane {lane} already exists")
        log("start", lane=lane, title=title, thread=tid, uid=uid)
        for step, fn in (("files", lambda: write_files(lane, title, tid, uid)), ("approve", lambda: approve_folder(lane)),
                         ("unit", lambda: start_unit(lane, sh)), ("dialogs", lambda: answer_dialogs(lane, sh, sleep, now)),
                         ("bridge", lambda: bridge_test(lane, call, sleep, now, sh=sh)), ("register", lambda: register(lane))):
            fn()
        log("done", lane=lane, thread=tid)
        return {"ok": True, "lane": lane, "thread": tid}
    except Fail as e:
        log("failed", lane=lane, step=e.step, why=e.why)
        return {"ok": False, "lane": lane, "step": e.step, "why": e.why}
    except Exception as e:                                       # anything else: still said, with the step
        log("failed", lane=lane, step=step, why=f"{type(e).__name__}: {e}"[:300])
        return {"ok": False, "lane": lane, "step": step, "why": f"{type(e).__name__}: {e}"[:300]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create"); c.add_argument("--title", required=True); c.add_argument("--thread", type=int, required=True)
    c.add_argument("--uid", type=int, default=OWNER); c.add_argument("--lane"); c.add_argument("--json", action="store_true")
    s = sub.add_parser("slug"); s.add_argument("title")
    d = sub.add_parser("remove"); d.add_argument("--lane", required=True); d.add_argument("--thread", type=int)
    d.add_argument("--json", action="store_true")
    t = sub.add_parser("retitle"); t.add_argument("--lane", required=True); t.add_argument("--title", required=True)
    n = sub.add_parser("rename"); n.add_argument("--lane", required=True); n.add_argument("--title", required=True)
    n.add_argument("--to"); n.add_argument("--thread", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "retitle":
        r = retitle(a.lane, a.title); print(json.dumps(r)); return 0 if r["ok"] else 1
    if a.cmd == "rename":
        r = rename(a.lane, a.to or slug(a.title, width=20), a.title, a.thread); print(json.dumps(r)); return 0 if r["ok"] else 1
    if a.cmd == "slug":
        print(slug(a.title)); return 0
    if a.cmd == "remove":
        r = remove(a.lane, a.thread)
        print(json.dumps(r) if a.json else (f"removed: lane {r['lane']} -> {r['archive']}" if r["ok"]
                                           else f"FAILED at {r['step']}: {r['why']}"))
        return 0 if r["ok"] else 1
    r = create(a.title, a.thread, a.uid, a.lane)
    print(json.dumps(r) if a.json else (f"ready: lane {r['lane']}" if r["ok"] else f"FAILED at {r['step']}: {r['why']}"))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
