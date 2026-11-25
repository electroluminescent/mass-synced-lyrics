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
        if len(starts) <= 2:                     # the worker can't start, nor on the retry (e.g. no CUDA)
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


def test_install_step_reports_pip_progress_and_stops_on_cancel(tmp_path):
    import threading
    import time
    script = tmp_path / "fakepip.py"
    script.write_text("import sys, time\n"
                      "print('Collecting torch')\n"
                      "print('Downloading torch-2.9.0-cp312-cp312-win_amd64.whl (2.5 GB)')\n"
                      "for i in range(0, 101, 25):\n"
                      "    print(f'Progress {i * 1000} of 100000', flush=True)\n"
                      "print('Successfully installed torch')\n"
                      "if len(sys.argv) > 1: time.sleep(30)\n", encoding="utf-8")
    seen, logged = [], []
    rc, _ = ai_align._run_step([sys.executable, str(script)], logged.append, threading.Event(),
                               lambda f, d: seen.append((round(f, 2), d)))
    assert rc == 0 and [f for f, _ in seen] == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert seen[-1][1] == "torch: 98 KB of 98 KB"
    assert any("Successfully installed" in x for x in logged)
    cancel = threading.Event()
    t0 = time.monotonic()
    threading.Timer(0.5, cancel.set).start()
    rc, _ = ai_align._run_step([sys.executable, str(script), "slow"], logged.append, cancel, lambda f, d: None)
    assert rc != 0 and time.monotonic() - t0 < 10                 # killed, not waited out


def _words(text, start, step=0.3, prob=0.95):
    out, t = [], start
    for w in text.split():
        out.append([" " + w, round(t, 2), round(t + step - 0.02, 2), prob])
        t += step
    return out


LIVE = ("[00:10.00]Paper boats along the canal\n[00:14.00]Carry every name I knew\n"
        "[00:18.00]Lanterns on the water fall\n[00:22.00]And the river takes them through\n"
        "[00:26.00]Morning comes with copper light\n[00:30.00]Every window turning gold\n")


def test_live_recording_talk_is_skipped_and_lines_go_where_they_are_sung():
    # live: the band talks at 25 s, and the second half comes 20 s later than in the studio timing
    transcript = (_words("Paper boats along the canal", 11.0) + _words("Carry every name I knew", 15.0)
                  + _words("Lanterns on the water fall", 19.0) + _words("And the river takes them through", 23.0)
                  + _words("thank you so much London this one is for the river", 28.0, prob=0.9)
                  + _words("Morning comes with copper light", 46.0) + _words("Every window turning gold", 50.0))
    # forced alignment, fooled by the talk: puts "Morning…" on it
    segs = [{"start": t, "words": _words(b, t)} for t, b in
            [(11.0, "Paper boats along the canal"), (15.0, "Carry every name I knew"),
             (19.0, "Lanterns on the water fall"), (23.0, "And the river takes them through"),
             (28.5, "Morning comes with copper light"), (50.0, "Every window turning gold")]]
    res = {"ok": True, "language": "en", "segments": segs, "transcript": transcript}
    out = ai_align.evaluate(LIVE, res, 60.0, 70)
    assert out.ok and out.verify == 100.0 and out.details["method"] == "heard"
    lines = [(t, b) for t, b in lrc.display_lines(out.text)]
    times = {b: t for t, b in lines if b}
    assert times["Paper boats along the canal"] == 11.0                  # both agree: forced onset
    assert abs(times["Morning comes with copper light"] - 46.0) < 0.1    # transcript wins over the talk
    assert abs(times["Every window turning gold"] - 50.0) < 0.1
    gaps = [t for t, b in lines if not b]
    assert len(gaps) == 1 and 24 < gaps[0] < 26                         # the talk sits in a gap
    assert out.ends == {lrc.pin_key(22.0): gaps[0]}
    assert all(0.9 <= c <= 1.0 for c in out.line_conf.values())
    # the verify score is about lyric words actually sung, not just any sound
    wrong = dict(res, transcript=_words("completely different words sung over the whole song tonight", 10, 3))
    bad = ai_align.evaluate(LIVE, wrong, 60.0, 70)
    assert not bad.ok and bad.verify < 20 and "sung" in bad.reason


