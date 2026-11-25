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
    rv.cmb_cand.focus_force()
    app.update()
    rv.cmb_cand.event_generate("<KeyPress-1>")
    app.update()
    assert rv.player.playing
    rv.player.pause()
    rv.cp_range = None
    rv.txt_cur.focus_force()
    app.update()
    (app.focus_get() or rv).event_generate("<KeyPress-1>")
    app.update()
    assert rv.player.playing
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
    assert rv.tooltip.label is not None and "New: Second line" in rv.tooltip.label.cget("text")
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

    app2 = new_app(settings)
    assert pump(app2, lambda: getattr(app2, "_session_dialog_win", None) is not None)
    app2._session_finish(True)                     # "Load session"
    assert set(app2.review.rows) == {ka, kb, kc}
    assert app2.review.rows[ka]["status"] == "embedded" and app2.review.rows[kc]["choice"] == "online"
    # restore originals for the written song
    app2.restore_paths([ka], confirm=False)
    assert pump(app2, lambda: app2.apply_worker is None)
    assert TagFile(a).read().lyrics == ["old a"] and app2.review.rows[ka]["status"] == "restored"
    # remembered choice: load without asking
    app2.cmb_session.set(dict(gui.SESSION_MODES)["load"])   # the Run-tab setting
    app2._on_close()
    app3 = new_app(None)
    assert pump(app3, lambda: len(app3.review.rows) == 3)
    assert getattr(app3, "_session_dialog_win", None) is None
    app3._on_close()
