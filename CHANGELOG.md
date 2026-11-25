# Changelog

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
