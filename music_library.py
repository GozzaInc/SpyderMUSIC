"""Local audio catalogue for SpyderMUSIC.

No network calls, audio decoding, ChromaDB mutations, or playback side effects.
The catalogue is deliberately separate from the existing preferences table.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Iterable

AUDIO_EXTENSIONS = frozenset({'.webm', '.mp3', '.flac', '.ogg', '.opus', '.wav', '.m4a', '.aac', '.wma'})


def setup_library(conn: sqlite3.Connection) -> None:
    """Create additive tables in the active music.db; safe to call repeatedly."""
    conn.execute('''CREATE TABLE IF NOT EXISTS local_library (
        id INTEGER PRIMARY KEY,
        path TEXT NOT NULL UNIQUE,
        track_key TEXT COLLATE NOCASE,
        title TEXT NOT NULL,
        artist TEXT NOT NULL DEFAULT '',
        duration REAL,
        size_bytes INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        available INTEGER NOT NULL DEFAULT 1,
        source TEXT NOT NULL DEFAULT 'import',
        imported_at REAL NOT NULL,
        checked_at REAL NOT NULL
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_local_library_track ON local_library(track_key)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_local_library_available ON local_library(available)')


def _metadata_from_name(path: Path) -> tuple[str, str, str | None]:
    stem = path.stem.strip()
    if ' - ' in stem:
        artist, title = stem.split(' - ', 1)
        artist, title = artist.strip(), title.strip()
        if artist and title:
            return artist, title, f'{artist} - {title}'
    return '', stem or path.name, None


def add_audio_file(conn: sqlite3.Connection, file_path: str | os.PathLike,
                   *, source: str = 'import', track_key: str | None = None) -> int:
    """Register an existing file, without copying it or modifying preferences.

    File identity is its canonical absolute path. Reimport updates metadata without
    resetting an existing row ID (future playlist references stay valid).
    """
    path = Path(file_path).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
        raise ValueError(f'Not a supported audio file: {path}')
    stat = path.stat()
    artist, title, inferred_key = _metadata_from_name(path)
    key = track_key or inferred_key
    now = time.time()
    conn.execute('''INSERT INTO local_library
        (path, track_key, title, artist, size_bytes, mtime_ns, source, imported_at, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
          size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns,
          available=1, checked_at=excluded.checked_at,
          track_key=COALESCE(local_library.track_key, excluded.track_key)
    ''', (str(path), key, title, artist, stat.st_size, stat.st_mtime_ns,
          source, now, now))
    return conn.execute('SELECT id FROM local_library WHERE path=?', (str(path),)).fetchone()[0]


def scan_folder(conn: sqlite3.Connection, folder: str | os.PathLike,
                *, recursive: bool = True, source: str = 'import') -> dict[str, int]:
    """Index supported audio files; failures are counted, not silently discarded.

    This does NOT remove entries for missing files or traverse symlinked folders.
    """
    root = Path(folder).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    result = {'found': 0, 'registered': 0, 'errors': 0}
    def visit(directory: Path) -> None:
        try:
            entries = list(directory.iterdir())
        except OSError:
            result['errors'] += 1
            return
        for path in entries:
            try:
                if path.is_symlink():
                    continue
                if path.is_dir():
                    if recursive:
                        visit(path)
                elif path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                    result['found'] += 1
                    try:
                        add_audio_file(conn, path, source=source)
                        result['registered'] += 1
                    except (OSError, ValueError, sqlite3.Error):
                        result['errors'] += 1
            except OSError:
                result['errors'] += 1
    visit(root)
    return result


def refresh_availability(conn: sqlite3.Connection) -> dict[str, int]:
    """Mark missing files unavailable without deleting library/playlist identity."""
    present = missing = 0
    for file_id, path, old in conn.execute('SELECT id, path, available FROM local_library').fetchall():
        exists = Path(path).is_file()
        present += int(exists)
        missing += int(not exists)
        if int(bool(old)) != int(exists):
            conn.execute('UPDATE local_library SET available=?, checked_at=? WHERE id=?',
                         (int(exists), time.time(), file_id))
    return {'available': present, 'missing': missing}


def list_audio(conn: sqlite3.Connection, *, available_only: bool = True,
               search: str = '', limit: int = 1000) -> list[dict]:
    """Return local catalogue records, never online-only preferences."""
    sql = 'SELECT id, path, track_key, title, artist, duration, available, source FROM local_library WHERE 1=1'
    params: list = []
    if available_only:
        sql += ' AND available=1'
    if search:
        sql += " AND (title LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\')"
        escaped = search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        params += [f'%{escaped}%', f'%{escaped}%']
    sql += ' ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE, id LIMIT ?'
    params.append(max(1, min(int(limit), 10000)))
    fields = ('id', 'path', 'track_key', 'title', 'artist', 'duration', 'available', 'source')
    return [dict(zip(fields, row)) for row in conn.execute(sql, params)]
