Icon sources. `icon.svg` is used for 48 px and larger, `icon-small.svg` for 20–40 px,
and `icon-16.svg` is pixel-aligned for 16 px. The runtime copies (`icon.ico` and the PNGs)
live in `src/mass_synced_lyrics/assets/`.

Regenerate them with `rsvg-convert`, then pack the PNGs (16, 20, 24, 32, 40, 48, 64, 128, 256) into `icon.ico`.
