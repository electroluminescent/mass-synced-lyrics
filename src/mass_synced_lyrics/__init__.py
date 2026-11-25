"""Mass Synced Lyrics: embed time-synced (LRC) lyrics into a music library."""

__version__ = "1.1.6"
APP_NAME = "mass-synced-lyrics"
APP_TITLE = "Mass Synced Lyrics"

try:  # generated at build time; never committed
    from . import _buildinfo  # type: ignore
except ImportError:  # pragma: no cover
    _buildinfo = None

DEFAULT_ACOUSTID_KEY = getattr(_buildinfo, "DEFAULT_ACOUSTID_KEY", "") if _buildinfo else ""
HOMEPAGE = getattr(_buildinfo, "HOMEPAGE", "") if _buildinfo else ""


def user_agent() -> str:
    ua = f"{APP_NAME}/{__version__}"
    if HOMEPAGE:
        ua += f" ({HOMEPAGE})"
    return ua
