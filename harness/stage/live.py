"""live.py - Live mode for the Mini App (TIK-1025 step 2).

Rav 2026-10-24 14:31: "I want for you to be able to see video kinda like how Gemini does on live mode."

The Live tab sends a camera frame every ~1.5 s while it is open (save_frame), and a push-to-talk clip when
the button is released (say). Frames stay on the host in a short ring; a clip becomes one spool file that the
head lane (head_lane.start_live, inside tg-gateway) picks up, transcribes and queues as a head turn with
the frames from around that moment. Nothing here talks to Telegram or the head directly.
"""
import json
import os
import sys
import shutil
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # harness/
import config  # noqa: E402

LIVE_DIR = Path(os.environ.get("LIVE_DIR", os.path.join(config.STATE_DIR, "live")))
SPOOL = Path(os.environ.get("LIVE_SPOOL", os.path.join(config.STATE_DIR, "live_spool")))
KEEP = 240                      # frames kept in the ring (6 min at 1.5 s): a long walk-and-talk hold fits
HOLD_CAP = 20                   # frames handed with ONE hold, spread over the whole hold (TIK-1025 house tour)
HOLD_SLACK_S = 2.0              # upload + clock slack at the start of a hold
CLIP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "render")
MAX_FRAME = 800_000
MAX_AUDIO = 8_000_000
RECENT_S = 15                   # frames handed with a clip: the ones from this long before release
RECENT_N = 6
AUDIO_EXT = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/wav": "wav"}


class LiveError(ValueError):
    pass


# ---- which head hears Live (Rav 2026-10-30: "a drop down at the top near the start button ... which thread it goes to")
LANE_NAMES = {"main": config.AGENT_NAME}


def lanes(uid):
    """[{lane, name}] - the heads with a topic of this person's (main first). Each one's Live spool is
    <claude-head>/<lane>/live_spool, the same path as that lane's head_lane.LIVE_SPOOL."""
    base = SPOOL.parent
    out = [{"lane": "main", "name": LANE_NAMES["main"]}]
    try:
        names = json.load(open(base / "lanes.json"))
    except (OSError, ValueError):
        return out
    for lane in names:
        try:
            if json.load(open(base / lane / "tg_threads.json")).get(str(uid)) is None:
                continue
        except (OSError, ValueError, AttributeError):
            continue
        try:
            title = json.load(open(base / lane / "made_by_new_lane.json")).get("title")
        except (OSError, ValueError, AttributeError):
            title = None
        out.append({"lane": lane, "name": title or LANE_NAMES.get(lane) or lane.replace("-", " ").title()})
    return out


def spool_for(uid, lane=None):
    """Live spool of the chosen head; unknown or not this person's -> LiveError (never a silent fall back to hb)."""
    if not lane or lane == "main":
        return SPOOL
    if lane not in {x["lane"] for x in lanes(uid)}:
        raise LiveError(f"no head for {lane!r}")
    return SPOOL.parent / lane / "live_spool"


def _frames():
    return LIVE_DIR / "frames"


def save_frame(data: bytes, now=None) -> str:
    if not data or len(data) > MAX_FRAME:
        raise LiveError("frame empty or too large")
    if data[:3] != b"\xff\xd8\xff":
        raise LiveError("frame is not a JPEG")
    d = _frames()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{int((now or time.time()) * 1000)}.jpg"
    tmp = p.with_suffix(".part")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    shutil.copyfile(p, LIVE_DIR / "latest.jpg")
    for old in sorted(d.glob("*.jpg"))[:-KEEP]:
        old.unlink(missing_ok=True)
    return str(p)


def recent(now=None, within=RECENT_S, n=RECENT_N):
    """-> frame paths from the last `within` seconds, oldest first, at most n (evenly picked)."""
    now = now or time.time()
    got = [p for p in sorted(_frames().glob("*.jpg")) if now - int(p.stem) / 1000 <= within] if _frames().is_dir() else []
    if len(got) > n:
        step = len(got) / n
        got = [got[int(i * step)] for i in range(n - 1)] + [got[-1]]
    return [str(p) for p in got]


