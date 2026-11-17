"""PyInstaller runtime hook: activate external yt-dlp/ytmusicapi before imports."""
from music_package_bootstrap import configure_music_package_paths

configure_music_package_paths()
