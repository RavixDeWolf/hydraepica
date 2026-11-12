"""server.py - the agent Mini App: one stage per person that the agent renders into (TIK-1004).

Opened from the bot. Every /api request is either:
  - a person: Telegram initData verified by shared/tg_webapp_auth (Rav, a family member, a family member only), or
  - the agent pushing: header X-Push-Key = the local key in <state_dir path> (stage.py reads it).
Serves only on a unix socket (the public door is added in a later phase); nothing listens on 0.0.0.0.

Stage items (newest first, per person): card {title, body} (content only; the page owns all styling, the
the auth gate "card" contract), image {src: data: URI}, video {src: data:video/ URI, played inline}, html {html} (rendered in a sandboxed iframe, no scripts
reach the app), url {url} (a link card). The open page gets each new item over SSE at once.
"""
import asyncio
import json
import hmac
import hashlib
import re
import os
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # harness/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import tg_webapp_auth  # noqa: E402

HERE = Path(__file__).parent
STATE = Path(os.environ.get("STAGE_STATE", os.path.join(config.STATE_DIR, "stage")))
PUSH_KEY_FILE = STATE / "push.key"
PEOPLE = dict(config.USERS)              # telegram id -> name: the only people with a stage
SETTINGS_ADMINS = set(config.ADMINS)     # only admins change settings, and only from the app itself
MAX_ITEMS = 60
MAX_ITEM_BYTES = 6_000_000
KINDS = {"card", "image", "video", "html", "url", "gallery"}
MAX_GALLERY = 60

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_subs = {}                                # person -> set of asyncio.Queue


def bot_token():
    try:
        return config.bot_token()
    except Exception:
        return ""


def push_key():
    try:
        return PUSH_KEY_FILE.read_text().strip()
    except OSError:
        return ""


TICKET_TTL_S = 3600      # a URL ticket: long enough for a film, short enough that a logged URL soon goes dead


def _ticket_key(token=None):
    return hmac.new((token if token is not None else bot_token()).encode(), b"stage-url-ticket", hashlib.sha256).digest()


