import csv
import os

import pytest
from conftest import FORMATS, LRC, LRC_B, PLAIN, FakeLrclib, make_track, record, write_lrc

from mass_synced_lyrics import lrc
from mass_synced_lyrics.engine import Engine
from mass_synced_lyrics.tags import Meta, TagFile

MTIME = 1_600_000_000


def run(settings, lrclib=None, **kw):
    events = []
    eng = Engine(settings, events.append, lrclib=lrclib or FakeLrclib(), musicbrainz=kw.get("mb"),
                 acoustid=kw.get("acoustid"))
    summary = eng.run()
    results = {os.path.basename(e["result"].path): e["result"] for e in events if e["type"] == "result"}
    return summary, results, events


def lyrics_of(p):
    return TagFile(p).read().lyrics


@pytest.mark.parametrize("fmt", FORMATS)
def test_sidecar_exact_embeds_restores_mtime_and_removes_lrc(settings, fmt):
    d = settings.music_folder
    p = make_track(os.path.join(d, f"01 Some Song.{fmt}"), fmt)
    l = write_lrc(os.path.join(d, "01 Some Song.lrc"))
    fake = FakeLrclib()
    summary, res, _ = run(settings, fake)
    r = res[os.path.basename(p)]
    assert r.status == "embedded" and r.source == "local .lrc", r.detail
    assert any(lrc.same_lyrics(t, LRC) for t in lyrics_of(p))
    assert int(os.stat(p).st_mtime) == MTIME
    assert not os.path.exists(l)
    assert fake.calls == []


def test_fuzzy_sidecar_local_wins_over_existing_synced(settings):
    d = settings.music_folder
    p = make_track(os.path.join(d, "03 - Some Song (Remastered 2011).flac"), "flac",
                   title="Some Song (Remastered 2011)", lyrics=LRC_B)
    l = write_lrc(os.path.join(d, "Some Artist - Some Song.lrc"))
    _, res, _ = run(settings)
    r = next(iter(res.values()))
    assert r.status == "embedded" and "replaces existing synced" in r.detail
    assert lrc.same_lyrics(lyrics_of(p)[0], LRC)
    assert not os.path.exists(l)


def test_identical_embedded_just_removes_lrc(settings):
    d = settings.music_folder
    make_track(os.path.join(d, "a.flac"), "flac", lyrics=LRC)
    l = write_lrc(os.path.join(d, "a.lrc"))
    _, res, _ = run(settings)
    assert res["a.flac"].status == "already_embedded"
    assert not os.path.exists(l)


def test_sidecar_too_long_is_rejected_and_falls_back_online(settings):
    d = settings.music_folder
    p = make_track(os.path.join(d, "a.mp3"), "mp3")
    l = write_lrc(os.path.join(d, "a.lrc"), "[00:01.00]x\n[03:30.00]y")
    _, res, _ = run(settings, FakeLrclib([record()]))
    r = res["a.mp3"]
    assert r.status == "embedded" and r.source == "lrclib"
    assert "rejected" in r.detail
    assert os.path.exists(l)  # not used, so not removed
    assert lrc.same_lyrics(lyrics_of(p)[0], LRC_B)


def test_existing_synced_without_local_is_left_alone(settings):
    p = make_track(os.path.join(settings.music_folder, "a.ogg"), "ogg", lyrics=LRC_B)
    fake = FakeLrclib([record(synced=LRC)])
    _, res, _ = run(settings, fake)
    assert res["a.ogg"].status == "has_synced" and fake.calls == []
    assert int(os.stat(p).st_mtime) == MTIME


def test_plain_upgraded_and_no_synced_left_alone(settings):
    d = settings.music_folder
    p1 = make_track(os.path.join(d, "a.flac"), "flac", lyrics=PLAIN)
    p2 = make_track(os.path.join(d, "b.flac"), "flac", title="Other", lyrics=PLAIN)
    fake = FakeLrclib([record(), record(title="Other", synced=None)])
    _, res, _ = run(settings, fake)
    assert res["a.flac"].status == "embedded" and lrc.is_synced(lyrics_of(p1)[0])
    assert res["b.flac"].status == "no_synced" and lyrics_of(p2) == [PLAIN]
    assert int(os.stat(p2).st_mtime) == MTIME