def hold_frames(start, now, cap=HOLD_CAP):
    """-> [(seconds into the hold, path)] for EVERY moment of a hold, thinned evenly to cap (first and last kept).
    The main head's field test (TIK-1025): a minute-long house tour handed over only its last 4 frames."""
    d = _frames()
    got = [p for p in sorted(d.glob("*.jpg")) if start - HOLD_SLACK_S <= int(p.stem) / 1000 <= now] if d.is_dir() else []
    if len(got) > cap:
        got = [got[round(i * (len(got) - 1) / (cap - 1))] for i in range(cap)]
    return [(max(0.0, int(p.stem) / 1000 - start), str(p)) for p in got]


def contact_sheet(frames, out, log=print):
    """One image of the whole hold, each frame labelled by its time (clip.py's sheet, reused). -> path or None."""
    if not frames:
        return None
    try:
        import sys
        if CLIP not in sys.path:
            sys.path.insert(0, CLIP)
        import clip
        clip.sheet(frames, out)
        return out
    except Exception as e:
        log(f"live: contact sheet failed: {e}")
        return None


def say(uid: int, audio: bytes, mime: str, now=None, hold_s=None, log=print, lane=None) -> str:
    """One push-to-talk clip -> one spool file for the head lane. -> the spool path.
    hold_s = how long the button was held (from the page): the frames and the sheet cover all of it."""
    if not audio or len(audio) > MAX_AUDIO:
        raise LiveError("clip empty or too large")
    spool = spool_for(uid, lane)
    now = now or time.time()
    ext = AUDIO_EXT.get((mime or "").split(";")[0].strip().lower(), "webm")
    stamp = int(now * 1000)
    talk = LIVE_DIR / "talk"
    talk.mkdir(parents=True, exist_ok=True)
    a = talk / f"{stamp}.{ext}"
    a.write_bytes(audio)
    spool.mkdir(parents=True, exist_ok=True)
    job = spool / f"{stamp}.json"
    tmp = job.with_suffix(".part")
    if hold_s and hold_s > 0:
        held = hold_frames(now - min(float(hold_s), KEEP * 1.5), now)
        frames = [p for _, p in held]
        sheet = contact_sheet(held, str(talk / f"{stamp}_sheet.jpg"), log=log)
    else:
        frames, sheet = recent(now), None
    tmp.write_text(json.dumps({"uid": int(uid), "audio": str(a), "frames": frames, "sheet": sheet,
                               "hold_s": hold_s, "ts": now}))
    os.replace(tmp, job)                      # the lane only ever sees a whole file
    return str(job)


# ---- One turn, many holds (Rav 20:12): "hold to talk multiple times before the package is sent ... stream the
# transcript and images to start processing before I hit ... an 'end turn' button". Each release is a SEGMENT,
# transcribed in the background at once; End turn joins the finished transcripts + ONE sheet of the whole turn.
SEG_MAX = 20
SEG_TTL_S = 600                 # an un-ended turn left alone this long is dropped (the next hold starts fresh)
END_WAIT_S = 45                 # End turn waits at most this long for a segment still being transcribed
LANE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # harness/
_segs = {}
_seg_lock = __import__("threading").Lock()


def _turns_file():
    return LIVE_DIR / "open_turns.json"


def _save_turns():
    """The open turns survive a Mini App restart (a deploy must never eat Rav's unsent clips)."""
    try:
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        p = _turns_file()
        tmp = p.with_suffix(".part")
        tmp.write_text(json.dumps({str(k): v for k, v in _segs.items()}))
        os.replace(tmp, p)
    except Exception as e:
        print(f"live: saving open turns failed: {e}")


_loaded = {"dir": None}


def _ensure_loaded():
    """Lazy, on first use (callers hold _seg_lock): tests repoint LIVE_DIR after import, so loading at import time
    would read, and re-transcribe, the LIVE turns from a test run."""
    if _loaded["dir"] != str(LIVE_DIR):
        _loaded["dir"] = str(LIVE_DIR)
        _load_turns()


def _load_turns():
    try:
        d = json.loads(_turns_file().read_text())
    except Exception:
        return
    for k, t in d.items():
        _segs[int(k)] = t
        for s in t.get("items", []):
            if s.get("state") == "transcribing":          # its transcription died with the old process: redo it
                __import__("threading").Thread(target=_rehear, args=(s,), daemon=True, name="live-stt").start()


