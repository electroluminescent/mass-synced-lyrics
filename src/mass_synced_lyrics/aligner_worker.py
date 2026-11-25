"""GPU lyric aligner: runs inside the optional add-on environment (PyTorch with CUDA, stable-ts/Whisper,
Demucs, PyAV), never inside the app itself. The app starts it as a separate process and talks to it with
one JSON object per line on stdin/stdout.

    python aligner_worker.py --check                 -> {"ok": true, "cuda": true, "device": "...", ...}
    python aligner_worker.py --serve [--model medium] [--no-separate]

Request:  {"id": 1, "audio": "C:/music/song.flac", "lines": ["first line", ...], "probe": 12.5,
           "language": null}
Response: {"id": 1, "ok": true, "language": "en", "segments": [{"start": s, "end": e,
           "words": [[word, start, end, probability], ...]}, ...]}
          {"id": 1, "ok": false, "error": "..."}

Steps per song: decode (PyAV) -> isolate the vocals (Demucs htdemucs) -> 16 kHz mono -> detect the
language around the first lyric if not given -> stable-ts forced alignment of the known lines, keeping
one segment per line (original_split) -> word times and probabilities.
"""
from __future__ import annotations

import json
import os
import sys
import traceback

SR_WHISPER = 16000


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _decode(path: str, rate: int):
    """Stereo float32 tensor (2, n) at `rate` Hz."""
    import av
    import numpy as np
    import torch

    chunks = []
    with av.open(path) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout="stereo", rate=rate)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray())
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray())
    data = np.concatenate(chunks, axis=1) if chunks else np.zeros((2, 0), dtype=np.float32)
    return torch.from_numpy(np.ascontiguousarray(data, dtype=np.float32))


class Worker:
    def __init__(self, model_name: str = "medium", device: str = "cuda", separate: bool = True):
        import torch

        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("PyTorch can't see an NVIDIA GPU (torch.cuda.is_available() is False)")
        self.device = device
        self.model_name = model_name
        self.separate = separate
        self._whisper = None
        self._demucs = None

    # Models load on first use, so --serve starts quickly.
    @property
    def whisper(self):
        if self._whisper is None:
            if os.environ.get("MSL_ALIGN_TEST_RANDOM"):          # tests: tiny random model, no download
                import stable_whisper
                import whisper
                dims = whisper.model.ModelDimensions(n_mels=80, n_audio_ctx=1500, n_audio_state=64,
                                                     n_audio_head=2, n_audio_layer=1, n_vocab=51865,
                                                     n_text_ctx=448, n_text_state=64, n_text_head=2,
                                                     n_text_layer=1)
                m = whisper.model.Whisper(dims).to(self.device)
                stable_whisper.modify_model(m)
                self._whisper = m
            else:
                import stable_whisper
                self._whisper = stable_whisper.load_model(self.model_name, device=self.device)
        return self._whisper

    @property
    def demucs(self):
        if self._demucs is None:
            if os.environ.get("MSL_ALIGN_TEST_RANDOM"):
                from demucs.htdemucs import HTDemucs
                self._demucs = HTDemucs(sources=["drums", "bass", "other", "vocals"], channels=8, t_layers=1)
            else:
                from demucs.pretrained import get_model
                self._demucs = get_model("htdemucs")
            self._demucs.to(self.device).eval()
        return self._demucs

    def vocals_16k(self, path: str):
        """Mono float32 numpy array at 16 kHz: the isolated vocals (or the whole mix)."""
        import julius
        import torch

        if self.separate:
            model = self.demucs
            mix = _decode(path, model.samplerate)
            ref = mix.mean(0)
            mean, std = ref.mean(), ref.std() + 1e-8
            with torch.no_grad():
                out = _apply(model, ((mix - mean) / std)[None].to(self.device), self.device)
            voc = out[0, model.sources.index("vocals")] * std + mean
            mono, rate = voc.mean(0).cpu(), model.samplerate
        else:
            mix = _decode(path, 44100)
            mono, rate = mix.mean(0), 44100
        return julius.resample_frac(mono, rate, SR_WHISPER).numpy().astype("float32")

    def detect_language(self, audio, probe: float) -> str:
        import torch
        import whisper

        model = self.whisper
        start = max(0, int((probe or 0) * SR_WHISPER) - SR_WHISPER)
        clip = whisper.pad_or_trim(torch.from_numpy(audio[start:start + 30 * SR_WHISPER]))
        mel = whisper.log_mel_spectrogram(clip, n_mels=model.dims.n_mels).to(self.device)
        _, probs = model.detect_language(mel)
        if isinstance(probs, list):
            probs = probs[0]
        return max(probs, key=probs.get)

    def align(self, req: dict) -> dict:
        lines = [str(x).strip() for x in req.get("lines") or []]
        lines = [x for x in lines if x]
        if len(lines) < 2:
            return {"ok": False, "error": "fewer than 2 lyric lines"}
        audio = self.vocals_16k(req["audio"])
        lang = req.get("language") or self.detect_language(audio, float(req.get("probe") or 0))
        result = self.whisper.align(audio, "\n".join(lines), language=lang, original_split=True,
                                    failure_threshold=0.5, verbose=None)
        if result is None:
            return {"ok": False, "error": "alignment failed (too many words could not be placed)",
                    "language": lang}
        segs = []
        for s in result.segments:
            segs.append({"start": float(s.start), "end": float(s.end),
                         "words": [[w.word, float(w.start), float(w.end), float(w.probability or 0.0)]
                                   for w in (s.words or [])]})
        return {"ok": True, "language": lang, "segments": segs}


def _apply(model, mix, device):
    from demucs.apply import apply_model
    return apply_model(model, mix, device=device, split=True, overlap=0.1, progress=False)


def check() -> dict:
    info = {"ok": False, "python": sys.version.split()[0]}
    try:
        import torch
        info["torch"] = torch.__version__
        info["cuda"] = bool(torch.cuda.is_available())
        if info["cuda"]:
            info["device"] = torch.cuda.get_device_name(0)
            info["vram_gb"] = round(torch.cuda.get_device_properties(0).total_memory / 2 ** 30, 1)
        import importlib
        for name in ("av", "demucs", "julius"):
            importlib.import_module(name)
        import stable_whisper
        info["stable_ts"] = getattr(stable_whisper, "__version__", "?")
        info["ok"] = info["cuda"]
        if not info["cuda"]:
            info["error"] = "PyTorch was installed without CUDA support or can't see the GPU"
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def serve(argv: list[str]) -> None:
    model = argv[argv.index("--model") + 1] if "--model" in argv else "medium"
    device = argv[argv.index("--device") + 1] if "--device" in argv else "cuda"
    try:
        worker = Worker(model, device, separate="--no-separate" not in argv)
    except Exception as e:
        _emit({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return
    _emit({"ready": True})
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except ValueError:
            continue
        if req.get("quit"):
            break
        try:
            res = worker.align(req)
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}",
                   "trace": traceback.format_exc(limit=3)}
        res["id"] = req.get("id")
        _emit(res)


if __name__ == "__main__":
    if "--check" in sys.argv:
        _emit(check())
    elif "--serve" in sys.argv:
        serve(sys.argv)
    else:
        print(__doc__)
