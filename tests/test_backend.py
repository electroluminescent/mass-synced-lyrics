"""The desktop app's backend (what the Electron window drives): Find, the song list, the player's
timing logic, Align, Apply and the session. No window needed."""
import json
import os
import re
import sys
import threading
import time

import pytest
from conftest import LRC, LRC_B, FakeLrclib, make_track, new_backend, record, write_lrc

from mass_synced_lyrics import ai_align, lrc
from mass_synced_lyrics.engine import Engine
from mass_synced_lyrics.paths import state_key
from mass_synced_lyrics.review_session import EDIT_A, EDIT_B, EDIT_USER, heard_color
from mass_synced_lyrics.tags import TagFile

THREE = "[00:00.50]Other first\n[00:05.00]Other second\n[00:07.00]Third line here"
FAKE = os.path.join(os.path.dirname(__file__), "fake_aligner.py")


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))


def _fake_gpu(monkeypatch, installed=True):
    monkeypatch.setattr(ai_align, "nvidia_gpu", lambda: "NVIDIA test GPU")
    monkeypatch.setattr(ai_align, "is_installed", lambda: installed)
    monkeypatch.setenv("MSL_ALIGNER_CMD", json.dumps([sys.executable, FAKE]))


def _run_find(b):
    out = b.call("find.start")
    assert out.get("started"), out
    assert b.pump(lambda: not (b.app.worker and b.app.worker.is_alive()) and not b.app.find_state["running"], 30)
    b.settle(0.1)


def _apply(b, paths=None):
    plan = b.call("apply.plan", paths=paths)
    assert plan["n"], plan
    b.call("apply.start", paths=paths)
    assert b.pump(lambda: b.app.apply_worker is None, 30)
    b.settle(0.1)


def _rows_view(b):
    """The rows as the window has them (from the batched `rows` events)."""
    b.settle(0.08)
    rows = {}
    for d in b.events("rows"):
        if d.get("clear"):
            rows = {}
        rows.update(d.get("upsert") or {})
        for p in d.get("remove") or []:
            rows.pop(p, None)
    return rows


def _open(settings, n_songs=1, lyr=THREE):
    paths = [make_track(os.path.join(settings.music_folder, f"{chr(97 + i)}.flac"), "flac") for i in range(n_songs)]
    settings.dry_run = True
    Engine(settings, lambda e: None, lrclib=FakeLrclib([record(synced=lyr)])).run()
    settings.dry_run = False
    b = new_backend(settings)
    keys = [state_key(p) for p in paths]
    for i, k in enumerate(keys):
        b.app.rows.add_row(k, "proposed", "lrclib", "Some Artist", f"Some Song {i}", "", "found", "online")
    b.open_song(keys[0])
    return b, b.rv, keys


def test_find_runs_a_job_then_apply_writes(settings):
    d = settings.music_folder
    make_track(os.path.join(d, "a.flac"), "flac")
    write_lrc(os.path.join(d, "a.lrc"))
    settings.use_online = False
    settings.lrc_action = "keep"
    b = new_backend(settings)
    init = b.call("app.init")
    assert init["version"] and init["settings"]["music_folder"] == d and init["session"] is None
    _run_find(b)
    assert "Finished" in b.app.find_state["status"], b.app.find_state
    assert b.app.last_csv and os.path.exists(b.app.last_csv)
    text = b.log_text()
    assert "Lyrics found" in text and "Nothing has been written yet" in text
    p = os.path.join(d, "a.flac")
    assert TagFile(p).read().lyrics == []
    bar = b.last("applybar")
    assert bar["shown"] and bar["n"] == 1 and bar["target"] == "embed into"
    assert b.last("select") == {"tab": "review", "first": True}
    lines = [x for x in text.splitlines() if x.strip()]
    assert all(re.match(r"^\d\d:\d\d:\d\d  ", x) for x in lines)            # log lines carry the time
    _apply(b)
    assert TagFile(p).read().lyrics and "Applied: 1 written" in b.log_text()
    assert _rows_view(b)[state_key(p)]["status"] == "embedded"
    b.close()


def test_player_checkpoints_line_to_playhead_and_use_edited(settings):
    d = settings.music_folder
    p = make_track(os.path.join(d, "Some Song.flac"), "flac", lyrics=LRC_B)
    write_lrc(os.path.join(d, "Some Song.lrc"), LRC)
    b = new_backend(settings)
    b.app.rows.add_row(p, "has_synced", "", "Some Artist", "Some Song")
    rv = b.open_song(state_key(p))
    assert rv.cand.key.startswith("sidecar")
    assert rv.checkpoints == [1.0, 8.2]                      # first and last lyric
    v = b.last("review")
    assert [len(v["lanes"]["cur"]), len(v["lanes"]["new"])] == [2, 3]
    audio = b.last("review_audio")
    assert audio["duration"] > 0 and audio["peaks"] and audio["path"].endswith("Some Song.flac")
    # checkpoints: numbered by creation; deleting keeps the others' numbers
    assert b.rv_call("add_checkpoint", 3.0) == 2
    assert rv.checkpoints == [1.0, 8.2, 3.0] and rv.cp_custom
    b.rv_call("delete_checkpoint", 1)
    assert rv.checkpoints == [1.0, None, 3.0]
    b.rv_call("add_checkpoint", 6.0, 4)                      # an unset key: set in its own slot
    assert rv.checkpoints == [1.0, None, 3.0, None, 6.0]
    b.rv_call("delete_checkpoint", 4)
    assert rv.checkpoints == [1.0, None, 3.0]
    b.rv_call("default_checkpoints")
    assert rv.checkpoints == [1.0, 8.2] and not rv.cp_custom
    # Line to playhead: line 2 (orig 4.5 s) is sung at 5.0 s -> +0.5 s offset
    assert b.rv_call("align_line", 1, 5.0) == ""
    assert rv.offset == pytest.approx(0.5, abs=0.01)
    assert lrc.display_lines(rv.new_text)[1][0] == pytest.approx(5.0, abs=0.01)
    assert b.last("review")["offset_ms"] == pytest.approx(500, abs=10)
    # Use edited: recorded only; Apply writes it
    b.rv_call("use_level", "edited")
    assert b.pump(lambda: rv.song.override is not None)
    assert _rows_view(b)[state_key(p)]["status"] == "chosen"
    assert lrc.same_lyrics(TagFile(p).read().lyrics[0], LRC_B)
    assert b.last("applybar")["n"] == 1
    _apply(b)
    assert lrc.display_lines(TagFile(p).read().lyrics[0])[1][0] == pytest.approx(5.0, abs=0.01)
    assert b.app.rows.rows[state_key(p)]["status"] == "embedded"
    b.close()