def make_ticket(uid, token=None, now=None):
    """-> 'uid.exp.sig'. Used in URLs (<video src>, EventSource) that cannot send the X-Init-Data header, so the
    Telegram login data itself never lands in a URL, a proxy log or a Referer."""
    exp = int((now or time.time()) + TICKET_TTL_S)
    sig = hmac.new(_ticket_key(token), f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{uid}.{exp}.{sig}"


def _ticket_uid(t, token=None, now=None):
    try:
        uid, exp, sig = t.split(".")
        good = hmac.new(_ticket_key(token), f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, good) and int(exp) > (now or time.time()) and int(uid) in PEOPLE:
            return int(uid)
    except (ValueError, AttributeError):
        pass
    raise HTTPException(401, "ticket invalid or expired")


def person_for(request: Request, token=None):
    """-> (person, uid). initData in the X-Init-Data header, or a short-lived ?t= ticket for URLs that cannot
    send headers. initData is never accepted from the query string."""
    t = request.query_params.get("t")
    if t and not request.headers.get("x-init-data"):
        uid = _ticket_uid(t, token)
        return PEOPLE[uid], uid
    data = request.headers.get("x-init-data") or ""
    try:
        user = tg_webapp_auth.verify(data, token if token is not None else bot_token(), allowed_ids=set(PEOPLE))
    except tg_webapp_auth.AuthError as e:
        raise HTTPException(e.status, e.detail)
    return PEOPLE[int(user["id"])], int(user["id"])


MEDIA_RE = re.compile(r"^media:(" + "|".join(map(re.escape, sorted(PEOPLE.values()))) + r")/([A-Za-z0-9._-]{1,120})$")


def media_path(src):
    """'media:<person>/<file>' (a big video kept on disk, not inline) -> its Path if it exists, else None."""
    m = MEDIA_RE.match(str(src or ""))
    if not m or m.group(2).startswith("."):
        return None
    f = STATE / "media" / m.group(1) / m.group(2)
    return f if f.is_file() else None


def is_pusher(request: Request):
    k = push_key()
    import hmac
    return bool(k) and hmac.compare_digest(request.headers.get("x-push-key", ""), k)


def _file(person):
    return STATE / "stage" / f"{person}.json"


_lock = threading.RLock()                 # sync handlers run in a threadpool: every read-modify-write holds this
ARCHIVE_DAYS = 90                         # Rav 2026-10-24: archived stages live 90 days, saved ones forever
HEAVY = ("body", "src", "html", "images")          # list views carry metadata only; the full item is fetched on open
LISTS = ("history", "saved", "inbox", "archive")


def load(person):
    """-> the person's state, migrated from the old {"items": [...]} shape and pruned of expired archive."""
    try:
        st = json.loads(_file(person).read_text())
    except Exception:
        st = {}
    if "items" in st and "history" not in st:          # pre-2026-10-24 shape: items newest first, all shown
        st["history"] = st.pop("items")
        st["live"] = st["history"][0]["id"] if st["history"] else None
    for k in LISTS:
        st.setdefault(k, [])
    st.setdefault("live", None)
    # ONE STAGE, ONE IDENTITY (Rav 2026-10-24: "when a stage is made, and snapshotted in this way it should be the
    # same stage"). Early saves made a new id per save (origin "history:<id>"): fold those back onto the source id.
    seen, kept = set(), []
    for i in st["saved"]:
        o = str(i.get("origin") or "")
        if ":" in o and o.split(":", 1)[0] in LISTS:
            i["id"] = o.split(":", 1)[1]
            i.pop("origin", None)
        if i["id"] not in seen:
            seen.add(i["id"]); kept.append(i)
    st["saved"] = kept
    cutoff = time.time() - ARCHIVE_DAYS * 86400
    gone = [i["id"] for i in st["archive"] if i.get("archived_ts", i.get("ts", 0)) < cutoff]
    if gone:
        st["archive"] = [i for i in st["archive"] if i["id"] not in gone]
        print(f"miniapp: {person}: {len(gone)} archived stage(s) past {ARCHIVE_DAYS} days deleted: {gone}", flush=True)
    return st


def save(person, st):
    f = _file(person)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, f)


def validate(item):
    """-> a clean item (kind checked, unknown fields dropped). Raises 400/413."""
    kind = item.get("kind")
    if kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(KINDS)}")
    if len(json.dumps(item)) > MAX_ITEM_BYTES:
        raise HTTPException(413, "item too large")
    if kind == "image" and not str(item.get("src", "")).startswith("data:image/"):
        raise HTTPException(400, "image src must be a data:image/ URI")
    if kind == "video" and not (str(item.get("src", "")).startswith("data:video/") or media_path(item.get("src"))):
        raise HTTPException(400, "video src must be a data:video/ URI or media:<person>/<file> already on disk")
    if kind == "url" and not str(item.get("url", "")).startswith(("https://", "http://")):
        raise HTTPException(400, "url must be http(s)")
    if kind == "gallery":
        ims = item.get("images")
        if not isinstance(ims, list) or not 1 <= len(ims) <= MAX_GALLERY:
            raise HTTPException(400, f"gallery images must be a list of 1-{MAX_GALLERY}")
        if not all(isinstance(i, dict) and str(i.get("src", "")).startswith("data:image/") for i in ims):
            raise HTTPException(400, "every gallery image src must be a data:image/ URI")
    clean = {k: item[k] for k in ("kind", "title", "body", "src", "html", "url", "caption") if k in item}
    if kind == "gallery":
        clean["images"] = [{"src": i["src"], "caption": str(i.get("caption") or "")[:200],
                            "intent": str(i.get("intent") or "")[:2000],
                            "note": str(i.get("note") or "")[:2000],
                            **({"video": i["video"]} if media_path(i.get("video")) else {})} for i in item["images"]]
    return clean


