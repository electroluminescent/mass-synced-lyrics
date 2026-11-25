import os

import numpy as np
import pytest
from conftest import LRC, LRC_B, PLAIN, FakeLrclib, make_track, record, write_lrc

PLAIN_K = PLAIN

from mass_synced_lyrics import lrc, review
from mass_synced_lyrics.applier import apply_all, plan
from mass_synced_lyrics.engine import Engine
from mass_synced_lyrics.player import Player, compute_peaks, decode, envelope
from mass_synced_lyrics.state import State
from mass_synced_lyrics.tags import TagFile

MTIME = 1_600_000_000


def test_retime_and_alignment():
    t = "[ti:x]\n[00:01.00]<00:01.00>a <00:01.50>b\n[00:10.00][00:20.00]chorus"
    out = lrc.retime(t, offset=0.5, factor=1.1)
    assert out.splitlines()[0] == "[ti:x]"
    assert out.splitlines()[1] == "[00:01.60]<00:01.60>a <00:02.15>b"
    assert out.splitlines()[2] == "[00:11.50][00:22.50]chorus"
    assert lrc.retime(t, -5) .splitlines()[1].startswith("[00:00.00]")  # clamped
    off, fac = lrc.solve_alignment([(10, 12)])
    assert (off, fac) == (2, 1.0)
    off, fac = lrc.solve_alignment([(10, 12), (110, 122)])
    assert fac == pytest.approx(1.1) and off == pytest.approx(1.0)


def test_segments_and_words():
    t = "[00:01.00]<00:01.00>Hello <00:01.50>big <00:02.00>world\n[00:03.00]\n[00:04.00]second"
    segs = lrc.segments(t, 10)
    assert [(s.start, s.end, s.text) for s in segs] == [(1, 3, "Hello big world"), (4, 9, "second")]
    assert segs[0].words[1] == (1.5, 2.0, "big")
    assert lrc.segment_at(segs, 3.5) == -1 and lrc.segment_at(segs, 4.2) == 1
    lines = lrc.display_lines(t)
    assert lines[0] == (1.0, "Hello big world")
    assert lrc.current_index(lines, 3.2) == 1 and lrc.current_index(lines, 0.5) == -1


def test_decode_peaks_envelope():
    data = decode(os.path.join(os.path.dirname(__file__), "fixtures", "silence.flac"))
    assert data.duration == pytest.approx(10, abs=0.1) and data.samples.shape[1] == 2
    s = np.zeros((44100 * 2, 2), dtype=np.int16)
    s[44100:44100 + 441] = 10000
    p = compute_peaks(s, 44100)
    assert len(p) == 200 and p.max() == 1.0 and p[100] == 1.0 and p[0] == 0
    env = envelope(p, 0.5, 1.5, 100)
    assert env[50] == 1.0 and env[0] == 0
    assert envelope(p, -5, -1, 10).sum() == 0
    zoom = envelope(p, 0.99, 1.02, 300)  # more pixels than peaks
    assert zoom.max() == 1.0


def test_player_silent_clock():
    pl = Player()
    s = np.zeros((44100 * 3, 2), dtype=np.int16)
    from mass_synced_lyrics.player import AudioData
    pl.load(AudioData(s, 44100, compute_peaks(s, 44100)))
    pl.silent_reason = "test"           # force the clock fallback
    pl.seek(1.0)
    pl.play()
    import time
    time.sleep(0.15)
    assert 1.1 <= pl.position() < 1.6
    pl.pause()
    p = pl.position()
    time.sleep(0.05)
    assert pl.position() == p


