"""Try transcribing a debug WAV with multiple settings to find what works."""
import sys
import glob
from faster_whisper import WhisperModel

wav = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob("listen_temp/debug_*.wav"))[-1]
print(f"file: {wav}", flush=True)

print("loading whisper small int8...", flush=True)
m = WhisperModel("small", device="cpu", compute_type="int8")
print("loaded", flush=True)

configs = [
    {"name": "default (vad off, en)", "kw": dict(language="en", beam_size=1, vad_filter=False)},
    {"name": "vad on, en",            "kw": dict(language="en", beam_size=1, vad_filter=True)},
    {"name": "no language hint",      "kw": dict(beam_size=1, vad_filter=False)},
    {"name": "beam=5, vad off, en",   "kw": dict(language="en", beam_size=5, vad_filter=False)},
    {"name": "task=transcribe, no_speech_threshold=0.1",
     "kw": dict(language="en", beam_size=1, vad_filter=False, no_speech_threshold=0.1)},
    {"name": "condition_on_previous=False, temperature=0",
     "kw": dict(language="en", beam_size=1, vad_filter=False,
                condition_on_previous_text=False, temperature=0.0)},
]

for c in configs:
    print(f"\n--- {c['name']} ---", flush=True)
    try:
        segs, info = m.transcribe(wav, **c["kw"])
        seg_list = list(segs)
        print(f"lang={info.language} prob={info.language_probability:.2f} dur={info.duration:.2f}s segs={len(seg_list)}", flush=True)
        for s in seg_list:
            print(f"  [{s.start:.2f}-{s.end:.2f}] no_speech={s.no_speech_prob:.2f} avg_logprob={s.avg_logprob:.2f}: '{s.text}'", flush=True)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", flush=True)
