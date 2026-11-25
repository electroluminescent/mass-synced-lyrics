import os

import pytest
from conftest import make_track, new_app, write_lrc

from mass_synced_lyrics import cli
from mass_synced_lyrics.paths import state_key
from mass_synced_lyrics.tags import TagFile


def test_cli_offline_sidecar(settings, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    d = settings.music_folder
    p = make_track(os.path.join(d, "a.flac"), "flac")
    write_lrc(os.path.join(d, "a.lrc"))
    rc = cli.main([d, "--offline", "--lrc-action", "keep", "--log-dir", settings.log_dir,
                   "--state-db", settings.state_db])
    out = capsys.readouterr().out
    assert rc == 0 and "Embedded [local .lrc]" in out and "CSV log:" in out
    assert TagFile(p).read().lyrics


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_gui_runs_a_job(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    from mass_synced_lyrics import gui

    d = settings.music_folder
    make_track(os.path.join(d, "a.flac"), "flac")
    write_lrc(os.path.join(d, "a.lrc"))
    settings.use_online = False
    settings.lrc_action = "keep"
    app = new_app(settings)
    app._start()
    for _ in range(200):
        app.update()
        if app.worker and not app.worker.is_alive() and app.events.empty():
            app.update()
            break
        app.after(20)
    for _ in range(20):
        app.update()
        app.after(10)
    assert "Finished" in app.v_status.get(), app.v_status.get()
    assert app.last_csv and os.path.exists(app.last_csv)
    text = app.txt.get("1.0", "end")
    assert "Lyrics found" in text and "Nothing has been written yet" in text
    p = os.path.join(d, "a.flac")
    assert TagFile(p).read().lyrics == []
    assert app._apply_bar_shown and "1 file" in app.btn_apply_all.cget("text")
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    app._apply_all()
    for _ in range(300):
        app.update()
        if app.apply_worker is None:
            break
        app.after(10)
    assert TagFile(p).read().lyrics and "Applied: 1 written" in app.txt.get("1.0", "end")
    app.destroy()


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_review_tab_player_checkpoints_timing_apply(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    from conftest import LRC, LRC_B

    from mass_synced_lyrics import gui, lrc

    d = settings.music_folder
    p = make_track(os.path.join(d, "Some Song.flac"), "flac", lyrics=LRC_B)
    write_lrc(os.path.join(d, "Some Song.lrc"), LRC)
    app = new_app(settings)
    app.geometry("1200x860")
    rv = app.review
    app.nb.select(rv)
    rv.add_row(p, "has_synced", "", "Some Artist", "Some Song")
    assert not rv.tree.exists(p)                       # nothing found: hidden by "Lyrics found"
    rv.v_only_found.set(False)
    rv._refill()
    rv.player.silent_reason = "test"

    def pump(cond, n=300):
        for _ in range(n):
            app.update()
            if cond():
                return True
            app.after(10)
        return False

    rv.tree.selection_set(p)
    assert pump(lambda: rv.song is not None and rv.audio is not None)
    assert rv.cand.key.startswith("sidecar")
    assert rv.checkpoints == [1.0, 8.2]                      # first and last lyric
    assert len(rv._ov_lane_items["cur"]) == 2 and len(rv._ov_lane_items["new"]) == 3

    # click on the overview adds a checkpoint; ctrl-click near it removes it
    w = rv.ov.winfo_width()

    class Ev:
        def __init__(self, x):
            self.x, self.y = x, 10
    rv._ov_press(Ev(w * 0.5))                       # plain click = jump, no checkpoint
    rv._ov_release(Ev(w * 0.5))
    assert len(rv.checkpoints) == 2
    assert rv.player.position() == pytest.approx(rv._duration() * 0.5, abs=0.1)
    rv._ov_ctrl_click(Ev(w * 0.5))                  # Ctrl+click = add
    assert len(rv.checkpoints) == 3 and rv.cp_custom
    rv._ov_ctrl_click(Ev(w * 0.5 + 3))              # Ctrl+click on it = remove
    assert len(rv.checkpoints) == 2

    # number key (real key event through the app's binding) plays a checkpoint window then stops,
    # even right after clicking a lyric line or choosing in the (read-only) candidate list
    rv.v_cp_len.set(0.3)
    started = []
    real_play_cp = rv.play_checkpoint
    rv.play_checkpoint = lambda i: (started.append(i), real_play_cp(i))[1]
    rv.cmb_cand.focus_force()
    app.update()
    rv.cmb_cand.event_generate("<KeyPress-1>")
    app.update()
    assert started == [0]                       # (a slow machine may already have finished the 0.6 s window)
    rv.player.pause()
    rv.cp_range = None
    rv.txt_cur.focus_force()
    app.update()
    (app.focus_get() or rv).event_generate("<KeyPress-1>")
    app.update()
    assert started == [0, 0]
    rv.play_checkpoint = real_play_cp
    rv.player.pause()
    rv.cp_range = None
    # typing digits into an editable box must not trigger checkpoints
    rv.player.seek(0)
    rv.tree.focus_force()
    app.update()
    rv.tree.event_generate("<KeyPress-1>")
    app.update()
    assert rv.player.playing and rv.cp_range == (pytest.approx(0.7), pytest.approx(1.3))
    assert pump(lambda: not rv.player.playing, 200)
    assert 1.25 <= rv.player.position() <= 1.45

    # clicking a checkpoint's play button plays it; right-click on it deletes the checkpoint
    rv.player.pause()
    app.update()
    b2 = rv._cp_buttons[1]
    b2.invoke()
    assert rv.player.playing and rv.cp_range[0] == pytest.approx(8.2 - 0.3)
    rv.player.pause()
    rv.cp_range = None
    rv._ov_ctrl_click(Ev(w * 0.3))
    app.update()
    assert len(rv.checkpoints) == 3 and len(rv._cp_buttons) == 3
    rv._cp_buttons[1].event_generate("<Button-3>", x=3, y=3)
    app.update()
    assert rv.checkpoints == [1.0, 8.2] and len(rv._cp_buttons) == 2
    rv._ov_right_click(Ev(w * 0.82 + 8))     # right-click near a checkpoint removes it
    assert rv.checkpoints == [1.0]
    rv._ov_right_click(Ev(w * 0.6))          # ...but never adds one
    assert rv.checkpoints == [1.0]

    # volume
    from mass_synced_lyrics.player import volume_gain
    rv.set_volume(25, save=False)
    assert rv.player.volume == pytest.approx(volume_gain(25))
    # click the % to type a level
    rv._edit_volume()
    app.update()
    rv._vol_editor.delete(0, "end")
    rv._vol_editor.insert(0, "60%")
    rv._vol_editor.event_generate("<Return>")
    app.update()
    assert rv.v_volume.get() == pytest.approx(60) and rv.v_volume_txt.get() == "60%"
    assert rv.player.volume == pytest.approx(volume_gain(60))
    rv._edit_volume()
    rv._vol_editor.delete(0, "end")
    rv._vol_editor.insert(0, "150")
    rv._vol_editor.event_generate("<Escape>")
    app.update()
    assert rv.v_volume.get() == pytest.approx(60)            # Esc keeps the old level
    rv._edit_volume()
    rv._vol_editor.delete(0, "end")
    rv._vol_editor.insert(0, "150")
    rv._vol_editor.event_generate("<Return>")
    app.update()
    assert rv.v_volume.get() == pytest.approx(100)           # clamped

    # timing: align line 2 (orig 4.5s) to 5.0s -> +0.5s offset
    rv.sel_line = 1
    rv.player.seek(5.0)
    rv._align()
    assert rv.offset == pytest.approx(0.5, abs=0.01)
    assert lrc.display_lines(rv.new_text)[1][0] == pytest.approx(5.0, abs=0.01)

    # hover tooltip text
    class HE:
        widget = rv.ov
        x = int(w * 0.55)
        x_root, y_root = 100, 100
    rv._hover(HE())
    assert rv.tooltip.label is not None and "Found: Second line" in rv.tooltip.label.cget("text")
    rv.tooltip.hide()

    # choose -> recorded only; the bottom bar writes it
    rv._apply()
    assert pump(lambda: rv.song is not None and rv.song.override is not None, 400)
    assert rv.rows[state_key(p)]["status"] == "chosen"
    assert lrc.same_lyrics(TagFile(p).read().lyrics[0], LRC_B)
    assert app._apply_bar_shown and "1 file" in app.btn_apply_all.cget("text")
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    app._apply_all()
    assert pump(lambda: app.apply_worker is None, 400)
    assert lrc.display_lines(TagFile(p).read().lyrics[0])[1][0] == pytest.approx(5.0, abs=0.01)
    assert rv.rows[state_key(p)]["status"] == "embedded"
    app.review.shutdown()
    app.destroy()


def test_cli_output_lrc(settings, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    d = settings.music_folder
    p = make_track(os.path.join(d, "a.flac"), "flac", lyrics="[00:01.00]x\n[00:02.00]y")
    rc = cli.main([d, "--offline", "--output", "lrc", "--log-dir", settings.log_dir,
                   "--state-db", settings.state_db])
    assert rc == 0 and os.path.exists(os.path.splitext(p)[0] + ".lrc")
    assert ".lrc saved" in capsys.readouterr().out


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_review_table_groups_choices_pins_and_scrub(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    from conftest import LRC, FakeLrclib, record

    from mass_synced_lyrics import lrc
    from mass_synced_lyrics.engine import Engine

    d = settings.music_folder
    a = make_track(os.path.join(d, "Band", "Album A", "01 Some Song.flac"), "flac")
    write_lrc(os.path.join(d, "Band", "Album A", "01 Some Song.lrc"), LRC)
    b = make_track(os.path.join(d, "Band", "Album A", "02 Other.flac"), "flac", title="Other")
    c = make_track(os.path.join(d, "Band", "Album B", "01 Third.flac"), "flac", title="Third", album="Album B")
    settings.dry_run = True
    app = new_app(settings)
    app.geometry("1300x900")
    rv = app.review
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib([record(), record(title="Other")])).run()
    for e in events:
        app._handle(e)
    app.update()
    assert str(app.nb.select()) == str(rv)                        # switched to Review after the run

    ka, kb, kc = state_key(a), state_key(b), state_key(c)
    ra, rb, rc = rv.rows[ka], rv.rows[kb], rv.rows[kc]
    assert rv.tree.exists(ka) and rv.tree.exists(kb) and not rv.tree.exists(kc)   # only songs with lyrics
    rv.v_only_found.set(False)
    rv._refill()
    assert (ra["local"], ra["online"], ra["choice"]) == ("sidecar", "found", "local")
    assert (rb["local"], rb["online"], rb["choice"]) == ("", "found", "online")
    assert (rc["online"], rc["choice"]) == ("not found", "keep")
    assert rv.tree.item(ka, "tags") == ("c_local",) and rv.tree.item(kb, "tags") == ("c_online",)
    assert rv.tree.item(kc, "tags") == ("c_keep",)
    album_a = rv.tree.parent(ka)
    artist = rv.tree.parent(album_a)
    assert album_a.startswith("grp:") and rv.tree.item(album_a, "text") == "Some Album"
    assert rv.tree.item(artist, "text") == "Some Artist" and rv.tree.item(album_a, "values")[0] == "2 songs"
    assert rv.tree.parent(kc) != album_a

    # Choice cell cycles local -> online -> keep -> local and colours follow
    assert rv.cycle_choice(ka) == "online" and rv.tree.item(ka, "tags") == ("c_online",)
    assert rv.cycle_choice(ka) == "keep" and rv.tree.item(ka, "tags") == ("c_keep",)
    assert rv.cycle_choice(ka) == "local"
    # group row: everything in the album at once
    assert rv.cycle_group_choice(album_a) == "local"          # mixed group starts at Local
    assert rv.rows[ka]["choice"] == "local" and rv.rows[kb]["choice"] == "online"   # b has no local
    assert rv.cycle_group_choice(album_a) == "online"
    assert rv.rows[ka]["choice"] == "online" and rv.rows[kb]["choice"] == "online"
    assert rv.cycle_group_choice(album_a) == "keep"
    assert rv.rows[ka]["choice"] == rv.rows[kb]["choice"] == "keep"
    assert "0 file" in app.btn_apply_all.cget("text")
    assert rv.cycle_group_choice(album_a) == "local"

    # load song a: Use new is green (its choice is the sidecar shown)
    rv.player.silent_reason = "test"
    rv.tree.selection_set(ka)

    def pump(cond, n=300):
        for _ in range(n):
            app.update()
            if cond():
                return True
            app.after(10)
        return False
    assert pump(lambda: rv.song is not None and rv.audio is not None and rv.song.path == ka)
    rv.cand = next(cd for cd in rv.cands if cd.text and lrc.same_lyrics(cd.text, LRC))
    rv._select_candidate(rv.cands.index(rv.cand))
    app.update()
    assert rv.btn_apply.cget("text").startswith("✓") and not rv.btn_keep.cget("text").startswith("✓")
    rv._lock()
    assert pump(lambda: rv.btn_keep.cget("text").startswith("✓"))
    assert rv.rows[ka]["choice"] == "keep"

    # pins, edited times, interpolation, clear buttons, stretch to length
    assert rv.orig_lines and [t for t, _ in rv.new_lines] == [1.0, 4.5, 8.2]
    rv.toggle_pin(0)                                   # pin line 1 at 1.0
    rv.set_line_time(2, 9.0)                           # edit line 3: 8.2 -> 9.0
    assert rv.pins and rv.edited
    rv._toggle_interpolate()
    times = [t for t, _ in rv.new_lines]
    assert times[0] == pytest.approx(1.0) and times[2] == pytest.approx(9.0)
    assert times[1] == pytest.approx(1.0 + (4.5 - 1.0) * (8.0 / 7.2), abs=0.01)   # interpolated
    txt = rv.txt_new.get("1.0", "end")
    assert "📌" in txt
    assert rv.txt_new.tag_ranges("edited") and rv.txt_new.tag_ranges("ts_pinned")
    rv.clear_edits()
    assert not rv.edited and rv.interpolate                       # pin 0 still there
    rv.clear_pins()
    assert not rv.pins and not rv.interpolate
    assert [t for t, _ in rv.new_lines] == [1.0, 4.5, 8.2]
    rv.cand.src_length = 5.0
    rv._stretch_to_length()
    assert rv.factor == pytest.approx(rv._duration() / 5.0)
    rv._reset_timing()

    # inline editor opened by a real click; clicking somewhere that doesn't take focus (the waveform)
    # saves and closes it (it used to stay open)
    w = rv.txt_new
    rng = w.tag_nextrange("ts", "2.0", "2.end")
    w.see(rng[0])
    app.update()
    x, y, _, h = w.bbox(rng[0])
    w.event_generate("<Button-1>", x=x + 3, y=y + h // 2)
    for _ in range(5):
        app.update()
    assert rv._editor is not None and rv._editor.winfo_exists()       # the opening click keeps it open
    rv._editor.delete(0, "end")
    rv._editor.insert(0, "0:05.25")
    assert str(app.tk.call("focus", "-lastfor", app)) == str(rv._editor)   # typing goes into it
    rv.event_generate("<ButtonPress-1>", x=2, y=2)                    # empty space: takes no focus
    app.update()
    assert rv._editor is None and [t for t, _ in rv.new_lines][1] == pytest.approx(5.25)
    rv._reset_timing()

    # inline editor: empty input resets
    rv.set_line_time(1, 6.0)
    rv._edit_time(1)
    app.update()
    rv._editor.delete(0, "end")
    rv._editor.focus_force()
    app.update()
    rv._editor.event_generate("<Return>")
    app.update()
    assert not rv.pins and [t for t, _ in rv.new_lines][1] == pytest.approx(4.5)

    # drag scrubbing on the overview (no checkpoint added) and on the scrolling waveform
    n_cp = len(rv.checkpoints)
    w = rv.ov.winfo_width()

    class Ev:
        def __init__(self, x):
            self.x, self.y = x, 10
    rv._ov_press(Ev(int(w * 0.2)))
    rv._ov_motion(Ev(int(w * 0.5)))
    rv._ov_release(Ev(int(w * 0.5)))
    assert len(rv.checkpoints) == n_cp
    assert rv.player.position() == pytest.approx(rv._duration() * 0.5, abs=0.2)
    pos = rv.player.position()
    sw = rv.sc.winfo_width()
    rv._sc_press(Ev(int(sw * 0.5)))
    rv._sc_motion(Ev(int(sw * 0.25)))
    rv._sc_release(Ev(int(sw * 0.25)))
    assert rv.player.position() == pytest.approx(pos + 2.0, abs=0.1)        # dragged left = forward
    # eased display: right after a jump the drawn position lags, then catches up within ANIM
    from mass_synced_lyrics.review_ui import ANIM
    rv.player.seek(1.0)
    first = rv._display_pos(rv.player.position())
    assert abs(first - 1.0) > 0.5
    import time as _t
    _t.sleep(ANIM + 0.05)
    assert rv._display_pos(rv.player.position()) == pytest.approx(1.0, abs=0.01)

    # lyric panes follow each other
    rv._scroll_to("cur", 0.0)
    rv._on_lyrics_scroll("cur")
    rv.shutdown()
    app.destroy()


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_session_multiselect_save_now_and_restore(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    from conftest import FakeLrclib, record

    from mass_synced_lyrics import gui
    from mass_synced_lyrics.engine import Engine

    d = settings.music_folder
    a = make_track(os.path.join(d, "a.flac"), "flac", lyrics="old a")
    b = make_track(os.path.join(d, "b.flac"), "flac", title="Other")
    c = make_track(os.path.join(d, "c.flac"), "flac", title="Third")
    settings.dry_run = True
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)

    def pump(app, cond, n=400):
        for _ in range(n):
            app.update()
            if cond():
                return True
            app.after(10)
        return False

    app = new_app(settings)
    rv = app.review
    events = []
    Engine(settings, events.append,
           lrclib=FakeLrclib([record(), record(title="Other"), record(title="Third")])).run()
    for e in events:
        app._handle(e)
    app.update()
    ka, kb, kc = state_key(a), state_key(b), state_key(c)
    # multi-select: Choice click on a selected row changes all selected
    rv.tree.selection_set([ka, kb])
    assert set(rv.selected_songs()) == {ka, kb}
    rv._cycle_many(rv.selected_songs(), "sel")
    assert rv.rows[ka]["choice"] == rv.rows[kb]["choice"] == "keep" and rv.rows[kc]["choice"] == "online"
    assert rv.set_many([ka, kb], "online") == 2
    rv.reset_many([kb])
    assert rv.rows[kb]["choice"] == "online"
    # Save now on a subset writes only those
    app.apply_paths([ka])
    assert pump(app, lambda: app.apply_worker is None)
    assert TagFile(a).read().lyrics != ["old a"] and TagFile(b).read().lyrics == []
    assert rv.rows[ka]["status"] == "embedded"
    # session saved on close; a new window offers it
    app._on_close()

    saved_lyrics = TagFile(a).read().lyrics
    app2 = new_app(settings)
    assert pump(app2, lambda: getattr(app2, "_session_dialog_win", None) is not None)
    app2._session_finish(True)                     # "Load session" — saved songs left out by default
    assert set(app2.review.rows) == {kb, kc} and app2.review.rows[kc]["choice"] == "online"
    app2.review.rows.clear()
    app2.review._refill()
    app2._offer_session()
    assert pump(app2, lambda: getattr(app2, "_session_dialog_win", None) is not None)
    app2._session_saved_var.set(True)              # "Also include songs that were already saved"
    app2._session_finish(True)
    app2.update()
    assert set(app2.review.rows) == {ka, kb, kc}
    assert app2.review.rows[ka]["status"] == "embedded"
    assert app2.review.tree.item(ka, "values")[0] == "Saved"
    assert TagFile(a).read().lyrics == saved_lyrics            # the file was NOT put back
    # restoring originals is only the explicit right-click action
    app2.restore_paths([ka], confirm=False)
    assert pump(app2, lambda: app2.apply_worker is None)
    assert TagFile(a).read().lyrics == ["old a"] and app2.review.rows[ka]["status"] == "restored"
    # remembered choice: load without asking
    app2.cmb_session.set(dict(gui.SESSION_MODES)["load"])   # the Run-tab setting
    app2._on_close()
    app3 = new_app(None)
    assert pump(app3, lambda: len(app3.review.rows) == 3)      # restored song is no longer "saved"
    assert getattr(app3, "_session_dialog_win", None) is None
    app3._on_close()


def test_cli_trim_and_straighten(settings, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    d = settings.music_folder
    p = make_track(os.path.join(d, "a.flac"), "flac")
    write_lrc(os.path.join(d, "a.lrc"), "[00:01.00] ‘x’ \n[00:02.00]y…")
    assert cli.main([d, "--offline", "--trim", "--straighten", "--lrc-action", "keep",
                     "--log-dir", settings.log_dir, "--state-db", settings.state_db]) == 0
    assert TagFile(p).read().lyrics[0] == "[00:01.00]'x'\n[00:02.00]y..."


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_switches_to_review_on_first_result_and_allows_saving_while_finding(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    import threading

    from conftest import FakeLrclib, record

    from mass_synced_lyrics import gui
    from mass_synced_lyrics.engine import Engine

    d = settings.music_folder
    a = make_track(os.path.join(d, "a.flac"), "flac")
    make_track(os.path.join(d, "b.flac"), "flac", title="Other")
    settings.dry_run = True
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib([record(), record(title="Other")])).run()
    app = new_app(settings)
    app.nb.select(0)
    app._switched_to_review = False
    busy = threading.Event()
    app.worker = threading.Thread(target=busy.wait, daemon=True)    # pretend Find is still running
    app.worker.start()
    results = [e for e in events if e["type"] in ("scan", "result")]
    app._handle(results[0])                                           # scan
    app._handle(results[1])                                           # first song found
    app.update()
    first = state_key(results[1]["result"].path)
    assert str(app.nb.select()) == str(app.review)
    assert app.review.tree.selection() == (first,)
    app.refresh_apply_bar()
    assert str(app.btn_apply_all.cget("state")) == "normal"           # can save while finding
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    app.apply_paths([first])
    for _ in range(300):
        app.update()
        if app.apply_worker is None:
            break
        app.after(10)
    assert TagFile(a if first == state_key(a) else os.path.join(d, "b.flac")).read().lyrics
    assert str(app.btn_start.cget("state")) == "disabled"             # Find is still running
    busy.set()
    app.worker.join(1)
    app.review.shutdown()
    app.destroy()


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_apply_confident_button_and_list_filters(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    from conftest import FakeLrclib, record

    from mass_synced_lyrics import gui
    from mass_synced_lyrics.engine import Engine

    d = settings.music_folder
    exact = make_track(os.path.join(d, "a.flac"), "flac")
    loose = make_track(os.path.join(d, "b.flac"), "flac", title="Other Tune")
    none = make_track(os.path.join(d, "c.flac"), "flac", title="Missing")
    settings.dry_run = True
    app = new_app(settings)
    rv = app.review
    assert rv.v_only_found.get() and app.v_only_found.get()          # on by default
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib([record(), record(title="Other Tune (Live)")])).run()
    for e in events:
        app._handle(e)
    app.update()
    ke, kl, kn = state_key(exact), state_key(loose), state_key(none)
    assert rv.tree.exists(ke) and rv.tree.exists(kl) and not rv.tree.exists(kn)
    assert rv.tree.set(ke, "match") == "100%" and rv.tree.set(ke, "fit") == "100%"   # fit from Find
    rv.v_hide_low.set(True)
    rv._filter_toggled("hide_low_confidence", rv.v_hide_low)
    assert rv.tree.exists(ke) and not rv.tree.exists(kl)
    assert app.settings.hide_low_confidence
    app.refresh_apply_bar()
    assert app.btn_apply_conf.cget("text") == "Apply confident (1)"

    def no_dialog(*a, **k):
        raise AssertionError("Apply confident must not ask")
    monkeypatch.setattr(gui.messagebox, "askyesno", no_dialog)
    app._apply_confident()
    for _ in range(300):
        app.update()
        if app.apply_worker is None:
            break
        app.after(10)
    assert TagFile(exact).read().lyrics and not TagFile(loose).read().lyrics
    assert rv.rows[ke]["status"] == "embedded"
    rv.v_hide_saved.set(True)
    rv._filter_toggled("hide_saved", rv.v_hide_saved)
    assert not rv.tree.exists(ke)
    rv.v_hide_low.set(False)
    rv.v_only_found.set(False)
    rv._filter_toggled("", rv.v_only_found)
    assert rv.tree.exists(kl) and rv.tree.exists(kn) and not rv.tree.exists(ke)
    app.v_confident.set(60)                                           # lower the bar
    app._confident_changed()
    assert app.btn_apply_conf.cget("text") == "Apply confident (1)"
    rv.shutdown()
    app.destroy()


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_fit_column_header_filters_folder_view_and_open_location(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    from mass_synced_lyrics import paths as pathsmod
    from mass_synced_lyrics import review_ui

    app = new_app(settings)
    rv = app.review
    d = settings.music_folder
    songs = []
    for i, (fit, conf) in enumerate([(95.0, 100.0), (60.0, 100.0), (85.0, 88.0)]):
        p = os.path.join(d, "Band", f"Album {i % 2}", f"{i:02d}.flac")
        rv.add_row(p, "proposed", "lrclib", "Band", f"Song {i}", "", "found", "online", f"Album {i % 2}",
                   conf_online=conf, fit_online=fit)
        songs.append(state_key(p))
    app.update()
    assert [rv.tree.set(p, "fit") for p in songs] == ["95%", "60%", "85%"]
    grp = rv.tree.parent(rv.tree.parent(songs[0]))
    assert rv.tree.set(grp, "status") == "3 songs" and rv.tree.set(grp, "fit") == "1 poor"
    assert rv.tree.set(grp, "match") == "1 low"
    # Fit header → slider popup; the filter hides songs below it
    rv._score_filter_popup("fit")
    app.update()
    assert rv._score_popup is not None and rv._score_popup.winfo_exists()
    rv.set_min_score("fit", 80)
    assert rv.tree.heading("fit", "text") == "Fit ≥80" and app.settings.list_min_fit == 80
    assert not rv.tree.exists(songs[1]) and rv.tree.exists(songs[2])
    assert rv.v_count.get() == "2 shown / 3 songs"
    rv.set_min_score("match", 90)
    assert [rv.tree.exists(p) for p in songs] == [True, False, False]
    rv.set_min_score("match", 0)
    rv.set_min_score("fit", 0)
    rv._score_filter_popup("fit")                     # second click closes it
    assert rv._score_popup is None
    assert rv.v_count.get() == "3 shown / 3 songs"
    # folder view: music folder ▸ Band ▸ Album n ▸ song
    rv.v_view.set(review_ui.VIEWS["folder"])
    rv._view_changed()
    parent = rv.tree.parent(songs[0])
    assert rv.tree.item(parent, "text") == "Album 0" and rv.group_folder(parent) == os.path.join(d, "Band", "Album 0")
    top = rv.tree.get_children("")
    assert len(top) == 1 and rv.tree.item(top[0], "text") == os.path.basename(d)
    assert rv.tree.set(top[0], "status") == "3 songs"
    assert app.settings.list_view == "folder"
    opened = []
    monkeypatch.setattr(review_ui, "open_folder", lambda f: opened.append(("folder", f)))
    monkeypatch.setattr(review_ui, "reveal_in_explorer", lambda f: opened.append(("file", f)))
    rv.open_location(parent)
    rv.open_location(songs[1])
    assert opened == [("folder", os.path.join(d, "Band", "Album 0")), ("file", songs[1])]
    # artist view: the album row opens the folder its songs share
    rv.v_view.set(review_ui.VIEWS["artist"])
    rv._view_changed()
    album = rv.tree.parent(songs[0])
    assert rv.group_folder(album) == os.path.join(d, "Band", "Album 0")
    assert rv.group_folder(rv.tree.parent(album)) == os.path.join(d, "Band")
    # a song leaving the list updates the totals of its groups
    rv.v_hide_saved.set(True)
    rv._filter_toggled("hide_saved", rv.v_hide_saved)
    rv.add_row(songs[0], "embedded")
    app.update()
    assert not rv.tree.exists(songs[0]) and rv.tree.set(rv.tree.parent(album), "status") == "2 songs"
    calls = []
    monkeypatch.setattr(pathsmod, "_launch", lambda args, shell=None: calls.append((args, shell)))
    pathsmod.reveal_in_explorer(songs[1])
    if os.name == "nt":
        assert calls[0][1].startswith("explorer /select,") and os.path.normpath(songs[1]) in calls[0][1]
    else:
        assert os.path.dirname(songs[1]) in str(calls[0])
    rv.shutdown()
    app.destroy()


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_score_levels_snap_to_5_and_change_from_the_list(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    from mass_synced_lyrics.config import load_settings, snap5
    assert [snap5(x) for x in (0, 2, 3, 82, 87.5, 99, 140)] == [0, 0, 5, 80, 90, 100, 100]
    assert snap5(40, 50, 100) == 50
    app = new_app(settings)
    rv = app.review
    d = settings.music_folder
    p = state_key(os.path.join(d, "a.flac"))
    rv.add_row(p, "proposed", "lrclib", "A", "Song", "", "found", "online", "Al", conf_online=92.0, fit_online=77.0)
    app.update()
    # Find tab: typed values snap to 5
    app.v_confident.set(92)
    app.v_fit.set(73)
    app._confident_changed()
    assert (app.settings.confident_threshold, app.settings.confident_fit) == (90, 75)
    assert (app.v_confident.get(), app.v_fit.get()) == (90, 75)
    assert rv.tree.set(rv.tree.parent(p), "match") == ""            # 92 ≥ 90: not low
    # from the list: the Match header popup changes the High confidence level too
    rv._score_filter_popup("match")
    app.update()
    rv.set_score_level("match", 96)
    assert app.settings.confident_threshold == 95 and app.v_confident.get() == 95
    assert rv.tree.set(rv.tree.parent(p), "match") == "1 low"
    rv.set_score_level("fit", 81)
    assert app.settings.confident_fit == 80 and rv.tree.set(rv.tree.parent(p), "fit") == "1 poor"
    rv.set_min_score("fit", 77)
    assert app.settings.list_min_fit == 75 and rv.tree.exists(p)       # 77 ≥ 75
    rv.set_min_score("fit", 79)
    assert app.settings.list_min_fit == 80 and not rv.tree.exists(p)
    assert load_settings().confident_threshold == 95                   # saved
    rv._score_filter_popup("match")
    rv.shutdown()
    app.destroy()


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_align_toggle_needs_nvidia_and_updates_the_list(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    import sys as _sys

    from conftest import FakeLrclib, record

    from mass_synced_lyrics import ai_align, gui
    from mass_synced_lyrics.engine import Engine

    lyr = "[00:00.50]Other first\n[00:05.00]Other second\n[00:07.00]Third line here"
    app = new_app(settings)
    assert not app.v_ai.get() and not app.settings.ai_align                  # off by default
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: errors.append(a))
    monkeypatch.setattr(ai_align, "nvidia_gpu", lambda: None)
    app.v_ai.set(True)
    app._ai_toggled()
    assert not app.v_ai.get() and errors and "NVIDIA" in errors[0][1]          # refused without a GPU
    monkeypatch.setattr(ai_align, "nvidia_gpu", lambda: "NVIDIA GeForce RTX 3070, 8192 MiB")
    monkeypatch.setattr(ai_align, "is_installed", lambda: True)
    app.v_ai.set(True)
    app._ai_toggled()
    assert app.v_ai.get() and app.settings.ai_align
    # a find, then the low-confidence song is aligned (fake worker) and the list shows it
    d = settings.music_folder
    loose = make_track(os.path.join(d, "b.flac"), "flac", title="Other Tune")
    settings.dry_run = True
    events = []
    Engine(settings, events.append, lrclib=FakeLrclib([record(title="Other Tune (Live)", synced=lyr,
                                                                duration=12)])).run()
    monkeypatch.setenv("MSL_ALIGNER_CMD", __import__("json").dumps(
        [_sys.executable, os.path.join(os.path.dirname(__file__), "fake_aligner.py")]))
    for e in events:
        app._handle(e)                  # the low-confidence song is queued for the GPU while Find runs
    assert app.align_service is not None
    k = state_key(loose)
    for _ in range(800):
        app.update()
        if app.review.rows[k]["status"] == "aligned" and app.align_service.idle():
            break
        app.after(10)
    k = state_key(loose)
    assert app.review.rows[k]["status"] == "aligned", app.txt.get("1.0", "end")[-1500:]
    assert app.review.rows[k]["conf_online"] == 100
    assert app.review.tree.set(k, "status") == "⚡ Aligned"
    assert "Aligned (100% of words heard)" in app.txt.get("1.0", "end")
    app.review.shutdown()
    app.destroy()


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_align_error_popup_choices(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    import threading

    from mass_synced_lyrics import ai_align

    app = new_app(settings)
    app.settings.ai_align = True
    app.v_ai.set(True)

    class Svc:
        def __init__(self):
            self.calls = []
            self.done = 0

        def pending(self):
            return 2

        def resume(self):
            self.calls.append("resume")

        def stop(self):
            self.calls.append("stop")

        def idle(self):
            return True

    def buttons():
        out = []

        def walk(w):
            for c in w.winfo_children():
                if c.winfo_class() == "TButton":
                    out.append(c)
                walk(c)
        walk(app._align_dialog)
        return {b.cget("text"): b for b in out}

    for choice in ("Ignore and keep aligning", "Stop using alignment", "Cancel the run"):
        svc = app.align_service = Svc()
        busy = threading.Event()
        app.worker = threading.Thread(target=busy.wait, daemon=True)      # a Find run in progress
        app.worker.start()
        app.cancel = threading.Event()
        app._handle({"type": "align_error", "error": "CUDA error: out of memory", "path": "x", "queued": 2})
        app.update()
        assert app._align_dialog is not None and app._align_dialog.winfo_exists()
        assert "2 song(s) are waiting" in app._align_dialog_wait.get()
        app._handle({"type": "align_error", "error": "again", "path": "y", "queued": 2})   # one dialog only
        buttons()[choice].invoke()
        app.update()
        assert app._align_dialog is None
        if choice.startswith("Ignore"):
            assert svc.calls == ["resume"] and app.settings.ai_align and not app.cancel.is_set()
        elif choice.startswith("Stop using"):
            assert svc.calls == ["stop"] and not app.settings.ai_align and not app.v_ai.get()
            assert not app.cancel.is_set()                                    # Find carries on
            app.settings.ai_align = True
            app.v_ai.set(True)
        else:
            assert "stop" in svc.calls and app.cancel.is_set() and app.settings.ai_align
        busy.set()
        app.worker.join(1)
    assert "Align: paused — CUDA error: out of memory" in app.txt.get("1.0", "end")
    assert ai_align.AlignService                              # the real one is used outside this test
    app.review.shutdown()
    app.destroy()


def _pump(app, cond, n=600):
    for _ in range(n):
        app.update()
        if cond():
            return True
        app.after(10)
    return False


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_player_auto_align_heard_lane_ripple_and_live_plot(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    import json
    import sys as _sys

    from conftest import FakeLrclib, record

    from mass_synced_lyrics import ai_align, lrc
    from mass_synced_lyrics.engine import Engine

    lyr = "[00:00.50]Other first\n[00:05.00]Other second\n[00:07.00]Third line here"
    monkeypatch.setattr(ai_align, "nvidia_gpu", lambda: "NVIDIA test GPU")
    monkeypatch.setattr(ai_align, "is_installed", lambda: True)
    monkeypatch.setenv("MSL_ALIGNER_CMD", json.dumps([_sys.executable,
                                                      os.path.join(os.path.dirname(__file__), "fake_aligner.py")]))
    d = settings.music_folder
    p = make_track(os.path.join(d, "a.flac"), "flac")
    settings.dry_run = True
    Engine(settings, lambda e: None, lrclib=FakeLrclib([record(synced=lyr)])).run()
    settings.ai_align = True
    app = new_app(settings)
    app.geometry("1300x900")
    rv = app.review
    app.nb.select(rv)
    k = state_key(p)
    rv.add_row(k, "proposed", "lrclib", "Some Artist", "Some Song", "", "found", "online")
    rv.player.silent_reason = "test"
    rv.tree.selection_set(k)
    assert _pump(app, lambda: rv.song is not None and rv.audio is not None)
    assert [t for t, _ in rv.new_lines] == [0.5, 5.0, 7.0] and rv.edited_segs() == []
    ver = rv._lanes_ver
    # live plot: an edit redraws the lanes at once
    rv.set_line_time(1, 5.5)
    assert rv._lanes_ver > ver and [round(s.start, 2) for s in rv.new_segs][1] == 5.5
    rv._draw_scroll(0.0)
    assert rv._scene["lanes"] == rv._lanes_ver
    rv.set_line_time(1, None)
    # Auto-align (GPU): fake worker hears line i at 2 + 3i with 90% certainty
    rv.auto_align()
    assert str(rv.btn_ai.cget("state")) == "disabled"
    assert _pump(app, lambda: rv._align_req is None)
    assert [t for t, _ in rv.new_lines] == [2.0, 5.0, 8.0]
    assert rv.ai_keys == set(rv.pins) and len(rv.ai_keys) == 3
    assert rv.txt_new.tag_ranges("ai_edited") and "Aligned: 100% of words heard" in rv.v_ai_note.get()
    # the Edited lane shows the edited lyrics, shaded by how surely each line was heard
    assert rv.has_edits() and rv.edited_segs() and rv.ai_conf
    rv._draw_overview()
    assert any(rv.ov.itemcget(i, "fill") == review_ui_heard(0.9) for i in rv.ov.find_all()
               if rv.ov.type(i) == "rectangle")
    # it stays after looking at another song and coming back (saved with the song)
    rv._save_prefs()
    rv.pins, rv.ai_keys, rv.ai_conf = {}, set(), {}
    rv.load_song(k)
    tok = rv._token
    assert _pump(app, lambda: getattr(rv, "_loaded_token", 0) == tok and rv.audio is not None)
    app.update()
    assert rv.edited_segs() and rv.ai_conf
    # an edit by hand takes over that line; the aligner's other lines stay purple
    rv.set_line_time(2, 9.0)
    assert lrc.pin_key(7.0) not in rv.ai_keys and len(rv.ai_keys) == 2 and rv.txt_new.tag_ranges("edited")
    # ripple: unpin everything, then one edit moves the lines around it (clamped to the song)
    rv.clear_edits()
    assert not rv.pins and not rv.ai_keys
    rv.v_ripple.set(True)
    rv._ripple_toggled()
    assert app.settings.ripple
    rv.set_line_time(1, 6.0)                                   # 5.0 -> 6.0: +1 s around it
    times = [t for t, _ in rv.new_lines]
    assert times[0] == pytest.approx(1.5) and times[1] == pytest.approx(6.0)
    assert times[2] <= rv._duration() + 1e-6                   # squeezed into the song
    assert rv.txt_new.tag_ranges("moved") and rv.btn_interp.cget("text").startswith("✓ Ripple")
    rv.v_ripple.set(False)
    rv._ripple_toggled()
    assert [t for t, _ in rv.new_lines] == [0.5, 6.0, 7.0]     # ripple off: only the edited line moved
    # below the minimum: shown in the lane, timings unchanged
    rv.clear_edits()
    monkeypatch.setenv("FAKE_PROB", "0.3")
    app.align_service.stop()
    app.align_service.join(10)
    app.align_service = None
    app._release_held_aligner()                                # fresh worker reads the new env
    app.settings.ignore_cache = True                           # (not the cached result of the first run)
    rv.auto_align()
    assert _pump(app, lambda: rv._align_req is None)
    assert not rv.pins and "Not applied" in rv.v_ai_note.get() and not rv.has_edits()
    rv.shutdown()
    app.destroy()


def review_ui_heard(p):
    from mass_synced_lyrics.review_ui import heard_color
    return heard_color(p)


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_song_aligned_while_finding_opens_with_its_edits_visible(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    import sys as _sys

    from conftest import FakeLrclib, record

    from mass_synced_lyrics import ai_align, lrc
    from mass_synced_lyrics.engine import Engine
    from mass_synced_lyrics.state import State

    lyr = "[00:00.50]Other first\n[00:05.00]Other second\n[00:07.00]Third line here"
    d = settings.music_folder
    p = make_track(os.path.join(d, "b.flac"), "flac", title="Other Tune")
    settings.dry_run = True
    Engine(settings, lambda e: None, lrclib=FakeLrclib([record(title="Other Tune (Live)", synced=lyr,
                                                                duration=12)])).run()
    al = ai_align.Aligner(command=[_sys.executable, os.path.join(os.path.dirname(__file__), "fake_aligner.py")])
    ai_align.run(settings, [p], lambda e: None, aligner=al)
    al.close()
    st = State(settings.resolved_state_db())
    pending = st.get_pending(p)[2]
    st.close()
    app = new_app(settings)
    rv = app.review
    app.nb.select(rv)
    k = state_key(p)
    rv.add_row(k, "aligned", "lrclib", "Some Artist", "Other Tune", "", "found", "online")
    rv.player.silent_reason = "test"
    rv.tree.selection_set(k)
    assert _pump(app, lambda: rv.song is not None and rv.audio is not None)
    assert getattr(rv.cand, "ai_detail", None) is not None
    assert lrc.same_lyrics(rv.new_text, pending)                      # exactly what Apply will write
    assert rv.ai_keys and rv.txt_new.tag_ranges("ai_edited") and rv.edited_segs()
    assert rv.btn_apply.cget("text").startswith("✓")                  # still the default, no choice made
    rv.clear_edits()                                                  # back to the original timing
    assert [t for t, _ in rv.new_lines] == [0.5, 5.0, 7.0]
    rv.shutdown()
    app.destroy()


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_activity_rows_install_block_log_times_and_current_pane(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    import re
    import threading

    from mass_synced_lyrics import ai_align, gui

    d = settings.music_folder
    make_track(os.path.join(d, "a.flac"), "flac")
    write_lrc(os.path.join(d, "a.lrc"))
    settings.use_online = False
    settings.lrc_action = "keep"
    app = new_app(settings)
    rv = app.review
    # the Current pane starts collapsed; Keep current sits beside Use new
    assert str(rv._lf) not in [str(x) for x in rv._lyrics_paned.panes()]
    assert rv.btn_keep2.winfo_manager() == "pack"
    rv.set_current_shown(True)
    assert str(rv._lf) in [str(x) for x in rv._lyrics_paned.panes()] and app.settings.show_current
    assert rv.btn_keep2.winfo_manager() == ""
    rv.set_current_shown(False)
    assert not app.settings.show_current
    # Find shows a row while it runs, gone when done
    app._start()
    assert app.activity.is_active("find")
    for _ in range(300):
        app.update()
        if app.worker and not app.worker.is_alive() and app.events.empty():
            app.update()
            break
        app.after(20)
    app.update()
    assert not app.activity.is_active("find")
    # log lines carry the time
    app.update()
    lines = [x for x in app.txt.get("1.0", "end").splitlines() if x.strip()]
    assert lines and all(re.match(r"^\d\d:\d\d:\d\d  ", x) for x in lines)
    # installing the add-on: progress row with Stop; Find waits
    monkeypatch.setattr(ai_align, "nvidia_gpu", lambda: "NVIDIA test GPU")
    monkeypatch.setattr(ai_align, "is_installed", lambda: False)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    infos = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: infos.append(a))
    gate = threading.Event()

    def fake_install(log, cancel, progress):
        progress(0.25, "Installing PyTorch with CUDA (about 2.5 GB)", "torch: 640.0 MB of 2.5 GB")
        while not cancel.is_set() and not gate.is_set():
            cancel.wait(0.05)
        return False
    monkeypatch.setattr(ai_align, "install", fake_install)
    app.v_ai.set(True)
    app._ai_toggled()
    assert _pump(app, lambda: app.activity.value("install") == 25.0)
    assert "640.0 MB" in app.activity.detail("install")
    assert str(app.btn_start.cget("state")) == "disabled"
    app._start()                                                      # blocked while installing
    assert infos and "still installing" in infos[-1][1] and not app.activity.is_active("find")
    app.activity.rows["install"]["btn"].invoke()                      # Stop
    assert _pump(app, lambda: not app.activity.is_active("install"))
    assert not app.settings.ai_align and str(app.btn_start.cget("state")) == "normal"
    # Align (GPU) with nothing low-confidence says so
    monkeypatch.setattr(ai_align, "is_installed", lambda: True)
    app.settings.ai_align = True
    app.align_command("low")
    assert "No songs in the list" in infos[-1][1]
    rv.shutdown()
    app.destroy()


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_list_progress_columns_hide_columns_new_find_clears_and_remove(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    from mass_synced_lyrics import gui
    from mass_synced_lyrics.config import load_settings
    from mass_synced_lyrics.engine import Result
    from mass_synced_lyrics.tags import Meta

    settings.session_restore = "fresh"          # no "load last session?" question mid-test
    app = new_app(settings)
    rv = app.review
    d = settings.music_folder
    a = state_key(os.path.join(d, "Band", "Album", "01.flac"))
    b = state_key(os.path.join(d, "Band", "Album", "02.flac"))
    # Find: in progress, then done
    app._handle({"type": "file_start", "path": a})
    app.update()
    assert rv.tree.exists(a) and rv.tree.set(a, "find") == "⟳" and "busy" in rv.tree.item(a, "tags")
    assert rv.tree.set(a, "status") == "Looking up…"                       # shown though nothing found yet
    r = Result(a, "proposed", "lrclib", Meta(title="One", artist="Band", album="Album"), 10.0,
               online="found", choice="online", conf_online=97.0, fit_online=90.0)
    app._handle({"type": "result", "result": r, "done": 1, "total": 2, "counts": {"proposed": 1}, "elapsed": 1})
    app._handle({"type": "file_start", "path": b})
    app.update()
    assert rv.tree.set(a, "find") == "✓" and "busy" not in rv.tree.item(a, "tags")
    grp = rv.tree.parent(rv.tree.parent(a))
    app._handle({"type": "file_start", "path": b})
    app.update()
    # Save: in progress → done; Align marks
    app._handle({"type": "apply_file_start", "path": a})
    app.update()
    assert rv.tree.set(a, "save") == "⟳"
    app._handle({"type": "apply_progress", "path": a, "done": 1, "total": 1, "status": "embedded",
                 "source": "lrclib", "detail": "", "counts": {}})
    assert rv.tree.set(a, "save") == "✓"
    app._handle({"type": "align_song_start", "path": a})
    assert rv.tree.set(a, "align") == "⟳"
    app._handle({"type": "align_song_start", "path": state_key(os.path.join(d, "x.flac"))})
    assert state_key(os.path.join(d, "x.flac")) not in rv.rows            # only Find adds songs
    # the end of a run clears anything still marked in progress
    app._handle({"type": "done", "counts": {}, "csv": "x.csv", "elapsed": 1, "cancelled": True})
    app.update()
    assert rv.rows[b]["op_find"] == ""
    # hide / show columns (remembered); clicks still find the Choice column by name
    rv.toggle_column("local")
    rv.toggle_column("online")
    assert "local" not in rv._shown_columns() and load_settings().list_columns and \
        "online" not in load_settings().list_columns
    app.update()
    box = rv.tree.bbox(a, column="choice")
    assert box and rv._column_at(box[0] + 5) == "choice"
    rv._apply_columns([c for c, _, _ in gui.__dict__.get("COLUMNS", [])] or ["status", "local", "online", "choice",
                                                                             "match", "fit", "find", "align", "save"])
    assert "local" in rv._shown_columns()
    menu = rv._columns_menu()
    assert menu.index("end") >= 9
    # remove a group from the list
    assert grp.startswith("grp:")
    rv.v_only_found.set(False)
    rv._refill()
    grp = rv.tree.parent(rv.tree.parent(a))
    n = rv.remove_from_list(rv._songs_under(grp))
    assert n == 2 and not rv.rows and not rv.tree.get_children("")
    # a new Find run starts with an empty list
    rv.add_row(a, "proposed", "lrclib", "Band", "One", "", "found", "online", "Album")
    monkeypatch.setattr(app, "_run_engine", lambda s: None)
    os.makedirs(d, exist_ok=True)
    app._start()
    assert not rv.rows and rv.v_count.get().startswith("0 shown")
    rv.shutdown()
    app.destroy()


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_player_resize_end_marks_and_edited_lane(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    from conftest import FakeLrclib, record

    from mass_synced_lyrics.engine import Engine

    lyr = "[00:00.50]Other first\n[00:05.00]Other second\n[00:07.00]Third line here"
    p = make_track(os.path.join(settings.music_folder, "a.flac"), "flac")
    settings.dry_run = True
    Engine(settings, lambda e: None, lrclib=FakeLrclib([record(synced=lyr)])).run()
    app = new_app(settings)
    app.geometry("1300x900")
    rv = app.review
    app.nb.select(rv)
    k = state_key(p)
    rv.add_row(k, "proposed", "lrclib", "Some Artist", "Some Song", "", "found", "online")
    rv.player.silent_reason = "test"
    rv.tree.selection_set(k)
    assert _pump(app, lambda: rv.song is not None and rv.audio is not None)
    assert not rv.has_edits() and rv.edited_segs() == []
    # drag the end of the first lyric earlier: a gap opens, the next lyric stays
    rv._resize_start((0, "end"))
    rv._resize["pending"] = 3.0
    rv._resize_apply(rv._resize)
    rv._resize = None
    assert [round(t, 2) for t, _ in rv.new_lines] == [0.5, 3.0, 5.0, 7.0] and rv.new_lines[1][1] == ""
    assert rv.has_edits() and rv.edited_segs()                     # the Edited lane is shown
    from mass_synced_lyrics.review_ui import EDIT_A, EDIT_B, EDIT_USER, heard_color
    # never aligned on the GPU: nothing in the Edited lane looks like the aligner's purple
    assert set(rv._edit_colours()) <= {EDIT_A, EDIT_B, EDIT_USER}
    assert heard_color(0.9) not in rv._edit_colours() and not rv.ai_keys
    # the Align column stays empty for it; ✎ only once you save your own timing
    rv.rows[k]["choice"] = "custom"
    rv._upsert(k)
    assert rv.tree.set(k, "align") == ""
    rv.rows[k]["hand"] = True
    rv._upsert(k)
    assert rv.tree.set(k, "align") == "✎"
    # the gap row can be moved like a lyric, and cleared
    rv.set_line_time(1, 4.0)
    assert [round(t, 2) for t, _ in rv.new_lines][:3] == [0.5, 4.0, 5.0]
    # drag the end of the second lyric later into the third: the third ripples
    rv._resize_start((2, "end"))
    rv._resize["pending"] = 7.5
    rv._resize_apply(rv._resize)
    rv._resize = None
    assert [round(t, 2) for t, _ in rv.new_lines][-1] == 7.5
    rv.clear_edits()
    assert [t for t, _ in rv.new_lines] == [0.5, 5.0, 7.0] and not rv.end_marks
    rv.shutdown()
    app.destroy()


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_align_row_finishes_when_find_ends_after_the_aligner_went_idle(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    app = new_app(settings)

    class Svc:
        paused, done, total = False, 2, 2
        counts = {"aligned": 2}

        def idle(self):
            return True

        def pending(self):
            return 0

        def reset_counts(self):
            self.done = 0

    app.align_service = Svc()
    app.activity.set("align", "Aligning on the GPU", detail="2 done · waiting for songs from Find…")
    assert app.activity.is_active("align")
    app._align_finish_if_idle()
    assert not app.activity.is_active("align") and app.align_service.done == 0
    app.update()
    assert "2 aligned" in app.txt.get("1.0", "end")
    app.destroy()


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_auto_align_button_belongs_to_its_song_and_shows_progress(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    from conftest import FakeLrclib, record

    from mass_synced_lyrics import ai_align
    from mass_synced_lyrics.engine import Engine

    lyr = "[00:00.50]Other first\n[00:05.00]Other second\n[00:07.00]Third line here"
    p = make_track(os.path.join(settings.music_folder, "a.flac"), "flac")
    settings.dry_run = True
    Engine(settings, lambda e: None, lrclib=FakeLrclib([record(synced=lyr)])).run()
    app = new_app(settings)
    rv = app.review
    app.nb.select(rv)
    k = state_key(p)
    rv.add_row(k, "proposed", "lrclib", "Some Artist", "Some Song", "", "found", "online")
    rv.player.silent_reason = "test"
    rv.tree.selection_set(k)
    assert _pump(app, lambda: rv.song is not None and rv.audio is not None)
    assert str(rv.btn_ai.cget("state")) == "normal" and "Auto-align" in rv.btn_ai.cget("text")
    rv._align_req = (rv.song.path, "x", 1)                    # waiting for this song
    rv._ai_button()
    assert str(rv.btn_ai.cget("state")) == "disabled"
    snap = {"frac": 0.42, "label": "Isolating the vocals", "stage_frac": 0.5, "left": 75}
    rv.auto_align_progress(snap, False)
    assert "42%" in rv.btn_ai.cget("text") and "Isolating the vocals (50%)" in rv.v_ai_note.get()
    assert "1:15 left" in rv.v_ai_note.get()
    rv.load_song(k)                                           # reloading keeps the request (it used to
    tok = rv._token                                           # forget it and leave the button greyed out)
    assert _pump(app, lambda: getattr(rv, "_loaded_token", 0) == tok)
    assert rv._align_req is not None and str(rv.btn_ai.cget("state")) == "disabled"
    rv._align_req = ("some other song.flac", "x", 2)          # another song is being aligned: this one can be
    rv._ai_button()
    assert str(rv.btn_ai.cget("state")) == "normal"
    rv.cancel_auto_align()
    # right-click → Align on the GPU queues the songs whatever their scores (offering to switch it on)
    queued = []
    monkeypatch.setattr(app, "_ensure_ai", lambda: True)
    monkeypatch.setattr(app, "queue_alignment", lambda songs, force=False: queued.append((songs, force)) or 1)
    app.align_command("songs", [k])
    assert queued == [([k], True)]
    # the Align column: GPU marks, progress while busy, and your own timing
    rv.set_op(k, "align", "busy")
    rv.set_align_pct(k, 37)
    assert rv.tree.set(k, "align") == "⟳ 37%"
    rv.set_op(k, "align", "done")
    assert rv.tree.set(k, "align") == "⚡✓"
    assert ai_align.STAGE_LABEL["separate"] == "Isolating the vocals"
    rv.shutdown()
    app.destroy()


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_batch_alignment_shows_on_a_song_opened_before_and_cache_can_be_cleared(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    import sys as _sys

    from conftest import FakeLrclib, record

    from mass_synced_lyrics import ai_align
    from mass_synced_lyrics.engine import Engine

    lyr = "[00:00.50]Other first\n[00:05.00]Other second\n[00:07.00]Third line here"
    p = make_track(os.path.join(settings.music_folder, "b.flac"), "flac", title="Other Tune")
    settings.dry_run = True
    Engine(settings, lambda e: None, lrclib=FakeLrclib([record(title="Other Tune (Live)", synced=lyr,
                                                                duration=12)])).run()
    app = new_app(settings)
    rv = app.review
    app.nb.select(rv)
    k = state_key(p)
    rv.add_row(k, "proposed", "lrclib", "Some Artist", "Other Tune", "", "found", "online")
    rv.player.silent_reason = "test"
    rv.tree.selection_set(k)
    assert _pump(app, lambda: rv.song is not None and rv.audio is not None)
    rv.set_line_time(0, 0.6)                                  # opened and touched before: prefs are saved
    rv._save_prefs()
    assert rv.song.prefs.get("pins")
    # the batch aligns it meanwhile
    al = ai_align.Aligner(command=[_sys.executable, os.path.join(os.path.dirname(__file__), "fake_aligner.py")])
    ai_align.run(settings, [p], lambda e: None, aligner=al, everything=True)
    al.close()
    rv.load_song(k)
    tok = rv._token
    assert _pump(app, lambda: getattr(rv, "_loaded_token", 0) == tok and rv.audio is not None)
    app.update()
    assert rv.ai_keys and rv.edited_segs()                     # the purple GPU edits show (they didn't)
    assert rv.pins[rv._line_key(0)] == pytest.approx(0.6)     # your own time is kept
    # buttons don't keep the keyboard focus: Space plays instead of pressing Auto-align again
    assert str(rv.btn_ai.cget("takefocus")) == "0"
    # Clear cached data: the alignment is undone
    monkeypatch.setattr(gui_messagebox(), "askyesno", lambda *a, **kw: True)
    app.clear_cache_command([k])
    tok = rv._token
    assert _pump(app, lambda: getattr(rv, "_loaded_token", 0) == tok and rv.audio is not None)
    app.update()
    assert not rv.ai_keys and rv.tree.set(k, "align") == ""
    assert "Cleared cached data for 1 of 1" in app.txt.get("1.0", "end")
    assert not app.v_ignore_cache.get() and not app.settings.ignore_cache          # off by default
    rv.shutdown()
    app.destroy()


def gui_messagebox():
    from mass_synced_lyrics import gui
    return gui.messagebox
