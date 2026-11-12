"""tgapi.py - the small Telegram Bot API layer every lane shares: send, reply, upload, download, log.

Flood control (429 retry_after) is honoured and every retry is logged. Outbound posts are mirrored into the
chat ledger so the history holds both sides of the conversation.
"""
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request

import config

LOG_PATH = os.path.join(config.STATE_DIR, "logs", "lane.log")
_lock = threading.Lock()
_j_groups = {}        # media_group_id -> expiry: albums whose first item was addressed to the head


def j_group(gid, now=None):
    now = time.time() if now is None else now
    with _lock:
        return bool(gid) and _j_groups.get(gid, 0) > now


def _log(event, **kw):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **kw}) + "\n")
    except Exception as e:
        print(f"tgapi log write failed: {e}")


RETRY_429 = 2               # tries after the first, honouring Telegram's retry_after
MAX_RETRY_AFTER_S = 30


def retry_after_of(err):
    """A urllib HTTPError -> seconds Telegram asked us to wait on a 429, else None. Never raises."""
    if getattr(err, "code", None) != 429:
        return None
    try:
        body = json.loads(err.read() or b"{}")
        return float((body.get("parameters") or {}).get("retry_after") or 1)
    except Exception:
        return 1.0


def _open(req, timeout, method, opener=None, sleep=time.sleep):
    """urlopen with Telegram flood control: a 429 waits retry_after (capped) and retries, each one LOGGED
    (2026-10-24 14:32: a burst of typing/bubble/voice calls drew 429s and the voice reply was simply lost)."""
    opener = opener or urllib.request.urlopen
    for attempt in range(RETRY_429 + 1):
        try:
            return opener(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            wait = retry_after_of(e)
            if wait is None or attempt == RETRY_429:
                if wait is not None:
                    _log("tg_429_gave_up", method=method, retry_after=wait, attempts=attempt + 1)
                raise
            _log("tg_429_retry", method=method, retry_after=wait, attempt=attempt + 1)
            sleep(min(wait, MAX_RETRY_AFTER_S))

def _api(tok, method, **kw):
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/{method}",
                                 data=json.dumps(kw).encode(), headers={"Content-Type": "application/json"})
    r = json.load(_open(req, 30, method))
    if method in ("sendMessage", "editMessageText"):
        mirror_out(method, kw, r, bot_id=str(tok).split(":", 1)[0])   # the bot's numeric id (public), never the token
    return r


def mirror_out(method, kw, r, db=None, now=time.time, bot_id=None):
    """TIK-1416: the bot's OWN posts (cards, replies, status bubbles) land in chat_log next to the inbound rows, through
    chat_ledger (the ONE writer for Telegram rows, TIK-1027): a send is a generic kind="post" row that a later
    record_out for the same message enriches; an edit follows the screen (a card keeps its '-> Allowed' mark).
    Never raises: a ledger miss is logged, the post already went out."""
    try:
        res = (r or {}).get("result") or {}
        if not isinstance(res, dict):
            return False
        mid = res.get("message_id") or kw.get("message_id")
        chat = (res.get("chat") or {}).get("id", kw.get("chat_id"))
        if mid is None or chat is None:
            return False
        text = kw.get("text") or kw.get("caption") or ""
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import chat_ledger
        if method == "editMessageText":
            # Telegram answers an edit with the message's ORIGINAL date (+ edit_date): that is the row's own clock
            return chat_ledger.record_sent_edit(chat, mid, text, db=db, now=float(res.get("date") or now()), bot_id=bot_id)
        buttons = [b.get("text") for row in (kw.get("reply_markup") or {}).get("inline_keyboard", []) for b in row
                   if isinstance(b, dict)]
        kind = "card" if buttons else "post"
        return chat_ledger.record_out(chat, kw.get("message_thread_id"), mid, text, kind=kind,
                                      reply_to=kw.get("reply_to_message_id"), db=db, now=float(res.get("date") or now()),
                                      bot_id=bot_id)
    except Exception as e:
        _log("mirror_out_fail", method=method, error=str(e)[:200])
        return False