def test_choices_cycle_pins_interpolation_and_typed_times(settings):
    d = settings.music_folder
    a = make_track(os.path.join(d, "Band", "Album A", "01 Some Song.flac"), "flac")
    write_lrc(os.path.join(d, "Band", "Album A", "01 Some Song.lrc"), LRC)
    bb = make_track(os.path.join(d, "Band", "Album A", "02 Other.flac"), "flac", title="Other")
    c = make_track(os.path.join(d, "Band", "Album B", "01 Third.flac"), "flac", title="Third", album="Album B")
    settings.dry_run = True
    b = new_backend(settings)
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib([record(), record(title="Other")])).run()
    for e in events:
        b.app._handle(e)
    b.settle(0.1)
    ka, kb, kc = state_key(a), state_key(bb), state_key(c)
    rows = b.app.rows.rows
    assert (rows[ka]["local"], rows[ka]["online"], rows[ka]["choice"]) == ("sidecar", "found", "local")
    assert (rows[kb]["local"], rows[kb]["online"], rows[kb]["choice"]) == ("", "found", "online")
    assert (rows[kc]["online"], rows[kc]["choice"]) == ("not found", "keep")
    # Choice cycles local -> online -> keep -> local
    assert b.call("rows.cycle", path=ka) == "online"
    assert b.call("rows.cycle", path=ka) == "keep"
    assert b.call("rows.cycle", path=ka) == "local"
    # a group (or a multi-selection): everything at once
    assert b.call("rows.cycle_many", songs=[ka, kb], node="grp:x") == "local"
    assert rows[ka]["choice"] == "local" and rows[kb]["choice"] == "online"          # b has no local
    assert b.call("rows.cycle_many", songs=[ka, kb], node="grp:x") == "online"
    assert b.call("rows.cycle_many", songs=[ka, kb], node="grp:x") == "keep"
    assert rows[ka]["choice"] == rows[kb]["choice"] == "keep"
    assert b.last("applybar")["n"] == 0
    assert b.call("rows.cycle_many", songs=[ka, kb], node="grp:x") == "local"
    # the player: Use found is the choice; nothing aligned
    rv = b.open_song(ka)
    idx = next(i for i, cd in enumerate(rv.cands) if cd.text and lrc.same_lyrics(cd.text, LRC))
    b.rv_call("select_candidate", idx)
    v = b.rv_call("view") if False else rv.view()
    assert v["choice"]["buttons"]["found"]["on"] and not v["choice"]["buttons"]["aligned"]["avail"]
    b.rv_call("lock")
    assert b.pump(lambda: rv.view()["choice"]["buttons"]["keep"]["on"])
    assert rows[ka]["choice"] == "keep"
    # pins, edited times, interpolation, clear, stretch to length
    assert [t for t, _ in rv.new_lines] == [1.0, 4.5, 8.2]
    b.rv_call("toggle_pin", 0)
    b.rv_call("set_line_time", 2, 9.0)
    assert b.rv_call("toggle_interpolate") == ""
    times = [t for t, _ in rv.new_lines]
    assert times[0] == pytest.approx(1.0) and times[2] == pytest.approx(9.0)
    assert times[1] == pytest.approx(1.0 + (4.5 - 1.0) * (8.0 / 7.2), abs=0.01)
    rows_v = rv.view()["new_lines"]
    assert rows_v[0][2] == "pinned" and rows_v[2][3] == "edited" and rows_v[0][3] == "ts_pinned"
    assert rows_v[1][3] == "moved" and rv.view()["interp"]["on"]
    b.rv_call("clear_edits")
    assert not rv.edited and rv.interpolate
    b.rv_call("clear_pins")
    assert not rv.pins and not rv.interpolate
    assert [t for t, _ in rv.new_lines] == [1.0, 4.5, 8.2]
    assert "Pin or edit" in b.rv_call("toggle_interpolate")
    rv.cand.src_length = 5.0
    assert b.rv_call("stretch_to_length") == {}
    assert rv.factor == pytest.approx(rv._duration() / 5.0)
    rv.cand.src_length = 0.0
    assert "ask" in b.rv_call("stretch_to_length")
    b.rv_call("stretch_to_length", "0:05")
    assert rv.factor == pytest.approx(rv._duration() / 5.0) and "stretched" in rv.anchors_note
    b.rv_call("transfer_all", "new")
    # typed timestamps: a time sets it, empty resets, nonsense says so
    assert b.rv_call("set_line_time_text", 1, "0:05.25") == ""
    assert [t for t, _ in rv.new_lines][1] == pytest.approx(5.25)
    assert "isn't a time" in b.rv_call("set_line_time_text", 1, "soon")
    assert b.rv_call("set_line_time_text", 1, "") == ""
    assert not rv.pins and [t for t, _ in rv.new_lines][1] == pytest.approx(4.5)
    # Offset / Stretch typed in the player
    b.rv_call("set_timing", 250, 100)
    assert [t for t, _ in rv.new_lines][0] == pytest.approx(1.25)
    b.close()


