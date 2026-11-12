#!/usr/bin/env python3
"""voice.py - a Telegram voice note in the agent's voice (Piper TTS, any .onnx voice model).

  python3 harness/render/voice.py "text to say"        -> prints the .ogg path (Opus, sendVoice-ready)
  python3 harness/render/voice.py --file notes.txt

Renders to a FILE only: nothing plays on the host's speakers.
Send it with a `MEDIA:/path.ogg` line (.ogg goes out as a real voice note).
config.toml:  [voice]  piper = "~/piper/venv/bin/piper"  model = "~/piper/models/en_GB-alan-medium.onnx"
"""
import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # harness/
import config  # noqa: E402

PIPER = os.path.expanduser(config.section("voice").get("piper", "piper"))
MODEL = os.path.expanduser(config.section("voice").get("model", ""))
OUT = os.path.join(config.STATE_DIR, "media", "renders")


def render(text, out_dir=OUT):
    text = " ".join((text or "").split())
    if not text:
        raise SystemExit("nothing to say")
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, time.strftime("%Y%m%d-%H%M%S") + "-voice")
    p = subprocess.run([PIPER, "--model", MODEL, "--output_file", base + ".wav"], input=text.encode(),
                       capture_output=True, timeout=120)
    if p.returncode != 0 or not os.path.exists(base + ".wav"):
        raise SystemExit(f"piper failed rc={p.returncode}: {p.stderr.decode(errors='replace')[-300:]}")
    f = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", base + ".wav", "-c:a", "libopus", "-b:a", "32k",
                        "-ar", "48000", "-ac", "1", base + ".ogg"], capture_output=True, timeout=120)
    if f.returncode != 0:
        raise SystemExit(f"ffmpeg failed: {f.stderr.decode(errors='replace')[-300:]}")
    os.unlink(base + ".wav")
    return base + ".ogg"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="?")
    ap.add_argument("--file")
    a = ap.parse_args()
    print(render(open(a.file).read() if a.file else a.text))
    sys.exit(0)