_last_id = [0]


def new_id(now=None):
    """Millisecond ids, strictly increasing (two pushes in one ms must not collide)."""
    with _lock:
        i = max(int((now or time.time()) * 1000), _last_id[0] + 1)
        _last_id[0] = i
        return str(i)


def _put_live(st, item):
    """Front of history + live. Overflow moves to the archive (never silently dropped). -> ids archived."""
    st["history"].insert(0, item)
    st["live"] = item["id"]
    over = st["history"][MAX_ITEMS:]
    st["history"] = st["history"][:MAX_ITEMS]
    for o in over:
        o["archived_ts"] = time.time()
        st["archive"].insert(0, o)
    return [o["id"] for o in over]


def add_item(person, item, now=None):
    """Validate + store one item as the person's new live stage. Pure apart from the file. -> the stored item."""
    if person not in PEOPLE.values():
        raise HTTPException(404, "no such person")
    clean = validate(item)
    clean["id"] = new_id(now)
    clean["ts"] = now or time.time()
    with _lock:
        st = load(person)
        _put_live(st, clean)
        save(person, st)
    return clean


def meta(it):
    return {k: v for k, v in it.items() if k not in HEAVY}


def view(st):
    """The state as the page gets it: every list as metadata, plus the live item in full."""
    live = next((i for i in st["history"] if i["id"] == st["live"]), None)
    return {"live": live, **{k: [meta(i) for i in st[k]] for k in LISTS}}


def find(st, where, iid):
    if where not in LISTS:
        raise HTTPException(400, f"where must be one of {list(LISTS)}")
    for i in st[where]:
        if i["id"] == iid:
            return i
    raise HTTPException(404, f"no stage {iid} in {where}")


