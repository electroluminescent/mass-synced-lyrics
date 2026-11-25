# Publishing on GitHub

The repository lives at **https://github.com/electroluminescent/mass-synced-lyrics** once it has been created. The local git repository is already set up, with `origin` pointing there.

## One-time setup

0. **Let git use the folder.** `A:` is a network share, so git asks for each repository on it to be marked as safe, as you did for your other projects:
   ```powershell
   git config --global --add safe.directory '%(prefix)///192.168.2.98/Data/Cloud/Projects/mass-synced-lyrics'
   ```
1. **Create the empty repository** at https://github.com/new
   - Owner: `electroluminescent`, name: `mass-synced-lyrics`. Public or private, as you prefer.
   - Do **not** add a README, .gitignore or license, because the project already has them.
2. **Push** from the project folder (PowerShell):
   ```powershell
   cd A:\Cloud\Projects\mass-synced-lyrics
   git push -u origin main
   ```
   The first push asks you to sign in to GitHub; Git Credential Manager opens a browser window.
3. **Add the AcoustID key as a secret** so release builds can identify untagged files:
   - Register an *application* key at https://acoustid.org/new-application. A personal user key is rejected.
   - On GitHub, go to repository **Settings → Secrets and variables → Actions → New repository secret**. Name it `ACOUSTID_API_KEY` and paste the key as the value.
   - Also paste the key into `src\mass_synced_lyrics\_buildinfo.py` (git-ignored) for local builds, or run
     `python scripts\write_buildinfo.py --key YOURKEY --homepage https://github.com/electroluminescent/mass-synced-lyrics`.

## Publishing a release

```powershell
git tag v1.0.3
git push origin v1.0.3
```

The **build** workflow (`.github/workflows/build.yml`) runs the tests on Windows and Linux and builds `MassSyncedLyrics-<version>-windows.zip` with the bundled fpcalc. It then creates a **GitHub Release** for the tag with the zip attached. Every push to `main` also builds and keeps the zip as a workflow artifact, under *Actions → the run → Artifacts*.

Before tagging, set the same version in `src\mass_synced_lyrics\__init__.py` and add an entry to `CHANGELOG.md`.

## Day to day

```powershell
git add -A
git commit -m "What changed"
git push
```

The following are never committed (see `.gitignore`):
- `MassSyncedLyrics.exe` in the project folder
- `build\` and `dist\`
- `_buildinfo.py`, which holds your key
