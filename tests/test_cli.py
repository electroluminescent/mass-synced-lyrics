import os

from conftest import make_track, write_lrc

from mass_synced_lyrics import cli
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


def test_cli_output_lrc(settings, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    d = settings.music_folder
    p = make_track(os.path.join(d, "a.flac"), "flac", lyrics="[00:01.00]x\n[00:02.00]y")
    rc = cli.main([d, "--offline", "--output", "lrc", "--log-dir", settings.log_dir,
                   "--state-db", settings.state_db])
    assert rc == 0 and os.path.exists(os.path.splitext(p)[0] + ".lrc")
    assert ".lrc saved" in capsys.readouterr().out


def test_cli_trim_and_straighten(settings, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    d = settings.music_folder
    p = make_track(os.path.join(d, "a.flac"), "flac")
    write_lrc(os.path.join(d, "a.lrc"), "[00:01.00] ‘x’ \n[00:02.00]y…")
    assert cli.main([d, "--offline", "--trim", "--straighten", "--lrc-action", "keep",
                     "--log-dir", settings.log_dir, "--state-db", settings.state_db]) == 0
    assert TagFile(p).read().lyrics[0] == "[00:01.00]'x'\n[00:02.00]y..."