def act(person, where, iid, action, to=None, title=None, i=None):
    """One stage action for one person. -> (result dict, [(other_person, event)]) . Holds the lock."""
    with _lock:
        st = load(person)
        it = find(st, where, iid)
        out, others = {"ok": True, "action": action}, []
        if action == "show":                        # make it the live stage
            if where == "history":
                st["live"] = iid
            elif where == "archive":                # restore: back into history, live
                st["archive"].remove(it)
                it.pop("archived_ts", None)
                out["archived"] = _put_live(st, it)
            elif any(h["id"] == iid for h in st["history"]):
                st["live"] = iid                    # the same stage is already in Recent: just make it live
            else:                                   # saved / inbox stay where they are; the SAME stage goes live
                cp = {k: v for k, v in it.items() if k not in ("fav", "from", "sent_ts", "saved_ts")}
                out["archived"] = _put_live(st, cp)
        elif action == "archive":
            if where == "archive":
                raise HTTPException(400, "already archived")
            st[where].remove(it)
            it["archived_ts"] = time.time()
            st["archive"].insert(0, it)
        elif action == "delete":
            st[where].remove(it)
        elif action == "save":                      # permanent copy in Saved (a template); never expires
            out["saved_id"] = iid                   # same stage, same id: saving twice is still one saved stage
            if where == "saved" or any(x["id"] == iid for x in st["saved"]):
                out["already"] = True
            else:
                cp = {k: v for k, v in it.items() if k not in ("archived_ts", "from", "sent_ts")}
                cp["saved_ts"] = time.time()
                st["saved"].insert(0, cp)
        elif action == "rename":                    # every place this stage appears gets the new name
            name = str(title or "").strip()
            if not name or len(name) > 120:
                raise HTTPException(400, "a name is 1-120 characters")
            for k in LISTS:
                for x in st[k]:
                    if x["id"] == iid:
                        x["title"] = name
            out["title"] = name
        elif action == "note":                      # a note under one gallery picture, for the agent to read
            if it.get("kind") != "gallery":
                raise HTTPException(400, "notes are for gallery pictures")
            try:
                k = int(i)
            except (TypeError, ValueError):
                raise HTTPException(400, "note needs i")
            if not 0 <= k < len(it["images"]):
                raise HTTPException(400, "i out of range")
            text = str(title or "").strip()[:2000]
            for lst in LISTS:                       # same stage everywhere it appears
                for x in st[lst]:
                    if x["id"] == iid and x.get("kind") == "gallery":
                        x["images"][k]["note"] = text
            out["note"] = text
        elif action == "rate":                      # 1-6 stars on one gallery picture (6 = bonus star); 0 clears
            if it.get("kind") != "gallery":
                raise HTTPException(400, "ratings are for gallery pictures")
            try:
                k, stars = int(i), int(title)
            except (TypeError, ValueError):
                raise HTTPException(400, "rate needs i and stars in title")
            if not 0 <= k < len(it["images"]) or not 0 <= stars <= 6:
                raise HTTPException(400, "i or stars out of range")
            for lst in LISTS:
                for x in st[lst]:
                    if x["id"] == iid and x.get("kind") == "gallery":
                        x["images"][k]["stars"] = stars
            out["stars"] = stars
        elif action in ("fav", "unfav"):
            if where != "saved":
                raise HTTPException(400, "only saved stages can be favorited")
            it["fav"] = action == "fav"
        elif action == "send":
            if to not in PEOPLE.values() or to == person:
                raise HTTPException(400, "send needs to= another person")
            cp = {k: v for k, v in it.items() if k not in ("archived_ts", "fav", "saved_ts")}
            cp["from"], cp["sent_ts"] = person, time.time()      # same stage id in their inbox; resending updates it
            other = load(to)
            other["inbox"] = [cp] + [x for x in other["inbox"] if x["id"] != iid]
            save(to, other)
            others.append((to, {"type": "inbox", "item": meta(cp)}))
        else:
            raise HTTPException(400, "unknown action")
        if st["live"] and not any(i["id"] == st["live"] for i in st["history"]):
            st["live"] = None                       # the live stage was archived or deleted
        save(person, st)
        return out, others


def _notify(person, event):
    for q in list(_subs.get(person, ())):
        q.put_nowait(event)


@app.get("/health")
def health():
    return {"ok": True, "service": "stage", "people_online": {p: len(s) for p, s in _subs.items()}}


@app.get("/app")
def page():
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-store"})


def _screen_names():
    try:
        import cast as _cast
        return sorted(_cast.screens())
    except Exception:
        return []


@app.get("/api/me")
def me(request: Request):
    person, uid = person_for(request)
    return {"person": person, "settings_admin": uid in SETTINGS_ADMINS, "agent": config.AGENT_NAME,
            "others": sorted(p for p in PEOPLE.values() if p != person), "live": uid in LIVE_USERS,
            "screens": _screen_names()}


@app.get("/api/ticket")
def ticket(request: Request):
    """A fresh URL ticket. Needs the real login header, so a leaked ticket cannot renew itself."""
    if not request.headers.get("x-init-data"):
        raise HTTPException(401, "send X-Init-Data")
    _, uid = person_for(request)
    return {"t": make_ticket(uid), "ttl": TICKET_TTL_S}


@app.get("/api/stage")
def stage(request: Request):
    person, _ = person_for(request)
    with _lock:
        return view(load(person))


@app.get("/api/media/{fname}")
def media(fname: str, request: Request):
    """A big stage video, streamed with Range support. ?t= ticket because a <video> tag cannot send headers."""
    person, _ = person_for(request)
    f = media_path(f"media:{person}/{fname}")
    if not f:
        raise HTTPException(404, "no such media")
    from fastapi.responses import FileResponse
    return FileResponse(f)