def test_gather_candidates_and_apply_pins(settings):
    d = settings.music_folder
    p = make_track(os.path.join(d, "01 Some Song.flac"), "flac", lyrics=LRC_B)
    write_lrc(os.path.join(d, "Some Song.lrc"), LRC)
    info = review.gather(p, settings)
    keys = [c.key.split(":")[0] for c in info.candidates]
    assert keys[0] == "sidecar" and "current" in keys and keys[-1] == "remove"
    side = info.candidates[0]
    assert review.default_checkpoints(side.text, info.duration) == [1.0, 8.2]

    shifted = lrc.retime(side.text, 0.25)
    review.choose(p, shifted, side.label + " (offset +250 ms)", settings, side.path)
    assert lrc.same_lyrics(TagFile(p).read().lyrics[0], LRC_B)     # nothing written yet
    summary = apply_all(settings, [p])
    assert summary["counts"] == {"embedded": 1}
    assert lrc.same_lyrics(TagFile(p).read().lyrics[0], shifted)
    assert int(os.stat(p).st_mtime) == MTIME
    assert not os.path.exists(side.path)      # sidecar recycled/deleted per settings
    info2 = review.gather(p, settings)
    assert info2.override[0] == "pin"
    assert any(c.key == "backup" and lrc.same_lyrics(c.text, LRC_B) for c in info2.candidates)

    # the pinned choice wins over a later run, and is re-applied if the file loses it
    write_lrc(os.path.join(d, "01 Some Song.lrc"), LRC)
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib([record()])).run()
    r = [e["result"] for e in events if e["type"] == "result"][0]
    assert r.status == "locked"
    assert os.path.exists(os.path.join(d, "01 Some Song.lrc"))   # sidecar untouched
    tf = TagFile(p)
    tf.remove_lyrics()
    tf.save()
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib()).run()
    r = [e["result"] for e in events if e["type"] == "result"][0]
    assert r.status == "embedded" and r.source == "your choice"
    assert lrc.same_lyrics(TagFile(p).read().lyrics[0], shifted)

    # clearing the choice hands the file back to the settings (sidecar wins again)
    review.clear_choice(p, settings)
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib()).run()
    r = [e["result"] for e in events if e["type"] == "result"][0]
    assert r.status == "embedded" and r.source == "local .lrc"


def test_lock_keeps_file_untouched(settings):
    d = settings.music_folder
    p = make_track(os.path.join(d, "a.flac"), "flac", lyrics=LRC_B)
    write_lrc(os.path.join(d, "a.lrc"), LRC)
    review.lock_current(p, settings)
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib()).run()
    assert [e["result"].status for e in events if e["type"] == "result"] == ["locked"]
    assert lrc.same_lyrics(TagFile(p).read().lyrics[0], LRC_B)


def test_dry_run_proposal_becomes_candidate_and_backup_on_real_run(settings):
    d = settings.music_folder
    p = make_track(os.path.join(d, "a.flac"), "flac", lyrics="plain words")
    settings.dry_run = True
    Engine(settings, lambda e: None, lrclib=FakeLrclib([record()])).run()
    info = review.gather(p, settings)
    assert info.candidates[0].key == "pending" and lrc.same_lyrics(info.candidates[0].text, LRC_B)
    settings.dry_run = False
    Engine(settings, lambda e: None, lrclib=FakeLrclib([record()])).run()
    st = State(settings.resolved_state_db())
    assert st.get_backup(p) == "plain words" and st.get_pending(p) is None
    st.close()


def test_song_prefs_roundtrip(settings):
    p = make_track(os.path.join(settings.music_folder, "a.flac"), "flac")
    review.save_prefs(p, {"checkpoints": [3.5, 7.0], "offset": 0.2}, settings)
    assert review.gather(p, settings).prefs["checkpoints"] == [3.5, 7.0]


def test_find_then_apply_embed_and_lrc_modes(settings):
    d = settings.music_folder
    a = make_track(os.path.join(d, "a.flac"), "flac")
    b = make_track(os.path.join(d, "b.mp3"), "mp3", title="Other")
    c = make_track(os.path.join(d, "c.ogg"), "ogg", title="Kept", lyrics=PLAIN_K)
    side = write_lrc(os.path.join(d, "b.lrc"), LRC)
    review.lock_current(c, settings)
    settings.dry_run = True
    Engine(settings, lambda e: None, lrclib=FakeLrclib([record(), record(title="Kept")])).run()
    assert TagFile(a).read().lyrics == [] and os.path.exists(side)
    assert sorted(plan(settings, [a, b, c])) == sorted([a, b])     # locked file excluded

    settings.output_mode = "lrc"
    out = apply_all(settings, [a, b, c])
    assert out["counts"] == {"lrc_written": 1, "unchanged": 1}
    assert lrc.same_lyrics(open(os.path.splitext(a)[0] + ".lrc", encoding="utf-8").read(), LRC_B)
    # b.lrc was both the sidecar and the output: it stays, unchanged content
    assert os.path.exists(side) and lrc.same_lyrics(open(side, encoding="utf-8").read(), LRC)
    assert TagFile(a).read().lyrics == [] and int(os.stat(a).st_mtime) == MTIME   # audio untouched
    assert plan(settings, [a, b, c]) == []


