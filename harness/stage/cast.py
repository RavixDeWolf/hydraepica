"""cast.py - put one stage item on a screen: a TV, a wall display, a spare monitor. Optional; off unless configured.

The item is rendered to ONE standalone full-screen page (cast_html). A screen shows it; the phone then drives it
(video play / pause / seek / restart, gallery picture). Screens come from config, by kind:

  [[cast.screens]]
  name = "tv"
  kind = "receiver"     # built in: open <stage url>/screen/tv?k=<key> full screen on the TV's browser,
  key_env = "SCREEN_TV_KEY"   # a Fire Stick, a Raspberry Pi kiosk, a spare laptop. Nothing to install.

  [[cast.screens]]
  name = "wall"
  kind = "http"         # your own display app: POST {"html", "name"} to url; state updates POST {"name", "state"}
  url = "http://127.0.0.1:4493/api/cast"

  [[cast.screens]]
  name = "den"
  kind = "ssh"          # copy the page to dir on host over ssh, then (optional) POST {"name"} to show_url
  host = "den-pc"
  dir = "~/screen"
  show_url = "http://den-pc.local:8781/api/show"

Every cast, control and failure is logged; a failure raises CastError with the reason, never a quiet success.
"""
import html as _html
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # harness/
import config  # noqa: E402

STATE = Path(os.environ.get("STAGE_STATE", os.path.join(config.STATE_DIR, "stage")))
SCREENS_DIR = STATE / "screens"
LOG = STATE / "cast.log"
KINDS = ("receiver", "http", "ssh")
VIDEO_CMDS = ("status", "play", "pause", "seek", "restart")
KEEP = 5


class CastError(Exception):
    pass


def screens():
    out = {}
    for s in (config.section("cast").get("screens") or []):
        if s.get("name") and s.get("kind") in KINDS:
            out[s["name"]] = s
    return out


MIN_KEY_LEN = 32     # e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` (43 chars)


def screen_key(name):
    """-> the receiver screen's key, or "" (screen off) when it is unset or too short to be a real secret.
    A short key is refused loudly rather than used: the key is the only thing guarding that screen."""
    s = screens().get(name) or {}
    if s.get("kind") != "receiver":
        return ""
    k = config.secret(s.get("key_env")) or ""
    if k and len(k) < MIN_KEY_LEN:
        print(f"cast: screen {name!r} key in ${s.get('key_env')} is {len(k)} chars, need {MIN_KEY_LEN}+; screen disabled",
              file=sys.stderr)
        return ""
    return k


def log(**kw):
    STATE.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **kw}) + "\n")


