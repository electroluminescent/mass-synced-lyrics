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
| Electron (with Chromium and Node.js; their licenses ship with the app as LICENSES.chromium.html) | MIT | https://www.electronjs.org |
| fpcalc (Chromaprint) | LGPL-2.1-or-later; static build includes FFmpeg (LGPL) | https://github.com/acoustid/chromaprint |
| PyInstaller bootloader | GPL-2.0 with bootloader exception | https://pyinstaller.org |

Because mutagen is GPL-licensed, this application as a whole is distributed under
GPL-3.0-or-later (see LICENSE).

## Optional GPU add-on (Align low-confidence lyrics)

Not part of the app download. When the option is switched on, the app installs these into a separate
Python environment in `%LOCALAPPDATA%\mass-synced-lyrics\aligner` and runs them as a separate process:

| Component | License | Source |
|---|---|---|
| PyTorch (CUDA build) | BSD-3-Clause | https://pytorch.org |
| stable-ts | MIT | https://github.com/jianfch/stable-ts |
| OpenAI Whisper (code and model weights) | MIT | https://github.com/openai/whisper |
| Demucs (code and htdemucs weights) | MIT | https://github.com/adefossez/demucs |
| julius, PyAV, NumPy | MIT / BSD-3-Clause / BSD-3-Clause | PyPI |

## Online services

- **LRCLIB** (https://lrclib.net): a free, community lyrics database. Lyrics are user-submitted.
- **AcoustID** (https://acoustid.org) and **MusicBrainz** (https://musicbrainz.org): used only
  to identify files that are missing title/artist tags.