def test_lrc_mode_direct_run(settings):
    d = settings.music_folder
    settings.output_mode = "lrc"
    a = make_track(os.path.join(d, "01 Some Song.flac"), "flac")
    write_lrc(os.path.join(d, "Some Song.lrc"), LRC)                    # fuzzy-named sidecar
    e = make_track(os.path.join(d, "02 Embedded.flac"), "flac", title="Embedded", lyrics=LRC_B)
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib()).run()
    res = {os.path.basename(x["result"].path): x["result"] for x in events if x["type"] == "result"}
    assert res["01 Some Song.flac"].status == "lrc_written"
    assert os.path.exists(os.path.join(d, "01 Some Song.lrc"))
    assert not os.path.exists(os.path.join(d, "Some Song.lrc"))          # renamed in effect
    assert res["02 Embedded.flac"].status == "lrc_written" and res["02 Embedded.flac"].source == "embedded"
    assert lrc.same_lyrics(open(os.path.join(d, "02 Embedded.lrc"), encoding="utf-8").read(), LRC_B)
    assert TagFile(a).read().lyrics == [] and lrc.same_lyrics(TagFile(e).read().lyrics[0], LRC_B)
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib()).run()
    assert {x["result"].status for x in events if x["type"] == "result"} == {"cached"}
    # switching back to embed mode re-judges the files
    settings.output_mode = "embed"
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib()).run()
    res = {os.path.basename(x["result"].path): x["result"] for x in events if x["type"] == "result"}
    assert res["01 Some Song.flac"].status == "embedded" and res["02 Embedded.flac"].status == "already_embedded"


def test_choose_remove_in_lrc_mode_deletes_lrc(settings):
    d = settings.music_folder
    settings.output_mode = "lrc"
    a = make_track(os.path.join(d, "a.flac"), "flac")
    out = write_lrc(os.path.join(d, "a.lrc"), LRC)
    info = review.gather(a, settings)
    assert lrc.same_lyrics(info.current, LRC)
    rm = next(c for c in info.candidates if c.key == "remove")
    review.choose(a, rm.text, rm.label, settings)
    assert apply_all(settings, [a])["counts"] == {"lrc_written": 1}
    assert not os.path.exists(out)


def test_player_volume_in_callback():
    s = np.full((2048, 2), 10000, dtype=np.int16)
    from mass_synced_lyrics.player import AudioData
    pl = Player()
    pl.load(AudioData(s, 44100, compute_peaks(s, 44100)))
    pl._playing = True
    out = np.zeros((1024, 2), dtype=np.int16)
    pl.volume = 0.5
    pl._callback(out, 1024, None, None)
    assert out[0, 0] == 5000
    pl.volume = 0.0
    pl._callback(out, 1024, None, None)
    assert not out.any()


def test_restore_originals_embed_and_lrc(settings):
    from mass_synced_lyrics.applier import restore_originals
    d = settings.music_folder
    a = make_track(os.path.join(d, "a.flac"), "flac", lyrics="old words")
    b = make_track(os.path.join(d, "b.flac"), "flac", title="Other")
    settings.dry_run = True
    Engine(settings, lambda e: None, lrclib=FakeLrclib([record(), record(title="Other")])).run()
    assert apply_all(settings, [a, b])["counts"] == {"embedded": 2}
    out = restore_originals(settings, [a, b])
    assert out["counts"] == {"restored": 2}
    assert TagFile(a).read().lyrics == ["old words"] and TagFile(b).read().lyrics == []
    assert int(os.stat(a).st_mtime) == MTIME
    assert restore_originals(settings, [a])["counts"] == {"nothing": 1}

    # .lrc mode: a created .lrc goes away, an existing one gets its old content back
    settings.output_mode = "lrc"
    old = write_lrc(os.path.join(d, "a.lrc"), "[00:01.00]old\n[00:02.00]lrc")
    review.choose(a, LRC, "mine", settings)
    review.choose(b, LRC, "mine", settings)
    assert apply_all(settings, [a, b])["counts"] == {"lrc_written": 2}
    assert lrc.same_lyrics(open(old, encoding="utf-8").read(), LRC)
    restore_originals(settings, [a, b])
    assert lrc.same_lyrics(open(old, encoding="utf-8").read(), "[00:01.00]old\n[00:02.00]lrc")
    assert not os.path.exists(os.path.join(d, "b.lrc"))


def test_session_roundtrip(tmp_path):
    from mass_synced_lyrics.session import load_session, save_session
    p = str(tmp_path / "s.json")
    rows = {"/m/a.flac": {"status": "proposed", "choice": "online", "title": "Ä"}}
    save_session(rows, "/m/a.flac", "All", "x", "/m", path=p)
    d = load_session(p)
    assert d["rows"] == rows and d["selected"] == "/m/a.flac" and d["search"] == "x"
    assert load_session(str(tmp_path / "missing.json")) is None