def test_words_stretched_over_silence_are_not_used_for_line_starts():
    tr = [[" Paper", 2.0, 10.9, 0.6]] + _words("boats along the canal", 11.3) + \
        _words("Carry every name I knew", 15.0) + _words("Lanterns on the water fall", 19.0) + \
        _words("And the river takes them through", 23.0) + _words("Morning comes with copper light", 27.0) + \
        _words("Every window turning gold", 31.0)
    out = ai_align.evaluate(LIVE, {"ok": True, "segments": [], "transcript": tr}, 60.0, 70)
    first = lrc.display_lines(out.text)[0][0]
    assert 10.9 <= first <= 11.1


def test_match_transcript_skips_adlibs():
    lines = ["Paper boats along the canal", "Carry every name I knew"]
    tr = _words("yeah yeah come on", 1.0) + _words("Paper boats along the canal", 5.0) + \
        _words("whoa oh", 7.0) + _words("Carry every name I knew", 9.0)
    m = ai_align.match_transcript(lines, tr)
    assert [len(x) for x in m] == [5, 5] and m[0][0][1] == 5.0 and m[1][0][1] == 9.0


def test_insert_blank_lines_keeps_time_order():
    out = lrc.insert_blank_lines("[00:01.00]a\n[00:05.00]b\n", [3.0, 9.0])
    assert out == "[00:01.00]a\n[00:03.00]\n[00:05.00]b\n[00:09.00]\n"


def test_aligner_reports_progress_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    seen = []
    al = ai_align.Aligner(command=fake_cmd())
    res = al.align("x.flac", ["a b", "c d"], on_progress=lambda st, f: seen.append((st, f)))
    assert res["ok"] and al.loaded
    assert seen[0] == ("start", None) and ("separate", 0.5) in seen and seen[-1][0] == "cleanup"
    seen.clear()
    al.align("x.flac", ["a b", "c d"], on_progress=lambda st, f: seen.append((st, f)))
    assert seen[0][0] == "decode"                                 # running already: no start step
    al.close()


