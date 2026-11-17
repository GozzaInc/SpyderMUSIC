"""Select independently updatable music libraries before SpyderMUSIC imports its engine.

PyInstaller always prefers modules inside its PYZ archive. The build therefore
EXCLUDES yt_dlp, ytmusicapi and yt_dlp_ejs from PYZ, and ships their original
wheel files as ordinary data in ``runtime/music_packages`` (one-directory)
or ``_MEIPASS/music_packages`` (one-file). New releases live under the app's
persistent ``data/music_packages`` directory; no external Python is needed.

Only versioned, complete releases described by active.json are activated.
Deleting active.json safely switches back to the bundled versions.
"""

import json
import re
import sys
from pathlib import Path

PACKAGE_MODULES = {"yt-dlp": "yt_dlp", "ytmusicapi": "ytmusicapi"}
_VERSION_FOLDER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+-]{0,100}\Z")


def application_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def persistent_package_dir():
    return application_dir() / "data" / "music_packages"


def bundled_package_dir():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "music_packages"
    return None


def read_active():
    """Corrupt or partially written manifests never prevent startup."""
    manifest = persistent_package_dir() / "active.json"
    try:
        if not manifest.exists() or manifest.stat().st_size > 16_384:
            return {}
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        active = payload.get("active", {})
        return active if isinstance(active, dict) else {}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def installed_release_dir(package, entry):
    """Never let a malformed manifest escape the versioned release subtree."""
    if package not in PACKAGE_MODULES or not isinstance(entry, dict):
        return None
    folder = entry.get("folder")
    if not isinstance(folder, str) or not _VERSION_FOLDER.fullmatch(folder):
        return None
    root = persistent_package_dir() / "releases" / package / folder
    module = PACKAGE_MODULES[package]
    if not (root / module / "__init__.py").is_file():
        return None
    if package == "yt-dlp" and not (root / "yt_dlp_ejs").is_dir():
        # Release must include a matching EJS distribution, not an unrelated
        # version accidentally found in the bundled baseline.
        return None
    return root


def configure_music_package_paths():
    """Safe to call from PyInstaller runtime hook and Python entrypoint."""
    # Ensure the bundled, known-good set is the fallback, not a stale global
    # site-packages directory. In source mode imports remain in the venv.
    bundled = bundled_package_dir()
    if bundled and bundled.is_dir() and str(bundled) not in sys.path:
        sys.path.insert(0, str(bundled))

    active = read_active()
    for package in reversed(tuple(PACKAGE_MODULES)):
        release = installed_release_dir(package, active.get(package))
        if release and str(release) not in sys.path:
            sys.path.insert(0, str(release))


if __name__ == "__main__":
    configure_music_package_paths()
    print("Music library paths:", *[p for p in sys.path if "music_packages" in p], sep="\n  ")