@app.get("/api/item/{where}/{iid}")
def item(where: str, iid: str, request: Request):
    person, _ = person_for(request)
    with _lock:
        return find(load(person), where, iid)


@app.post("/api/act")
async def act_route(request: Request):
    person, _ = person_for(request)
    b = await request.json()
    if b.get("action") in ("cast", "pick"):    # optional casting: network work, so it runs outside the state lock
        import cast as _cast
        with _lock:
            it = find(load(person), b.get("where", ""), str(b.get("id", "")))
        if b.get("to") not in _cast.screens():
            raise HTTPException(400, f"to must be one of {sorted(_cast.screens())}")
        try:
            if b["action"] == "cast":
                res = await asyncio.to_thread(_cast.cast, it, b["to"], person, media_url=lambda x: _screen_media(b["to"], x.get("src")))
            else:
                if it.get("kind") != "gallery":
                    raise HTTPException(400, "pick needs a gallery")
                res = await asyncio.to_thread(_cast.pick, it, b["to"], int(b.get("i", 0)), person)
            return {"ok": True, "action": b["action"], **res}
        except _cast.CastError as e:
            raise HTTPException(502, f"{b['to']}: {e}")
    out, others = act(person, b.get("where", ""), str(b.get("id", "")), b.get("action", ""), b.get("to"), b.get("title"), b.get("i"))
    for who, ev in others:
        _notify(who, ev)
    _notify(person, {"type": "state"})
    return out


def _screen_media(screen, src):
    """A big stage video ("media:<person>/<file>") as a URL the receiver screen can load with its own key."""
    m = MEDIA_RE.match(str(src or ""))
    if not m:
        return src
    import cast as _cast
    return f"/screen/{screen}/k/{_cast.screen_key(screen)}/media/{m.group(1)}/{m.group(2)}"


@app.post("/api/uncast")
async def uncast(request: Request):
    person, _ = person_for(request)
    import cast as _cast
    to = (await request.json()).get("to")
    try:
        return {"ok": True, **(await asyncio.to_thread(_cast.stop, to, person))}
    except _cast.CastError as e:
        raise HTTPException(502, f"{to}: {e}")


@app.post("/api/castctl")
async def castctl(request: Request):
    person, _ = person_for(request)
    import cast as _cast
    b = await request.json()
    try:
        t = float(b["t"]) if b.get("cmd") == "seek" else None
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "seek needs t (seconds)")
    try:
        return {"ok": True, **(await asyncio.to_thread(_cast.control, b.get("to"), b.get("cmd"), person, t))}
    except _cast.CastError as e:
        raise HTTPException(502, f"{b.get('to')}: {e}")


# ---- the built-in receiver screen: a TV's browser opens /screen/<name>?k=<key> full screen ----
def _screen_gate(name, request, key=None):
    """The receiver's only credential is its own key (?k= on the page, a path segment for the files the cast page
    loads relative to itself). No key configured = the screen is off."""
    import cast as _cast
    import hmac
    k = _cast.screen_key(name)
    if not k or not hmac.compare_digest(key if key is not None else request.query_params.get("k", ""), k):
        raise HTTPException(401, "screen key required")
    return _cast


@app.get("/screen/{name}")
def screen_page(name: str, request: Request):
    _screen_gate(name, request)
    return FileResponse(HERE / "screen.html", media_type="text/html")


@app.get("/screen/{name}/now")
def screen_now(name: str, request: Request):
    _cast = _screen_gate(name, request)
    now = _cast.now_on(name) or {}
    return {"page": now.get("page"), "agent": config.AGENT_NAME}


@app.get("/screen/{name}/k/{key}/f/{fname}")
def screen_file(name: str, key: str, fname: str, request: Request):
    _cast = _screen_gate(name, request, key)
    if not re.fullmatch(r"cast-[A-Za-z0-9_-]{1,80}\.(html|js)", fname):
        raise HTTPException(404, "no such file")
    f = _cast.SCREENS_DIR / name / fname
    if not f.is_file():
        raise HTTPException(404, "no such file")
    return FileResponse(f, media_type="text/html" if fname.endswith(".html") else "text/javascript",
                        headers={"Cache-Control": "no-store"})