def test_ambiguous_local_matches_are_not_applied(settings):
    d = settings.music_folder
    p = make_track(os.path.join(d, "01 Halo.flac"), "flac", title="Halo")
    write_lrc(os.path.join(d, "Halo!.lrc"), LRC)
    write_lrc(os.path.join(d, "Halo (alt).lrc"), LRC_B)
    write_lrc(os.path.join(d, "halo.LRC.lrc"), LRC_B)
    settings.use_online = False
    _, res, _ = run(settings)
    assert res["01 Halo.flac"].status == "ambiguous", res["01 Halo.flac"]
    assert lyrics_of(p) == []


def test_collection_used_before_online_and_kept(settings, tmp_path):
    coll = tmp_path / "my-lrcs"
    c1_text = "[ar:Some Artist]\n[ti:Some Song]\n" + LRC_B
    c1 = write_lrc(str(coll / "whatever name.lrc"), c1_text)
    body = "[00:01.00]Another one\n[00:06.00]line two"
    write_lrc(str(coll / "Some Artist - Another.lrc"), body)
    p = make_track(os.path.join(settings.music_folder, "x.m4a"), "m4a")
    p2 = make_track(os.path.join(settings.music_folder, "y.m4a"), "m4a", title="Another")
    make_track(os.path.join(settings.music_folder, "z.m4a"), "m4a", title="Another",
                    artist="Wrong Artist")
    settings.collection = [str(coll)]
    fake = FakeLrclib()
    _, res, _ = run(settings, fake)
    assert res["x.m4a"].source == "collection" and lrc.same_lyrics(lyrics_of(p)[0], c1_text)
    assert res["y.m4a"].source == "collection" and lrc.same_lyrics(lyrics_of(p2)[0], body)
    assert res["z.m4a"].status == "not_found"
    assert os.path.exists(c1)
    assert all(c[1] == "Another" for c in fake.calls)  # only z went online


def test_dry_run_changes_nothing(settings):
    d = settings.music_folder
    p = make_track(os.path.join(d, "a.flac"), "flac")
    l = write_lrc(os.path.join(d, "a.lrc"))
    settings.dry_run = True
    summary, res, _ = run(settings)
    assert res["a.flac"].status == "proposed"
    assert lyrics_of(p) == [] and os.path.exists(l)
    assert summary["csv"].endswith("-dryrun.csv")


