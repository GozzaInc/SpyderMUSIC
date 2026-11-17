"""Local playlist playback adapter for the existing SpyderMUSIC MPVEngine.

No YouTube Music resolution, downloads, ChromaDB writes, or second player.
The UI/mode switch is intentionally deferred to the next implementation step.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import json
import random
import threading
import time
from typing import Callable

from playlist_manager import list_playlist_tracks



def _probe_duration(path: str) -> float:
    """Probe local audio without downloading or decoding it into memory."""
    probes = [shutil.which('ffprobe')]
    here = os.path.dirname(os.path.abspath(__file__))
    probes += [os.path.join(here, 'bin', 'ffprobe.exe'), os.path.join(here, 'ffprobe.exe')]
    for executable in probes:
        if not executable or not os.path.isfile(executable):
            continue
        try:
            result = subprocess.run(
                [executable, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', path],
                capture_output=True, text=True, timeout=8,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            duration = float(result.stdout.strip())
            if 0 < duration < 86400 * 7:
                return duration
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    # Mutagen is optional and can read many common formats without ffprobe.
    try:
        from mutagen import File
        audio = File(path)
        duration = float(audio.info.length) if audio and audio.info else 0.0
        if 0 < duration < 86400 * 7:
            return duration
    except (ImportError, OSError, ValueError, AttributeError):
        pass
    return 0.0

class OfflinePlaybackController:
    def __init__(self, player, connection_factory: Callable, *, crossfade_seconds: Callable[[], float] = lambda: 5.0,
                 on_track: Callable[[dict], None] | None = None,
                 repeat_enabled: Callable[[], bool] = lambda: False,
                 stop_after_enabled: Callable[[], bool] = lambda: False,
                 on_stop_after: Callable[[], None] | None = None):
        self.player = player
        self.connection_factory = connection_factory
        self.crossfade_seconds = crossfade_seconds
        self.on_track = on_track
        self.repeat_enabled = repeat_enabled
        self.stop_after_enabled = stop_after_enabled
        self.on_stop_after = on_stop_after
        self._lock = threading.RLock()
        self._tracks: list[dict] = []
        self._index = -1
        self._generation = 0
        self._active = False
        self._monitor = None
        self._shuffle = False
        self._play_order = []
        self._history = []
        self._local_queue = []
        self._playlist_id = None
        self._base_count = 0
        self._played_indices = set()

    def load_playlist(self, playlist_id: int) -> int:
        with self.connection_factory() as conn:
            tracks = list_playlist_tracks(conn, playlist_id)
        # Missing files are excluded at playback time as well, since availability can change.
        with self._lock:
            self._generation += 1
            self._tracks = tracks
            self._playlist_id = playlist_id
            self._base_count = len(tracks)
            self._play_order = list(range(len(tracks)))
            self._history.clear()
            self._played_indices.clear()
            self._local_queue.clear()
            self._index = -1
            self._active = False
        return len(tracks)

    def _valid(self, track: dict) -> bool:
        return bool(track.get('available') and os.path.isfile(track['path']))

    def _find_next(self, start: int) -> int | None:
        for idx in range(start, len(self._tracks)):
            if self._valid(self._tracks[idx]):
                return idx
        return None

    def set_shuffle(self, enabled: bool) -> None:
        """Shuffle remaining playback only; never rewrite SQLite order."""
        with self._lock:
            self._shuffle = bool(enabled)
            remaining = [i for i in range(self._base_count)
                         if i != self._index and i not in self._played_indices]
            if self._shuffle:
                random.shuffle(remaining)
            else:
                remaining.sort()
            self._play_order = remaining

    def enqueue_local(self, tracks: list[dict], *, first: bool = False) -> None:
        """Local queue contains concrete paths, never DJ/YouTube track names."""
        with self._lock:
            items = [dict(t) for t in tracks if self._valid(t)]
            if first:
                self._local_queue = items + self._local_queue
            else:
                self._local_queue.extend(items)

    def _next_index(self) -> int | None:
        while self._play_order:
            idx = self._play_order.pop(0)
            if idx != self._index and self._valid(self._tracks[idx]):
                return idx
        return None


    def _start_index(self, index: int, *, crossfade: bool) -> dict:
        track = self._tracks[index]
        duration = float(track.get('duration') or 0)
        if duration <= 0:
            duration = _probe_duration(track['path'])
            if duration > 0:
                track['duration'] = duration
                with self.connection_factory() as conn:
                    conn.execute('UPDATE local_library SET duration=? WHERE id=?',
                                 (duration, track['audio_id']))
        if crossfade and self._active:
            self.player.load_next(track['path'])
            self.player.trigger_transition(duration, crossfade_sec=max(0.1, float(self.crossfade_seconds())))
        else:
            self.player.play(track['path'], duration)
        self._index = index
        self._active = True
        if self.on_track:
            self.on_track(dict(track))
        return dict(track)

    def play(self, index: int = 0) -> dict:
        with self._lock:
            if not 0 <= index < len(self._tracks):
                raise IndexError('Playlist position out of range')
            found = self._find_next(index)
            if found is None:
                raise FileNotFoundError('No available audio files at or after the selected position')
            self._generation += 1
            track = self._start_index(found, crossfade=False)
            self._history = []
            self._played_indices = set(range(found + 1)) if not self._shuffle else {found}
            self.set_shuffle(self._shuffle)
            generation = self._generation
            self._monitor = threading.Thread(target=self._watch, args=(generation,), daemon=True)
            self._monitor.start()
            return track

    def previous(self) -> dict | None:
        with self._lock:
            if not self._active:
                return None
            if self._history:
                idx = self._history.pop()
                if self._valid(self._tracks[idx]):
                    self._play_order.insert(0, self._index)
                    return self._start_index(idx, crossfade=False)
            self.player.seek_absolute(0)
            return dict(self._tracks[self._index])

    def next(self, *, crossfade: bool = True) -> dict | None:
        with self._lock:
            if not self._active:
                return None
            if self._local_queue:
                queued = self._local_queue.pop(0)
                # Queue items can belong to another playlist; keep their path and
                # return to the original playlist after playback.
                self._history.append(self._index)
                duration = float(queued.get('duration') or 0) or _probe_duration(queued['path'])
                if crossfade:
                    self.player.load_next(queued['path'])
                    self.player.trigger_transition(duration, crossfade_sec=max(0.1, float(self.crossfade_seconds())))
                else:
                    self.player.play(queued['path'], duration)
                queued['duration'] = duration
                if self.on_track:
                    self.on_track(dict(queued))
                # This item becomes the current track for end monitoring.
                self._tracks.append(queued)
                self._index = len(self._tracks) - 1
                return dict(queued)
            found = self._next_index()
            if found is None:
                self._active = False
                self._generation += 1
                self.player.stop()
                return None
            self._history.append(self._index)
            self._played_indices.add(found)
            return self._start_index(found, crossfade=crossfade)

    def deactivate(self) -> None:
        """Release playlist ownership without stopping a fade already in progress."""
        with self._lock:
            self._generation += 1
            self._active = False

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            self._active = False
            self.player.stop()

    def status(self) -> dict:
        with self._lock:
            return {'active': self._active, 'index': self._index, 'total': len(self._tracks),
                    'track': dict(self._tracks[self._index]) if 0 <= self._index < len(self._tracks) else None}

    def _watch(self, generation: int) -> None:
        """Advance on known duration. Unknown duration requires manual next until metadata probing exists."""
        while True:
            time.sleep(0.25)
            with self._lock:
                if generation != self._generation or not self._active:
                    return
                track = self._tracks[self._index]
                duration = float(track.get('duration') or 0)
                if duration <= 0:
                    duration = float(getattr(self.player, 'current_duration', 0) or 0)
                if duration <= 0 or getattr(self.player, 'is_paused', False):
                    continue
                elapsed = self.player.get_elapsed()
                # The MPV native loop keeps the current track alive. Do not
                # automatically advance a playlist while Repeat One is enabled.
                if getattr(self.player, 'is_paused', False) or self.repeat_enabled():
                    continue
                fade = max(0.1, float(self.crossfade_seconds()))
                if elapsed <= 0 or elapsed < max(0.1, duration - fade):
                    continue
                if self.stop_after_enabled():
                    # Wait for the end of the track, not the crossfade threshold.
                    # MPV may reach EOF before the estimated duration exactly.
                    # Never require a timestamp that the player cannot report.
                    if elapsed < max(0.1, duration - 0.35):
                        continue
                    self.stop()
                    if self.on_stop_after:
                        self.on_stop_after()
                    return
                # Avoid triggering a second transition for the same track.
                self.next(crossfade=True)
