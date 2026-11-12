#!/usr/bin/env python3
"""clip.py - let a head SEE and HEAR a video: frames + a contact sheet + the audio transcript.

  python3 harness/render/clip.py /path/to/video.mp4 [--fps 1] [--max 24]

Prints a short report: duration, the contact sheet path (one image, every frame labelled with its time, so a single
Read sees the whole clip), the frame folder (for a closer look at one moment), scene-change times, and the transcript.
Frames: one every 1/fps seconds, plus every scene change, capped at --max (spread evenly over the clip).
Audio: the lane's own transcribe() (the [stt] urls, in order) -- reused, not duplicated.
Rav 2026-10-24: "I want for you to be able to see video kinda like how Gemini does on live mode." (step 1: clips)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

LANE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # harness/ (head_lane.transcribe lives there)
sys.path.insert(0, LANE)
import config  # noqa: E402

OUT = os.path.join(config.STATE_DIR, "media", "clips")


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                        "-of", "json", path], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise SystemExit(f"not a readable video: {r.stderr.strip()[:200]}")
    d = json.loads(r.stdout)
    return float(d["format"].get("duration") or 0), any(s.get("codec_type") == "audio" for s in d.get("streams", []))


def scene_times(path, thresh=0.3):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-vf", f"select='gt(scene,{thresh})',showinfo",
                        "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=600)
    return [round(float(t), 2) for t in re.findall(r"pts_time:([0-9.]+)", r.stderr)]


def pick_times(duration, fps, scenes, cap):
    step = 1.0 / fps
    ts = sorted({round(i * step, 2) for i in range(int(duration / step) + 1) if i * step < duration} | set(scenes))
    if len(ts) > cap:                                   # keep the scene changes, thin the regular ticks evenly
        keep = set(scenes[:cap])
        rest = [t for t in ts if t not in keep]
        room = cap - len(keep)
        if room > 0 and rest:
            keep |= {rest[round(i * (len(rest) - 1) / max(1, room - 1))] for i in range(room)}
        ts = sorted(keep)[:cap]
    return ts


def grab(path, t, out):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t}", "-i", path, "-frames:v", "1",
                    "-vf", "scale='min(960,iw)':-2", out], check=True, timeout=60)


def sheet(frames, out, cols=4, w=320):
    from PIL import Image, ImageDraw
    ims = [(t, Image.open(p).convert("RGB")) for t, p in frames]
    ims = [(t, im.resize((w, max(1, round(im.height * w / im.width))))) for t, im in ims]
    h = max(im.height for _, im in ims) + 22
    rows = (len(ims) + cols - 1) // cols
    S = Image.new("RGB", (cols * w, rows * h), (12, 14, 20))
    d = ImageDraw.Draw(S)
    for i, (t, im) in enumerate(ims):
        x, y = (i % cols) * w, (i // cols) * h
        S.paste(im, (x, y + 22))
        d.text((x + 6, y + 4), f"{int(t // 60)}:{t % 60:04.1f}", fill=(230, 230, 240))
    S.save(out, quality=85)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--fps", type=float, default=1.0)
    ap.add_argument("--max", type=int, default=24)
    a = ap.parse_args()
    dur, has_audio = probe(a.video)
    d = os.path.join(OUT, time.strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"\W+", "_", os.path.basename(a.video))[:40])
    os.makedirs(d, exist_ok=True)
    scenes = scene_times(a.video)
    frames = []
    for t in pick_times(dur, a.fps, scenes, a.max):
        p = os.path.join(d, f"t{t:07.2f}.jpg")
        grab(a.video, t, p)
        if os.path.exists(p):
            frames.append((t, p))
    sh = os.path.join(d, "sheet.jpg")
    if frames:
        sheet(frames, sh)
    text = None
    if has_audio:
        sys.path.insert(0, LANE)
        import head_lane                                 # the lane's transcribe(): same STT path as voice notes
        text = head_lane.transcribe(a.video)
    print(f"clip: {a.video}\nduration: {dur:.1f}s · {len(frames)} frames · scene changes at: "
          f"{', '.join(str(s) for s in scenes) or 'none'}\nsheet: {sh if frames else '(no frames)'}\nframes: {d}\n"
          f"transcript: {text if text is not None else ('(no audio track)' if not has_audio else '(STT failed, see tg_print.log)')}")


if __name__ == "__main__":
    main()