def cast_html(it, video_src=None):
    """One stage -> a standalone full-screen page (dark, centred, nothing else on it). Pure."""
    e = lambda s: _html.escape("" if s is None else str(s))
    title = f'<div class="t">{e(it.get("title"))}</div>' if it.get("title") else ""
    cap = f'<div class="c">{e(it.get("caption"))}</div>' if it.get("caption") else ""
    k = it.get("kind")
    if k == "card":
        body = f'<div class="card">{title}<div class="b">{e(it.get("body"))}</div></div>'
        title = ""
    elif k == "image":
        body = f'<img src="{e(it.get("src"))}" alt="">'
    elif k == "video" and video_src:
        # the phone drives play/pause/seek: it updates <name>.js beside this page (vid({p,t,at})); the page polls it
        # like the gallery does and follows: paused at t, or playing from t since `at` (the screen host's clock).
        body = (f'<video id="v" src="{e(video_src)}" playsinline autoplay></video>'
                f'<script>const v=document.getElementById("v"),F={json.dumps(_name(it) + ".js")};let S=null,B=false;'
                # a file server without byte ranges cannot seek: play at once, load the file whole in the background
                # and switch to that in-memory copy (always seekable), keeping the place
                'fetch(v.currentSrc||v.src).then(r=>r.ok?r.blob():Promise.reject(r.status)).then(b=>{'
                'const t=v.currentTime,p=v.paused;v.src=URL.createObjectURL(b);'
                'v.addEventListener("loadedmetadata",()=>{v.currentTime=t;B=true;if(!p)v.play().catch(()=>{})},{once:true})})'
                '.catch(()=>{});'
                'window.vid=s=>{S=s};function sync(){if(!S)return;'
                'if(S.p){if(!v.paused)v.pause();if(B&&Math.abs(v.currentTime-S.t)>0.1)v.currentTime=S.t;return}'
                'const want=S.t+(Date.now()-S.at)/1000;if(B&&Math.abs(v.currentTime-want)>0.35)v.currentTime=want;'
                'if(v.paused&&want<(v.duration||1e9))v.play().catch(()=>{})}'
                'function poll(){const s=document.createElement("script");s.src=F+"?t="+Date.now();'
                's.onload=s.onerror=()=>{s.remove();sync();setTimeout(poll,400)};document.head.appendChild(s)}poll();</script>')
    elif k == "video":
        # sound first; a browser that refuses unmuted autoplay gets it muted rather than a frozen frame
        body = (f'<video id="v" src="{e(it.get("src"))}" playsinline loop></video>'
                '<script>const v=document.getElementById("v");v.play().catch(()=>{v.muted=true;v.play();'
                'document.body.insertAdjacentHTML("beforeend","<div class=m>🔇 muted by the browser</div>")});</script>')
    elif k == "html":                       # agent-made pages stay sandboxed here too: no access to the display's origin
        body = f'<iframe sandbox="allow-scripts" srcdoc="{e(it.get("html"))}"></iframe>'
    elif k == "gallery":
        # srcs: the picture URLs on this screen (files beside it, or data: URIs). The page polls its state file
        # (<name>.js beside it: gal(n)) and crossfades to picture n; the phone moves it with pick().
        srcs = video_src or [im["src"] for im in it.get("images", [])]
        caps = [im.get("caption", "") for im in it.get("images", [])]
        title = cap = ""
        body = ('<img id="a" class="g on"><img id="b" class="g"><div id="gc" class="c gc"></div>'
                f'<script>const S={json.dumps(srcs)},C={json.dumps(caps)},F={json.dumps(_name(it) + ".js")};'
                'let cur=-1,tp="a";function go(i){if(i===cur||!S[i])return;cur=i;const n=tp==="a"?"b":"a",'
                'el=document.getElementById(n);el.onload=()=>{el.classList.add("on");'
                'document.getElementById(tp).classList.remove("on");tp=n};el.src=S[i];'
                'document.getElementById("gc").textContent=C[i]?C[i]+"  ·  "+(i+1)+"/"+S.length:(i+1)+"/"+S.length}'
                # a <script> load, not fetch: a display that draws casts in a sandboxed frame (origin null) blocks fetch
                # by CORS, but a script from the same folder still loads
                'window.gal=i=>go(i|0);function poll(){const s=document.createElement("script");'
                's.src=F+"?t="+Date.now();s.onload=s.onerror=()=>{s.remove();setTimeout(poll,400)};'
                'document.head.appendChild(s)}go(0);poll();</script>')
    elif k == "url":
        body = f'<div class="card">{title}<div class="b">{e(it.get("url"))}</div></div>'
        title = ""
    else:
        raise CastError(f"cannot cast kind {k!r}")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(it.get("title") or "stage")}</title><style>
