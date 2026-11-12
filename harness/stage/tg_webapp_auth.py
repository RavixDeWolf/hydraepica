"""tg_webapp_auth.py - the ONE check that a Telegram Mini App request comes from who it says (TIK-1004).

Telegram signs the Mini App's `initData` with the bot's token. This verifies that signature on the server, as
documented at https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app:
  secret = HMAC_SHA256(key="WebAppData", msg=bot_token);  hash = HMAC_SHA256(secret, data_check_string)
where data_check_string = the received fields (minus `hash`) as sorted "key=value" lines joined by "\n".

Lifted from a product bot's verify_telegram_init_data, with two gaps closed:
  - auth_date max age (default 24 h): a captured initData can no longer be replayed forever;
  - a missing / unparsable `user` FAILS (a product bot's copy returned {} and skipped the allow-list).
Framework-free (raises AuthError), so FastAPI, aiohttp or a plain script can all use it.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_S = 24 * 3600


class AuthError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def data_check_string(fields):
    return "\n".join(f"{k}={fields[k]}" for k in sorted(fields))


def sign(fields, bot_token):
    """The hash Telegram would put on these fields. Used by verify() and by tests to build valid initData."""
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, data_check_string(fields).encode(), hashlib.sha256).hexdigest()


def verify(init_data, bot_token, allowed_ids=None, max_age=MAX_AGE_S, now=None):
    """-> the verified Telegram user dict ({"id": ..., "first_name": ...}). Raises AuthError (401/403/500)."""
    if not bot_token:
        raise AuthError(500, "bot token not configured")
    if not init_data:
        raise AuthError(401, "missing initData")
    fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    got = fields.pop("hash", None)
    if not got:
        raise AuthError(401, "missing hash in initData")
    if not hmac.compare_digest(got, sign(fields, bot_token)):
        raise AuthError(401, "invalid initData signature")
    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if not auth_date or (now if now is not None else time.time()) - auth_date > max_age:
        raise AuthError(401, "initData expired, reopen the app")
    try:
        user = json.loads(fields["user"])
        uid = int(user["id"])
    except Exception:
        raise AuthError(401, "initData has no user")
    if allowed_ids is not None and uid not in allowed_ids:
        raise AuthError(403, "user not authorized")
    return user