def _reply(tok, m, text):
    kw = {"chat_id": m["chat"]["id"], "text": text, "reply_to_message_id": m.get("message_id")}
    if m.get("message_thread_id") is not None:
        kw["message_thread_id"] = m["message_thread_id"]
    try:
        _api(tok, "sendMessage", **kw)
    except Exception as e:
        _log("reply_fail", error=str(e)[:200])


def _send_photo(tok, m, path, caption):
    _send_document(tok, m, path, caption, method="sendPhoto", field="photo", ctype="image/jpeg")


def _send_document(tok, m, path, caption, method="sendDocument", field="document", ctype="application/octet-stream"):
    b = "----prl" + os.urandom(8).hex()
    fields = {"chat_id": str(m["chat"]["id"]), "caption": caption}
    if m.get("message_thread_id") is not None:
        fields["message_thread_id"] = str(m["message_thread_id"])
    body = b""
    for k, v in fields.items():
        body += f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    with open(path, "rb") as fh:
        data = fh.read()
    body += (f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{os.path.basename(path)}\"\r\n"
             f"Content-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n" + f"--{b}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/{method}", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    _open(req, 120, method)


LOCAL_BOT_API = os.environ.get("TG_LOCAL_BOT_API", "http://127.0.0.1:8086")   # optional self-hosted telegram-bot-api --local (no 20 MB cap)


def _local_get_file(tok, file_id, opener=None):
    """getFile on a local Bot API server (no 20 MB cap) -> the file's absolute path on this machine."""
    req = urllib.request.Request(f"{LOCAL_BOT_API}/bot{tok}/getFile", data=json.dumps({"file_id": file_id}).encode(),
                                 headers={"Content-Type": "application/json"})
    r = json.load((opener or urllib.request.urlopen)(req, timeout=600))   # it downloads the whole file before answering
    return r["result"]["file_path"]


def _download(tok, file_id, dest, cloud=None, local=None, copy=None):
    """Telegram file -> dest. The cloud Bot API refuses files over 20 MB ("file is too big", HTTP 400; Rav's 63 MB video
    2026-11-01): then a local Bot API server fetches it (up to 2 GB) and it is copied from its disk. Logged."""
    try:
        fp = (cloud or (lambda: _api(tok, "getFile", file_id=file_id)))()["result"]["file_path"]
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        _log("tg_download_local", reason="cloud getFile 400 (over 20 MB)")
        t0 = time.time()
        try:
            src = (local or _local_get_file)(tok, file_id)
            (copy or shutil.copyfile)(src, dest)
        except Exception as e2:
            _log("tg_download_local_fail", error=str(e2)[:200].replace(tok, "<tok>"))
            raise
        _log("tg_download_local_ok", seconds=round(time.time() - t0, 1), bytes=os.path.getsize(dest) if os.path.exists(dest) else None)
        return
    urllib.request.urlretrieve(f"https://api.telegram.org/file/bot{tok}/{fp}", dest)


def _run_json(cmd, timeout):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "error": (r.stderr or r.stdout or f"rc={r.returncode}").strip()[:300]}


def to_printable(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in DIRECT_EXT:
        return path
    if ext in CONVERT_IMG_EXT:
        from PIL import Image
        out = os.path.splitext(path)[0] + ".png"
        Image.open(path).save(out)
        return out
    if ext in OFFICE_EXT:
        outdir = os.path.dirname(path)
        r = subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", outdir, path],
                           capture_output=True, text=True, timeout=120)
        pdf = os.path.splitext(path)[0] + ".pdf"
        if r.returncode != 0 or not os.path.isfile(pdf):
            raise ValueError(f"could not convert {ext} to PDF")
        return pdf
    raise ValueError(f"I can't print {ext or 'that file type'} files. PDF, images, text and Office documents work.")
