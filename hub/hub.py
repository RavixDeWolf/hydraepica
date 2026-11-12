#!/usr/bin/env python3
"""hub.py - the example MCP hub: tickets, exec and senses as tools for every head.

  python3 hub/hub.py                     # stdio (Claude Code launches it per head)
  python3 hub/hub.py --http 9000         # streamable HTTP on 127.0.0.1, shared by all heads; needs HUB_TOKEN set,
                                         # and clients send "Authorization: Bearer $HUB_TOKEN"

Each head's MCP config sets HUB_IDENTITY (e.g. "head:main"), so the audit log says which head made each call.
The tools are thin: the logic lives in tickets.py / exec_tool.py / senses.py and is tested without MCP.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

import exec_tool  # noqa: E402
import senses as senses_mod  # noqa: E402
import tickets as tickets_mod  # noqa: E402

hub = MCPServer(name="hub", instructions="Shared status (tickets), remote commands (exec) and cameras (senses) "
                "for the harness heads. Every call is audited to the calling head.")


@hub.tool(description="Ticket board shared by every head. action: file | list | get | comment | status.")
def tickets(action: str, key: str = "", title: str = "", body: str = "", status: str = "", resolution: str = "") -> dict:
    try:
        if action == "file":
            return tickets_mod.file(title, body)
        if action == "list":
            return {"tickets": tickets_mod.list_(status or None)}
        if action == "get":
            return tickets_mod.get(key)
        if action == "comment":
            return tickets_mod.comment(key, body)
        if action == "status":
            return tickets_mod.set_status(key, status, resolution)
        return {"error": "action must be file | list | get | comment | status"}
    except tickets_mod.TicketError as e:
        return {"error": str(e)}


@hub.tool(description="Run a shell command on a configured host ('local' = this machine). Read-only commands run "
          "directly; anything else needs confirm=true; destructive patterns never run.",
          annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True))
def exec(host: str, command: str, confirm: bool = False, timeout: int = 60) -> dict:  # noqa: A001
    try:
        return exec_tool.run(host, command, confirm=confirm, timeout=min(max(timeout, 1), 600))
    except exec_tool.ExecRefused as e:
        return {"ok": False, "refused": str(e), "hosts": sorted(exec_tool.hosts())}


@hub.tool(description="See the room. action: cameras (list) | look (camera=name: a still frame saved to a local "
          "path you can Read) | room (the tail of the configured transcript).",
          annotations=ToolAnnotations(readOnlyHint=True))
def senses(action: str, camera: str = "", lines: int = 20) -> dict:
    try:
        if action == "cameras":
            return {"cameras": [{"name": n, "kind": c.get("kind")} for n, c in senses_mod.cameras().items()]}
        if action == "look":
            return senses_mod.look(camera)
        if action == "room":
            return senses_mod.room(lines)
        return {"error": "action must be cameras | look | room"}
    except senses_mod.SenseError as e:
        return {"error": str(e)}


def bearer_gate(app, token):
    """ASGI wrapper: every HTTP request must carry the hub token. Constant-time compare; a miss is a plain 401."""
    import hmac
    want = f"Bearer {token}".encode()

    async def gated(scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, want):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"hub token required"})
                return
        await app(scope, receive, send)
    return gated


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--http", type=int, metavar="PORT")
    a = ap.parse_args()
    if a.http:
        token = os.environ.get("HUB_TOKEN", "")
        if len(token) < 32:
            sys.exit("--http needs HUB_TOKEN (32+ random chars): without it any local process could call exec")
        import uvicorn
        uvicorn.run(bearer_gate(hub.streamable_http_app(), token), host="127.0.0.1", port=a.http, log_level="warning")
    else:
        hub.run("stdio")