def test_session_multiselect_save_now_and_restore(settings, monkeypatch):
    d = settings.music_folder
    a = make_track(os.path.join(d, "a.flac"), "flac", lyrics="old a")
    bb = make_track(os.path.join(d, "b.flac"), "flac", title="Other")
    c = make_track(os.path.join(d, "c.flac"), "flac", title="Third")
    settings.dry_run = True
    b = new_backend(settings)
    events = []
    Engine(settings, events.append,
           lrclib=FakeLrclib([record(), record(title="Other"), record(title="Third")])).run()
    for e in events:
        b.app._handle(e)
    settings.dry_run = False
    ka, kb, kc = state_key(a), state_key(bb), state_key(c)
    rows = b.app.rows.rows
    b.call("rows.cycle_many", songs=[ka, kb], node="sel")
    assert rows[ka]["choice"] == rows[kb]["choice"] == "keep" and rows[kc]["choice"] == "online"
    assert b.call("rows.set_choice", songs=[ka, kb], kind="online") == 2
    b.call("rows.reset", songs=[kb])
    assert rows[kb]["choice"] == "online"
    _apply(b, [ka])                                         # Save now on a subset writes only those
    assert TagFile(a).read().lyrics != ["old a"] and TagFile(bb).read().lyrics == []
    assert rows[ka]["status"] == "embedded"
    b.call("session.ui", selected=kb, filter="All", search="", only_found=True)
    b.close()                                               # session saved on close
    saved_lyrics = TagFile(a).read().lyrics
    b2 = new_backend(settings)
    init = b2.call("app.init")
    assert init["session"]["n"] == 3 and init["loaded"] == 0          # "ask": offered
    out = b2.call("session.load", load=True)                # saved songs left out by default
    assert out["loaded"] == 2 and out["ui"]["selected"] == kb
    assert set(b2.app.rows.rows) == {kb, kc} and b2.app.rows.rows[kc]["choice"] == "online"
    b2.app.rows.clear()
    b2.call("session.load", load=True, include_saved=True)
    assert set(b2.app.rows.rows) == {ka, kb, kc} and b2.app.rows.rows[ka]["status"] == "embedded"
    assert TagFile(a).read().lyrics == saved_lyrics         # the file was NOT put back
    b2.call("restore.start", paths=[ka])                    # restoring is only the explicit action
    assert b2.pump(lambda: b2.app.apply_worker is None)
    b2.settle(0.1)
    assert TagFile(a).read().lyrics == ["old a"] and b2.app.rows.rows[ka]["status"] == "restored"
    b2.call("session.load", load=False, remember=True)      # "Start fresh" + remember: no question next time
    assert b2.app.settings.session_restore == "fresh"
    b2.call("settings.set", patch={"session_restore": "load"})
    b2.close()
    b3 = new_backend(None)
    init = b3.call("app.init")
    assert init["session"] is None and init["loaded"] == 3   # remembered: loaded without asking
    b3.close()


def test_first_result_switches_to_review_and_saving_while_finding(settings):
    d = settings.music_folder
    a = make_track(os.path.join(d, "a.flac"), "flac")
    make_track(os.path.join(d, "b.flac"), "flac", title="Other")
    settings.dry_run = True
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib([record(), record(title="Other")])).run()
    settings.dry_run = False
    b = new_backend(settings)
    b.app._switched_to_review = False
    busy = threading.Event()
    b.app.worker = threading.Thread(target=busy.wait, daemon=True)      # pretend Find is still running
    b.app.worker.start()
    results = [e for e in events if e["type"] in ("scan", "result")]
    b.app._handle(results[0])
    b.app._handle(results[1])
    b.settle(0.1)
    first = state_key(results[1]["result"].path)
    sel = b.last("select")
    assert sel["tab"] == "review" and sel["song"] == first
    b.app.refresh_apply_bar()
    assert b.last("applybar")["n"] >= 1 and b.last("applybar")["finding"]
    _apply(b, [first])
    assert TagFile(a if first == state_key(a) else os.path.join(d, "b.flac")).read().lyrics
    assert b.call("app.busy")["finding"]
    busy.set()
    b.app.worker.join(1)
    b.close()


def test_apply_confident_and_score_levels(settings):
    from mass_synced_lyrics.config import load_settings, snap5

    assert [snap5(x) for x in (0, 2, 3, 82, 87.5, 99, 140)] == [0, 0, 5, 80, 90, 100, 100]
    d = settings.music_folder
    exact = make_track(os.path.join(d, "a.flac"), "flac")
    loose = make_track(os.path.join(d, "b.flac"), "flac", title="Other Tune")
    make_track(os.path.join(d, "c.flac"), "flac", title="Missing")
    settings.dry_run = True
    b = new_backend(settings)
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib([record(), record(title="Other Tune (Live)")])).run()
    for e in events:
        b.app._handle(e)
    settings.dry_run = False
    ke = state_key(exact)
    r = b.app.rows.rows[ke]
    assert (r["conf_online"], r["fit_online"]) == (100, 100)
    b.app.refresh_apply_bar()
    assert b.last("applybar")["nc"] == 1
    plan = b.call("apply.confident_plan")
    assert plan["paths"] == [ke] and "question" not in plan                 # no question to ask
    b.call("apply.start", paths=plan["paths"])
    assert b.pump(lambda: b.app.apply_worker is None)
    assert TagFile(exact).read().lyrics and not TagFile(loose).read().lyrics
    # score levels typed on the Find tab (or set from the list header) snap to 5 and are saved
    out = b.call("settings.set", patch={"confident_threshold": 92, "confident_fit": 73, "list_min_fit": 77})
    assert (out["confident_threshold"], out["confident_fit"], out["list_min_fit"]) == (90, 75, 75)
    assert load_settings().confident_threshold == 90 and b.last("settings")["confident_fit"] == 75
    b.close()


