"""Stands in for the GPU worker in tests: same JSON-lines protocol. Line i is "heard" at
FAKE_START + i * FAKE_STEP seconds with probability FAKE_PROB (env vars)."""
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
    segs = []
    for i, line in enumerate(req["lines"]):
        t = start + i * step
        words = line.split()
        dur = 1.5 / max(1, len(words))
        segs.append({"start": t, "end": t + 1.5,
                     "words": [[" " + w, t + k * dur, t + (k + 1) * dur, prob] for k, w in enumerate(words)]})
    out = {"id": req["id"], "ok": True, "language": "en", "segments": segs}
    if os.environ.get("FAKE_TRANSCRIPT"):        # what was "heard": the same words, plus some talk
        out["transcript"] = [w for s in segs for w in s["words"]] + [[" thanks", 0.1, 0.4, 0.9]]
    print(json.dumps(out), flush=True)
