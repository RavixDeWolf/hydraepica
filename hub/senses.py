"""senses.py - let a head see: a still frame from any camera the config lists, saved to a file it can Read.

  [[cameras]]
  name = "desk"
  kind = "usb"        # usb  -> source = "/dev/video0"           (ffmpeg v4l2)
  source = "/dev/video0"
  # kind = "rtsp"     # rtsp -> source = "rtsp://cam.local/stream" (ffmpeg, TCP)
  # kind = "http"     # http -> source = "http://cam.local/snapshot.jpg" (a plain GET)

Optional: [senses] transcript_file = a text file some speech-to-text process appends to; `room` returns its tail.
Frames never leave the machine: the tool returns a local path, not an upload.
"""
import os
import subprocess
import time
import urllib.request

import audit

OUT = os.path.join(audit.config.STATE_DIR, "frames")
KINDS = ("usb", "rtsp", "http")


class SenseError(ValueError):
    pass


def cameras():
    return {c["name"]: c for c in (audit.config.section("cameras") or []) if c.get("name")}


def _ffmpeg_argv(cam, dest):
    if cam["kind"] == "usb":
        return ["ffmpeg", "-y", "-loglevel", "error", "-f", "v4l2", "-i", cam["source"], "-frames:v", "1", dest]
    return ["ffmpeg", "-y", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", cam["source"], "-frames:v", "1", dest]


def look(name, runner=subprocess.run, fetch=None, now=time.time):
    cams = cameras()
    if name not in cams:
        audit.record("senses", "look", "refused", camera=name, why="unknown camera")
        raise SenseError(f"unknown camera {name!r}; configured: {sorted(cams)}")
    cam = cams[name]
    if cam.get("kind") not in KINDS:
        raise SenseError(f"camera {name!r}: kind must be one of {KINDS}")
    os.makedirs(OUT, exist_ok=True)
    dest = os.path.join(OUT, f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now()))}-{name}.jpg")
    try:
        if cam["kind"] == "http":
            data = (fetch or (lambda u: urllib.request.urlopen(u, timeout=10).read()))(cam["source"])
            with open(dest, "wb") as f:
                f.write(data)
        else:
            r = runner(_ffmpeg_argv(cam, dest), capture_output=True, text=True, timeout=20)
            if r.returncode != 0:
                raise SenseError(f"ffmpeg rc={r.returncode}: {(r.stderr or '').strip()[:200]}")
    except SenseError as e:
        audit.record("senses", "look", "failed", camera=name, why=str(e)[:200])
        raise
    except Exception as e:
        audit.record("senses", "look", "failed", camera=name, why=f"{type(e).__name__}: {str(e)[:160]}")
        raise SenseError(f"camera {name!r} gave no frame: {type(e).__name__}: {e}")
    if not os.path.getsize(dest):
        audit.record("senses", "look", "failed", camera=name, why="empty frame")
        raise SenseError(f"camera {name!r} returned an empty frame")
    audit.record("senses", "look", "ok", camera=name, path=dest)
    return {"camera": name, "path": dest, "bytes": os.path.getsize(dest)}


def room(lines=20):
    p = (audit.config.section("senses") or {}).get("transcript_file")
    if not p:
        return {"configured": False, "lines": []}
    p = os.path.expanduser(p)
    try:
        with open(p, errors="replace") as f:
            tail = f.readlines()[-max(1, min(int(lines), 200)):]
    except FileNotFoundError:
        return {"configured": True, "lines": [], "note": "transcript file not there yet"}
    audit.record("senses", "room", "ok", lines=len(tail))
    return {"configured": True, "lines": [l.rstrip("\n") for l in tail]}