def test_resume_skips_finished_files_and_writes_csv(settings):
    d = settings.music_folder
    make_track(os.path.join(d, "a.flac"), "flac")
    make_track(os.path.join(d, "b.flac"), "flac", title="Missing")
    fake = FakeLrclib([record()])
    s1, res1, _ = run(settings, fake)
    assert res1["a.flac"].status == "embedded" and res1["b.flac"].status == "not_found"
    with open(s1["csv"], encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert {r["status"] for r in rows} == {"embedded", "not_found"}
    fake2 = FakeLrclib([record(title="Missing")])
    _, res2, _ = run(settings, fake2)
    assert res2["a.flac"].status == "cached" and res2["b.flac"].status == "cached"
    assert fake2.calls == []
    settings.retry_misses = True
    _, res3, _ = run(settings, fake2)
    assert res3["b.flac"].status == "embedded"


def test_untagged_identified_via_acoustid(settings):
    class FakeAcoustId:
        def identify(self, path):
            return Meta(title="Some Song", artist="Some Artist", album="")

    p = make_track(os.path.join(settings.music_folder, "track01.wav"), "wav", title="", artist="", album="")
    _, res, _ = run(settings, FakeLrclib([record()]), acoustid=FakeAcoustId())
    assert res["track01.wav"].status == "embedded" and "AcoustID" in res["track01.wav"].detail
    assert lrc.is_synced(lyrics_of(p)[0])

    make_track(os.path.join(settings.music_folder, "track02.wav"), "wav", title="", artist="", album="")
    _, res, _ = run(settings, FakeLrclib([record()]))
    assert res["track02.wav"].status == "no_metadata"


def test_corrupt_file_reported_not_fatal(settings):
    bad = os.path.join(settings.music_folder, "bad.flac")
    with open(bad, "wb") as fh:
        fh.write(b"not audio at all")
    make_track(os.path.join(settings.music_folder, "a.flac"), "flac")
    _, res, _ = run(settings, FakeLrclib([record()]))
    assert res["bad.flac"].status in ("unsupported", "error")
    assert res["a.flac"].status == "embedded"


def test_invalid_acoustid_key_disables_fingerprinting_once(settings):
    from mass_synced_lyrics.providers import ServiceError

    class BadKey:
        calls = 0

        def identify(self, path):
            BadKey.calls += 1
            raise ServiceError('HTTP 400: {"error": {"code": 4, "message": "invalid API key"}}')

    for n in ("a.wav", "b.wav"):
        make_track(os.path.join(settings.music_folder, n), "wav", title="", artist="", album="")
    _, res, events = run(settings, FakeLrclib(), acoustid=BadKey())
    assert BadKey.calls == 1
    assert {r.status for r in res.values()} == {"no_metadata"}
    assert sum("rejected the API key" in e.get("text", "") for e in events) == 1


def test_parallel_workers_are_faster_and_consistent(settings):
    import time as _t

    class SlowLrclib(FakeLrclib):
        def get(self, *a, **k):
            _t.sleep(0.05)
            return super().get(*a, **k)

        def search(self, *a, **k):
            _t.sleep(0.05)
            return super().search(*a, **k)

    recs = []
    for f in range(6):
        for n in range(5):
            title = f"T{f}x{n} Song"
            make_track(os.path.join(settings.music_folder, f"album{f}", f"{n:02d} {title}.flac"), "flac",
                       title=title)
            if n % 2 == 0:
                recs.append(record(title=title))
    settings.dry_run = True
    results = {}
    timings = {}
    for workers in (1, 6):
        settings.workers = workers
        settings.state_db = os.path.join(os.path.dirname(settings.state_db), f"state-{workers}.db")
        events = []
        t0 = _t.monotonic()
        Engine(settings, events.append, lrclib=SlowLrclib(recs)).run()
        timings[workers] = _t.monotonic() - t0
        res = {os.path.basename(e["result"].path): e["result"].status for e in events if e["type"] == "result"}
        assert len(res) == 30 and "error" not in res.values(), res
        results[workers] = res
    assert results[1] == results[6]
    assert sum(1 for v in results[6].values() if v == "proposed") == 18
    assert timings[6] < timings[1] * 0.6, timings


def test_rate_limit_backoff_is_shared():
    from mass_synced_lyrics.providers import RateLimiter
    rl = RateLimiter(6000)
    slept = []
    rl.defer(5.0)
    rl.wait(slept.append)
    assert slept and slept[0] > 4.5            # another thread's 429 pauses this one too


def test_no_online_requests_for_songs_with_synced_lyrics_embedded(settings):
    d = settings.music_folder
    make_track(os.path.join(d, "a.flac"), "flac", lyrics=LRC_B)                    # synced, embedded
    write_lrc(os.path.join(d, "a.lrc"), LRC)                                       # and a sidecar
    make_track(os.path.join(d, "b.flac"), "flac", title="Other", lyrics=LRC_B)
    settings.dry_run = True
    lib = FakeLrclib([record(), record(title="Other")])
    events = []
    Engine(settings, events.append, lrclib=lib).run()
    assert lib.calls == []                                                         # nothing sent
    res = {os.path.basename(e["result"].path): e["result"] for e in events if e["type"] == "result"}
    assert res["a.flac"].status == "proposed" and res["a.flac"].local == "sidecar"  # sidecar still offered
    assert res["b.flac"].status == "has_synced"
    # .lrc mode: the embedded lyrics are exported without asking LRCLIB
    settings.output_mode = "lrc"
    settings.state_db = os.path.join(os.path.dirname(settings.state_db), "s2.db")
    lib = FakeLrclib([record(title="Other")])
    events = []
    Engine(settings, events.append, lrclib=lib).run()
    assert lib.calls == []
    res = {os.path.basename(e["result"].path): e["result"] for e in events if e["type"] == "result"}
    assert res["b.flac"].local == "embedded" and "synced lyrics already embedded" in res["b.flac"].detail
    # switched off: they are looked up as before
    settings.skip_synced_online = False
    settings.state_db = os.path.join(os.path.dirname(settings.state_db), "s3.db")
    lib = FakeLrclib([record(title="Other")])
    Engine(settings, lambda e: None, lrclib=lib).run()
    assert any(c[1] == "Other" for c in lib.calls)
