"""Re-interpret saved PCM bytes as different formats to find the right one."""
import wave
import sys
from pathlib import Path

src = "listen_temp/debug_<user_id>_<timestamp>.wav"
out_dir = Path("listen_temp")

# Read raw bytes from saved wav
with wave.open(src, "rb") as wf:
    print(f"src params: ch={wf.getnchannels()} sw={wf.getsampwidth()} fr={wf.getframerate()} nframes={wf.getnframes()}")
    raw = wf.readframes(wf.getnframes())
print(f"raw bytes: {len(raw)}")

# Variants to try
variants = [
    ("mono_48k",  1, 2, 48000),
    ("mono_24k",  1, 2, 24000),
    ("mono_16k",  1, 2, 16000),
    ("mono_8k",   1, 2, 8000),
    ("stereo_48k_orig", 2, 2, 48000),  # baseline
    ("stereo_24k", 2, 2, 24000),
]

for name, ch, sw, fr in variants:
    out = out_dir / f"reint_{name}.wav"
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(ch)
        wf.setsampwidth(sw)
        wf.setframerate(fr)
        wf.writeframes(raw)
    print(f"wrote {out.name}: {ch}ch {sw*8}bit {fr}Hz dur~{len(raw)/(ch*sw*fr):.2f}s")
