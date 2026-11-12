"""config.py - every value that belongs to one install, in one place.

Read from $HARNESS_CONFIG (default ~/.config/harness/config.toml). Secrets never live in the file:
the file names the environment variable that holds them. Nothing else in the repo hardcodes a person,
an id, a host or a path.
"""
import os
import tomllib

PATH = os.path.expanduser(os.environ.get("HARNESS_CONFIG", "~/.config/harness/config.toml"))


def _load():
    try:
        with open(PATH, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise SystemExit(f"harness: no config at {PATH} (copy config.example.toml there)")


_C = _load()
_tg, _paths = _C.get("telegram", {}), _C.get("paths", {})

USERS = {int(u["id"]): u["name"] for u in _C.get("users", [])}          # telegram user id -> short name
OWNER_ID = int(_tg["owner_id"])                                         # the person the heads work for
ADMINS = {int(u["id"]) for u in _C.get("users", []) if u.get("admin")} or {OWNER_ID}
CHAT_ID = int(_tg.get("chat_id", OWNER_ID))                             # private chat with the bot (topics on)
AGENT_NAME = _C.get("agent", {}).get("name", "Agent")
STATE_DIR = os.path.expanduser(_paths.get("state_dir", "~/.local/state/harness"))
WORK_ROOT = os.path.expanduser(_paths.get("work_root", "~"))            # each head's working dir lives under here
STT_URLS = tuple(_C.get("stt", {}).get("urls", ()))                    # optional speech-to-text endpoints
HUB_URL = _C.get("mcp", {}).get("hub_url", "")


def bot_token():
    """The bot token, from the env var the config names. Never logged, never written."""
    var = _tg.get("bot_token_env", "HARNESS_BOT_TOKEN")
    tok = os.environ.get(var)
    if not tok:
        raise RuntimeError(f"bot token missing: set ${var}")
    return tok


def section(name):
    """A whole table, for optional plugins (cameras, cast targets, exec hosts)."""
    return _C.get(name, {})
