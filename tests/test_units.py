
import pytest
from conftest import FORMATS, LRC, LRC_B, PLAIN, FakeLrclib, make_track, record

from mass_synced_lyrics import lrc
from mass_synced_lyrics.names import best_ratio, name_variants, primary_artist
from mass_synced_lyrics.providers import HttpClient, find_synced
from mass_synced_lyrics.tags import Meta, TagFile


def test_analyze_and_duration_checks():
    info = lrc.analyze(LRC)
    assert info.synced and info.timed_lines == 3 and info.last_ts == pytest.approx(8.2)
    assert info.headers["ti"] == "Some Song"
    assert lrc.check_against_duration(info, 10.0) is None
    assert "past track end" in lrc.check_against_duration(info, 4.0)
    assert lrc.check_against_duration(lrc.analyze(PLAIN), 10.0).startswith("no timestamps")
    assert not lrc.is_synced(PLAIN)
    with_len = lrc.analyze("[length:03:30]\n[00:01.00]a\n[00:02.00]b")
    assert "differs" in lrc.check_against_duration(with_len, 120)
    assert lrc.analyze("[00:01][00:30.5]chorus\n[01:02.123]x").last_ts == pytest.approx(62.123)


def test_decode_legacy_encodings():
    text = "[00:01.00]Café déjà vu\n[00:02.00]naïve"
    assert lrc.decode_bytes(text.encode("cp1252")) == text
    assert lrc.decode_bytes(b"\xef\xbb\xbf" + text.encode()) == text
    assert lrc.decode_bytes(text.encode("utf-16")) == text


@pytest.mark.parametrize("lrc_stem,audio_stem,title,artist,expect", [
    ("Some Song", "03 - Some Song (Remastered 2011)", "Some Song (Remastered 2011)", "Band", True),
    ("Band - Some Song", "03 Some Song", "Some Song", "Band", True),
    ("1-07 Some Song", "07 Some Song", "Some Song", "Band", True),
    ("99 Luftballons", "05 99 Luftballons", "99 Luftballons", "Nena", True),
    ("Beyonce - Halo", "01 Halo", "Halo", "Beyoncé", True),
    ("Completely Different", "01 Halo", "Halo", "Beyoncé", False),
])
def test_name_matching(lrc_stem, audio_stem, title, artist, expect):
    a = name_variants(audio_stem, artist) | name_variants(title, artist)
    score = best_ratio(name_variants(lrc_stem), a)
    assert (score >= 90) == expect, score


def test_primary_artist():
    assert primary_artist("Daft Punk feat. Pharrell Williams") == "Daft Punk"
    assert primary_artist("A; B") == "A"


@pytest.mark.parametrize("fmt", FORMATS)
def test_tag_roundtrip(tmp_path, fmt):
    p = make_track(str(tmp_path / f"t.{fmt}"), fmt)
    tf = TagFile(p)
    m = tf.read()
    assert (m.title, m.artist, m.album) == ("Some Song", "Some Artist", "Some Album")
    assert tf.duration == pytest.approx(10, abs=0.2)
    tf.set_lyrics(LRC)
    tf.save()
    assert any(lrc.same_lyrics(t, LRC) for t in TagFile(p).read().lyrics)
    tf2 = TagFile(p)
    tf2.set_lyrics(LRC_B)  # replacing must not leave the old one behind
    tf2.save()
    ly = TagFile(p).read().lyrics
    assert len(ly) == 1 and lrc.same_lyrics(ly[0], LRC_B)


def test_id3v23_preserved(tmp_path):
    p = make_track(str(tmp_path / "t.mp3"), "mp3", id3v23=True)
    tf = TagFile(p)
    assert tf.audio.tags.version[:2] == (2, 3)
    tf.set_lyrics(LRC)
    tf.save()
    tf = TagFile(p)
    assert tf.audio.tags.version[:2] == (2, 3)
    assert lrc.same_lyrics(tf.read().lyrics[0], LRC)


def test_find_synced_paths():
    m = Meta(title="Some Song", artist="Some Artist", album="Other Album")
    fake = FakeLrclib([record(album="Some Album")])
    res = find_synced(fake, m, 10.0)
    assert res.status == "found" and res.how == "get (no album)"

    # search fallback with duration filtering and featured artist variant
    m2 = Meta(title="Some Song (Remastered)", artist="Some Artist feat. X", album="")
    fake2 = FakeLrclib([record(title="Some Song", duration=50, rid=7),
                        record(title="Some Song", rid=8)])
    res2 = find_synced(fake2, m2, 10.0)
    assert res2.status == "found" and res2.record_id == 8

    assert find_synced(FakeLrclib([record(instrumental=True)]), m, 10.0).status == "instrumental"
    assert find_synced(FakeLrclib([record(synced=None)]), m, 10.0).status == "no_synced"
    assert find_synced(FakeLrclib([]), m, 10.0).status == "not_found"
    # synced lyrics running past the end of the track are refused
    long = "[00:01.00]a\n[04:00.00]b"
    assert find_synced(FakeLrclib([record(synced=long)]), m, 10.0).status == "not_found"


