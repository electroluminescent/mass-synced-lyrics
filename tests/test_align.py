import json
import os
import sys

import pytest
from conftest import FakeLrclib, make_track, record

from mass_synced_lyrics import ai_align, lrc
from mass_synced_lyrics.engine import Engine
from mass_synced_lyrics.state import State
from mass_synced_lyrics.tags import TagFile

HERE = os.path.dirname(os.path.abspath(__file__))
LYR = "[00:00.50]Other first\n[00:05.00]Other second\n[00:07.00]Third line here"


def fake_cmd():
    return [sys.executable, os.path.join(HERE, "fake_aligner.py")]


def seg(t, words, p=0.9):
    d = 1.0 / len(words)
    return {"start": t, "end": t + 1, "words": [[w, t + i * d, t + (i + 1) * d, p] for i, w in enumerate(words)]}


def test_evaluate_retimes_from_clear_lines_and_verifies():
    res = {"ok": True, "language": "en", "segments": [seg(2.0, ["Other", "first"]), seg(6.5, ["Other", "second"]),
                                                     seg(8.5, ["Third", "line", "here"], 0.3)]}
    out = ai_align.evaluate(LYR, res, 10.0, 50)
    assert out.ok and out.pins == 2 and out.lines == 3 and out.verify == pytest.approx(57.1)
    times = [t for t, _ in lrc.display_lines(out.text)]
    assert times[0] == pytest.approx(2.0) and times[1] == pytest.approx(6.5)
    assert times[2] == pytest.approx(8.5)            # interpolated from the two pins (+1.5 s)
    assert out.moved == pytest.approx(1.5)
    bad = ai_align.evaluate(LYR, res, 10.0, 70)        # needs 70%: kept as found
    assert not bad.ok and "57%" in bad.reason and bad.text == ""
    weak = ai_align.evaluate(LYR, {"ok": True, "segments": [seg(2, ["Other", "first"], 0.2),
                                                           seg(6, ["Other", "second"], 0.2),
                                                           seg(8, ["Third", "line", "here"], 0.2)]}, 10.0, 50)
    assert not weak.ok and weak.verify == 0
    fail = ai_align.evaluate(LYR, {"ok": False, "error": "alignment failed"}, 10.0, 50)
    assert not fail.ok and fail.reason == "alignment failed"
    # zero-length words (couldn't be placed) never count as heard
    zero = {"ok": True, "segments": [{"words": [["Other", 3, 3, 0.99], ["first", 3, 3, 0.99]]},
                                     seg(6, ["Other", "second"]), seg(8, ["Third", "line", "here"])]}
    z = ai_align.evaluate(LYR, zero, 10.0, 50)
    assert z.verify == pytest.approx(71.4) and z.pins == 2


def test_map_lines_when_segments_dont_match_lines():
    lines = ["Other first", "Other second", "Third line here"]
    segs = [{"words": [["Other", 1, 1.2, 0.9], ["first", 1.2, 1.5, 0.9], ["Other", 5, 5.2, 0.9]]},
            {"words": [["second", 5.2, 5.6, 0.9], ["Third", 8, 8.2, 0.9], ["line", 8.2, 8.4, 0.9],
                       ["here", 8.4, 8.8, 0.9]]}]
    m = ai_align.map_lines(lines, segs)
    assert [[w[0] for w in ws] for ws in m] == [["Other", "first"], ["Other", "second"], ["Third", "line", "here"]]


def test_nvidia_gpu_detection(monkeypatch):
    monkeypatch.setattr(ai_align.shutil, "which", lambda n: "/usr/bin/nvidia-smi" if n == "nvidia-smi" else None)

    class R:
        returncode, stdout = 0, "NVIDIA GeForce RTX 3070, 8192 MiB\n"
    monkeypatch.setattr(ai_align, "_run", lambda *a, **k: R())
    assert ai_align.nvidia_gpu() == "NVIDIA GeForce RTX 3070, 8192 MiB"
    monkeypatch.setattr(ai_align.shutil, "which", lambda n: None)
    monkeypatch.setattr(ai_align.os.path, "exists", lambda p: False)
    assert ai_align.nvidia_gpu() is None


