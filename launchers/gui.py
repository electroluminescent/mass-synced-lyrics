import sys


def selftest(audio_path: str, out_path: str) -> None:
    """MassSyncedLyrics.exe --selftest <audio file> <report.txt>: checks the bundled
    decoder, audio output and fpcalc without opening the window."""
    lines = []
    try:
        from mass_synced_lyrics import __version__
        from mass_synced_lyrics.player import decode
        from mass_synced_lyrics.providers import find_fpcalc

        lines.append(f"version {__version__}")
        d = decode(audio_path)
        lines.append(f"decode ok: {d.duration:.2f}s, {len(d.peaks)} peaks")
        try:
            import sounddevice as sd

            lines.append(f"audio output ok: {sd.query_devices(kind='output')['name']}")
        except Exception as e:
            lines.append(f"audio output unavailable: {type(e).__name__}: {e}")
        fp = find_fpcalc()
        lines.append(f"fpcalc: {fp or 'NOT FOUND'}")
        import tkinter

        lines.append(f"tk {tkinter.TkVersion}")
        from mass_synced_lyrics import theme

        root = tkinter.Tk()
        root.withdraw()
        theme.apply(root, "dark")
        lines.append(f"theme: {'sun valley' if theme._state['sv'] else 'fallback (sv-ttk missing)'}")
        root.destroy()
        import os

        from mass_synced_lyrics import ai_align

        if not os.path.exists(ai_align.worker_source()):
            raise RuntimeError("aligner_worker.py is missing from the build")
        lines.append(f"aligner worker bundled; GPU: {ai_align.nvidia_gpu() or 'no NVIDIA GPU'}; "
                     f"add-on {'installed' if ai_align.is_installed() else 'not installed'}")
        lines.append("SELFTEST OK")
    except Exception as e:
        lines.append(f"SELFTEST FAILED: {type(e).__name__}: {e}")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2], sys.argv[3])
    else:
        from mass_synced_lyrics.gui import main

        main()