def test_align_toggle_needs_nvidia_and_aligned_songs_update_the_list(settings, monkeypatch):
    b = new_backend(settings)
    assert not b.app.settings.ai_align
    monkeypatch.setattr(ai_align, "nvidia_gpu", lambda: None)
    out = b.call("ai.toggle", on=True)
    assert "NVIDIA" in out["error"] and not b.app.settings.ai_align
    monkeypatch.setattr(ai_align, "nvidia_gpu", lambda: "NVIDIA GeForce RTX 3070, 8192 MiB")
    monkeypatch.setattr(ai_align, "is_installed", lambda: False)
    out = b.call("ai.toggle", on=True)
    assert out["need_install"] and "3 GB" in out["question"] and not b.app.settings.ai_align
    _fake_gpu(monkeypatch)
    assert b.call("ai.toggle", on=True) == {"ok": True} and b.app.settings.ai_align
    assert b.last("settings")["ai_align"]
    d = settings.music_folder
    loose = make_track(os.path.join(d, "b.flac"), "flac", title="Other Tune")
    settings.dry_run = True
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib([record(title="Other Tune (Live)", synced=THREE,
                                                                duration=12)])).run()
    settings.dry_run = False
    for e in events:
        b.app._handle(e)                  # the low-confidence song is queued for the GPU while Find runs
    assert b.app.align_service is not None
    k = state_key(loose)
    assert b.pump(lambda: b.app.rows.rows[k]["status"] == "aligned" and b.app.align_service.idle(), 30), \
        b.log_text()[-1500:]
    assert b.app.rows.rows[k]["conf_online"] == 100 and b.app.rows.rows[k]["level"] == "aligned"
    assert "Aligned (100% of words heard)" in b.log_text()
    b.close()


def test_align_error_choices(settings):
    b = new_backend(settings)
    b.app.settings.ai_align = True

    class Svc:
        def __init__(self):
            self.calls = []
            self.done = 0
            self.paused = True

        def pending(self):
            return 2

        def resume(self):
            self.calls.append("resume")

        def stop(self):
            self.calls.append("stop")

        def idle(self):
            return True

    for choice in ("ignore", "disable", "cancel"):
        svc = b.app.align_service = Svc()
        busy = threading.Event()
        b.app.worker = threading.Thread(target=busy.wait, daemon=True)     # a Find run in progress
        b.app.worker.start()
        b.app.cancel = threading.Event()
        b.app._handle({"type": "align_error", "error": "CUDA error: out of memory", "path": "x", "queued": 2})
        assert b.last("align_error") == {"error": "CUDA error: out of memory", "waiting": 2}
        b.settle(1.1)
        assert b.last("align_waiting") == {"waiting": 2}
        b.call("align.error_choice", what=choice)
        if choice == "ignore":
            assert svc.calls == ["resume"] and b.app.settings.ai_align and not b.app.cancel.is_set()
        elif choice == "disable":
            assert svc.calls == ["stop"] and not b.app.settings.ai_align and not b.app.cancel.is_set()
            b.app.settings.ai_align = True
        else:
            assert "stop" in svc.calls and b.app.cancel.is_set() and b.app.settings.ai_align
        busy.set()
        b.app.worker.join(1)
    assert "Align: paused — CUDA error: out of memory" in b.log_text()
    b.close()


def test_player_auto_align_words_ripple_and_not_applied(settings, monkeypatch):
    _fake_gpu(monkeypatch)
    settings.ai_align = True
    b, rv, keys = _open(settings)
    k = keys[0]
    assert [t for t, _ in rv.new_lines] == [0.5, 5.0, 7.0] and rv.edited_segs() == []
    ver = rv._lanes_ver
    b.rv_call("set_line_time", 1, 5.5)                       # an edit redraws the lanes at once
    assert rv._lanes_ver > ver and b.last("review")["lanes"]["edit"][1][0] == 5.5
    b.rv_call("set_line_time", 1, None)
    # Auto-align (GPU): fake worker hears line i at 2 + 3i with 90% certainty
    assert b.rv_call("auto_align") == ""
    assert b.last("review")["ai_busy"]
    assert b.pump(lambda: rv._align_req is None, 30)
    b.settle()
    assert [t for t, _ in rv.new_lines] == [2.0, 5.0, 8.0]
    assert rv.ai_keys == set(rv.pins) and len(rv.ai_keys) == 3
    v = b.last("review")
    assert not v["ai_busy"] and "Aligned: 100% of words heard" in v["ai_note"]
    assert any(row[3] == "ai_edited" for row in v["new_lines"]) and v["has_aligned"]
    assert not lrc.WORD_TS_RE.search(rv.new_text)
    # lines and words: every word timed, and they move with their line
    b.rv_call("auto_align", True)
    assert b.pump(lambda: rv._align_req is None, 30)
    assert "word-synced" in rv.ai_note
    assert "[00:02.00]<00:02.00>Other <00:02.75>first" in rv.new_text
    b.rv_call("set_line_time", 0, 2.5)
    assert "[00:02.50]<00:02.50>Other <00:03.25>first" in rv.new_text
    b.rv_call("set_line_time", 0, None)
    b.rv_call("auto_align", False)
    assert b.pump(lambda: rv._align_req is None, 30)
    assert not lrc.WORD_TS_RE.search(rv.new_text)
    # the Edited lane is shaded by how surely each line was heard; it stays after coming back
    assert rv.has_edits() and rv.edited_segs() and rv.ai_conf
    assert any(bar[5] == heard_color(0.9) for bar in b.last("review")["lanes"]["aligned"])
    rv._save_prefs()
    rv.pins, rv.ai_keys, rv.ai_conf = {}, set(), {}
    b.open_song(k)
    assert rv.edited_segs() and rv.ai_conf
    # an edit by hand takes over that line; the aligner's other lines stay purple
    b.rv_call("set_line_time", 2, 9.0)
    assert lrc.pin_key(7.0) not in rv.ai_keys and len(rv.ai_keys) == 2
    # ripple: one edit moves the lines around it (clamped to the song)
    b.rv_call("clear_edits")
    assert not rv.pins and not rv.ai_keys
    b.call("settings.set", patch={"ripple": True})
    assert b.app.settings.ripple and rv.warp_mode == "ripple"
    b.rv_call("set_line_time", 1, 6.0)
    times = [t for t, _ in rv.new_lines]
    assert times[0] == pytest.approx(1.5) and times[1] == pytest.approx(6.0)
    assert times[2] <= rv._duration() + 1e-6
    assert any(row[3] == "moved" for row in rv.view()["new_lines"])
    assert rv.view()["interp"]["text"].startswith("✓ Ripple")
    b.call("settings.set", patch={"ripple": False})
    assert [t for t, _ in rv.new_lines] == [0.5, 6.0, 7.0]
    # below the minimum: timings unchanged
    b.rv_call("clear_edits")
    monkeypatch.setenv("FAKE_PROB", "0.3")
    b.app.align_service.stop()
    b.app.align_service.join(10)
    b.app.align_service = None
    b.app._release_held_aligner()
    b.app.settings.ignore_cache = True
    b.rv_call("auto_align")
    assert b.pump(lambda: rv._align_req is None, 30)
    assert not rv.pins and "Not applied" in rv.ai_note and not rv.has_edits()
    b.close()


