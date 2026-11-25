"""Write src/mass_synced_lyrics/_buildinfo.py (git-ignored) before building.

The AcoustID key is read from the ACOUSTID_API_KEY environment variable (a
GitHub Actions secret in CI) so it never lands in the repository.
"""
import argparse
import os

parser = argparse.ArgumentParser()
parser.add_argument("--homepage", default="")
parser.add_argument("--key", default=os.environ.get("ACOUSTID_API_KEY", ""))
args = parser.parse_args()

target = os.path.join(os.path.dirname(__file__), "..", "src", "mass_synced_lyrics", "_buildinfo.py")
with open(target, "w", encoding="utf-8") as fh:
    fh.write("# Generated at build time. Do not commit.\n")
    fh.write(f"DEFAULT_ACOUSTID_KEY = {args.key.strip()!r}\n")
    fh.write(f"HOMEPAGE = {args.homepage.strip()!r}\n")
print(f"wrote {os.path.normpath(target)} (key {'set' if args.key.strip() else 'empty'})")