class _Resp:
    def __init__(self, code, headers=None, body=None):
        self.status_code, self.headers, self._b, self.text = code, headers or {}, body, ""

    def json(self):
        return self._b


class _Session:
    def __init__(self, responses):
        self.responses, self.headers, self.n = list(responses), {}, 0

    def request(self, *a, **k):
        self.n += 1
        return self.responses.pop(0)


def test_http_retry_after(monkeypatch):
    slept = []
    s = _Session([_Resp(429, {"Retry-After": "3"}), _Resp(503), _Resp(200, body={"ok": 1})])
    c = HttpClient("ua", 6000, session=s)
    monkeypatch.setattr(c, "sleep", lambda sec: slept.append(sec))
    r = c.request("GET", "http://x")
    assert r.json() == {"ok": 1} and s.n == 3
    assert slept[0] >= 3.5  # Retry-After plus jitter
    assert c.request.__self__ is c
    s2 = _Session([_Resp(404)])
    assert HttpClient("ua", 6000, session=s2).request("GET", "http://x") is None


@pytest.mark.parametrize("text,enc", [
    ("[00:01.00]Привет мир, как дела\n[00:02.00]хорошо", "cp1251"),
    ("[00:01.00]夜空ノムコウ\n[00:02.00]あの頃", "shift_jis"),
    ("[00:01.00]月亮代表我的心\n[00:02.00]你问我爱你有多深", "gbk"),
])
def test_decode_other_legacy_encodings(text, enc):
    assert lrc.decode_bytes(text.encode(enc)) == text


def test_retime_pins_and_interpolation():
    t = "[00:01.00]a\n[00:05.00]<00:05.00>b <00:06.00>c\n[00:09.00]d"
    pinned = lrc.retime(t, offset=2.0, pins={lrc.pin_key(5.0): 5.0})
    assert [x for x, _ in lrc.display_lines(pinned)] == [3.0, 5.0, 11.0]
    assert "<00:05.00>b <00:06.00>c" in pinned            # words move with their pinned line
    interp = lrc.retime(t, pins={lrc.pin_key(1.0): 2.0, lrc.pin_key(9.0): 18.0}, interpolate=True)
    assert [x for x, _ in lrc.display_lines(interp)] == pytest.approx([2.0, 10.0, 18.0])
    assert lrc.warp(0.0, [(1.0, 2.0), (9.0, 18.0)]) == pytest.approx(0.0)       # extrapolated
    assert lrc.warp(4.0, [(1.0, 3.0)]) == pytest.approx(6.0)                     # single pin = shift
    head = lrc.retime("[length: 03:00.00]\n[00:01.00]x\n[00:02.00]y", factor=1.1)
    assert head.splitlines()[0] == "[length: 03:18.00]"


def test_tidy_trim_and_straighten():
    t = "[ti: Café ]\n[00:01.00]  “Don’t stop” — now…  \n[00:02.00][00:05.00] <00:02.00>a b​  \n  plain  "
    assert lrc.straighten("‘a’ “b” … – ´") == "'a' \"b\" ... - '"
    trimmed = lrc.trim_lines(t)
    assert trimmed.splitlines()[1] == "[00:01.00]“Don’t stop” — now…"
    assert trimmed.splitlines()[0] == "[ti: Café ]"                # header untouched by trim
    both = lrc.tidy(t, trim=True, quotes=True)
    assert both.splitlines()[1] == '[00:01.00]"Don\'t stop" - now...'
    assert both.splitlines()[2] == "[00:02.00][00:05.00]<00:02.00>a b"
    assert both.splitlines()[3] == "plain"
    assert lrc.tidy(t) == t and lrc.tidy(None, True, True) is None
    assert lrc.tidy(both, True, True) == both                       # idempotent


def test_fit_score():
    import numpy as np

    from mass_synced_lyrics.fit import fit_score
    text = "\n".join(f"[{m:02d}:{s:02d}.00]line {i}" for i, (m, s) in
                     enumerate([(0, 10), (0, 40), (1, 10), (1, 40), (2, 10), (2, 40), (3, 0)]))
    assert fit_score(text, 200.0, 200.0) == (100.0, [])
    f, why = fit_score(text, 200.0, 205.0)                       # timed for a 5 s longer version
    assert f == 73.0 and "longer" in why[0]
    f, why = fit_score(text, 150.0)                              # last line after the end
    assert f <= 50 and "after the song ends" in why[0]
    f, _ = fit_score(text, 600.0)                                # lyrics only in the first third
    assert f == 85.0
    assert fit_score("plain words", 200.0) == (None, [])
    assert fit_score(text, None) == (None, [])
    # audio: loud from 0-120 s, then silence; lines at 130+ s start on silence and run into the end silence
    peaks = np.zeros(200 * 100, dtype=np.float32)
    peaks[: 120 * 100] = 0.8
    f, why = fit_score(text, 200.0, 200.0, peaks)
    assert f < 70 and any("silence" in w for w in why)
    peaks[:] = 0.8
    assert fit_score(text, 200.0, 200.0, peaks)[0] == 100.0
