# Changelog

## 1.2.1
- Alignment runs alongside Find: low-confidence songs are queued as they are found and aligned on the
  GPU while finding carries on.
- If the aligner fails (out of GPU memory, a damaged add-on, the GPU missing), alignment pauses and a
  window shows the error. Finding carries on and eligible songs keep queueing. The window offers:
  - **Ignore and keep aligning:** skip the failed song, restart the aligner, and work through the queue.
  - **Stop using alignment:** switch the feature off; songs keep their found lyrics.
  - **Cancel the run:** stop finding and aligning.

  The song that failed always keeps its found lyrics. 3 single-song failures in a row open the same window.
- After about 2 minutes with nothing to align, the aligner closes and frees its GPU memory.

## 1.2.0
- **Align low-confidence lyrics (NVIDIA GPU)**: an optional switch under **Scores and alignment** on
  the Find lyrics tab, off by default.
  - It needs an NVIDIA graphics card; without one the switch stays off. The first time, it offers to
    install the add-on (PyTorch with CUDA, Whisper via stable-ts, and Demucs, about 3 GB) into its own
    folder. The app itself doesn't grow.
  - After Find, songs below High confidence or Good fit are aligned. Demucs isolates the vocals,
    Whisper locates the lyric words, lines heard clearly are placed, and the rest are interpolated.
  - The result is kept only if enough of the words were clearly heard: **Aligned lyrics must match**,
    70 % by default, in steps of 5 %. Otherwise the song keeps its found lyrics.
  - Kept results show as **Aligned**, with updated Match and Fit, and the original timing stays
    selectable in the player.
  - Right-click → **Align on the GPU** aligns chosen songs. **Stop** stops after the current song.
- The score levels and the audio switches moved to a **Scores and alignment** section in the left
  card, so the activity log keeps its space.

## 1.1.6
- The score levels can be changed in steps of 5 %: **High confidence** (default 95 %), **Good fit**
  (default 80 %) and **Match confidence** (default 90 %) on the Find lyrics tab. Typed values snap to the
  nearest 5 %.
- The **Match** / **Fit** header popup has a second slider for the level itself (**High confidence at ≥**
  / **Good fit at ≥**). It's the same setting as on the Find lyrics tab, and it updates Apply confident,
  Hide low confidence and the group counts right away.
- The header filter sliders also move in 5 % steps (arrow keys too).

## 1.1.5
- **Apply confident** button beside Apply writes, without asking, the found lyrics at or above both the
  **High confidence** (95 % by default) and **Good fit** (80 %) levels on the Find lyrics tab. Songs with
  your own choices or edits are left for Apply.
- Two scores per song, as new list columns:
  - **Match**: how sure it is that the lyrics belong to this song. It's based on how exactly LRCLIB matched
    (title, artist, album) or how closely a sidecar/collection name matched, and the length difference.
    Songs identified by AcoustID lose 5 points, and LRCLIB search matches top out at 90 %.
  - **Fit**: how well the lyrics fit this recording. It checks the length the lyrics were timed for, lines
    past the end, and how the lines spread over the song. When the audio is checked, it also looks for lines
    that start on silence and lyrics that run into the silence at the end.
  - Songs opened in the player are always checked against their audio. The new **Check lyrics against the
    audio (slower)** switch does it for every song while finding.
  - The New pane shows each candidate's fit and what lowered it.
- Click the **Match** or **Fit** column header for a slider that hides songs below a chosen %.
- More song list filters: **Lyrics found** (only songs with lyrics to write), **Hide saved**, and **Hide
  low confidence** (below either level). The Find lyrics tab's **List only songs with lyrics found** switch,
  on by default, sets Lyrics found for each run.
- The list can be arranged by **Artist ▸ Album** or by **Folders** on disk, both as a tree.
- Right-click a song for **Open file location**, or a group for **Open folder**. Either opens it in
  File Explorer.
- Speed: the app no longer slows down as the list grows. Adding a song used to get slower with every song
  listed (about 35 ms each at 40,000 songs, and worse in the folder view). It now stays about 0.2 ms.
  - Group totals are updated once per batch, from their direct children.
  - The shown-songs count is tracked as songs are added and removed.
  - The activity log is written in batches.
  - The Apply counts are read in one database pass, off the UI thread during a run.
  - The session autosave runs in the background, and the search box waits for a pause in typing.
- Fixed: the activity log listed only some songs during a run (1.1.4).

## 1.1.4
- Songs are looked up in parallel (6 workers by default, `--workers N`). The LRCLIB rate limit and
  its back-off are shared by all workers; AcoustID lookups stay one at a time.
- Saving (Apply, **Save now**, and restoring originals) also uses the worker threads. Two songs
  that save to the same .lrc are never written at the same moment, and a failure on one file
  doesn't stop the rest.
- The app switches to **Review & player** as soon as the first song with new lyrics is found.
  Songs can be played, edited and saved (bottom bar or right-click **Save now**) while the
  rest are still being looked up.

