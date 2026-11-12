#!/usr/bin/env python3
"""stage.py - the agent puts something on a person's stage in the agent Mini App (TIK-1004).

  stage.py --to rav card "Title" "Body text"
  stage.py --to rav image /path/to.png [--caption "..."]
  stage.py --to rav gallery "/dir/*.png" [--title "..."]      (thumbnails; tap = big view; cast it and the TV follows)
  stage.py --to rav video /path/to.mp4 [--caption "..."]       (up to ~4.4 MB; plays inline)
  stage.py --to rav html /path/to/page.html [--title "..."]     (runs sandboxed in the app)
  stage.py --to rav url https://example.com [--title "..."]
People: the [[users]] names in config. The person's open app shows it at once; otherwise it is there next time.
"""
import argparse
import base64
import http.client
import json
import mimetypes
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # harness/
import config  # noqa: E402

STATE = os.environ.get("STAGE_STATE", os.path.join(config.STATE_DIR, "stage"))
SOCK = os.environ.get("STAGE_SOCK", os.path.expanduser(config.section("stage").get("socket", os.path.join(config.STATE_DIR, "stage.sock"))))
VIDEO_MAX_BYTES = 4_400_000
KEY = os.environ.get("STAGE_KEY", os.path.join(STATE, "push.key"))


class _UDS(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=20)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(20)
        self.sock.connect(self._path)


def item_from(args):
    """argparse namespace -> the stage item. Pure apart from reading the named file."""
    if args.kind == "card":
        return {"kind": "card", "title": args.a, "body": args.b or ""}
    if args.kind == "image":
        mt = mimetypes.guess_type(args.a)[0] or "image/png"
        if not mt.startswith("image/"):
            raise SystemExit(f"not an image: {args.a}")
        src = f"data:{mt};base64," + base64.b64encode(open(args.a, "rb").read()).decode()
        return {"kind": "image", "src": src, "caption": args.caption, "title": args.title}
    if args.kind == "gallery":
        import glob, io, subprocess
        files = sorted(glob.glob(os.path.expanduser(args.a)))
        intents = json.load(open(args.intents)) if args.intents else {}   # {"name": "what the agent meant by this shot"}
        if not files:
            raise SystemExit(f"no pictures match {args.a}")
        ims = []
        for f in files:                     # 1600 px JPEG keeps a 20-picture gallery under the 6 MB item limit
            jpg = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", f, "-vf", "scale='min(1600,iw)':-2", "-q:v", "4",
                                  "-f", "image2", "-c:v", "mjpeg", "-"], capture_output=True, check=True).stdout
            cap = os.path.splitext(os.path.basename(f))[0]
            ims.append({"src": "data:image/jpeg;base64," + base64.b64encode(jpg).decode(), "caption": cap,
                        "intent": intents.get(cap, "")})
        return {"kind": "gallery", "images": ims, "title": args.title}
    if args.kind == "video":
        mt = mimetypes.guess_type(args.a)[0] or ""
        if not mt.startswith("video/"):
            raise SystemExit(f"not a video: {args.a}")
        size = os.path.getsize(args.a)
        if size > VIDEO_MAX_BYTES:   # too big to inline (base64 + the 6 MB item cap): keep it on disk, stream it
            import re, shutil, time
            name = f"{int(time.time())}-" + re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(args.a))[-100:]
            d = os.path.join(STATE, "media", args.to)
            os.makedirs(d, exist_ok=True)
            shutil.copyfile(args.a, os.path.join(d, name + ".tmp")); os.replace(os.path.join(d, name + ".tmp"), os.path.join(d, name))
            return {"kind": "video", "src": f"media:{args.to}/{name}", "caption": args.caption, "title": args.title}
        src = f"data:{mt};base64," + base64.b64encode(open(args.a, "rb").read()).decode()
        return {"kind": "video", "src": src, "caption": args.caption, "title": args.title}
    if args.kind == "html":
        return {"kind": "html", "html": open(args.a, encoding="utf-8").read(), "title": args.title}
    if args.kind == "url":
        return {"kind": "url", "url": args.a, "title": args.title}
    raise SystemExit(f"unknown kind {args.kind}")


def push(person, item, sock=SOCK, key_file=KEY):
    c = _UDS(sock)
    c.request("POST", f"/api/push/{person}", body=json.dumps(item),
              headers={"Content-Type": "application/json", "X-Push-Key": open(key_file).read().strip()})
    r = c.getresponse()
    body = r.read().decode()
    if r.status != 200:
        raise RuntimeError(f"stage push failed ({r.status}): {body[:200]}")
    return json.loads(body)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--intents", help="gallery: JSON {picture name: the agent's intent}, shown under the big view")
    ap.add_argument("--to", required=True, choices=sorted(config.USERS.values()))
    ap.add_argument("kind", choices=["card", "image", "video", "html", "url", "gallery"])
    ap.add_argument("a")
    ap.add_argument("b", nargs="?")
    ap.add_argument("--caption")
    ap.add_argument("--title")
    a = ap.parse_args(argv)
    try:
        r = push(a.to, {k: v for k, v in item_from(a).items() if v is not None})
        print(f"on {a.to}'s stage: {r['id']}")
        return 0
    except Exception as e:
        print(f"stage push failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