def test_song_aligned_while_finding_opens_with_its_edits_visible(settings):
    from mass_synced_lyrics.state import State

    d = settings.music_folder
    p = make_track(os.path.join(d, "b.flac"), "flac", title="Other Tune")
    settings.dry_run = True
    Engine(settings, lambda e: None, lrclib=FakeLrclib([record(title="Other Tune (Live)", synced=THREE,
                                                                duration=12)])).run()
    al = ai_align.Aligner(command=[sys.executable, FAKE])
    ai_align.run(settings, [p], lambda e: None, aligner=al)
    al.close()
    st = State(settings.resolved_state_db())
    pending = st.get_pending(p)[2]
    st.close()
    settings.dry_run = False
    b = new_backend(settings)
    k = state_key(p)
    b.app.rows.add_row(k, "aligned", "lrclib", "Some Artist", "Other Tune", "", "found", "online")
    rv = b.open_song(k)
    assert getattr(rv.cand, "ai_detail", None) is not None
    assert lrc.same_lyrics(rv.new_text, pending)
    btn = rv.view()["choice"]["buttons"]["aligned"]
    assert rv.ai_keys and rv.edited_segs() and btn["on"] and "(default)" in btn["label"]
    assert rv.aligned_text and rv.lane_bars["aligned"] and rv.aligned_verify
    b.rv_call("clear_edits")
    assert [t for t, _ in rv.new_lines] == [0.5, 5.0, 7.0]
    b.close()


def test_activity_rows_install_blocks_find_and_align_low(settings, monkeypatch):
    d = settings.music_folder
    make_track(os.path.join(d, "a.flac"), "flac")
    write_lrc(os.path.join(d, "a.lrc"))
    settings.use_online = False
    settings.lrc_action = "keep"
    b = new_backend(settings)
    b.call("find.start")
    assert b.app.activity.is_active("find")
    assert b.pump(lambda: not b.app.find_state["running"], 30)
    b.settle(0.1)
    assert not b.app.activity.is_active("find")
    assert b.last("activity") == {"rows": []}
    monkeypatch.setattr(ai_align, "nvidia_gpu", lambda: "NVIDIA test GPU")
    monkeypatch.setattr(ai_align, "is_installed", lambda: False)
    gate = threading.Event()

    def fake_install(log, cancel, progress):
        progress(0.25, "Installing PyTorch with CUDA (about 2.5 GB)", "torch: 640.0 MB of 2.5 GB")
        while not cancel.is_set() and not gate.is_set():
            cancel.wait(0.05)
        return False
    monkeypatch.setattr(ai_align, "install", fake_install)
    assert b.call("ai.toggle", on=True)["need_install"]
    b.call("ai.install")
    assert b.pump(lambda: b.app.activity.value("install") == 25.0)
    b.settle(0.05)
    row = b.last("activity")["rows"][0]
    assert row["key"] == "install" and "640.0 MB" in row["detail"] and row["stop"] == "install"
    assert "still installing" in b.call("find.start")["info"]
    b.call("activity.stop", key="install")
    assert b.pump(lambda: not b.app.activity.is_active("install"))
    assert not b.app.settings.ai_align
    monkeypatch.setattr(ai_align, "is_installed", lambda: True)
    b.app.settings.ai_align = True
    assert "No songs in the list" in b.call("align.command", which="low")["info"]
    b.close()