def _rehear(seg):
    try:
        seg["text"] = ((_stt(seg["audio"])) or "").strip()
    except Exception as e:
        print(f"live: re-transcribing segment {seg.get('i')} failed: {e}")
        seg["text"] = ""
    seg["state"] = "heard" if seg["text"] else "unheard"
    with _seg_lock:
        _save_turns()


def _stt(path):
    """The lane's transcribe(): same STT as voice notes (the STT server :8002, CPU :8003). Reused, not duplicated."""
    import sys
    if LANE not in sys.path:
        sys.path.insert(0, LANE)
    import head_lane
    return head_lane.transcribe(path)


def add_segment(uid, audio, mime, hold_s, now=None, stt=None, notify=None, log=print, run=None):
    """One release of Hold to talk -> a segment of the open turn, transcribed in the background right away.
    notify(seg) is called when its transcript is ready. -> the segment (dict)."""
    if not audio or len(audio) > MAX_AUDIO:
        raise LiveError("clip empty or too large")
    now = now or time.time()
    ext = AUDIO_EXT.get((mime or "").split(";")[0].strip().lower(), "webm")
    talk = LIVE_DIR / "talk"
    talk.mkdir(parents=True, exist_ok=True)
    a = talk / f"{int(now * 1000)}.{ext}"
    a.write_bytes(audio)
    with _seg_lock:
        _ensure_loaded()
        t = _segs.get(uid)
        if t is None or now - t["last"] > SEG_TTL_S:
            t = _segs[uid] = {"items": [], "last": now}
        if len(t["items"]) >= SEG_MAX:
            raise LiveError(f"a turn holds at most {SEG_MAX} clips: tap the agent's turn")
        seg = {"i": len(t["items"]) + 1, "audio": str(a), "start": now - max(0.0, float(hold_s or 0)), "end": now,
               "text": None, "state": "transcribing"}
        t["items"].append(seg)
        t["last"] = now
        _save_turns()

    def work():
        try:
            seg["text"] = ((stt or _stt)(str(a)) or "").strip()
        except Exception as e:
            log(f"live: segment {seg['i']} transcription failed: {e}")
            seg["text"] = ""
        seg["state"] = "heard" if seg["text"] else "unheard"
        with _seg_lock:
            _save_turns()
        if notify:
            try:
                notify(seg)
            except Exception as e:
                log(f"live: segment notify failed: {e}")
    (run or (lambda f: __import__("threading").Thread(target=f, daemon=True, name="live-stt").start()))(work)
    return seg


def open_turn(uid, now=None):
    """The clips waiting to be sent, for a page that was closed and reopened (Rav 20:49). [] when none / expired."""
    with _seg_lock:
        _ensure_loaded()
        t = _segs.get(uid)
        if not t or (now or time.time()) - t["last"] > SEG_TTL_S:
            return []
        return [{"i": s["i"], "state": s["state"], "text": s["text"] or ""} for s in t["items"]]


def clear_turn(uid):
    with _seg_lock:
        _ensure_loaded()
        n = len((_segs.pop(uid, None) or {}).get("items", []))
        _save_turns()
        return n


def end_turn(uid, now=None, sleep=time.sleep, log=print, lane=None):
    """the agent's turn: wait for the transcripts still running, then ONE spool job with every heard segment (each
    stamped m:ss into the turn) and the whole turn's frames + contact sheet. -> the spool path."""
    spool = spool_for(uid, lane)               # before the turn is taken: a bad pick keeps the recording
    with _seg_lock:
        _ensure_loaded()
        t = _segs.pop(uid, None)
        _save_turns()
    items = (t or {}).get("items") or []
    if not items:
        raise LiveError("nothing recorded yet: hold the button and talk first")
    waited = 0.0
    while any(s["state"] == "transcribing" for s in items) and waited < END_WAIT_S:
        sleep(0.25)
        waited += 0.25
    now = now or time.time()
    start = items[0]["start"]
    lines = [f"[{int((s['start'] - start) // 60)}:{int((s['start'] - start) % 60):02d}] {s['text']}"
             for s in items if s.get("text")]
    if not lines:
        raise LiveError("couldn't make out any of it: hold the button while you talk")
    held = hold_frames(start, now)
    stamp = int(now * 1000)
    sheet = contact_sheet(held, str(LIVE_DIR / "talk" / f"{stamp}_sheet.jpg"), log=log)
    spool.mkdir(parents=True, exist_ok=True)
    job = spool / f"{stamp}.json"
    tmp = job.with_suffix(".part")
    tmp.write_text(json.dumps({"uid": int(uid), "audio": items[-1]["audio"], "transcript": "\n".join(lines),
                               "segments": len(items), "frames": [p for _, p in held], "sheet": sheet,
                               "hold_s": round(now - start, 1), "ts": now}))
    os.replace(tmp, job)
    return str(job)