@app.get("/screen/{name}/k/{key}/media/{person}/{fname}")
def screen_media(name: str, key: str, person: str, fname: str, request: Request):
    _screen_gate(name, request, key)
    f = media_path(f"media:{person}/{fname}")
    if not f:
        raise HTTPException(404, "no such media")
    return FileResponse(f)


@app.post("/api/push/{person}")
async def push(person: str, request: Request):
    if not is_pusher(request):
        raise HTTPException(401, "push key required")
    item = add_item(person, await request.json())
    _notify(person, {"type": "state"})
    return {"ok": True, "id": item["id"]}


@app.post("/api/clear")
def clear(request: Request):
    """Blank the live stage. History, saved, inbox and archive are kept (Rav 2026-10-24)."""
    person, _ = person_for(request)
    with _lock:
        st = load(person)
        st["live"] = None
        save(person, st)
    _notify(person, {"type": "state"})
    return {"ok": True}


@app.get("/api/events")
async def events(request: Request):
    person, _ = person_for(request)
    q = asyncio.Queue()
    _subs.setdefault(person, set()).add(q)

    async def stream():
        try:
            yield "event: hello\ndata: {}\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            _subs.get(person, set()).discard(q)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


BROWSER_CDP = config.section("stage").get("browser_cdp", "http://127.0.0.1:9222")   # optional: Chrome --remote-debugging-port
MAX_BROWSER_URL = 2048
BROWSER_AUDIT = STATE / "browser_open.jsonl"


def _browser_log(event, **kw):
    """Every browser action and failure leaves a line in the journal AND in an audit file (Iron Rule 12)."""
    line = {"ts": round(time.time(), 3), "event": event, **kw}
    print("miniapp: browser " + json.dumps(line), flush=True)
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(BROWSER_AUDIT, "a") as f:
            f.write(json.dumps(line) + "\n")
    except OSError as e:
        print(f"miniapp: browser audit write FAILED: {e}", flush=True)


def _browser_503(what, e):
    """A CDP call that fails or hangs is a 503 with the truth - never a frozen image shown as live (TIK-1509 slice 4)."""
    hang = (isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)
            or "timed out" in str(e).lower())
    _browser_log("fail", what=what, hang=hang, error=f"{type(e).__name__}: {e}"[:200])
    raise HTTPException(503, ("browser not answering" if hang else "browser unavailable")
                        + f" ({what})")


def _clean_browser_url(u):
    """-> a safe http(s) URL or None. No file:, chrome:, javascript:, data:, no credentials in the URL, no spaces/controls."""
    import urllib.parse
    if not isinstance(u, str):
        return None
    u = u.strip()
    if not u or len(u) > MAX_BROWSER_URL or any(ord(c) < 33 or ord(c) == 127 for c in u):
        return None
    try:
        p = urllib.parse.urlsplit(u)
        host = p.hostname
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not host or p.username or p.password:
        return None
    return p.geturl()


@app.post("/api/browser/open")
async def browser_open(request: Request):
    """Push a URL into a NEW tab of the agent's browser (TIK-1509 slice 1). Rav only; never navigates the tab the agent is using."""
    import urllib.parse
    import urllib.request
    person, uid = person_for(request)
    if uid not in BROWSER_ADMINS:
        raise HTTPException(403, "the browser is Rav's")
    try:
        b = await request.json()
    except Exception:
        b = {}
    url = _clean_browser_url(b.get("url")) if isinstance(b, dict) else None
    if not url:
        raise HTTPException(400, "need url= (http or https only)")

    def _open():
        req = urllib.request.Request(BROWSER_CDP + "/json/new?" + urllib.parse.quote(url, safe=""), method="PUT")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    try:
        tab = await asyncio.to_thread(_open)
    except Exception as e:
        _browser_503("open", e)
    _browser_log("open", by=person, url=url, tab=(tab or {}).get("id"))
    return {"ok": True, "url": url, "tab": (tab or {}).get("id")}


