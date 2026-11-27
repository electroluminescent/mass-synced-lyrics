import sys


def selftest(audio_path: str, out_path: str) -> None:
    """msl-backend --selftest <audio file> <report.txt>: checks the bundled decoder, fpcalc and the
    aligner worker without starting the app."""
    lines = []
    try:
        import os

        from mass_synced_lyrics import __version__, ai_align
        from mass_synced_lyrics.player import decode
        from mass_synced_lyrics.providers import find_fpcalc

        lines.append(f"version {__version__}")
        d = decode(audio_path)
        lines.append(f"decode ok: {d.duration:.2f}s, {len(d.peaks)} peaks")
        lines.append(f"fpcalc: {find_fpcalc() or 'NOT FOUND'}")
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
        from mass_synced_lyrics.server import main

        main()