html,body{{margin:0;height:100%;background:#05070d;color:#e7e9ee;font-family:-apple-system,"Segoe UI",Roboto,sans-serif;overflow:hidden}}
body{{display:flex;flex-direction:column;align-items:center;justify-content:center}}
img,video{{max-width:100vw;max-height:{"88vh" if title or cap else "100vh"};object-fit:contain}}
iframe{{width:100vw;height:100vh;border:0;background:#fff}}
.t{{font-size:3.2vh;margin:1.5vh 0;opacity:.9}} .c{{font-size:2.4vh;color:#8a93a6;margin-top:1.2vh}}
.card{{max-width:80vw;background:#151a23;border:1px solid #262d3a;border-radius:2vh;padding:5vh 6vh}}
.card .t{{font-size:5vh;margin:0 0 2.5vh}} .card .b{{font-size:3.4vh;line-height:1.45;white-space:pre-wrap;word-break:break-word}}
.m{{position:fixed;right:2vh;bottom:2vh;font-size:2vh;color:#8a93a6}}
.g{{position:fixed;inset:0;width:100vw;height:100vh;max-height:100vh;object-fit:contain;opacity:0;transition:opacity .35s}}
.g.on{{opacity:1}} .gc{{position:fixed;left:0;right:0;bottom:2.5vh;text-align:center;text-shadow:0 0 1vh #000}}
</style></head><body>{title}{body}{cap}</body></html>"""


def _name(it):
    return f"cast-{it['id']}"


def _post(url, body, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as ex:
        return ex.code, {}
    except Exception as ex:
        raise CastError(f"{url} did not answer: {type(ex).__name__}: {ex}")


def _ssh(host, args, data=None, run=subprocess.run):
    r = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, " ".join(shlex.quote(a) for a in args)],
            input=data, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise CastError(f"ssh {host}: {(r.stderr or b'').decode(errors='replace').strip()[:200]}")
    return r.stdout.decode(errors="replace")


def _write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _now_file(name):
    return SCREENS_DIR / name / "now.json"


def now_on(name):
    """What is on this screen: {"page", "kind", "id", "state"} or None."""
    try:
        return json.loads(_now_file(name).read_text())
    except (OSError, ValueError):
        return None


def _state_js(st):
    if "i" in st:
        return f"gal({int(st['i'])})"
    return f"vid({{p:{1 if st.get('p') else 0},t:{float(st.get('t', 0)):.3f},at:{int(st.get('at', time.time() * 1000))}}})"


def _push_state(scr, page, st, post=_post, ssh=_ssh):
    """The screen's page follows <page>.js: write it where that page can load it."""
    js = _state_js(st)
    if scr["kind"] == "receiver":
        _write_atomic(SCREENS_DIR / scr["name"] / (page + ".js"), js)
    elif scr["kind"] == "http":
        code, r = post(scr["url"], {"name": page, "state": js})
        if code != 200:
            raise CastError(f"{scr['name']} refused the state update ({code})")
    else:
        d = scr.get("dir", "~/screen")
        ssh(scr["host"], ["sh", "-c", f"cat > {d}/{page}.js.tmp && mv {d}/{page}.js.tmp {d}/{page}.js"], data=js.encode())


def cast(it, name, who, post=_post, ssh=_ssh, media_url=None):
    """Put item `it` on screen `name`. -> result dict. Raises CastError (logged)."""
    scr = screens().get(name)
    try:
        if not scr:
            raise CastError(f"unknown screen {name!r}; configured: {sorted(screens())}")
        page = _name(it)
        video_src = None
        if it.get("kind") == "video":
            src = str(it.get("src") or "")
            if src.startswith("media:") and scr["kind"] != "receiver":
                raise CastError("a big stored video casts to receiver screens only (inline videos go anywhere)")
            video_src = media_url(it) if (media_url and scr["kind"] == "receiver") else src
        html = cast_html(it, video_src=video_src)
        if scr["kind"] == "receiver":
            _write_atomic(SCREENS_DIR / name / (page + ".html"), html)
            pages = sorted((SCREENS_DIR / name).glob("cast-*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
            for p in pages[KEEP:]:
                p.unlink(); p.with_suffix(".js").unlink(missing_ok=True)
        elif scr["kind"] == "http":
            code, r = post(scr["url"], {"name": page, "html": html})
            if code != 200:
                raise CastError(f"{name} refused the page ({code}): {r.get('error') or r}")
        else:
            d = scr.get("dir", "~/screen")
            ssh(scr["host"], ["sh", "-c", f"mkdir -p {d} && cat > {d}/{page}.html.tmp && mv {d}/{page}.html.tmp {d}/{page}.html"],
                data=html.encode())
            if scr.get("show_url"):
                code, r = post(scr["show_url"], {"name": page})
                if code != 200:
                    raise CastError(f"{name} did not switch to the page ({code})")
        st = {"i": 0} if it.get("kind") == "gallery" else ({"p": 0, "t": 0.0, "at": int(time.time() * 1000)} if it.get("kind") == "video" else {})
        if st:
            _push_state(scr, page, st, post=post, ssh=ssh)
        _write_atomic(_now_file(name), json.dumps({"page": page, "kind": it.get("kind"), "id": it.get("id"), "state": st}))
        log(event="cast", screen=name, who=who, id=it.get("id"), kind=it.get("kind"))
        return {"screen": name, "page": page}
    except CastError as ex:
        log(event="cast_failed", screen=name, who=who, id=it.get("id"), error=str(ex))
        raise


def pick(it, name, i, who, post=_post, ssh=_ssh):
    """A gallery already on screen `name`: show picture i there."""
    try:
        scr, now = screens().get(name), now_on(name)
        if not scr or not now or now.get("id") != it.get("id"):
            raise CastError(f"this gallery is not on {name}; cast it first")
        _push_state(scr, now["page"], {"i": int(i)}, post=post, ssh=ssh)
        now["state"] = {"i": int(i)}
        _write_atomic(_now_file(name), json.dumps(now))
        log(event="pick", screen=name, who=who, i=i)
        return {"screen": name, "i": int(i)}
    except CastError as ex:
        log(event="pick_failed", screen=name, who=who, error=str(ex))
        raise


def control(name, cmd, who, t=None, post=_post, ssh=_ssh, clock=time.time):
    """Video transport on screen `name`. The screen plays the file; this keeps where it should be.
    -> {"t", "paused"}. Raises CastError (logged)."""
    try:
        scr, now = screens().get(name), now_on(name)
        if cmd not in VIDEO_CMDS:
            raise CastError(f"unknown control {cmd!r}")
        if not scr or not now or now.get("kind") != "video":
            raise CastError(f"no video is on {name}")
        st = dict(now.get("state") or {"p": 0, "t": 0.0, "at": int(clock() * 1000)})
        pos = st["t"] if st.get("p") else st["t"] + (clock() * 1000 - st["at"]) / 1000.0
        if cmd == "status":
            return {"t": round(pos, 2), "paused": bool(st.get("p"))}
        if cmd == "pause":
            st = {"p": 1, "t": pos, "at": int(clock() * 1000)}
        elif cmd == "play":
            st = {"p": 0, "t": pos, "at": int(clock() * 1000)}
        elif cmd == "seek":
            if t is None:
                raise CastError("seek needs t")
            st = {"p": st.get("p", 0), "t": max(0.0, float(t)), "at": int(clock() * 1000)}
        else:
            st = {"p": 0, "t": 0.0, "at": int(clock() * 1000)}
        _push_state(scr, now["page"], st, post=post, ssh=ssh)
        now["state"] = st
        _write_atomic(_now_file(name), json.dumps(now))
        log(event="control", screen=name, who=who, cmd=cmd, t=round(st["t"], 2), paused=bool(st["p"]))
        return {"t": st["t"], "paused": bool(st["p"])}
    except CastError as ex:
        log(event="control_failed", screen=name, who=who, cmd=cmd, error=str(ex))
        raise


def stop(name, who, post=_post, ssh=_ssh):
    """Take the cast off screen `name` (a receiver goes back to its idle card)."""
    try:
        scr = screens().get(name)
        if not scr:
            raise CastError(f"unknown screen {name!r}")
        if scr["kind"] == "http" and scr.get("stop_url"):
            code, _ = post(scr["stop_url"], {})
            if code != 200:
                raise CastError(f"{name} did not stop ({code})")
        _now_file(name).unlink(missing_ok=True)
        log(event="stop", screen=name, who=who)
        return {"screen": name, "stopped": True}
    except CastError as ex:
        log(event="stop_failed", screen=name, who=who, error=str(ex))
        raise