def _find(settings, recs):
    settings.dry_run = True
    Engine(settings, lambda e: None, lrclib=FakeLrclib(recs)).run()


def test_run_aligns_low_confidence_songs_and_keeps_only_verified(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    d = settings.music_folder
    good = make_track(os.path.join(d, "a.flac"), "flac")                         # exact match: 100 / 100
    loose = make_track(os.path.join(d, "b.flac"), "flac", title="Other Tune")    # search match: low
    _find(settings, [record(synced=LYR), record(title="Other Tune (Live)", synced=LYR, duration=12)])
    assert ai_align.pick_low_confidence(settings, [good, loose]) == [loose]
    events = []
    al = ai_align.Aligner(command=fake_cmd())
    monkeypatch.setenv("FAKE_PROB", "0.9")
    out = ai_align.run(settings, [good, loose], events.append, aligner=al)
    al.close()
    assert out["counts"]["aligned"] == 1 and out["total"] == 1
    prog = [e for e in events if e["type"] == "align_progress"][0]
    assert prog["status"] == "aligned" and prog["kind"] == "online" and prog["verify"] == 100
    st = State(settings.resolved_state_db())
    try:
        pend = st.get_pending(loose)
        times = [t for t, _ in lrc.display_lines(pend[2])]
        assert times == [2.0, 5.0, 8.0] and "aligned" in pend[1]
        cands = st.get_candidates(loose)
        assert lrc.same_lyrics(cands["online"][1], pend[2]) and lrc.same_lyrics(cands["online-original"][1], LYR)
        c, f = st.pending_scores()[loose]
        assert c == 100 and f is not None
        assert st.current_choice(loose) == "online"
        assert st.get_aligned(loose)[1] == 1
    finally:
        st.close()
    # the player offers the original timing too
    from mass_synced_lyrics import review
    info = review.gather(loose, settings)
    labels = [c.label for c in info.candidates]
    assert any(lb.startswith("Original timing (online)") for lb in labels)
    # Apply writes the aligned version
    from mass_synced_lyrics.applier import apply_all
    apply_all(settings, [loose])
    assert [t for t, _ in lrc.display_lines(TagFile(loose).read().lyrics[0])] == [2.0, 5.0, 8.0]


def test_run_rejects_below_minimum_and_remembers(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    loose = make_track(os.path.join(settings.music_folder, "b.flac"), "flac", title="Other Tune")
    _find(settings, [record(title="Other Tune (Live)", synced=LYR, duration=12)])
    settings.ai_align_min = 80
    monkeypatch.setenv("FAKE_PROB", "0.45")                      # heard, but not clearly
    events = []
    al = ai_align.Aligner(command=fake_cmd())
    out = ai_align.run(settings, [loose], events.append, aligner=al)
    assert out["counts"]["rejected"] == 1
    st = State(settings.resolved_state_db())
    try:
        assert lrc.same_lyrics(st.get_pending(loose)[2], LYR)          # unchanged
        assert st.get_aligned(loose)[1] == 0
    finally:
        st.close()
    # same lyrics again: not sent to the GPU a second time
    out = ai_align.run(settings, [loose], events.append, aligner=al)
    al.close()
    assert out["counts"]["rejected"] == 1
    assert [e["reason"] for e in events if e["type"] == "align_progress"][-1].startswith("only")


def test_aligner_reports_a_dead_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    al = ai_align.Aligner(command=[sys.executable, "-c", "print('{\"ready\": false, \"error\": \"no cuda\"}')"])
    with pytest.raises(ai_align.AlignerError, match="no cuda"):
        al.start(timeout=30)
    al2 = ai_align.Aligner(command=[sys.executable, "-c", "import json; print(json.dumps({'ready': True}))"])
    with pytest.raises(ai_align.AlignerError, match="closed"):
        al2.align("x.flac", ["a b", "c d"])
    al2.close()


def test_worker_script_answers_check_without_torch():
    import subprocess
    r = subprocess.run([sys.executable, "-I", ai_align.worker_source(), "--check"], capture_output=True, text=True,
                       timeout=120)
    info = json.loads(r.stdout.strip().splitlines()[-1])
    assert info["ok"] is False and "error" in info


def _wait(cond, n=600):
    import time
    for _ in range(n):
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_service_pauses_on_failure_queues_and_resumes(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    monkeypatch.setattr(ai_align, "nvidia_gpu", lambda: "NVIDIA test GPU")
    d = settings.music_folder
    songs = [make_track(os.path.join(d, f"{i}.flac"), "flac", title=f"Tune{i} Song") for i in range(4)]
    _find(settings, [record(title=f"Tune{i} Song (Live)", synced=LYR, duration=12) for i in range(4)])
    events = []
    starts = []

    def factory(s):
        starts.append(1)
        if len(starts) == 1:                     # the first worker can't start (e.g. no CUDA)
            return ai_align.Aligner(command=[sys.executable, "-c",
                                             "print('{\"ready\": false, \"error\": \"CUDA error: out of memory\"}')"])
        return ai_align.Aligner(command=fake_cmd())
    svc = ai_align.AlignService(lambda: settings, events.append, factory)
    assert svc.submit(songs[:2]) == 2
    assert _wait(lambda: any(e["type"] == "align_error" for e in events))
    err = next(e for e in events if e["type"] == "align_error")
    assert "out of memory" in err["error"] and svc.paused
    assert svc.submit(songs[2:]) == 2                       # Find carries on: songs keep queueing
    assert svc.pending() == 3                               # the failed one was skipped, 3 waiting
    failed = [e for e in events if e["type"] == "align_progress" and e["status"] == "error"]
    assert len(failed) == 1 and failed[0]["path"] == songs[0]
    svc.resume()                                            # "Ignore and keep aligning"
    assert _wait(lambda: svc.idle() and sum(1 for e in events if e.get("status") == "aligned") == 3)
    assert svc.counts == {"aligned": 3, "rejected": 0, "error": 1, "skipped": 0}
    st = State(settings.resolved_state_db())
    try:
        assert lrc.same_lyrics(st.get_pending(songs[0])[2], LYR)            # the failed song is untouched
        assert [t for t, _ in lrc.display_lines(st.get_pending(songs[3])[2])] == [2.0, 5.0, 8.0]
    finally:
        st.close()
    svc.stop()
    svc.join(5)
    assert svc.submit(songs) == 0                           # stopped: nothing more is taken


def test_service_stop_drops_the_queue(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    monkeypatch.setattr(ai_align, "nvidia_gpu", lambda: None)      # e.g. the GPU went away
    songs = [make_track(os.path.join(settings.music_folder, f"{i}.flac"), "flac", title=f"Tune{i} Song")
             for i in range(3)]
    _find(settings, [record(title=f"Tune{i} Song (Live)", synced=LYR, duration=12) for i in range(3)])
    events = []
    svc = ai_align.AlignService(lambda: settings, events.append, lambda s: ai_align.Aligner(command=fake_cmd()))
    svc.submit(songs)
    assert _wait(lambda: svc.paused)
    assert "NVIDIA" in next(e for e in events if e["type"] == "align_error")["error"]
    svc.stop()
    svc.join(5)
    assert svc.pending() == 0 and not svc._thread.is_alive()


def test_service_skips_songs_that_became_confident(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    monkeypatch.setattr(ai_align, "nvidia_gpu", lambda: "NVIDIA test GPU")
    good = make_track(os.path.join(settings.music_folder, "a.flac"), "flac")
    _find(settings, [record(synced=LYR)])
    events = []
    svc = ai_align.AlignService(lambda: settings, events.append, lambda s: ai_align.Aligner(command=fake_cmd()))
    svc.submit([good])                                       # 100% / 100%: not low confidence
    assert _wait(lambda: any(e["type"] == "align_idle" for e in events))
    assert not any(e["type"] == "align_progress" for e in events)
    svc.submit([good], force=True)                           # right-click → Align on the GPU
    assert _wait(lambda: any(e.get("status") == "aligned" for e in events))
    svc.stop()
    svc.join(5)