## 1.1.3
- New options (Find lyrics tab, off by default; `--trim` / `--straighten` on the command line):
  **Trim spaces from each lyric line** and **Straighten smart quotes and punctuation**
  (‘ ’ → '  “ ” → "  … → ...  – — → -  unusual/invisible spaces normalised). They apply to every
  lyric that gets written, and the player's New pane shows the cleaned-up text.
- Time rulers under both waveforms (m:ss); the scrolling ruler is part of the pre-rendered view.

## 1.1.2
- Loading the last session never changes files. The option is now **Also include songs that were
  already saved**: those songs come back listed as **Saved** (off by default = only songs still to
  be saved). "Remember my choice" also remembers this option.
- Restoring original lyrics is only done from the right-click menu (with confirmation).
- "Written" is now shown as **Saved** in the song list, legend and filter.

## 1.1.1
- Volume slider is logarithmic (decibel scale, 40 dB range): 100 % = full, 50 % ≈ −20 dB, 0 % = mute,
  so most of the slider covers the quieter, more useful range.
- Click the volume % to type a level (Enter or click away to set, Esc to cancel); mouse wheel over
  the slider nudges it by 2 %.
- Overview waveform: a plain click now jumps there (drag scrubs). **Ctrl+click** adds a checkpoint,
  or removes the one under the pointer; right-click on a checkpoint still deletes it.
- Hover hints on every button, switch and field (and on the song-list column headers and colour
  legend); explanatory text and the ⓘ line were removed from the screen.
- Faster waveform: the scrolling waveform is rendered once into image tiles and the lyric lanes are
  built once; each frame only scrolls the view (about 12× less work per frame). The overview
  waveform is a single image.

## 1.1.0
- Modern interface: Sun Valley theme (Windows 11 style) with a header, card layout, accent buttons,
  switches, flat green ✓ choice buttons and restyled lists and lyric panes. **Dark mode** switch in the
  header (remembered). The waveform timeline is unchanged.
- The "Run" tab is now **Find lyrics**; library/collection and options sit side by side.
- Shortcut help moved into a hover tip (ⓘ) to give the lyric panes more room.

## 1.0.3
- Sessions: the review list is saved while you work and on exit. At start the app offers to load the
  last session ("Remember my choice"; change it later on the Run tab), with an option — off by
  default — to also restore files that were overwritten.
- Restore originals: files the app wrote to get back the lyrics / `.lrc` they had before (the app keeps
  a backup of the original before its first write). Sidecar `.lrc` files sent to the Recycle Bin are
  not brought back automatically.
- Song list: select several songs (Ctrl/Shift+click, or artist/album rows); a Choice click changes
  all of them. Right-click: **Save now** (write just those songs), Use Local / Use Online / Don't change,
  Reset to default, Restore original lyrics.

## 1.0.2
- Song table is grouped Artist ▸ Album ▸ song, with **Local**, **Online** and **Choice** columns.
  Click a Choice cell to cycle Local → Online → Don't change (on an artist/album row: all its songs).
  Rows are coloured by what Apply will do (local / online / custom / don't change / written / check).
  Find now also checks LRCLIB for songs with local lyrics so both can be offered; songs finished in
  earlier runs stay listed.
- After Find lyrics the app switches to the Review & player tab.
- Review: **Keep current** / **Use new** buttons under each lyrics pane; the one Apply will use is green.
- Resizable split (drag handle) between the song list and the player, and between the lyrics panes.
- Lyrics panes scroll together, follow playback (current line centred) and animate with a 0.2 s ease;
  seeks and scrubbing glide too. Drag either waveform to scrub.
- New lyrics: pin column (📌), click a timestamp to edit it (empty = reset), right-click menu
  (edit / set to playhead / pin / reset). Pinned and edited times are highlighted yellow.
  **Interpolate from pins** re-times every untouched line between your pins; **Clear pins**,
  **Clear edited times**, and **Stretch to song length** (uses the LRCLIB/[length:] source length).
- Review player: volume slider (remembered between sessions).
- Checkpoints: right-click a checkpoint's ▶ button to delete it; right-click on the overview
  also removes the nearest checkpoint, and Ctrl+click has a wider target. Number keys work
  unless you are typing in an editable box.

## 1.0.1
- New workflow: Find lyrics → review → **Apply** from a bar fixed to the bottom of both tabs.
  Final action: **Overwrite files (embed lyrics)** (default) or **Save .lrc beside each file**.
  Per-file choices in the Review tab are recorded and written by Apply.
- `.lrc` output mode in the engine and on the command line (`--output lrc`); embedded synced
  lyrics are exported to `.lrc`; a `<song>.lrc` that is already the output is never removed.
- App icon (waveform, playhead and lyric lines) on the .exe files, window and taskbar.
- Windows: per-file choices, dry-run proposals and backups now use one path spelling
  (long-path prefix mismatch fixed).
- Command line no longer crashes on song names the Windows console can't display.
- A rejected AcoustID key is reported once and fingerprinting stops for that run.
- `MassSyncedLyrics.exe --selftest <audio file> <report.txt>` checks decoding, audio output and fpcalc.
- Build script downloads fpcalc with Python (works on Windows PowerShell 5.1).

## 1.0.0
- First release: desktop interface and command line.
- Sources: sidecar .lrc (fuzzy name matching), personal LRC collection, LRCLIB.
- AcoustID / MusicBrainz identification for untagged files.
- Embeds into MP3, FLAC, Ogg, Opus, WAV, AIFF, M4A, WMA, APE, WavPack and more.
- Restores modified dates, verifies writes, recycles used sidecar .lrc files.
- Resumable runs (SQLite), CSV log per run, dry-run mode.
- Review & player tab: overview and scrolling waveforms with lyric lanes (line spans,
  word timings, playing-line progress), hover tooltips, per-song checkpoints
  (click / Ctrl+click / number keys, global length and loop settings), side-by-side
  current vs. candidate lyrics, offset/stretch and align-to-playhead re-timing,
  LRCLIB search, and per-file choices (pin / lock / follow settings) that take
  precedence over settings and later runs. Original lyrics are backed up before replacing.
- Windows build with bundled fpcalc; GitHub Actions release workflow.