PIPER = os.path.expanduser(config.section("voice").get("piper", "piper"))
VOICE_MODEL = os.path.expanduser(config.section("voice").get("model", ""))
VOICE_RATE = int(config.section("voice").get("sample_rate", 22050))   # the voice model's sample rate (its .onnx.json); the page plays s16le mono at this
CHUNK = 4096


WARM_MAX_S = 300                   # a warmed voice older than this is replaced (never pile up idle Pipers)
_warm = {"p": None, "ts": 0.0}


def _kill(p):
    """Kill AND reap: the Mini App server runs for days, an unreaped Piper would stay a zombie."""
    p.kill()
    try:
        p.wait(timeout=2)
    except Exception:
        pass


def _piper(popen=None):
    import subprocess
    return (popen or subprocess.Popen)([PIPER, "--model", VOICE_MODEL, "--output-raw", "--sentence-silence", "0.15"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def warm(popen=None, now=None):
    """Rav 18:21: 'Can the voice be preloaded when I hit the start button? Or when I hold the hold to talk button.'
    Starts ONE Piper that loads the voice (~1 s) and waits on stdin, so the answer's first sound skips the load.
    -> 'warm' (already one ready) | 'started'."""
    now = now or time.time()
    p = _warm["p"]
    if p is not None and p.poll() is None and now - _warm["ts"] < WARM_MAX_S:
        return "warm"
    if p is not None and p.poll() is None:
        _kill(p)
    _warm["p"], _warm["ts"] = _piper(popen), now
    return "started"


def _take_warm(now=None):
    p, _warm["p"] = _warm["p"], None
    if p is not None and p.poll() is None and (now or time.time()) - _warm["ts"] < WARM_MAX_S:
        return p
    if p is not None and p.poll() is None:
        _kill(p)
    return None


def speak_pcm(text, popen=None, log=print):
    """Rav 2026-10-24: in Live mode the answer comes out of the phone, starting ASAP. Piper --output-raw writes each
    sentence's audio as soon as it is made, so this yields raw s16le PCM while the rest is still being made.
    A warmed Piper (warm()) is used when there is one. A Piper failure is logged and ends the stream (the page
    shows it); never a silent nothing."""
    p = (None if popen else _take_warm()) or _piper(popen)
    p.stdin.write(" ".join(text.split()).encode())
    p.stdin.close()
    sent, done = 0, False
    try:
        while True:
            b = p.stdout.read(CHUNK)
            if not b:
                done = True
                break
            sent += len(b)
            yield b
    finally:
        if not done:                  # the page went away mid-answer: Piper would block on a full pipe forever
            _kill(p)
            log(f"live speak: stream abandoned after {sent} bytes, piper killed")
            return
        rc = p.wait(timeout=60)
        if rc != 0 or not sent:
            log(f"live speak: piper rc={rc} bytes={sent} err={p.stderr.read()[-300:]!r}")


BEAT_FRESH_S = 8                  # the page beats every 3 s while the Live session is on
_beat = {"ts": 0.0}


def beat(on=True, now=None):
    _beat["ts"] = (now or time.time()) if on else 0.0


def session_on(now=None):
    """Is the Live session open on the phone? From its heartbeat, NOT from frames: Rav pauses the camera and keeps
    talking (20:45), and a paused camera must not send the answer to Telegram instead of the speaker."""
    return (now or time.time()) - _beat["ts"] < BEAT_FRESH_S


def status(now=None):
    now = now or time.time()
    fs = sorted(_frames().glob("*.jpg")) if _frames().is_dir() else []
    return {"frames": len(fs), "last_age_s": round(now - int(fs[-1].stem) / 1000, 1) if fs else None,
            "waiting": len(list(SPOOL.glob("*.json"))) if SPOOL.is_dir() else 0}