@app.get("/api/browser")
def browser(request: Request):
    """A fresh screenshot of the agent browser (CDP on 127.0.0.1). Admins only: it shows signed-in pages."""
    _, uid = person_for(request)
    if uid not in BROWSER_ADMINS:
        raise HTTPException(403, "the browser is the owner's")
    try:
        import base64
        import urllib.request
        tabs = json.load(urllib.request.urlopen(BROWSER_CDP + "/json/list", timeout=5))
        page_tab = next(t for t in tabs if t.get("type") == "page")
        png = _cdp_screenshot(page_tab["webSocketDebuggerUrl"])
        return {"title": page_tab.get("title"), "url": page_tab.get("url"),
                "src": "data:image/png;base64," + base64.b64encode(png).decode()}
    except StopIteration:
        return {"title": None, "url": None, "src": None, "note": "no page open"}
    except Exception as e:
        _browser_503("view", e)


def _cdp_screenshot(ws_url):
    import base64
    from websockets.sync.client import connect
    with connect(ws_url, max_size=50_000_000, open_timeout=5) as ws:
        ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
        while True:
            m = json.loads(ws.recv(timeout=15))
            if m.get("id") == 1:
                return base64.b64decode(m["result"]["data"])


BROWSER_ADMINS = set(config.ADMINS)        # only admins drive the browser
LIVE_USERS = {config.OWNER_ID}           # live mode talks to the main head, which is the owner's


def _live_user(request):
    person, uid = person_for(request)
    if uid not in LIVE_USERS:
        raise HTTPException(403, "live mode talks to the main head, which is the owner's")
    return uid


@app.post("/api/live/frame")
async def live_frame(request: Request):
    _live_user(request)
    import live
    try:
        live.save_frame(await request.body())
    except live.LiveError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/live/say")
async def live_say(request: Request):
    uid = _live_user(request)
    import live
    try:
        try:
            hold_s = float(request.headers.get("x-hold-ms") or 0) / 1000
        except ValueError:
            hold_s = 0
        job = live.say(uid, await request.body(), request.headers.get("content-type", ""), hold_s=hold_s,
                       lane=request.headers.get("x-live-lane"))
    except live.LiveError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "job": os.path.basename(job)}


_speak = {}                               # id -> (person, text, ts): a spoken answer waiting for the page to fetch it
SPEAK_TTL = 300


@app.post("/api/push-speak/{person}")    # under /api/push*: the public door answers 403, only the host's socket reaches it
async def push_speak(person: str, request: Request):
    """The head lane, after a Live-mode answer: say this on that person's open Live page. -> live (false = the Live
    tab is not open; the lane then sends a Telegram voice note instead)."""
    if not is_pusher(request):
        raise HTTPException(401, "push key required")
    body = await request.json()
    text = " ".join(str(body.get("text") or "").split())[:4000]
    if person not in PEOPLE.values() or not text:
        raise HTTPException(400, "person and text required")
    now = time.time()
    for k in [k for k, v in _speak.items() if now - v[2] > SPEAK_TTL]:
        del _speak[k]
    sid = os.urandom(8).hex()
    _speak[sid] = (person, text, now)
    import live
    on = bool(_subs.get(person)) and live.session_on(now)     # the Live session's heartbeat, not camera frames
    if on:
        _notify(person, {"type": "speak", "id": sid})
    return {"ok": True, "id": sid, "live": on}


LIVE_STATES = {"heard", "unheard", "answered"}


