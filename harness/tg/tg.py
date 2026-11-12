#!/usr/bin/env python3
"""tg.py - Telegram as a tool for the main head (TIK-1027). Run it from Bash; it prints one JSON line.

Rav 2026-10-24: "awareness of which messages I am replying to ... Knowing when a message has been forwarded.
Being able to forward messages and basically, use all the telegram features."

The head's chat is fixed: Rav's DM with the bot, and by default its main thread. It cannot reach
any other chat. --thread picks another thread in that same DM. Edit and delete work only on the bot's OWN
messages (checked against the lab chat history). Every action is logged (tg_print.log, event head_tg) and
every message it sends becomes a row in the lab chat history (chat_ledger).

  tg.py context <msg_id> [--n 3]          the message and its neighbours, from the chat history
  tg.py search <words...>                 last matches in this thread
  tg.py forward <msg_id> [--thread T]     forward one message (keeps "Forwarded from")
  tg.py copy <msg_id> [--thread T]        copy one message (no forward header)
  tg.py react <msg_id> <emoji>            set a reaction (Telegram's allowed set)
  tg.py pin <msg_id> | unpin <msg_id>
  tg.py edit <msg_id> <text>              edit one of the bot's own text messages
  tg.py delete <msg_id>                   delete one of the bot's own messages
  tg.py send <text>                       a plain text message
  tg.py progress KEY PCT "label" [ETA]    one small progress message, edited in place (silent); `progress KEY done`
                                          deletes it
  tg.py photo|video|voice|videonote|file <path> [caption]   send media
  tg.py buttons "<question>" "<choice>" "<choice>" ...   up to 8 tap-able choices; Rav's tap comes back to you as a
                                                        turn: [Rav tapped "<choice>" on your question: "..."]
  tg.py sticker <file_id | path.webp>
  tg.py poll "<question>" "<option>" "<option>" ...     (votes are not reported back; use buttons to get an answer)
"""
import argparse
import json
import os
import time
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # harness/
import config  # noqa: E402
import chat_ledger  # noqa: E402
import tgapi  # noqa: E402  (retrying _open + the shared log)

# A second head runs with HEAD_STATE_DIR=<state_dir>/<lane>: its sends, buttons and progress bars go to ITS thread.
STATE_DIR = os.path.expanduser(os.environ.get("HEAD_STATE_DIR", config.STATE_DIR))
THREAD_FILE = os.path.join(STATE_DIR, "tg_threads.json")
BUTTONS = os.path.join(STATE_DIR, "buttons.json")   # head_lane.on_choice_button reads it
OWNER = config.CHAT_ID                                 # the owner's private chat with the bot
MEDIA = {"photo": ("sendPhoto", "photo", "image/jpeg"), "video": ("sendVideo", "video", "video/mp4"),
         "voice": ("sendVoice", "voice", "audio/ogg"), "videonote": ("sendVideoNote", "video_note", "video/mp4"),
         "file": ("sendDocument", "document", "application/octet-stream")}


# "sending photo..." at the top of the chat while the upload runs (a lagging upload otherwise looks missing)
UPLOAD_ACTION = {"photo": "upload_photo", "video": "upload_video", "voice": "upload_voice",
                 "videonote": "upload_video_note", "file": "upload_document"}


def _with_action(action, base, call, fn):
    """run fn() while Telegram shows `action`; the action lasts ~5 s, so it is renewed every 4 s. Never fatal."""
    import threading
    stop = threading.Event()

    def beat():
        while not stop.is_set():
            try:
                api("sendChatAction", call, action=action, **base)
            except Exception as e:
                tgapi._log("head_tg", event2="chat_action_fail", error=str(e)[:120]); return
            stop.wait(4)
    t = threading.Thread(target=beat, daemon=True); t.start()
    try:
        return fn()
    finally:
        stop.set(); t.join(1)


