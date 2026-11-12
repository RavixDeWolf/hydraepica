#!/usr/bin/env python3
"""gateway.py - the ONE process that reads the bot's updates (getUpdates), and routes each one.

Telegram allows a single poller per bot; a second one gets HTTP 409. So everything inbound comes through here:
  message           -> the head lane that owns its thread (head_lane.handle), else a short reply from the llm
  callback_query    -> the registered handler for its prefix ("hd:" = head buttons: Allow / Deny / choices)
  message_reaction  -> feedback on a head's answer (never a turn)
  edited_message    -> the head sees the corrected text
Every inbound message is mirrored into the chat ledger (sqlite). Nothing is dropped silently: a failure in one
update is logged and the offset still moves, so one bad update can never wedge the bot.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import llm  # noqa: E402

TOK = config.bot_token()
LOG = os.path.join(config.STATE_DIR, "logs", "gateway.log")
OFFSET_FILE = os.path.join(config.STATE_DIR, "gateway_offset.json")
STRANGER_MSG = config.section("gateway").get(
    "stranger_message", "Hi! This bot is private, sorry. Ask its owner if you'd like access.")
CHAT_SYS = config.section("gateway").get(
    "chat_system_prompt", f"You are {config.AGENT_NAME}, a helpful assistant. Answer briefly and plainly.")
ALLOWED_UPDATES = ["message", "callback_query", "message_reaction", "edited_message"]


def log(m):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + m + "\n")


def api(method, http_timeout=35, **kw):
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOK}/{method}",
                                 data=json.dumps(kw).encode(), headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=http_timeout))


def offset(write=None):
    try:
        cur = json.load(open(OFFSET_FILE)).get("offset", 0)
    except FileNotFoundError:
        cur = 0
    if write is not None:
        tmp = OFFSET_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"offset": write}, f)
        os.replace(tmp, OFFSET_FILE)        # atomic: a crash mid-write never loses the position
    return cur


def mirror(m, update_id):
    """Inbound row in the chat ledger. A ledger failure is logged; the message is still handled."""
    try:
        import chat_ledger
        chat_ledger.record_update(m, update_id)
    except Exception as e:
        log(f"MIRROR-FAIL {update_id}: {e}")


def reply(m, text):
    kw = {"chat_id": m["chat"]["id"], "text": text, "reply_to_message_id": m.get("message_id")}
    if m.get("message_thread_id") is not None:
        kw["message_thread_id"] = m["message_thread_id"]
    api("sendMessage", http_timeout=15, **kw)


def handle_message(u):
    m = u["message"]
    uid = (m.get("from") or {}).get("id")
    if uid not in config.USERS:
        log(f"STRANGER uid={uid} chat={(m.get('chat') or {}).get('id')} text={(m.get('text') or '')[:60]!r}")
        if (m.get("chat") or {}).get("type") == "private":
            reply(m, STRANGER_MSG)
        return
    mirror(m, u["update_id"])
    import head_lane
    if head_lane.handle(m, TOK):           # its thread belongs to a head (or a refusal was logged)
        log(f"HEAD handled uid={uid} thread={m.get('message_thread_id')}")
        return
    text = m.get("text")
    if isinstance(text, str) and text.strip() and not text.startswith("/"):
        answer = llm.chat(text, CHAT_SYS, log=log)
        reply(m, answer or f"{config.AGENT_NAME} can't answer right now (no model reachable); it's logged.")


CB_HANDLERS = {}   # prefix -> fn(cq, data)


def _cb_head(cq, data):
    import head_lane
    head_lane.on_button(cq, data, TOK)


CB_HANDLERS["hd:"] = _cb_head


def handle_callback(u):
    cq = u["callback_query"]
    data, uid = cq.get("data", ""), (cq.get("from") or {}).get("id")
    try:
        api("answerCallbackQuery", http_timeout=10, callback_query_id=cq["id"])
    except Exception as e:
        log(f"ANSWER-CB-FAIL: {e}")
    if uid not in config.USERS:             # a forwarded card's buttons are still only ours to press
        log(f"CB from non-user uid={uid} ignored: {data}")
        return
    for pfx, fn in CB_HANDLERS.items():
        if data.startswith(pfx):
            fn(cq, data)
            log(f"CB handled: {pfx}")
            return
    log(f"CB unrouted: {data}")


def handle(u):
    import head_lane
    if "message" in u:
        handle_message(u)
    elif "callback_query" in u:
        handle_callback(u)
    elif "message_reaction" in u:
        head_lane.on_reaction(u["message_reaction"])
    elif "edited_message" in u:
        head_lane.on_edit(u["edited_message"], tok=TOK)


def main():
    log("gateway start")
    try:
        import head_lane
        head_lane.start_live(TOK)           # the stage's push-to-talk spool
    except Exception as e:
        log(f"LIVE spool start FAILED: {e}")
    backoff = 2
    while True:
        try:
            data = api("getUpdates", http_timeout=35, offset=offset(), timeout=25, allowed_updates=ALLOWED_UPDATES)
            backoff = 2
            for u in data.get("result", []):
                try:
                    handle(u)
                except Exception as e:
                    log(f"HANDLE-FAIL update {u.get('update_id')}: {type(e).__name__}: {e}")
                offset(write=u["update_id"] + 1)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                log("409 conflict: another process is polling this bot. Backing off 30s.")
                time.sleep(30)
            else:
                log(f"HTTP {e.code}: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
        except Exception as e:
            log(f"LOOP-FAIL: {type(e).__name__}: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
