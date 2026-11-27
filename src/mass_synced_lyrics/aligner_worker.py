"""GPU lyric aligner: runs inside the optional add-on environment (PyTorch with CUDA, stable-ts/Whisper,
Demucs, PyAV), never inside the app itself. The app starts it as a separate process and talks to it with
one JSON object per line on stdin/stdout.

    python aligner_worker.py --check                 -> {"ok": true, "cuda": true, "device": "...", ...}
    python aligner_worker.py --serve [--model medium] [--no-separate]

Request:  {"id": 1, "audio": "C:/music/song.flac", "lines": ["first line", ...], "probe": 12.5,
           "language": null}
Progress: {"id": 1, "progress": "separate", "frac": 0.4}   (any number, before the response; frac may be
          null when the step can't tell how far it is)
Response: {"id": 1, "ok": true, "language": "en", "segments": [{"start": s, "end": e,
           "words": [[word, start, end, probability], ...]}, ...],
           "transcript": [[word, start, end, probability], ...], "duration": seconds,
           "timings": {"decode": s, "separate": s, ...}}
          {"id": 1, "ok": false, "error": "..."}

Steps per song: decode (PyAV) -> isolate the vocals (Demucs htdemucs) -> 16 kHz mono -> detect the
language around the first lyric if not given -> stable-ts forced alignment of the known lines, keeping
one segment per line (original_split) -> word times and probabilities.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

SR_WHISPER = 16000


def _emit(obj) -> None:
    # ASCII-only JSON: the pipe's encoding can't break it. (The app starts this with python -I, which
    # ignores PYTHONIOENCODING, so on Windows stdout would otherwise be cp1252 and a heard word such as
    # "와" or a "♪" in the lyrics crashed the worker with UnicodeEncodeError.)
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
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
        self._req = None                  # id of the request being worked on (for progress messages)
        self._vocals_out = None           # where to keep the isolated vocals (no extension), if anywhere
        self._savers: list = []
        self._pre: dict = {}              # audio path -> (thread, [decoded mix or error]) read ahead
        self._sep_len = 0
        self._on_gpu: str | None = None   # which model is on the GPU now: "demucs" or "whisper"
        self._stage = None
        self._stage_t0 = 0.0
        self._last_emit = 0.0
        self.timings: dict = {}

    # -- progress: one message when a step starts, then (throttled) how far it is
    def stage(self, name: str) -> None:
        self._end_stage()
        self._stage, self._stage_t0, self._last_emit = name, time.monotonic(), 0.0
        if self._req is not None:
            _emit({"id": self._req, "progress": name, "frac": None})

    def frac(self, f: float) -> None:
        now = time.monotonic()
        if self._req is not None and now - self._last_emit >= 0.25:
            self._last_emit = now
            _emit({"id": self._req, "progress": self._stage, "frac": round(max(0.0, min(1.0, f)), 3)})

    def _end_stage(self) -> None:
        if self._stage is not None:
            self.timings[self._stage] = round(self.timings.get(self._stage, 0.0)
                                              + time.monotonic() - self._stage_t0, 3)
        self._stage = None

    # Models load on first use, so --serve starts quickly.
    @property
    def whisper(self):
        if self._whisper is None:
            self.stage("load_whisper")
            self._load_whisper()
        return self._whisper

    def _load_whisper(self) -> None:
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
            if self.device == "cuda" and self._demucs is not None:
                self._demucs.to("cpu")                      # make room before loading
                import torch
                torch.cuda.empty_cache()
            self._whisper = stable_whisper.load_model(self.model_name, device=self.device)
        self._on_gpu = "whisper"

    @property
    def demucs(self):
        if self._demucs is None:
            self.stage("load_separator")
            if os.environ.get("MSL_ALIGN_TEST_RANDOM"):
                from demucs.htdemucs import HTDemucs
                self._demucs = HTDemucs(sources=["drums", "bass", "other", "vocals"], channels=8, t_layers=1)
            else:
                from demucs.pretrained import get_model
                self._demucs = get_model("htdemucs")
            self._demucs.eval()
            self._on_gpu = None
            self._place("demucs")
        return self._demucs

    def vocals_16k(self, path: str):
        """Mono float32 numpy array at 16 kHz: the isolated vocals (or the whole mix)."""
        import julius
        import torch

        if self.separate:
            model = self.demucs
            self.stage("decode")
            mix = self._decoded(path, model.samplerate)
            self.duration = mix.shape[-1] / model.samplerate
            self.stage("separate")
            self._place("demucs")
            self._sep_len = mix.shape[-1]
            ref = mix.mean(0)
            mean, std = ref.mean(), ref.std() + 1e-8
            with torch.no_grad():
                out = _apply(model, ((mix - mean) / std)[None].to(self.device), self.device, self._demucs_cb)
            voc = out[0, model.sources.index("vocals")] * std + mean
            mono, rate = voc.mean(0).cpu(), model.samplerate
            if self._vocals_out:                  # a copy for the player's Vocals switch, saved alongside
                self._save_vocals(voc.cpu().numpy(), rate, self._vocals_out)
        else:
            self.stage("decode")
            mix = self._decoded(path, 44100)
            self.duration = mix.shape[-1] / 44100
            mono, rate = mix.mean(0), 44100
        return julius.resample_frac(mono, rate, SR_WHISPER).numpy().astype("float32")

    def _save_vocals(self, arr, rate: int, base: str) -> None:
        """Save the isolated vocals (Opus, or FLAC where Opus isn't available) on a helper thread, so the
        speech model doesn't wait for it."""
        import threading

        def work():
            import av
            import numpy as np

            data = np.ascontiguousarray(np.clip(arr, -1.0, 1.0).astype(np.float32))
            if data.ndim == 1:
                data = np.stack([data, data])
            os.makedirs(os.path.dirname(base), exist_ok=True)
            for codec, ext, fmt, out_rate in (("libopus", ".ogg", "ogg", 48000), ("flac", ".flac", "flac", rate)):
                out = base + ext
                tmp = out + ".part"
                try:
                    with av.open(tmp, "w", format=fmt) as c:
                        st = c.add_stream(codec, rate=out_rate, layout="stereo")
                        if codec == "libopus":
                            st.bit_rate = 96000
                        for i in range(0, data.shape[1], rate):
                            fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(data[:, i:i + rate]), format="fltp",
                                                            layout="stereo")
                            fr.sample_rate = rate
                            for pk in st.encode(fr):
                                c.mux(pk)
                        for pk in st.encode(None):
                            c.mux(pk)
                    os.replace(tmp, out)
                    return
                except Exception:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        t = threading.Thread(target=work, daemon=False)
        t.start()
        self._savers.append(t)

    def prefetch(self, path: str) -> None:
        """Decode the next song while the GPU works on this one (decoding is CPU and disk/network work)."""
        import threading
        if path in self._pre:
            return
        for old in list(self._pre)[:-1]:                # keep at most two read ahead
            self._pre.pop(old, None)
        box: list = []
        rate = 44100

        def work():
            try:
                box.append(_decode(path, rate))
            except Exception as e:                       # read it normally when its turn comes
                box.append(e)
        t = threading.Thread(target=work, daemon=True)
        t.start()
        self._pre[path] = (t, box, rate)

    def _decoded(self, path: str, rate: int):
        got = self._pre.pop(path, None)
        if got is not None:
            t, box, r = got
            t.join()
            if box and r == rate and not isinstance(box[0], Exception):
                return box[0]
        return _decode(path, rate)

    def _place(self, which: str) -> None:
        """Keep only the model in use on the GPU (the other waits in system memory). Both together need
        about 4.5 GB; with other programs using GPU memory, Windows starts lending system memory to the
        GPU, which made vocal isolation 25x slower. Moving a model takes well under a second."""
        if self.device != "cuda" or self._on_gpu == which:
            return
        import torch
        other = self._whisper if which == "demucs" else self._demucs
        mine = self._demucs if which == "demucs" else self._whisper
        if other is not None:
            other.to("cpu")
        torch.cuda.empty_cache()
        if mine is not None:
            mine.to(self.device)
        self._on_gpu = which

    def _free(self) -> None:
        import gc

        import torch
        gc.collect()
        if self.device == "cuda":
            torch.cuda.empty_cache()

    def _demucs_cb(self, d: dict) -> None:
        # demucs 4.1 calls this per chunk with the chunk's offset in samples (not the length: we know it)
        try:
            if d.get("state") == "end" and self._sep_len:
                n = max(1, int(d.get("models") or 1))
                seg = getattr(self._demucs, "segment", 7.8) * getattr(self._demucs, "samplerate", 44100)
                part = min(1.0, (float(d.get("segment_offset") or 0) + seg) / self._sep_len)
                self.frac((int(d.get("model_idx_in_bag") or 0) + part) / n)
        except Exception:
            pass

    def _transcribe_cb(self, seek, total) -> None:
        try:
            self.frac(float(seek) / float(total or 1))
        except Exception:
            pass

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
        self._req, self.timings, self.duration = req.get("id"), {}, 0.0
        self._vocals_out = req.get("vocals_out") or None
        self._savers = [t for t in self._savers if t.is_alive()]
        vram = self._vram_start()
        try:
            try:
                out = self._align(req, lines)
            except Exception as e:
                if "out of memory" not in str(e).lower():
                    raise
                self._free()                    # leftovers from the previous song: free them, try once more
                out = self._align(req, lines)
            self.stage("cleanup")
            self._free()
        finally:
            self._end_stage()
            self._req = None
        out["timings"] = self.timings
        out["duration"] = round(self.duration, 2)
        if vram:
            try:
                import torch
                vram["peak_gb"] = round(torch.cuda.max_memory_reserved() / 2 ** 30, 2)
            except Exception:
                pass
            out["vram"] = vram
        return out

    def _vram_start(self) -> dict:
        """GPU memory: total, and free when this song starts (other programs use it too)."""
        if self.device != "cuda":
            return {}
        try:
            import torch
            torch.cuda.reset_peak_memory_stats()
            free, total = torch.cuda.mem_get_info()
            return {"free_gb": round(free / 2 ** 30, 2), "total_gb": round(total / 2 ** 30, 2),
                    "mine_gb": round(torch.cuda.memory_reserved() / 2 ** 30, 2)}
        except Exception:
            return {}

    def _align(self, req: dict, lines: list) -> dict:
        audio = self.vocals_16k(req["audio"])
        self._free()                                  # the separation's working memory
        self.whisper                                  # loads the speech model the first time (its own step)
        self._place("whisper")
        self.stage("language")
        lang = req.get("language") or self.detect_language(audio, float(req.get("probe") or 0))
        self.stage("align")
        result = self.whisper.align(audio, "\n".join(lines), language=lang, original_split=True,
                                    failure_threshold=0.5, verbose=None)
        segs = []
        for s in (result.segments if result is not None else []):
            segs.append({"start": float(s.start), "end": float(s.end),
                         "words": [[w.word, float(w.start), float(w.end), float(w.probability or 0.0)]
                                   for w in (s.words or [])]})
        out = {"ok": True, "language": lang, "segments": segs}
        if result is None:
            # Forced alignment gave up (over half the words couldn't be placed in order — typical of live
            # recordings with talk or a different arrangement). What was sung can still be matched.
            out["forced_error"] = "forced alignment gave up (too many words could not be placed in order)"
            if not req.get("transcribe", True):
                return {"ok": False, "error": out["forced_error"], "language": lang}
        if req.get("transcribe", True):
            # What was actually sung or said, word by word: lets the app tell the lyrics apart from
            # ad-libs and talk (live recordings), which forced alignment alone can't.
            self.stage("transcribe")
            try:
                # temperature 0 only: no slow re-decoding of hard passages (that made this step take 10 s
                # one time and 25 s the next on the same song); stray words are skipped when matching anyway.
                tr = self.whisper.transcribe(audio, language=lang, word_timestamps=True, regroup=False,
                                             condition_on_previous_text=False, verbose=None, temperature=0.0,
                                             progress_callback=self._transcribe_cb)
                out["transcript"] = [[w.word, float(w.start), float(w.end), float(w.probability or 0.0)]
                                     for seg in tr.segments for w in (seg.words or [])]
            except Exception as e:                  # the alignment is still usable without it
                out["transcript_error"] = f"{type(e).__name__}: {e}"
            if result is None and not out.get("transcript"):
                return {"ok": False, "error": out["forced_error"], "language": lang}
        return out


def _apply(model, mix, device, callback=None):
    import inspect

    from demucs.apply import apply_model
    kw = {}
    if callback is not None and "callback" in inspect.signature(apply_model).parameters:
        kw["callback"] = callback                     # demucs 4.1: called per chunk, for progress
    return apply_model(model, mix, device=device, split=True, overlap=0.1, progress=False, **kw)


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
    for stream in (sys.stdin, sys.stdout):              # the app speaks UTF-8 whatever the locale
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
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
        if "prefetch" in req:                           # the next song: read its audio in the background
            worker.prefetch(str(req["prefetch"]))
            continue
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