def test_list_progress_columns_remove_and_new_find_clears(settings, monkeypatch):
    from mass_synced_lyrics.engine import Result
    from mass_synced_lyrics.tags import Meta

    b = new_backend(settings)
    d = settings.music_folder
    a = state_key(os.path.join(d, "Band", "Album", "01.flac"))
    bb = state_key(os.path.join(d, "Band", "Album", "02.flac"))
    b.app._handle({"type": "file_start", "path": a})
    rows = b.app.rows.rows
    assert rows[a]["op_find"] == "busy" and rows[a]["status"] == "searching"
    r = Result(a, "proposed", "lrclib", Meta(title="One", artist="Band", album="Album"), 10.0,
               online="found", choice="online", conf_online=97.0, fit_online=90.0)
    b.app._handle({"type": "result", "result": r, "done": 1, "total": 2, "counts": {"proposed": 1}, "elapsed": 1})
    b.app._handle({"type": "file_start", "path": bb})
    assert rows[a]["op_find"] == "done"
    b.app._handle({"type": "apply_file_start", "path": a})
    assert rows[a]["op_save"] == "busy"
    b.app._handle({"type": "apply_progress", "path": a, "done": 1, "total": 1, "status": "embedded",
                   "source": "lrclib", "detail": "", "counts": {}})
    assert rows[a]["op_save"] == "done"
    b.app._handle({"type": "align_song_start", "path": a})
    assert rows[a]["op_align"] == "busy"
    b.app.rows.set_align_pct(a, 37)
    assert rows[a]["align_pct"] == 37
    b.app._handle({"type": "align_song_start", "path": state_key(os.path.join(d, "x.flac"))})
    assert state_key(os.path.join(d, "x.flac")) not in rows               # only Find adds songs
    b.app._handle({"type": "done", "counts": {}, "csv": "x.csv", "elapsed": 1, "cancelled": True})
    assert rows[bb]["op_find"] == ""                       # the end of a run clears what was in progress
    view = _rows_view(b)
    assert view[a]["op_save"] == "done" and view[bb]["op_find"] == ""
    assert b.call("rows.remove", songs=[a, bb]) == 2 and not rows
    assert _rows_view(b) == {}
    # a new Find run starts with an empty list
    b.app.rows.add_row(a, "proposed", "lrclib", "Band", "One", "", "found", "online", "Album")
    monkeypatch.setattr(b.app, "_run_engine", lambda s: None)
    b.call("find.start")
    assert not rows and _rows_view(b) == {}
    b.close()


def test_player_resize_end_marks_and_edited_lane(settings):
    b, rv, keys = _open(settings)
    assert not rv.has_edits() and rv.edited_segs() == []
    assert (0, "end") not in [(r, e) for r, e, _ in rv.edge_handles()]          # no gap after it yet
    b.rv_call("resize_begin", 0, "end")
    b.rv_call("resize", 3.0)
    b.rv_call("resize_end")
    assert [round(t, 2) for t, _ in rv.new_lines] == [0.5, 3.0, 5.0, 7.0] and rv.new_lines[1][1] == ""
    assert rv.has_edits() and rv.edited_segs()
    fills = [bar[5] for bar in rv.view()["lanes"]["edit"]]
    assert set(fills) <= {EDIT_A, EDIT_B, EDIT_USER} and heard_color(0.9) not in fills and not rv.ai_keys
    assert (0, "end") in [(r, e) for r, e, _ in rv.edge_handles()]
    b.rv_call("set_line_time", 1, 4.0)                       # the gap row can be moved like a lyric
    assert [round(t, 2) for t, _ in rv.new_lines][:3] == [0.5, 4.0, 5.0]
    b.rv_call("resize_begin", 2, "end")                      # into the third: it ripples
    b.rv_call("resize_end", 7.5)
    assert [round(t, 2) for t, _ in rv.new_lines][-1] == 7.5
    b.rv_call("clear_edits")
    assert [t for t, _ in rv.new_lines] == [0.5, 5.0, 7.0] and not rv.end_marks
    b.close()


def test_align_row_finishes_when_find_ends_after_the_aligner_went_idle(settings):
    b = new_backend(settings)

    class Svc:
        paused, done, total = False, 2, 2
        counts = {"aligned": 2}

        def idle(self):
            return True

        def pending(self):
            return 0

        def reset_counts(self):
            self.done = 0

    b.app.align_service = Svc()
    b.app.activity.set("align", "Aligning on the GPU", detail="2 done · waiting for songs from Find…")
    b.app._align_finish_if_idle()
    assert not b.app.activity.is_active("align") and b.app.align_service.done == 0
    assert "2 aligned" in b.log_text()
    b.app.align_service = None
    b.close()


def test_auto_align_belongs_to_its_song_progress_and_align_command(settings, monkeypatch):
    b, rv, keys = _open(settings)
    k = keys[0]
    assert not rv.view()["ai_busy"]
    rv._align_req = (rv.song.path, "x", 1)                    # waiting for this song
    rv.auto_align_progress({"frac": 0.42, "label": "Isolating the vocals", "stage_frac": 0.5, "left": 75}, False)
    v = rv.view()
    assert v["ai_busy"] and v["ai_pct"] == 42 and "Isolating the vocals (50%)" in v["ai_note"]
    assert "1:15 left" in v["ai_note"]
    b.open_song(k)                                            # reloading keeps the request
    assert rv._align_req is not None and rv.view()["ai_busy"]
    rv._align_req = ("some other song.flac", "x", 2)          # another song is being aligned
    assert not rv.view()["ai_busy"]
    b.rv_call("cancel_auto_align")
    assert rv._align_req is None and "cancelled" in rv.ai_note
    # right-click → Align on the GPU queues the songs whatever their scores, lines or words
    queued = []
    monkeypatch.setattr(b.app, "queue_alignment",
                        lambda songs, force=False, words=None: queued.append((songs, force, words)) or 1)
    assert "Switch on" in b.call("align.command", which="songs", songs=[k])["info"]
    b.app.settings.ai_align = True
    b.call("align.command", which="songs", songs=[k])
    b.call("align.command", which="songs", songs=[k], words=True)
    b.call("align.command", which="all")
    assert queued == [([k], True, None), ([k], True, True), ([k], True, None)]
    # the setting: Synced lyrics to the line / to the word (the window warns first)
    b.call("settings.set", patch={"sync_words": True, "word_sync_warned": True})
    from mass_synced_lyrics.config import load_settings
    assert load_settings().sync_words and load_settings().word_sync_warned
    assert "to the word" in b.log_text()
    b.close()


