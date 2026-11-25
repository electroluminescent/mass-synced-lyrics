# Mass Synced Lyrics

Walks a music folder and adds **time-synced (LRC) lyrics** to each song. It has a desktop interface with a review player, plus a command line.

The interface is a modern light/dark theme; switch with **Dark mode** in the header. Hover over any button, switch or field for a short explanation.

The app works in three steps:

1. **Find lyrics** (Run tab). The app scans the folder and finds lyrics. **Nothing is written yet.**
2. **Review** (optional, Review & player tab). Listen, compare old and new lyrics, re-time them, and make per-file choices.
3. **Apply** (the bar fixed to the bottom of both tabs, which appears once files are loaded). Choose the final action:
   - **Overwrite files (embed lyrics)**, the default, writes the lyrics into each audio file's tags.
   - **Save .lrc beside each file** writes `<song name>.lrc` next to each song and leaves the audio files untouched.

Lyrics are taken from these sources, in this order:

1. **A sidecar `.lrc` file in the same folder.** It is matched by file name or title, including loose matches like `Song.lrc` ↔ `03 - Artist - Song (Remastered).flac`. A sidecar **wins** even over synced lyrics already embedded in the file. After it is embedded and verified, the `.lrc` is moved to the Recycle Bin by default.
2. **Your own LRC collection.** These are any folders or individual `.lrc` files you add. Each file is identified by its `[ar:]`/`[ti:]` headers or by an `Artist - Title.lrc` name. Collection files are never removed.
3. **[LRCLIB](https://lrclib.net).** Lookups use title, artist, album and duration, where LRCLIB accepts a duration difference of up to ±2 s. If nothing matches, a search is scored by name similarity.

Files that lack title/artist tags are identified first. The tool uses the MusicBrainz recording ID when the file has one; otherwise it uses an **AcoustID** fingerprint (made with `fpcalc`, which is bundled in the Windows build). AcoustID needs an *application* API key, which you register at acoustid.org/new-application. A personal user key is rejected.

## Behaviour

| Situation | Result |
|---|---|
| Matching sidecar `.lrc` exists | Used, replacing whatever is there. On Apply, the `.lrc` is recycled, deleted or kept, as configured. A sidecar that is already the `<song>.lrc` output is never removed. |
| File already has synced lyrics, no sidecar | Left alone. In *.lrc* mode, synced lyrics already embedded in the file are saved out as the `.lrc`. |
| File already has synced lyrics embedded | No online requests are sent for it (LRCLIB or AcoustID) while **No online requests for songs with synced lyrics embedded** is on, which is the default. A sidecar `.lrc` or your collection can still replace its lyrics. |
| File has plain lyrics only | Replaced if synced lyrics are found. |
| Only plain lyrics found online | Left alone. |
| Instrumental on LRCLIB | Left alone, and recorded so it isn't looked up again. |
| Two or more `.lrc` files match equally well | Not applied; reported as *ambiguous*. |
| LRC timestamps run past the end of the track | Rejected (wrong version/edit). |

Other behaviour:

- File **modified dates are restored** after writing (an option), so cloud sync and library scanners don't treat the files as changed.
- Each write is **verified** by re-reading the tags before any `.lrc` is removed.
- Progress is stored in SQLite. Stopping and re-running **resumes**: finished files are skipped, and earlier misses are not looked up again unless *Retry* is ticked. Earlier misses are still re-checked against your collection on every run.
- Every run writes a **CSV log**: status, source, path, artist/title/album, the `.lrc` used, match score and details.

## Review & player tab

As soon as **Find lyrics** finds the first song with new lyrics, the app switches here. Drag the handle between the song list and the player to resize them.

**Song list:** songs are grouped **Artist ▸ Album ▸ song**, using the tags or the folder names when tags are missing.

- **Status:** what happened to the song.
- **Local:** sidecar `.lrc`, collection, or embedded lyrics (in `.lrc` mode).
- **Online:** found, plain lyrics only, instrumental, or not found.
- **Choice:** what Apply will do. Click a Choice cell to cycle **Local → Online → Don't change**. On an artist or album row, the click sets every song in that group.
- **Match:** how sure it is that the lyrics in Choice belong to this song. **yours** means you chose or edited the lyrics.
  - 100 %: exact title, artist, album and length on LRCLIB, an exact sidecar file name, or embedded lyrics.
  - 97 %: LRCLIB matched without the album. 92 %: matched by the primary artist. 88 %: matched by a simplified title.
  - Up to 90 %: found by LRCLIB search. Sidecar and collection files score by how closely the names match.
  - The score drops by up to 10 points as the recording's length drifts from the song's (2 s at most), and by 5 for songs identified by AcoustID.
- **Fit:** how well those lyrics fit this recording. It starts at 100 % and loses points for:
  - lyrics timed for a longer or shorter version;
  - lines after the end of the song;
  - lyrics that end before 45 % of the song, or start after half of it;
  - with the audio checked: lines that start on silence, and lyrics that run into the silence at the end.

  Songs opened in the player are always checked against the audio. **Check lyrics against the audio (slower)** on the Find lyrics tab does it for every song while finding. The New pane shows each candidate's fit and what lowered it.

**Progress columns:**
- **Find**, **Align** and **Save** show ⟳ while a song is being worked on, then:
  - ✓ done;
  - – nothing to do, or kept as found;
  - ✗ error.
- Group rows show how many are done.
- Songs being worked on are highlighted, and stay visible whatever the filters.
- Every operation still running also has a row at the top of the window, with a progress bar and **Stop**.

**Columns:** right-click a column header, or use **Columns** under the list, to show or hide columns. **Right-click → Remove from the list** (or the Delete key) takes songs, albums, artists or folders out of the list without touching their files. Starting **Find lyrics** begins a new list.

Click the **Match** or **Fit** header for two sliders, both in 5 % steps. One hides songs below a chosen % (songs without a score stay listed). The other sets the level that counts as **High confidence** or **Good fit**, the same setting as on the Find lyrics tab. The box at the top left arranges the list by **Artist ▸ Album** (from the tags) or by **Folders** on disk. Right-click a song for **Open file location**, or a group row for **Open folder**.

**Filters** (above the list): **Lyrics found** shows only songs with lyrics to write. It is on by default; the **List only songs with lyrics found** switch on the Find lyrics tab sets the default for each run. **Hide saved** hides songs already written or up to date. **Hide low confidence** hides songs below the **High confidence** (95 %) or **Good fit** (80 %) level set on the Find lyrics tab.

**Several songs at once:** Ctrl+click and Shift+click select several songs, and an artist or album row counts as all its songs. Clicking a Choice cell on a selected row changes them all. **Right-click** for:

- **Save now:** writes just these songs immediately.
- **Use Local / Use Online / Don't change** and **Reset to default.**
- **Restore original lyrics:** gives files this app wrote to back the lyrics or `.lrc` they had before.

Row colours show the action: local (gold), online (blue), custom (purple), don't change (grey), written (green), needs a look (red). Filter and search sit above the list; *Load earlier results* and *Open file…* add more songs.

**Player:**

- **Overview waveform** of the whole song, with lyric lanes for the **current** (gold) and **new** (green) lyrics. Each bar spans the time its line is shown. Word timings appear as divisions inside a bar, and the playing line lights up.
- **Scrolling waveform** that moves past a fixed red playhead (8 s view), with line text, progress through the line, and the current word.
- Seeks and scrubbing glide with a 0.2 s ease.
- **Time rulers** under both waveforms show where you are.
- **Hovering** over a waveform shows the current and new lyric at that point.
- **Click a waveform to jump there; drag it to scrub.**
- **Checkpoints:**
  - **Ctrl+click** the overview to add one; **Ctrl+click** on an existing one removes it. Right-click a checkpoint or its ▶ button also deletes it.
  - Keys **1–9** play checkpoint N; **0** plays the next one.
  - Each plays ± the checkpoint length (default 1.0 s), then stops, or repeats if *Loop* is ticked.
  - Defaults are the first and last lyric line. Your own checkpoints are saved per song; *Reset points* restores the defaults.
- **Volume slider** next to the play button, on a logarithmic (decibel) scale so most of its travel covers the quieter range. Click the **%** to type a level, or use the mouse wheel over the slider. **Space** plays and pauses. Number keys and Space work unless you are typing in an editable box.

**Lyrics panes:** current lyrics on the left, a choice of new lyrics on the right: what Find found, sidecars, collection matches, LRCLIB (cached or *Search LRCLIB*), the current lyrics (to re-time them), the original lyrics, or *No lyrics*. The panes scroll together, keep the playing line centred, and animate smoothly. Click a line to jump to it.

- Under each pane is a button: **Keep current** or **Use new**. The one Apply will use is **green with a ✓**.
- *Reset to default* removes your choice for the song.

**The lyric panes:** the song's **Current** lyrics are collapsed by default; **▸ Current** shows them beside the new ones and **◂ Hide** collapses them again (remembered). While they're hidden, **Keep current** sits beside **Use new**.

**Timing of the new lyrics** (the controls under the waveforms):

- **Offset** (ms) and **Stretch** (%).
- **Stretch to song length:** scales the lyrics to this song's length, using the length of the LRCLIB version or the `[length:]` tag. If neither is known, the app asks for it.
- **Line to playhead:** aligns the selected line to the playhead. One aligned line sets the offset; two set offset and stretch together.
- **📌 Pin column:** pin a line at its current time. Pinned lines keep their time whatever else changes.
- **Click a timestamp to edit it.** Enter saves and Esc cancels; clearing the box resets it. **Right-click a line** for *Edit time*, *Set to playhead*, *Pin/Unpin* and *Reset this timestamp*.
- **Timestamp colours:**
  - **Yellow:** pinned and edited times (edited ones in bold).
  - **Purple:** times set by the aligner.
  - **Amber text:** times that moved because of interpolation or ripple.
- **Interpolate:** re-times every line you have not pinned or edited by stretching the original timing between your pins. The ends follow the nearest stretch.
- **Ripple changes:** when a time is edited, by you or by the aligner, the lines around it move too.
  - Lines between two pinned or edited lines are stretched to fit.
  - Lines before the first or after the last move by the same amount, squeezed so they stay within 0 and the song's length.

  Interpolate and Ripple are mutually exclusive: with Ripple on, the Interpolate button shows *✓ Ripple*.
- **Auto-align (GPU):** locates the New lyrics in this song's vocals (needs the NVIDIA add-on; see below).
  - If enough lyric words were heard being sung (**Aligned lyrics must match**), the lines it placed get those times (purple) and the others interpolate or ripple. Times you set yourself are kept.
  - Otherwise nothing changes, and a note says why.
- **Clear pins** and **Clear edits** remove them; *Reset timing* clears everything.

**Lanes under the waveforms:** *Current*, *Found* (the lyrics as found) and *Edited*. The Edited lane appears whenever the timing has been changed, by you or by the aligner, and stays when you come back to the song.
- Lines placed by the aligner are purple, brighter where they were heard more surely. Times you set are yellow.
- Hover over a lyric to see its found and edited times and the aligner's certainty.

**Resize a lyric** by dragging either edge in the scrolling waveform (the cursor changes on an edge):
- **Start:** moves only that lyric.
- **End, with empty time after it:** moves only the gap. Nothing after it moves until the gap is used up.
- **End, with the next lyric right after it:** later pushes the following lyrics up to the next gap, which absorbs the change. Earlier opens a gap.

The lyric lanes and the New list follow every change live, whether it comes from you or from the aligner.

Songs aligned while finding open the same way: the found lyrics with the aligner's purple times on top of the original timing. **Clear edits** goes back to the original timing.

Pins, edits and checkpoints are saved per song.

**Apply confident** (bottom bar, beside Apply) writes the found lyrics at or above both the **High confidence** and **Good fit** levels straight away, without asking. Songs with your own choices or edits are left for **Apply**.

**Per-file choices** are recorded here and written by the bottom bar's **Apply**. They take precedence over the saved settings and the current run's options, and later runs keep them.

On Windows, sound is played through the default output device. Without an output device, playback still runs silently so the waveform and lyrics keep moving.

## Align low-confidence lyrics (NVIDIA GPU, optional)

Off by default. Switch it on under **Scores and alignment** on the Find lyrics tab.
- **Requirements:** an NVIDIA graphics card with its driver installed. Without one, the option can't be switched on.
- **First use:** the app offers to install a one-time add-on into `%LOCALAPPDATA%\mass-synced-lyrics\aligner`. It needs Python 3.10–3.12 from python.org.
  - The add-on is PyTorch with CUDA, Whisper (via stable-ts) and Demucs, about 3 GB.
  - The speech and vocal-separation models, about 1.6 GB more, download the first time it runs.

While **Find lyrics** runs, songs whose found lyrics are below **High confidence** or **Good fit** (and have no per-file choice) are queued and aligned one at a time on the GPU. Finding carries on meanwhile, and low-confidence songs from earlier runs are added when Find finishes. For each song:
1. Demucs isolates the vocals.
2. Whisper writes down what was actually sung or said, word by word, and also locates each lyric word.
3. The lyrics are matched to the words heard, in order. Words that aren't in the lyrics (talk between songs, band introductions, ad-libs) are skipped, so lyrics aren't placed on them. This is what makes live recordings work.
4. Lines that were clearly heard are placed where they were sung. The rest are interpolated between them, the same as *Interpolate*. Where the performer talks or plays without singing for a while, a gap is inserted.
5. The result is **verified**: the share of the lyric's words heard being sung. It is kept only if it reaches **Aligned lyrics must match** (70 % by default, steps of 5 %).

**If the song passes:**
- Its found lyrics are replaced by the re-timed version, shown as **Aligned** in the list.
- Its Match becomes at least the verify score, and its Fit is recomputed against the audio.
- The original timing stays available in the player as **Original timing**.

**If it doesn't pass,** the song keeps its found lyrics unchanged. The activity log says why.

**If the aligner fails** (for example the GPU runs out of memory, the add-on is damaged, or the GPU can't be found), alignment pauses and a window explains the error. Finding carries on, and songs that need aligning keep queueing. The song that failed keeps its found lyrics. Choose:
- **Ignore and keep aligning:** skip that song, restart the aligner, and work through the queue.
- **Stop using alignment:** switch the option off. Finding carries on, and queued songs keep their found lyrics.
- **Cancel the run:** stop finding and aligning.

If 3 songs in a row fail on their own (unreadable files, say), the same window appears.

Songs are aligned only once for the same lyrics. To align particular songs regardless of their scores, select them and use **right-click → Align on the GPU**. **Stop** stops the alignment after the current song.

## Cleaning up lyrics

Two options on the Find lyrics tab tidy every lyric the app writes. Both are off by default.

- **Trim spaces from each lyric line:** removes spaces at the start and end of each line, after its timestamp.
- **Straighten smart quotes and punctuation:** turns typographic characters into plain ones: ‘ ’ → `'`, “ ” → `"`, … → `...`, – — → `-`. Unusual or invisible spaces become normal spaces or are removed.

The player's New pane shows the cleaned-up text. On the command line, use `--trim` and `--straighten`.

## Sessions and restoring

The review list is saved automatically while you work and when you close the app. At the next start the app offers to **load the last session**:

- **Remember my choice** skips the question next time, along with your choice for the box below. Change it later under *When the app starts* on the Find lyrics tab.
- **Also include songs that were already saved** (off by default) brings back songs whose lyrics were already written, shown as **Saved**. Their files are not changed. Otherwise only songs still waiting to be saved come back.

Before its first write to a file, the app keeps a copy of the original lyrics or `.lrc`. To put those originals back, select songs and right-click → **Restore original lyrics**. The app asks before doing it. Sidecar `.lrc` files that were moved to the Recycle Bin can be restored from the Recycle Bin.

## Where lyrics are written

| Format | Tag |
|---|---|
| MP3, WAV, AIFF, DSF (ID3v2) | `USLT` (keeps the file's ID3 version, 2.3 or 2.4) |
| FLAC, Ogg Vorbis, Opus, Speex | `LYRICS` |
| M4A / ALAC | `©lyr` |
| WMA | `WM/Lyrics` |
| APE, WavPack, Musepack, TAK, OptimFROG | APEv2 `Lyrics` |

The LRC text, timestamps included, goes into the normal lyrics field. That is what foobar2000 (OpenLyrics), MusicBee, Poweramp, Navidrome, Jellyfin, Plex and most other players read for synced lyrics.

## Using it

**Windows (no install needed):**

1. Unzip `MassSyncedLyrics-<version>-windows.zip` anywhere, for example to your Desktop.
2. Double-click **`MassSyncedLyrics.exe`**.
3. Choose your music folder and press **Find lyrics**. Nothing is changed until you press **Apply** in the bar at the bottom.

The app isn't code-signed, so the first launch may show *Windows protected your PC*. Click **More info → Run anyway**. Starting it takes a few seconds because the single-file app unpacks itself first.

`mass-synced-lyrics-cli.exe` is the same engine for scripts and scheduled tasks, for example `mass-synced-lyrics-cli.exe "D:\Music" --dry-run`.

**From source** (Python 3.10+):

```
pip install .
mass-synced-lyrics-gui                 # desktop interface
mass-synced-lyrics "D:\Music" --dry-run
```

Command line options: `--output embed|lrc`, `--lookup-synced` (also look up files that already have synced lyrics embedded), `--trim`, `--straighten`, `--collection PATH` (repeatable), `--offline`, `--dry-run` (find only; the app's Apply button can write the results later), `--lrc-action recycle|delete|keep`, `--no-restore-mtime`, `--threshold 90`, `--no-acoustid`, `--acoustid-key KEY`, `--fpcalc PATH`, `--retry-misses`, `--rpm 80`, `--workers 6`, `--log-dir DIR`, `--state-db FILE`.

Settings, the state database and logs are stored in `%APPDATA%\mass-synced-lyrics\`.

## Speed

LRCLIB allows about 90 requests/minute for identified clients, and this tool uses 80 by default. Most tracks need 1–2 requests, so a 40,000-track library takes roughly 8–16 hours on the first pass. Sidecar and collection matches are instant. Stopping and resuming is safe.

Songs are processed in parallel (6 workers by default; `--workers N`, or `"workers"` in `settings.json`). Saving uses the same number of workers. All lookup workers share the one LRCLIB rate limit, and a "slow down" reply from LRCLIB pauses all of them. AcoustID lookups run one at a time.

The app switches to the **Review & player** tab as soon as the first song with new lyrics is found. You can play, edit and save songs there while the rest are still being looked up; the list keeps growing as results come in.

## Building

```
.\build.ps1          # Windows: runs tests, downloads fpcalc, builds both .exe files and the zip
```

GitHub Actions (`.github/workflows/build.yml`) does the same on every push. Pushing a tag such as `v1.0.3` publishes a Release with the zip attached. See `PUBLISHING.md` for the one-time GitHub setup. Set the repository secret **`ACOUSTID_API_KEY`** to build a default AcoustID key into the app. Locally, `python scripts/write_buildinfo.py --key YOURKEY` does the same. The key is never committed.

## Limitations

- An edit or remaster can have the same duration as the version on LRCLIB but a different lead-in, which leaves the lyrics offset by a few seconds. This can't be detected automatically.
- LRCLIB lyrics are community-submitted, so their timing quality varies.
- If the library is in a cloud folder with files-on-demand (OneDrive and similar), reading tags downloads the files.

## License

GPL-3.0-or-later (required by mutagen). See `LICENSE` and `THIRD_PARTY_NOTICES.md`.
