"""Try the converted 16kHz mono file."""
from faster_whisper import WhisperModel
wav = "listen_temp/converted_16k_mono.wav"
print(f"file: {wav}", flush=True)
m = WhisperModel("small", device="cpu", compute_type="int8")
print("loaded", flush=True)
for name, kw in [
    ("16k mono, en, beam=1, vad off", dict(language="en", beam_size=1, vad_filter=False)),
    ("16k mono, no lang hint, beam=1", dict(beam_size=1, vad_filter=False)),
    ("16k mono, en, beam=5, vad off", dict(language="en", beam_size=5, vad_filter=False)),
]:
    print(f"\n--- {name} ---", flush=True)
    segs, info = m.transcribe(wav, **kw)
    seg_list = list(segs)
    print(f"lang={info.language} prob={info.language_probability:.2f} segs={len(seg_list)}", flush=True)
    for s in seg_list:
        print(f"  [{s.start:.2f}-{s.end:.2f}] nspeech={s.no_speech_prob:.2f} lp={s.avg_logprob:.2f}: '{s.text}'", flush=True)