# Rav 2026-10-26: a progress bar "in its own message that disappears when done, so it can stay small. Try to not
# let it scroll horizontal". One message per KEY, edited in place; `done` deletes it. Plain text (a code block
# scrolls sideways on a phone); every line kept short.
PROGRESS_FILE = os.path.join(STATE_DIR, "progress.json")
BAR_W, LINE_MAX = 10, 30
GRAD, EMPTY = "🟩" * 10, "🟨"


def progress_text(pct, label, eta="", now=None):
    """two short lines, e.g. '🟩🟩🟩🟩🟩🟩🟨🟨🟨🟨 58%' / 'avatar outro · ETA 4m20s'. Pure (pass `now` for "busy").
    pct "busy" = the job gives no progress and has no history: a block walks along the bar so it visibly lives,
    and no percentage is claimed (Rav 2026-10-28: a bar frozen at 0% "is not exactly a progress bar")."""
    if str(pct) == "busy":
        # no bar at all (Rav 2026-10-28: a walking blue square read as nonsense): an hourglass that flips each redraw
        glass = "⏳" if int((now if now is not None else time.time()) // 20) % 2 == 0 else "⌛"
        line2 = label.strip()
        if len(line2) > LINE_MAX:
            line2 = line2[:LINE_MAX - 1].rstrip() + "…"
        return f"{glass} working, no % reported\n{line2}"
    pct = max(0, min(100, int(round(float(pct)))))
    fill = int(round(pct / 100 * BAR_W))
    line2 = label.strip()
    if eta:
        line2 = f"{line2} · ETA {eta}" if line2 else f"ETA {eta}"
    if len(line2) > LINE_MAX:
        line2 = line2[:LINE_MAX - 1].rstrip() + "…"
    # Telegram text has no colour, so emoji squares: the bar starts all yellow and turns green as it fills (Rav 2026-10-27)
    return f"{''.join(GRAD[i] for i in range(fill))}{EMPTY * (BAR_W - fill)} {pct}%\n{line2}"


def progress(args, base, thread, call=None):
    key = args[0]
    try:
        st = json.load(open(PROGRESS_FILE))
    except (OSError, ValueError):
        st = {}
    cur = st.get(key)
    if args[1] == "done":
        if cur:
            try:
                api("deleteMessage", call, chat_id=OWNER, message_id=cur["id"])
            except Exception as e:
                tgapi._log("head_tg", event2="progress_delete_fail", error=str(e)[:120])
            st.pop(key, None); json.dump(st, open(PROGRESS_FILE, "w"))
        return {"ok": True, "deleted": bool(cur)}
    text = progress_text(args[1], args[2] if len(args) > 2 else "", args[3] if len(args) > 3 else "")
    if cur and cur.get("text") == text:
        return {"ok": True, "message_id": cur["id"], "unchanged": True}     # Telegram refuses a no-op edit
    if cur:
        try:
            api("editMessageText", call, chat_id=OWNER, message_id=cur["id"], text=text)
            cur["text"] = text; json.dump(st, open(PROGRESS_FILE, "w"))
            return {"ok": True, "message_id": cur["id"]}
        except Exception as e:                                # deleted by hand / too old: start a fresh one
            tgapi._log("head_tg", event2="progress_edit_fail", error=str(e)[:120])
    res = api("sendMessage", call, text=text, disable_notification=True, **base)
    mid = _record(res, thread, text, "progress")
    st[key] = {"id": mid, "text": text}; json.dump(st, open(PROGRESS_FILE, "w"))
    # pinned while active, silently; deleting the message on `done` removes the pin too. The thread MUST be named:
    # without message_thread_id Telegram pins it chat-wide and the pin never shows inside the thread (Rav 2026-10-27)
    try:
        api("pinChatMessage", call, chat_id=OWNER, message_id=mid, message_thread_id=thread, disable_notification=True)
        pinned = True
    except Exception as e:
        tgapi._log("head_tg", event2="progress_pin_fail", error=str(e)[:120])
        pinned = f"FAILED: {str(e)[:120]}"
    return {"ok": True, "message_id": mid, "new": True, "pinned": pinned}


class TgError(Exception):
    pass


def token():
    try:
        return config.bot_token()
    except Exception:
        raise TgError("bot token not readable")


def default_thread():
    try:
        return int(json.load(open(THREAD_FILE))[str(config.OWNER_ID)])
    except Exception:
        raise TgError("main thread id not on file")


def api(method, call=None, **kw):
    if call:
        return call(method, kw)
    req = urllib.request.Request(f"https://api.telegram.org/bot{token()}/{method}", data=json.dumps(kw).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(tgapi._open(req, 60, method))


def upload(method, field, ctype, path, fields, call=None):
    if call:
        return call(method, dict(fields, **{field: path}))
    b = "----tg" + os.urandom(8).hex()
    body = b"".join(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode() for k, v in fields.items())
    body += (f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{os.path.basename(path)}\"\r\n"
             f"Content-Type: {ctype}\r\n\r\n").encode() + open(path, "rb").read() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token()}/{method}", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    return json.load(tgapi._open(req, 180, method))


def own(mid, thread):
    r = chat_ledger.find(OWNER, mid, thread)
    if not r or r.get("role") != "assistant":
        raise TgError(f"message #{mid} is not one of the bot's own messages in this thread (edit/delete refused)")


def _record(res, thread, text, kind):
    mid = ((res or {}).get("result") or {}).get("message_id")
    if mid:
        try:
            chat_ledger.record_out(OWNER, thread, mid, text, kind=kind)
        except Exception as e:
            tgapi._log("head_tg", event2="ledger_fail", error=str(e)[:150])
    return mid


def run(argv, call=None):
    p = argparse.ArgumentParser(prog="tg.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action")
    p.add_argument("args", nargs="*")
    p.add_argument("--thread", type=int)
    p.add_argument("--n", type=int, default=3)
    a = p.parse_args(argv)
    here = default_thread()
    thread = a.thread or here
    act, args = a.action, a.args
    base = {"chat_id": OWNER, "message_thread_id": thread}

    def need(n):
        if len(args) < n:
            raise TgError(f"{act} needs {n} argument(s); see tg.py --help")

    if act == "context":
        need(1)
        b, t, af = chat_ledger.around(OWNER, int(args[0]), here, n=a.n)
        return {"ok": True, "before": [chat_ledger.line(x) for x in b], "message": chat_ledger.line(t) if t else None,
                "after": [chat_ledger.line(x) for x in af]}
    if act == "search":
        need(1)
        return {"ok": True, "hits": [chat_ledger.line(x) for x in chat_ledger.search(OWNER, " ".join(args), here)]}
    if act in ("forward", "copy"):
        need(1)
        res = api("forwardMessage" if act == "forward" else "copyMessage", call, from_chat_id=OWNER,
                  message_id=int(args[0]), **base)
        return {"ok": True, "message_id": _record(res, thread, f"[{act} of #{args[0]}]", act)}
    if act == "react":
        need(2)
        api("setMessageReaction", call, chat_id=OWNER, message_id=int(args[0]), reaction=[{"type": "emoji", "emoji": args[1]}])
        return {"ok": True}
    if act in ("pin", "unpin"):
        need(1)
        api("pinChatMessage" if act == "pin" else "unpinChatMessage", call, chat_id=OWNER, message_id=int(args[0]),
            **({"disable_notification": True, "message_thread_id": thread} if act == "pin" else {}))
        return {"ok": True}
    if act == "edit":
        need(2)
        own(int(args[0]), here)
        api("editMessageText", call, chat_id=OWNER, message_id=int(args[0]), text=" ".join(args[1:]))
        return {"ok": True}
    if act == "delete":
        need(1)
        own(int(args[0]), here)
        api("deleteMessage", call, chat_id=OWNER, message_id=int(args[0]))
        return {"ok": True}
    if act == "progress":                                  # tg.py progress KEY PCT "label" [ETA] | progress KEY done
        need(2)
        return progress(args, base, thread, call)
    if act == "send":
        need(1)
        text = " ".join(args)
        return {"ok": True, "message_id": _record(api("sendMessage", call, text=text, **base), thread, text, "text")}
    if act == "buttons":
        need(3)
        q, labels = args[0], args[1:9]
        key = os.urandom(4).hex()
        d = {}
        try:
            d = json.load(open(BUTTONS))
        except Exception:
            pass
        d[key] = {"text": q, "labels": labels}
        d = dict(list(d.items())[-50:])                   # the oldest unanswered questions drop off
        os.makedirs(os.path.dirname(BUTTONS), exist_ok=True)
        json.dump(d, open(BUTTONS + ".part", "w"))
        os.replace(BUTTONS + ".part", BUTTONS)
        kb = [[{"text": l, "callback_data": f"hd:u:{key}:{i}"}] for i, l in enumerate(labels)]
        res = api("sendMessage", call, text=q, reply_markup={"inline_keyboard": kb}, **base)
        return {"ok": True, "key": key, "message_id": _record(res, thread, q + "  [" + " | ".join(labels) + "]", "buttons")}
    if act == "sticker":
        need(1)
        if os.path.isfile(args[0]):
            res = upload("sendSticker", "sticker", "image/webp", args[0], {k: str(v) for k, v in base.items()}, call)
        else:
            res = api("sendSticker", call, sticker=args[0], **base)
        return {"ok": True, "message_id": _record(res, thread, "[sticker]", "sticker")}
    if act == "poll":
        need(3)
        res = api("sendPoll", call, question=args[0][:300], options=[{"text": o[:100]} for o in args[1:11]],
                  is_anonymous=False, **base)
        return {"ok": True, "message_id": _record(res, thread, "[poll] " + args[0], "poll")}
    if act in MEDIA:
        need(1)
        path = args[0]
        if not os.path.isfile(path):
            raise TgError(f"no such file: {path}")
        bad = media_problem(act, path)
        if bad:                                              # never send Rav a broken file (model-proofing, TIK-1039)
            raise TgError(f"not sent: {bad}")
        method, field, ctype = MEDIA[act]
        fields = {k: str(v) for k, v in base.items()}
        if len(args) > 1 and act != "videonote":            # round videos take no caption
            fields["caption"] = " ".join(args[1:])[:1000]
        res = _with_action(UPLOAD_ACTION[act], base, call, lambda: upload(method, field, ctype, path, fields, call))
        return {"ok": True, "message_id": _record(res, thread, fields.get("caption") or f"[{act}] {os.path.basename(path)}", act)}
    raise TgError(f"unknown action {act!r}; see tg.py --help")


def media_problem(act, path):
    """a cheap sanity check before anything is sent: the file decodes, is not empty, not a blank frame, a sane size.
    Returns what is wrong, or None. Whatever drives the head (a small model too) cannot send a broken render."""
    if os.path.getsize(path) == 0:
        return f"{path} is empty"
    if act == "photo":
        try:
            from PIL import Image, ImageStat
            im = Image.open(path); im.load()
        except Exception as e:
            return f"{path} does not decode as an image ({type(e).__name__})"
        if min(im.size) < 16:
            return f"{path} is only {im.size[0]}x{im.size[1]} px"
        st = ImageStat.Stat(im.convert("L").resize((64, 64)))
        if st.stddev[0] < 1.0:
            return f"{path} is a single flat colour (mean {st.mean[0]:.0f}): a blank render"
    if act in ("video", "videonote"):
        import subprocess
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True)
        try:
            if float(r.stdout.strip()) <= 0.1:
                return f"{path} has no duration"
        except ValueError:
            return f"{path} does not probe as a video ({r.stderr.strip()[:80]})"
    return None


def main(argv=None, call=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        out = run(argv, call)
    except (TgError, ValueError) as e:
        out = {"ok": False, "error": str(e)}
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    tgapi._log("head_tg", action=(argv or ["?"])[0], ok=out.get("ok"), error=out.get("error"))
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
