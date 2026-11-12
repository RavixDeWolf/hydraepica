#!/usr/bin/env python3
"""redact.py - hide private content in a video by OVERWRITING it, default deny (TIK-1427).

Real blur or pixelation of text is not safe (VIDEO_REDACTION_RESEARCH_2026-11-02.md), so source pixels never reach a
masked area: every pixel is replaced by fake blurred bars unless a time window's keep-list names it.

  redact.py render PLAN.json IN.mp4 OUT.mp4     writes OUT (picture only: audio is not redacted here), then verifies it; exit 1 and no .ok file on a leak
  redact.py verify PLAN.json OUT.mp4            every frame of the final file vs the expected mask; exit 1 on any leak

PLAN: {"windows": [{"a": s, "b": s, "keep": [[x, y, w, h], ...]}]}. Time outside every window keeps NOTHING.
verify only proves masked areas hold the fake. It says nothing about what is in a keep rect: that is the plan's job.
Skill: SKILL_video_privacy_pass.md."""
import argparse, json, os, subprocess, sys, time
import numpy as np

FPS = 30
BLOCK, LEAK = 8, 10.0     # mean abs difference per 8x8 block that counts as a leak (compression stays far below it)
LOG = os.environ.get("REDACT_LOG", os.path.expanduser("~/.local/state/harness/redact.log"))


def log(**k):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S%z ") + json.dumps({"ts": time.time(), **k}) + "\n")


def fake(h, w):
    """Blurred chat-like bars from a fixed seed. Nothing from the source goes in."""
    import cv2
    r = np.random.default_rng(7); im = np.full((h, w, 3), (38, 26, 16), np.uint8); y = int(h * .03)
    while y < h:
        bh = int(r.integers(h // 40, h // 14)); x = int(r.integers(0, w // 3)); bw = int(r.integers(w // 3, w - x))
        im[y:y + bh, x:x + bw] = (74, 56, 40) if r.random() < .6 else (120, 84, 50)
        y += bh + int(r.integers(h // 80, h // 30))
    return cv2.GaussianBlur(im, (0, 0), max(4, w / 50))


def keep_mask(plan, t, h, w):
    """True = source allowed. Default False: a time in no window keeps nothing."""
    m = np.zeros((h, w), bool)
    for win in plan["windows"]:
        if win["a"] <= t < win["b"]:
            for x, y, rw, rh in win.get("keep", []):
                m[y:y + rh, x:x + rw] = True
    return m


def size(path):
    o = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
                       capture_output=True, text=True, check=True).stdout.split(",")
    return int(o[0]), int(o[1])


def reader(path, w, h):
    return subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-vf", f"fps={FPS}", "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)


def frames(path, w, h):
    p = reader(path, w, h); n = w * h * 3
    while True:
        b = p.stdout.read(n)
        if len(b) < n:
            break
        yield np.frombuffer(b, np.uint8).reshape(h, w, 3)
    p.wait()


def render(plan, src, out):
    w, h = size(src); w -= w % 2; h -= h % 2; sw, sh = size(src); F = fake(h, w); n = 0
    enc = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
                            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p", out], stdin=subprocess.PIPE)
    cache = (None, None)
    for fr in frames(src, sw, sh):
        t = n / FPS; key = tuple(i for i, x in enumerate(plan["windows"]) if x["a"] <= t < x["b"])
        if cache[0] != key:
            cache = (key, keep_mask(plan, t, h, w))
        o = F.copy(); o[cache[1]] = fr[:h, :w][cache[1]]
        enc.stdin.write(o.tobytes()); n += 1
    enc.stdin.close(); enc.wait()
    if enc.returncode:
        raise SystemExit("encode failed")
    return n


def verify(plan, video, expect_frames=None):
    """Check every frame. Any masked 8x8 block that differs from the fake is a leak. Returns (ok, frames, leaks)."""
    w, h = size(video); F = fake(h, w).astype(np.int16); leaks = []; n = 0; cache = (None, None)
    for fr in frames(video, w, h):
        t = n / FPS; key = tuple(i for i, x in enumerate(plan["windows"]) if x["a"] <= t < x["b"])
        if cache[0] != key:
            cache = (key, ~keep_mask(plan, t, h, w))
        d = np.abs(fr.astype(np.int16) - F).mean(axis=2) * cache[1]
        bh, bw = h // BLOCK, w // BLOCK
        b = d[:bh * BLOCK, :bw * BLOCK].reshape(bh, BLOCK, bw, BLOCK).mean(axis=(1, 3))
        if b.max() > LEAK:
            leaks.append(round(t, 3))
        n += 1
    ok = n > 0 and not leaks and (expect_frames is None or n == expect_frames)
    log(event="verify", video=video, ok=ok, frames=n, leaks=len(leaks), first=leaks[:5])
    return ok, n, leaks


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["render", "verify"]); ap.add_argument("plan"); ap.add_argument("files", nargs="+")
    a = ap.parse_args(); plan = json.load(open(a.plan))
    exp = None
    if a.cmd == "render":
        exp = render(plan, a.files[0], a.files[1]); target = a.files[1]
    else:
        target = a.files[0]
    ok, n, leaks = verify(plan, target, exp)
    print(json.dumps({"ok": ok, "frames": n, "leaks": len(leaks), "first": leaks[:10]}))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