def test_volume_curve_is_logarithmic():
    from mass_synced_lyrics.player import volume_gain
    assert volume_gain(0) == 0 and volume_gain(100) == pytest.approx(1.0)
    assert volume_gain(50) == pytest.approx(0.1)           # -20 dB: quieter for most of the slider
    assert volume_gain(75) == pytest.approx(10 ** (-10 / 20))
    assert volume_gain(25) < volume_gain(50) < volume_gain(75)
    assert volume_gain(-5) == 0 and volume_gain(500) == pytest.approx(1.0)


def test_tidy_options_apply_to_written_lyrics(settings):
    d = settings.music_folder
    fancy = "[00:01.00]  “Hello” it’s me  \n[00:04.00]…and you"
    a = make_track(os.path.join(d, "a.flac"), "flac")
    write_lrc(os.path.join(d, "a.lrc"), fancy)
    settings.tidy_trim = settings.tidy_quotes = True
    Engine(settings, lambda e: None, lrclib=FakeLrclib()).run()
    assert TagFile(a).read().lyrics[0] == '[00:01.00]"Hello" it\'s me\n[00:04.00]...and you'
    # the apply step tidies too (e.g. a choice made before the options were switched on)
    b = make_track(os.path.join(d, "b.flac"), "flac", title="Other")
    settings.tidy_trim = settings.tidy_quotes = False
    review.choose(b, fancy, "mine", settings)
    settings.tidy_quotes = True
    apply_all(settings, [b])
    assert "“" not in TagFile(b).read().lyrics[0] and "  " in TagFile(b).read().lyrics[0]


def test_saving_runs_in_parallel_and_restores(settings):
    from mass_synced_lyrics.applier import restore_originals
    d = settings.music_folder
    paths, recs = [], []
    for i in range(24):
        title = f"P{i}q Song"
        paths.append(make_track(os.path.join(d, f"al{i % 4}", f"{i:02d}.flac"), "flac", title=title))
        recs.append(record(title=title))
    settings.dry_run = True
    settings.workers = 6
    Engine(settings, lambda e: None, lrclib=FakeLrclib(recs)).run()
    events = []
    out = apply_all(settings, paths, events.append)
    assert out["counts"] == {"embedded": 24} and out["total"] == 24
    done = [e["done"] for e in events if e["type"] == "apply_progress"]
    assert done == list(range(1, 25))                                   # progress counts in order
    assert {e["path"] for e in events if e["type"] == "apply_progress"} == set(paths)
    for p in paths:
        assert lrc.same_lyrics(TagFile(p).read().lyrics[0], LRC_B) and int(os.stat(p).st_mtime) == MTIME
    with open(out["csv"], encoding="utf-8-sig") as fh:
        assert len(fh.read().strip().splitlines()) == 25
    assert plan(settings, paths) == []
    r = restore_originals(settings, paths)
    assert r["counts"] == {"restored": 24}
    assert all(TagFile(p).read().lyrics == [] for p in paths)


def test_parallel_save_cancel_stops_early(settings):
    import threading
    d = settings.music_folder
    paths, recs = [], []
    for i in range(30):
        title = f"C{i}z Song"
        paths.append(make_track(os.path.join(d, f"{i:02d}.flac"), "flac", title=title))
        recs.append(record(title=title))
    settings.dry_run = True
    settings.workers = 2
    Engine(settings, lambda e: None, lrclib=FakeLrclib(recs)).run()
    cancel = threading.Event()

    def emit(e):
        if e["type"] == "apply_progress" and e["done"] == 3:
            cancel.set()
    out = apply_all(settings, paths, emit, cancel)
    assert out["cancelled"] and 3 <= sum(out["counts"].values()) < 30
    assert len(plan(settings, paths)) == 30 - sum(out["counts"].values())


def test_match_confidence_scores():
    from mass_synced_lyrics.providers import LOOKUP_CONFIDENCE, find_synced, match_confidence
    from mass_synced_lyrics.tags import Meta
    assert match_confidence(100, 10.0, 10.0) == 100
    assert match_confidence(100, 10.0, 11.0) == 95 and match_confidence(100, 10.0, 14.0) == 90
    assert LOOKUP_CONFIDENCE["get"] == 100
    m = Meta(title="Some Song", artist="Some Artist", album="Some Album")
    assert find_synced(FakeLrclib([record()]), m, 10.0).confidence == 100          # exact
    m2 = Meta(title="Some Song", artist="Some Artist", album="Other Album")
    assert find_synced(FakeLrclib([record()]), m2, 10.0).confidence == 97          # album differs
    r = find_synced(FakeLrclib([record(title="Some Song (Remix)")]), m, 10.5)     # search, loose name
    assert r.status == "found" and r.confidence <= 90


