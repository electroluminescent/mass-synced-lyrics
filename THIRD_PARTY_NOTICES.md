# Third-party components

The Windows build bundles the following software. Each remains under its own license.

| Component | License | Source |
|---|---|---|
| Python runtime | PSF License | https://www.python.org |
| mutagen | GPL-2.0-or-later | https://github.com/quodlibet/mutagen |
| RapidFuzz | MIT | https://github.com/rapidfuzz/RapidFuzz |
| requests (and urllib3, idna, certifi) | Apache-2.0 / MIT / BSD / MPL-2.0 | https://github.com/psf/requests |
| charset-normalizer | MIT | https://github.com/jawah/charset_normalizer |
| Send2Trash | BSD-3-Clause | https://github.com/arsenetar/send2trash |
| PyAV (with bundled FFmpeg libraries) | BSD-3-Clause; FFmpeg LGPL-2.1-or-later | https://github.com/PyAV-Org/PyAV |
| NumPy | BSD-3-Clause | https://numpy.org |
| python-sounddevice (with PortAudio) | MIT | https://github.com/spatialaudio/python-sounddevice |
| Sun Valley ttk theme (sv-ttk) | MIT | https://github.com/rdbende/Sun-Valley-ttk-theme |
| Tcl/Tk (tkinter) | Tcl/Tk license (BSD-style) | https://www.tcl.tk |
| fpcalc (Chromaprint) | LGPL-2.1-or-later; static build includes FFmpeg (LGPL) | https://github.com/acoustid/chromaprint |
| PyInstaller bootloader | GPL-2.0 with bootloader exception | https://pyinstaller.org |

Because mutagen is GPL-licensed, this application as a whole is distributed under
GPL-3.0-or-later (see LICENSE).

## Online services

- **LRCLIB** (https://lrclib.net): a free, community lyrics database. Lyrics are user-submitted.
- **AcoustID** (https://acoustid.org) and **MusicBrainz** (https://musicbrainz.org): used only
  to identify files that are missing title/artist tags.
