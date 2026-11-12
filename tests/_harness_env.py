"""Test setup: a throwaway config + state dir, so no test can read or write a real install. Import first."""
import os
import sys
import tempfile

_ROOT = tempfile.mkdtemp(prefix="harness-test-")
_CFG = os.path.join(_ROOT, "config.toml")
with open(_CFG, "w") as f:
    f.write(f'''[agent]
name = "Atlas"
[telegram]
owner_id = 111111111
[[users]]
id = 111111111
name = "alex"
admin = true
[[users]]
id = 222222222
name = "sam"
[[users]]
id = 333333333
name = "jo"
[stt]
urls = ["http://127.0.0.1:8002/inference", "http://127.0.0.1:8003/inference"]
[paths]
state_dir = "{_ROOT}/state"
work_root = "{_ROOT}/heads"
[[bots]]
name = "shop"
label = "Shop"
token_file = "{_ROOT}/shop.env"
token_var = "BOT_TOKEN"
[[bots]]
name = "crm"
label = "CRM"
token_file = "{_ROOT}/crm.env"
[[bots]]
name = "desk"
label = "Desk"
token_file = "{_ROOT}/desk.env"
[[bots]]
name = "ops"
label = "Ops"
token_file = "{_ROOT}/ops.token"
[[bots]]
name = "blog"
label = "Blog"
token_file = "{_ROOT}/blog.token"
[[bots]]
name = "mall"
label = "Mall"
token_file = "{_ROOT}/mall.token"
[[cast.screens]]
name = "tv"
kind = "receiver"
key_env = "TEST_TV_KEY"
[[cast.screens]]
name = "wall"
kind = "http"
url = "http://wall.test/api/cast"
[[cast.screens]]
name = "den"
kind = "ssh"
host = "den"
dir = "~/screen"
[[relay.tease]]
id = 444444444
name = "Pat"
lines = ["Nice try, Pat.", "Access denied.", "This line is for the owner only."]
''')
os.environ["HARNESS_CONFIG"] = _CFG
os.environ.setdefault("HARNESS_BOT_TOKEN", "123:test")
os.environ.setdefault("TEST_TV_KEY", "tv-key-0123456789abcdef0123456789abcdef")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness"))