def test_batch_alignment_shows_on_a_song_opened_before_and_cache_can_be_cleared(settings):
    p = make_track(os.path.join(settings.music_folder, "b.flac"), "flac", title="Other Tune")
    settings.dry_run = True
    Engine(settings, lambda e: None, lrclib=FakeLrclib([record(title="Other Tune (Live)", synced=THREE,
                                                                duration=12)])).run()
    settings.dry_run = False
    b = new_backend(settings)
    k = state_key(p)
    b.app.rows.add_row(k, "proposed", "lrclib", "Some Artist", "Other Tune", "", "found", "online")
    rv = b.open_song(k)
    b.rv_call("set_line_time", 0, 0.6)                        # opened and touched before: prefs are saved
    rv._save_prefs()
    assert rv.song.prefs.get("pins")
    al = ai_align.Aligner(command=[sys.executable, FAKE])
    ai_align.run(settings, [p], lambda e: None, aligner=al, everything=True)
    al.close()
    b.open_song(k)
    assert rv.ai_keys and rv.edited_segs()                     # the purple GPU edits show
    assert rv.pins[rv._line_key(0)] == pytest.approx(0.6)     # your own time is kept
    out = b.call("cache.clear", songs=[k])
    assert out == {"cleared": 1}
    tok = rv._token
    assert b.pump(lambda: rv._loaded_token == tok and rv.song is not None)
    assert not rv.ai_keys and b.app.rows.rows[k]["op_align"] == ""
    assert "Cleared cached data for 1 of 1" in b.log_text()
    assert not b.app.settings.ignore_cache
    b.close()


def test_timelines_transfer_levels_move_needle_and_lines(settings):
    b, rv, keys = _open(settings)
    v = rv.view()["choice"]
    assert v["buttons"]["found"]["on"] and "(default)" in v["buttons"]["found"]["label"]
    assert not v["buttons"]["aligned"]["avail"]
    rv._set_aligned({"text": "[00:01.20]Other first\n[00:04.00]\n[00:05.40]Other second\n[00:07.30]Third line here",
                     "verify": 88.0, "pins": {"0.50": 1.2, "5.00": 5.4, "7.00": 7.3}, "ends": {"0.50": 4.0},
                     "confs": {"0.50": 0.9, "5.00": 0.6, "7.00": 0.7}})
    rv._recompute()
    assert [bar.key for bar in rv.lane_bars["aligned"]] == ["0.50", "5.00", "7.00"]
    btn = rv.view()["choice"]["buttons"]["aligned"]
    assert "88%" in btn["label"] and btn["avail"]
    # two aligned lyrics dragged down onto Edited
    assert b.rv_call("transfer", "aligned", ["0.50", "7.00"]) == 2
    times = {bar.key: round(bar.start, 2) for bar in rv.lane_bars["edit"]}
    assert times["0.50"] == 1.2 and times["7.00"] == 7.3 and times["5.00"] != 5.4
    assert {"0.50", "7.00"} <= rv.ai_keys and "Brought 2 lyric(s) down" in rv.ai_note
    # the edits make Edited the choice by themselves (after a pause)
    assert b.pump(lambda: rv.current_level()[0] == "edited", 5)
    assert b.pump(lambda: b.app.rows.rows[keys[0]]["level"] == "edited")
    b.rv_call("use_level", "aligned")
    assert b.pump(lambda: rv.current_level()[0] == "aligned")
    assert b.app.rows.rows[keys[0]]["level"] == "aligned"
    b.rv_call("use_level", "found")
    assert b.pump(lambda: rv.current_level()[0] == "found")
    # a level picked right after an edit stands (the pending switch to Edited is dropped)
    b.rv_call("set_line_time", 1, 5.5)
    b.rv_call("use_level", "aligned")
    b.settle(1.5)
    assert rv.current_level()[0] == "aligned"
    b.rv_call("transfer_all", "aligned")
    assert [round(bar.start, 2) for bar in rv.lane_bars["edit"]] == [1.2, 5.4, 7.3]
    b.rv_call("transfer_all", "new")
    assert [round(bar.start, 2) for bar in rv.lane_bars["edit"]] == [0.5, 5.0, 7.0] and not rv.has_edits()
    # stretch handles: a start on every lyric, an end only before a gap (or the song's end)
    handles = [(r, e) for r, e, _ in rv.edge_handles()]
    assert (1, "start") in handles and (0, "end") not in handles and (2, "end") in handles
    # the target button puts the next lyric at the needle
    b.rv_call("next_line_to_needle", 3.0)
    assert [round(t, 2) for t, bb in rv.new_lines if bb][1] == 3.0 and "now starts at the needle" in rv.ai_note
    b.rv_call("transfer_all", "new")
    # move a lyric in Edited (start and end); the attached one after it is squeezed
    b.rv_call("move_begin", ["5.00"])
    b.rv_call("move", 0.5)
    b.rv_call("move_end", 1.0)
    starts = [round(bar.start, 1) for bar in rv.lane_bars["edit"]]
    assert starts[0] == 0.5 and abs(starts[1] - 6.0) < 0.15 and 7.0 < starts[2] < 10.0
    b.rv_call("transfer_all", "new")
    # insert and delete lines (Edited only; Found keeps them)
    b.rv_call("insert_line", 0, True, "A new line")
    assert [bb for _, bb in rv.new_lines if bb] == ["Other first", "A new line", "Other second", "Third line here"]
    assert len(rv.lane_bars["new"]) == 3
    b.rv_call("delete_line", 0)
    assert [bb for _, bb in rv.new_lines if bb][0] == "A new line"
    rv._save_prefs()
    assert "base" in rv.song.prefs
    b.close()


