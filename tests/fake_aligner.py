"""Stands in for the GPU worker in tests: same JSON-lines protocol. Line i is "heard" at
FAKE_START + i * FAKE_STEP seconds with probability FAKE_PROB (env vars). It reports progress steps like
the real worker. FAKE_CRASH_ONCE=<file>: the first request (while <file> doesn't exist) kills the worker."""
import json
import os
import sys

start = float(os.environ.get("FAKE_START", "2.0"))
step = float(os.environ.get("FAKE_STEP", "3.0"))
prob = float(os.environ.get("FAKE_PROB", "0.9"))
print(json.dumps({"ready": True}), flush=True)
for raw in sys.stdin:
    req = json.loads(raw)
    if req.get("quit"):
        break
    if "prefetch" in req:                       # like the real worker: read ahead, no answer
        log = os.environ.get("FAKE_PREFETCH_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(req["prefetch"] + "\n")
        continue
    if req.get("vocals_out") and os.environ.get("FAKE_VOCALS"):      # the isolated vocals, for the player
        import shutil
        os.makedirs(os.path.dirname(req["vocals_out"]), exist_ok=True)
        shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "silence.ogg"), req["vocals_out"] + ".ogg")
    crash = os.environ.get("FAKE_CRASH_ONCE")
    if crash and not os.path.exists(crash):
        open(crash, "w").close()
        sys.exit(3)
    for stage, frac in (("decode", None), ("separate", 0.5), ("separate", 1.0), ("align", None),
                        ("transcribe", 0.5), ("cleanup", None)):
        print(json.dumps({"id": req["id"], "progress": stage, "frac": frac}), flush=True)
    segs = []
    for i, line in enumerate(req["lines"]):
        t = start + i * step
        words = line.split()
        dur = 1.5 / max(1, len(words))
        segs.append({"start": t, "end": t + 1.5,
                     "words": [[" " + w, t + k * dur, t + (k + 1) * dur, prob] for k, w in enumerate(words)]})
    out = {"id": req["id"], "ok": True, "language": "en", "segments": segs, "duration": 12.0,
           "timings": {"decode": 0.01}}
    if os.environ.get("FAKE_TRANSCRIPT"):        # what was "heard": the same words, plus some talk
        out["transcript"] = [w for s in segs for w in s["words"]] + [[" thanks", 0.1, 0.4, 0.9]]
    print(json.dumps(out), flush=True)
