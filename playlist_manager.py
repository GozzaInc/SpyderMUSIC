"""Persistent local playlists for SpyderMUSIC.

Only references local_library.id; never deletes or copies audio files.
All mutations use the caller's SQLite transaction and can be rolled back.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from music_library import add_audio_file


def setup_playlists(conn: sqlite3.Connection) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS local_playlists (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS local_playlist_items (
        id INTEGER PRIMARY KEY,
        playlist_id INTEGER NOT NULL REFERENCES local_playlists(id) ON DELETE CASCADE,
        audio_id INTEGER NOT NULL REFERENCES local_library(id),
        position INTEGER NOT NULL,
        added_at REAL NOT NULL,
        UNIQUE(playlist_id, position)
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_local_playlist_items_audio ON local_playlist_items(audio_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_local_playlist_items_order ON local_playlist_items(playlist_id, position)')
    # Foreign keys are connection-local in SQLite, so enable for each connection.
    conn.execute('PRAGMA foreign_keys=ON')


def _name(value: str) -> str:
    name = str(value).strip()
    if not name or len(name) > 200:
        raise ValueError('Playlist name must contain 1 to 200 characters')
    return name


def _require_playlist(conn: sqlite3.Connection, playlist_id: int) -> None:
    if not conn.execute('SELECT 1 FROM local_playlists WHERE id=?', (playlist_id,)).fetchone():
        raise ValueError(f'Unknown playlist: {playlist_id}')


def create_playlist(conn: sqlite3.Connection, name: str) -> int:
    now = time.time()
    cur = conn.execute('INSERT INTO local_playlists(name,created_at,updated_at) VALUES(?,?,?)',
                       (_name(name), now, now))
    return cur.lastrowid


def rename_playlist(conn: sqlite3.Connection, playlist_id: int, name: str) -> None:
    _require_playlist(conn, playlist_id)
    conn.execute('UPDATE local_playlists SET name=?, updated_at=? WHERE id=?',
                 (_name(name), time.time(), playlist_id))


def delete_playlist(conn: sqlite3.Connection, playlist_id: int) -> None:
    _require_playlist(conn, playlist_id)
    # Explicit deletion works even if caller's connection has foreign_keys disabled.
    conn.execute('DELETE FROM local_playlist_items WHERE playlist_id=?', (playlist_id,))
    conn.execute('DELETE FROM local_playlists WHERE id=?', (playlist_id,))


def list_playlists(conn: sqlite3.Connection) -> list[dict]:
    return [dict(zip(('id', 'name', 'track_count', 'created_at', 'updated_at'), row))
            for row in conn.execute('''SELECT p.id,p.name,COUNT(i.id),p.created_at,p.updated_at
                FROM local_playlists p LEFT JOIN local_playlist_items i ON i.playlist_id=p.id
                GROUP BY p.id ORDER BY p.name COLLATE NOCASE''')]


def list_playlist_tracks(conn: sqlite3.Connection, playlist_id: int) -> list[dict]:
    _require_playlist(conn, playlist_id)
    fields = ('item_id','position','audio_id','path','artist','title','duration','available','track_key')
    return [dict(zip(fields, row)) for row in conn.execute('''
        SELECT i.id,i.position,l.id,l.path,l.artist,l.title,l.duration,l.available,l.track_key
        FROM local_playlist_items i JOIN local_library l ON l.id=i.audio_id
        WHERE i.playlist_id=? ORDER BY i.position,i.id''', (playlist_id,))]


def _touch(conn: sqlite3.Connection, playlist_id: int) -> None:
    conn.execute('UPDATE local_playlists SET updated_at=? WHERE id=?', (time.time(), playlist_id))


def _set_order(conn: sqlite3.Connection, playlist_id: int, item_ids: list[int]) -> None:
    # Two-pass numbering avoids UNIQUE(playlist_id, position) collisions.
    for idx, item_id in enumerate(item_ids):
        conn.execute('UPDATE local_playlist_items SET position=? WHERE id=? AND playlist_id=?',
                     (-idx - 1, item_id, playlist_id))
    for idx, item_id in enumerate(item_ids):
        conn.execute('UPDATE local_playlist_items SET position=? WHERE id=? AND playlist_id=?',
                     (idx, item_id, playlist_id))
    _touch(conn, playlist_id)


def add_track(conn: sqlite3.Connection, playlist_id: int, audio_id: int, position: int | None = None) -> int:
    _require_playlist(conn, playlist_id)
    if not conn.execute('SELECT 1 FROM local_library WHERE id=?', (audio_id,)).fetchone():
        raise ValueError(f'Unknown local audio ID: {audio_id}')
    existing = [row['item_id'] for row in list_playlist_tracks(conn, playlist_id)]
    if position is None:
        position = len(existing)
    position = max(0, min(int(position), len(existing)))
    # Append first, then reorder, to keep existing playlist item IDs stable.
    cur = conn.execute('INSERT INTO local_playlist_items(playlist_id,audio_id,position,added_at) VALUES(?,?,?,?)',
                       (playlist_id, audio_id, len(existing), time.time()))
    existing.insert(position, cur.lastrowid)
    _set_order(conn, playlist_id, existing)
    return cur.lastrowid


def remove_items(conn: sqlite3.Connection, playlist_id: int, item_ids: list[int]) -> int:
    _require_playlist(conn, playlist_id)
    ids = set(int(i) for i in item_ids)
    existing = [row['item_id'] for row in list_playlist_tracks(conn, playlist_id)]
    to_remove = ids.intersection(existing)
    if not to_remove:
        return 0
    conn.executemany('DELETE FROM local_playlist_items WHERE playlist_id=? AND id=?',
                     [(playlist_id, item_id) for item_id in to_remove])
    _set_order(conn, playlist_id, [i for i in existing if i not in to_remove])
    return len(to_remove)


def reorder_playlist(conn: sqlite3.Connection, playlist_id: int, item_ids: list[int]) -> None:
    existing = [row['item_id'] for row in list_playlist_tracks(conn, playlist_id)]
    if len(item_ids) != len(existing) or set(item_ids) != set(existing) or len(set(item_ids)) != len(item_ids):
        raise ValueError('Order must contain every playlist item ID exactly once')
    _set_order(conn, playlist_id, item_ids)


def duplicate_playlist(conn: sqlite3.Connection, playlist_id: int, new_name: str) -> int:
    tracks = list_playlist_tracks(conn, playlist_id)
    new_id = create_playlist(conn, new_name)
    for track in tracks:
        add_track(conn, new_id, track['audio_id'])
    return new_id


def export_m3u8(conn: sqlite3.Connection, playlist_id: int, destination: str | os.PathLike) -> int:
    """Write a UTF-8 M3U8 using relative paths when possible."""
    tracks = list_playlist_tracks(conn, playlist_id)
    target = Path(destination).expanduser().resolve()
    lines = ['#EXTM3U']
    for track in tracks:
        duration = int(track['duration']) if track['duration'] is not None else -1
        label = f"{track['artist']} - {track['title']}" if track['artist'] else track['title']
        lines.append(f'#EXTINF:{duration},{label.replace(chr(10), " ").replace(chr(13), " ")}')
        lines.append(os.path.relpath(track['path'], target.parent))
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return len(tracks)


def import_m3u(conn: sqlite3.Connection, source: str | os.PathLike, *, name: str | None = None) -> dict:
    """Import local paths from M3U/M3U8. URLs are skipped, never downloaded."""
    path = Path(source).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError('Not a playlist file')
    if path.suffix.lower() not in {'.m3u', '.m3u8'}:
        raise ValueError('Only M3U and M3U8 are supported')
    raw = path.read_bytes()
    if path.suffix.lower() == '.m3u8':
        content = raw.decode('utf-8-sig')
    else:
        try:
            content = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            content = raw.decode('cp1252')
    playlist_id = create_playlist(conn, name or path.stem)
    imported = skipped = 0
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '://' in line or line.lower().startswith(('file:', '\\\\')):
            skipped += 1
            continue
        candidate = Path(line)
        if not candidate.is_absolute():
            candidate = path.parent / candidate
        try:
            audio_id = add_audio_file(conn, candidate, source='playlist_import')
            add_track(conn, playlist_id, audio_id)
            imported += 1
        except (OSError, ValueError, sqlite3.Error):
            skipped += 1
    return {'playlist_id': playlist_id, 'imported': imported, 'skipped': skipped}