def test_song_list_rows_reach_the_window_in_batches(settings):
    b = new_backend(settings)
    d = settings.music_folder
    for i in range(50):
        b.app.rows.add_row(os.path.join(d, f"{i}.flac"), "proposed", "lrclib", "A", f"S{i}", "", "found", "online")
    b.settle(0.1)
    batches = b.events("rows")
    assert len(batches) <= 2 and len(_rows_view(b)) == 50
    assert b.call("rows.get")["rows"].keys() == b.app.rows.rows.keys()
    n = b.call("rows.load_earlier")
    assert n == 0
    p = b.call("rows.add", path=os.path.join(d, "x.flac"))
    assert p in _rows_view(b)
    b.close()


def test_server_protocol_round_trip(tmp_path, monkeypatch, settings):
    """The real transport: requests in, a response and events out, one JSON object per line."""
    import io

    from mass_synced_lyrics.config import save_settings
    from mass_synced_lyrics.controller import Backend
    from mass_synced_lyrics.server import Server

    save_settings(settings)
    out = io.StringIO()
    srv = Server(out)
    srv.backend = Backend(srv)
    srv.handle_line(json.dumps({"id": 1, "method": "app.init", "params": {}}))
    srv.handle_line(json.dumps({"id": 2, "method": "nope"}))
    srv.handle_line("not json")
    srv.handle_line(json.dumps({"id": 3, "method": "settings.set", "params": {"patch": {"confident_fit": 71}}}))
    end = time.monotonic() + 2
    while time.monotonic() < end and out.getvalue().count('"id":') < 3:
        srv.loop.run_once(timeout=0.02)
    msgs = [json.loads(x) for x in out.getvalue().splitlines()]
    by_id = {m["id"]: m for m in msgs if "id" in m}
    assert by_id[1]["result"]["settings"]["music_folder"] == settings.music_folder
    assert "unknown method" in by_id[2]["error"]
    assert by_id[3]["result"]["confident_fit"] == 70
    assert any(m.get("event") == "settings" for m in msgs)
    srv.backend.shutdown()


def test_word_times_can_be_edited_in_the_edited_timeline(settings):
    b, rv, keys = _open(settings)
    assert not rv.lane_bars["edit"][0].words
    # a line without word times: right-click → Time this line's words (shared out by length)
    assert b.rv_call("time_words", 2) == ""                       # "Third line here"
    bar = rv.lane_bars["edit"][2]
    assert [w for _, _, w in bar.words] == ["Third", "line", "here"] and bar.words[0][0] == 7.0
    assert "<00:07.00>Third" in rv.new_text and rv.has_edits()
    assert rv.view()["word_rows"] == {2: [1, 2]}
    # drag a word's start: kept between its neighbours and inside the line
    b.rv_call("set_word_time", 2, 1, 7.4)
    assert round(rv.lane_bars["edit"][2].words[1][0], 2) == 7.4
    b.rv_call("set_word_time", 2, 1, 50.0)
    w = rv.lane_bars["edit"][2].words
    assert w[1][0] < w[2][0]
    b.rv_call("set_word_time", 2, 0, 9.0)                         # the first word goes with the line
    assert rv.lane_bars["edit"][2].words[0][0] == 7.0
    # words move with their line
    b.rv_call("set_line_time", 2, 8.0)
    assert rv.lane_bars["edit"][2].words[0][0] == 8.0 and rv.lane_bars["edit"][2].words[1][0] > 8.0
    # the next word starts at the needle
    b.rv_call("next_word_to_needle", 8.2)
    assert round(rv.lane_bars["edit"][2].words[1][0], 2) == 8.2 and "now starts at the needle" in rv.ai_note
    b.rv_call("next_word_to_needle", 1.0)
    assert "no word times yet" in rv.ai_note
    # kept with the song, and part of what Use edited writes
    rv._save_prefs()
    assert rv.song.prefs["words"]
    b.open_song(keys[0])
    assert rv.lane_bars["edit"][2].words and "<00:08.20>line" in rv.new_text
    b.rv_call("clear_word_times", 2)
    assert not rv.lane_bars["edit"][2].words and not rv.word_marks
    b.rv_call("time_words", 1)
    b.rv_call("clear_edits")
    assert not rv.word_marks and not lrc.WORD_TS_RE.search(rv.new_text)
    b.close()


def test_isolated_vocals_are_kept_for_the_player(settings, monkeypatch):
    _fake_gpu(monkeypatch)
    monkeypatch.setenv("FAKE_VOCALS", "1")
    settings.ai_align = True
    b, rv, keys = _open(settings)
    assert b.last("review_audio")["vocals"] == ""
    b.rv_call("auto_align")
    assert b.pump(lambda: rv._align_req is None, 30)
    v = ai_align.find_vocals(keys[0])
    assert v.endswith(".ogg") and os.path.dirname(v) == ai_align.vocals_dir()
    assert b.last("review_vocals")["vocals"] == v
    b.open_song(keys[0])
    assert b.last("review_audio")["vocals"] == v
    # a changed file isn't played with old vocals; Clear cached data removes them
    assert ai_align.vocals_base(keys[0]) == os.path.splitext(v)[0]
    b.call("cache.clear", songs=[keys[0]])
    assert not ai_align.find_vocals(keys[0])
    b.close()