@app.post("/api/push-live/{person}")     # under /api/push*: the host's socket only
async def push_live(person: str, request: Request):
    """The lane tells the open Live page where its clip is (Rav 18:35: no indicator after letting go)."""
    if not is_pusher(request):
        raise HTTPException(401, "push key required")
    body = await request.json()
    if person not in PEOPLE.values() or body.get("state") not in LIVE_STATES:
        raise HTTPException(400, "person and a known state required")
    _notify(person, {"type": "live", "state": body["state"], "text": str(body.get("text") or "")[:300]})
    return {"ok": True}


@app.get("/api/live/voice/{sid}")
def live_voice(sid: str, request: Request):
    person, _ = person_for(request)
    it = _speak.get(sid)
    if not it or it[0] != person:
        raise HTTPException(404, "nothing to say")
    import live
    return StreamingResponse(live.speak_pcm(it[1]), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": str(live.VOICE_RATE), "Cache-Control": "no-store"})


@app.post("/api/live/warm")
def live_warm(request: Request):
    """Start + each Hold-to-talk press: have the agent voice loaded before the answer exists."""
    _live_user(request)
    import live
    return {"ok": True, "voice": live.warm()}


@app.post("/api/live/segment")
async def live_segment(request: Request):
    """One release of Hold to talk: kept as a segment of the open turn and transcribed in the background now.
    The page hears {type:"seg", i, state, text} when the transcript is ready."""
    uid = _live_user(request)
    person = PEOPLE[uid]
    import live
    loop = asyncio.get_running_loop()
    def notify(seg):                       # from the STT thread: hand the event to the loop, never touch it directly
        loop.call_soon_threadsafe(_notify, person, {"type": "seg", "i": seg["i"], "state": seg["state"],
                                                    "text": seg["text"] or ""})
    try:
        hold_s = float(request.headers.get("x-hold-ms") or 0) / 1000
    except ValueError:
        hold_s = 0
    try:
        seg = live.add_segment(uid, await request.body(), request.headers.get("content-type", ""), hold_s, notify=notify)
    except live.LiveError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "i": seg["i"]}


@app.post("/api/live/beat")
def live_beat(request: Request):
    """The Live session is open (every 3 s); ?off=1 when Stop is tapped."""
    _live_user(request)
    import live
    live.beat(on=request.query_params.get("off") != "1")
    return {"ok": True}


@app.get("/api/live/lanes")
def live_lanes(request: Request):
    """The heads Live can talk to (the picker next to Start)."""
    import live
    return {"lanes": live.lanes(_live_user(request))}


@app.post("/api/live/end")
def live_end(request: Request):
    """the agent's turn: the finished transcripts + one sheet of the whole turn go to the head lane."""
    uid = _live_user(request)
    import live
    try:
        job = live.end_turn(uid, lane=request.headers.get("x-live-lane"))
    except live.LiveError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "job": os.path.basename(job)}


@app.get("/api/live/turn")
def live_turn(request: Request):
    """The open turn's clips: a reopened page shows them, still sendable (Rav 20:49)."""
    uid = _live_user(request)
    import live
    return {"segments": live.open_turn(uid)}


@app.post("/api/live/clear")
def live_clear(request: Request):
    uid = _live_user(request)
    import live
    return {"ok": True, "dropped": live.clear_turn(uid)}


@app.get("/api/live/status")
def live_status(request: Request):
    _live_user(request)
    import live
    return live.status()


@app.get("/api/head")
def head(request: Request):
    """Read-only head status for the Settings tab: model / mode as the head last reported them."""
    person_for(request)
    ev = os.path.join(config.STATE_DIR, "events.jsonl")
    mode = None
    try:
        with open(ev, "rb") as f:
            f.seek(max(0, os.path.getsize(ev) - 200_000))
            for line in f.read().splitlines()[::-1]:
                d = (json.loads(line).get("data") or {})
                if d.get("permission_mode"):
                    mode = d["permission_mode"]
                    break
    except Exception:
        pass
    return {"mode": mode}