def test_song_progress_shares_the_bar_by_estimates(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    monkeypatch.setattr(ai_align, "_cost_cache", None)
    now = [100.0]
    cold = ai_align.SongProgress("a", 240, warm=False, running=False, clock=lambda: now[0])
    warm = ai_align.SongProgress("a", 240, warm=True, running=True, clock=lambda: now[0])
    assert cold.plan[0] == "start" and "load_whisper" in cold.plan
    assert "start" not in warm.plan and "load_whisper" not in warm.plan
    assert cold.snapshot()["left"] > warm.snapshot()["left"] + 8           # loading the models costs time
    # a longer song is estimated to take longer
    long_ = ai_align.SongProgress("b", 600, warm=True, running=True, clock=lambda: now[0])
    assert long_.snapshot()["left"] > warm.snapshot()["left"]
    # progress only moves forward, through the steps, and never claims to be done early
    p, last = warm, -1.0
    for stage, frac in [("decode", None), ("separate", 0.2), ("separate", 0.9), ("language", None),
                        ("align", None), ("transcribe", 0.5), ("cleanup", None), ("check", None)]:
        p.step(stage, frac)
        for _ in range(3):
            now[0] += 2.0
            snap = p.snapshot()
            assert last <= snap["frac"] < 1.0 and snap["label"] == ai_align.STAGE_LABEL[stage]
            last = snap["frac"]
    assert last > 0.85
    times = p.finish()
    assert p.snapshot()["frac"] == 1.0 and times["separate"] == pytest.approx(12.0)
    # a crash and retry starts over with loading
    q = ai_align.SongProgress("a", 240, warm=True, running=True, clock=lambda: now[0])
    q.step("separate", 0.5)
    q.restart()
    assert q.plan[0] == "start" and "(retrying)" in q.snapshot()["label"]


def test_measured_timings_replace_the_estimates(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    monkeypatch.setattr(ai_align, "_cost_cache", None)
    before = ai_align.stage_costs()["separate"]
    for _ in range(10):
        ai_align.learn_timings({"separate": 40.0, "load_whisper": 30.0}, 240)     # a slower GPU
    after = ai_align.stage_costs()
    assert after["separate"][1] > before[1] * 3 and after["load_whisper"][0] > 25
    monkeypatch.setattr(ai_align, "_cost_cache", None)                           # read back from disk
    assert ai_align.stage_costs()["separate"] == after["separate"]


def test_worker_output_survives_any_text(monkeypatch):
    import io
    sys.path.insert(0, os.path.join(HERE, "..", "src", "mass_synced_lyrics"))
    try:
        import aligner_worker
    finally:
        sys.path.pop(0)
    buf = io.BytesIO()
    out = io.TextIOWrapper(buf, encoding="cp1252")            # Windows pipe under python -I
    monkeypatch.setattr(sys, "stdout", out)
    aligner_worker._emit({"transcript": [[" 와", 1.0, 1.2, 0.3]], "text": "♪ naïve"})
    out.flush()
    assert json.loads(buf.getvalue().decode("ascii"))["transcript"][0][0] == " 와"


def test_service_retries_a_crashed_worker_and_force_realigns_from_the_original(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    monkeypatch.setattr(ai_align, "nvidia_gpu", lambda: "NVIDIA test GPU")
    monkeypatch.setenv("FAKE_CRASH_ONCE", str(tmp_path / "crashed"))
    p = make_track(os.path.join(settings.music_folder, "b.flac"), "flac", title="Other Tune")
    _find(settings, [record(title="Other Tune (Live)", synced=LYR, duration=12)])
    events = []
    svc = ai_align.AlignService(lambda: settings, events.append, lambda s: ai_align.Aligner(command=fake_cmd()))
    svc.submit([p])
    assert _wait(lambda: any(e.get("status") == "aligned" for e in events))
    assert os.path.exists(tmp_path / "crashed") and not svc.paused            # crashed once, retried, fine
    assert not any(e["type"] == "align_error" for e in events) and svc.current is None
    # asked again for this song (right-click): it is aligned again from the original timing, not skipped
    monkeypatch.setenv("FAKE_START", "3.0")
    svc._aligner.close()
    svc._aligner = None                                       # fresh worker reads the new env
    events.clear()
    svc.submit([p], force=True)
    assert _wait(lambda: any(e.get("type") == "align_progress" for e in events))
    ev = next(e for e in events if e.get("type") == "align_progress")
    assert ev["status"] == "aligned" and ev["reason"] not in ("aligned before", "already aligned")
    st = State(settings.resolved_state_db())
    try:
        assert [t for t, _ in lrc.display_lines(st.get_pending(p)[2])] == [3.0, 6.0, 9.0]
        assert lrc.same_lyrics(st.get_candidates(p)["online-original"][1], LYR)     # still the original
        assert st.get_pending(p)[1].count("aligned") == 1
    finally:
        st.close()
    svc.stop()
    svc.join(5)


def test_stop_answers_waiting_player_requests(settings, monkeypatch, tmp_path):
    events = []
    svc = ai_align.AlignService(lambda: settings, events.append, lambda s: ai_align.Aligner(command=fake_cmd()))
    svc.paused = True                                          # e.g. waiting on an error
    svc.request_raw("x.flac", ["a", "b"], 0.0, "tok")
    assert svc.raw_waiting("tok") and svc.pending() == 0
    svc.stop()
    svc.join(5)
    raw = [e for e in events if e["type"] == "align_raw"]
    assert raw and raw[0]["token"] == "tok" and not raw[0]["result"]["ok"]


def test_songs_with_your_own_choice_say_why_they_are_skipped(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    p = make_track(os.path.join(settings.music_folder, "b.flac"), "flac", title="Other Tune")
    _find(settings, [record(title="Other Tune (Live)", synced=LYR, duration=12)])
    st = State(settings.resolved_state_db())
    st.set_override(p, "keep")
    al = ai_align.Aligner(command=fake_cmd())
    ev = ai_align.align_one(settings, p, al, st, 70, force=True)
    st.close()
    al.close()
    assert ev["status"] == "skipped" and "your own choice" in ev["reason"]


def test_heard_alignment_is_stored_and_saved_as_plain_json(settings, monkeypatch, tmp_path):
    # the transcript matcher's similarity scores came from numpy (float32): storing the per-line
    # certainty failed with "Object of type float32 is not JSON serializable" right at the end
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    monkeypatch.setenv("FAKE_TRANSCRIPT", "1")
    p = make_track(os.path.join(settings.music_folder, "b.flac"), "flac", title="Other Tune")
    _find(settings, [record(title="Other Tune (Live)", synced=LYR, duration=12)])
    st = State(settings.resolved_state_db())
    al = ai_align.Aligner(command=fake_cmd())
    try:
        ev = ai_align.align_one(settings, p, al, st, 70, force=True)
        assert ev["status"] == "aligned", ev
        det = st.get_aligned_detail(p)
        assert det["confs"] and all(type(v) is float for v in det["confs"].values())
        st.set_song_prefs(p, {"ai_conf": det["confs"]})
    finally:
        al.close()
        st.close()
    m = ai_align.match_transcript(["a b"], [[" a", 1.0, 1.2, 0.9], [" b", 1.3, 1.5, 0.9]])
    assert all(type(x) is float for w in m[0] for x in w[1:])


def test_keep_models_loaded_hands_the_worker_to_the_next_run(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    monkeypatch.setattr(ai_align, "nvidia_gpu", lambda: "NVIDIA test GPU")
    p = make_track(os.path.join(settings.music_folder, "b.flac"), "flac", title="Other Tune")
    _find(settings, [record(title="Other Tune (Live)", synced=LYR, duration=12)])
    assert settings.ai_keep_loaded                                  # on by default
    held, made = [], []

    def factory(s):
        if held:
            return held.pop()
        made.append(ai_align.Aligner(command=fake_cmd()))
        return made[-1]
    events = []
    svc = ai_align.AlignService(lambda: settings, events.append, factory, handoff=held.append)
    svc.IDLE_RELEASE = 0.0                                          # would release at once if not kept
    svc.submit([p], force=True)
    assert _wait(lambda: any(e.get("status") == "aligned" for e in events) and svc.idle())
    import time
    time.sleep(1.5)
    assert svc._aligner is not None and svc._aligner.running()      # kept while idle
    svc.stop()
    svc.join(5)
    assert len(held) == 1 and held[0].running() and held[0].loaded   # handed over, still loaded
    # the next run takes it: no start-up, no model loading
    events.clear()
    svc2 = ai_align.AlignService(lambda: settings, events.append, factory, handoff=held.append)
    svc2.IDLE_RELEASE = 0.0
    svc2.submit([p], force=True)
    assert _wait(lambda: svc2.current is not None or any(e.get("type") == "align_progress" for e in events))
    assert _wait(lambda: any(e.get("type") == "align_progress" for e in events))
    assert len(made) == 1
    # switched off: the idle worker is let go
    settings.ai_keep_loaded = False
    assert _wait(lambda: svc2._aligner is None)
    svc2.stop()
    svc2.join(5)
    assert not held and not made[0].running()
