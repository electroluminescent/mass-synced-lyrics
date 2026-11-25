# Publishing on GitHub

Repository: **https://github.com/electroluminescent/mass-synced-lyrics**. Run the commands in PowerShell.

| # | Who | Step |
|---|-----|------|
| 1 | Claude (done) | Created the local git repository, made the commits, and pointed `origin` at the URL above. |
| 2 | You (once) | Let git use the network folder: `git config --global --add safe.directory '%(prefix)///192.168.2.98/Data/Cloud/Projects/mass-synced-lyrics'` |
| 3 | You (once) | At https://github.com/new, create `electroluminescent/mass-synced-lyrics`, public or private, with **no** README, .gitignore or license. |
| 4 | You | `cd A:\Cloud\Projects\mass-synced-lyrics` then `git push -u origin main`, and sign in when the browser opens. |
| 5 | You (once) | Get an *application* key at https://acoustid.org/new-application. On GitHub, open **Settings → Secrets and variables → Actions → New repository secret**, name it `ACOUSTID_API_KEY` and paste the key. |
| 6 | You | Publish a release with `git tag v1.1.4` then `git push origin v1.1.4`. |
| 7 | GitHub (automatic) | Runs the tests, builds `MassSyncedLyrics-1.1.4-windows.zip`, and attaches it to a new Release. |

For local builds with the key, run `python scripts\write_buildinfo.py --key YOURKEY --homepage https://github.com/electroluminescent/mass-synced-lyrics`. This writes the git-ignored `_buildinfo.py`.

Before each new tag, set the version in `src\mass_synced_lyrics\__init__.py` and add a `CHANGELOG.md` entry. Every push to `main` also builds the zip as a workflow artifact, under *Actions → the run → Artifacts*.

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