def test_apply_confident_plan_and_confidences_reach_the_list(settings):
    from mass_synced_lyrics.applier import plan_confident
    d = settings.music_folder
    exact = make_track(os.path.join(d, "a.flac"), "flac")
    loose = make_track(os.path.join(d, "b.flac"), "flac", title="Other Tune")
    mine = make_track(os.path.join(d, "c.flac"), "flac", title="Third")
    side = make_track(os.path.join(d, "Fourth Song.flac"), "flac", title="Fourth Song")
    write_lrc(os.path.join(d, "Fourth Song.lrc"), LRC)                    # exact sidecar name = 100
    settings.dry_run = True
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib([
        record(), record(title="Other Tune (Live)"), record(title="Third")])).run()
    res = {os.path.basename(e["result"].path): e["result"] for e in events if e["type"] == "result"}
    assert res["a.flac"].conf_online == 100 and res["a.flac"].score == 100
    assert res["b.flac"].status == "proposed" and res["b.flac"].conf_online < 95
    assert res["Fourth Song.flac"].conf_local == 100
    review.choose(mine, LRC, "edited", settings)                         # a per-file choice
    ok = plan_confident(settings, [exact, loose, mine, side])
    assert sorted(ok) == sorted([exact, side])
    assert sorted(plan_confident(settings, [exact, loose, mine, side], threshold=50)) == sorted([exact, loose, side])
    out = apply_all(settings, ok)
    assert out["counts"] == {"embedded": 2}
    assert plan_confident(settings, [exact, loose, mine, side]) == []
    # a rerun finds them cached and still knows the confidence
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib([])).run()
    cached = {os.path.basename(e["result"].path): e["result"] for e in events if e["type"] == "result"}
    assert cached["a.flac"].status == "cached" and cached["a.flac"].conf_online == 100


def test_resize_rules():
    from mass_synced_lyrics.review_ui import resize_lyric
    # rows: lyric 10, lyric 14 (butted), gap 17, lyric 20, lyric 24
    times = [10.0, 14.0, 17.0, 20.0, 24.0]
    texts = ["a", "b", "", "c", "d"]
    # end of "a" later: "b" is butted to it, so it ripples; the gap after "b" absorbs it
    out = resize_lyric(times, texts, 0, "end", 15.0, 60)
    assert out[1] == 15.0 and out[2] == 18.0 and 3 not in out and out[("stop", 0)] == 3
    # far enough to use up the gap: "c" and "d" move too
    out = resize_lyric(times, texts, 0, "end", 18.5, 60)
    assert out[1] == 18.5 and out[3] > 20.0 and out[4] > 24.0
    # end of "a" earlier: a new gap opens, "b" stays
    out = resize_lyric(times, texts, 0, "end", 12.0, 60)
    assert out == {("end", 0): 12.0}
    # end of "b" (gap after it): only the gap's start moves while there's room
    out = resize_lyric(times, texts, 1, "end", 19.0, 60)
    assert out == {2: 19.0}
    # start of "c": only "c" moves, kept after the gap and before its own end
    assert resize_lyric(times, texts, 3, "start", 18.0, 60) == {3: 18.0}
    assert resize_lyric(times, texts, 3, "start", 1.0, 60) == {3: 17.05}
    # the last lyric gets an end
    assert resize_lyric(times, texts, 4, "end", 27.0, 60) == {("end", 4): 27.0}
    # pushes are clamped to the song
    out = resize_lyric([1.0, 2.0, 3.0], ["a", "b", "c"], 0, "end", 9.9, 10.0)
    assert max(v for k, v in out.items() if isinstance(k, int)) <= 10.0


def test_old_alignments_become_visible_edits():
    from mass_synced_lyrics.review_ui import _diff_as_edits
    orig = "[00:01.00]a\n[00:05.00]b\n[00:09.00]c\n"
    aligned = "[00:01.50]a\n[00:04.00]\n[00:05.00]b\n[00:10.00]c\n"
    d = _diff_as_edits(orig, aligned)
    assert d["pins"] == {"1.00": 1.5, "9.00": 10.0} and d["ends"] == {"1.00": 4.0}
