"""Stage 13: bounded, timestamped Opus/WebM -> mono float32 PCM via PyAV.

Decoder work happens on a daemon thread, never on the Qt/UI thread.
No FFT or theme coupling. Call `window()` with the MPV playback position.
"""
from __future__ import annotations

from collections import deque
import os
import threading
import time
from typing import Callable

import numpy as np


class PCMDecoder:
    def __init__(self, position: Callable[[], float], *, sample_rate: int = 48000,
                 block_samples: int = 2048, ahead_seconds: float = 8.0,
                 history_seconds: float = 6.0):
        self.position = position
        self.sample_rate = sample_rate
        self.block_samples = block_samples
        self.ahead_seconds = ahead_seconds
        self.history_seconds = history_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._blocks = deque()  # (first sample index, contiguous mono float32 array)
        self._path = None
        self.error = None

    def start(self, path: str):
        self.stop()
        if not path or not os.path.isfile(path):
            return False
        self._path = os.path.abspath(path)
        self.error = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._path, self._stop),
                                        name='SpyderPCM', daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=1.5)
            self._thread = None
        with self._lock:
            self._blocks.clear()

    def window(self, seconds: float, samples: int = 2048):
        """Return a contiguous PCM window ending at seconds, or None if not buffered."""
        if samples <= 0:
            raise ValueError('samples must be positive')
        end = max(0, round(float(seconds) * self.sample_rate))
        start = end - samples
        if start < 0:
            return None
        with self._lock:
            blocks = list(self._blocks)
        out = np.empty(samples, dtype=np.float32)
        covered = 0
        for first, data in blocks:
            lo, hi = max(start, first), min(end, first + len(data))
            if lo < hi:
                out[lo - start:hi - start] = data[lo - first:hi - first]
                covered += hi - lo
        return out if covered == samples else None

    def _run(self, path, stop):
        try:
            import av  # optional runtime dependency; only needed when decoding
            with av.open(path) as container:
                stream = next((s for s in container.streams if s.type == 'audio'), None)
                if stream is None:
                    raise ValueError('No audio stream found')
                resampler = av.audio.resampler.AudioResampler(
                    format='fltp', layout='mono', rate=self.sample_rate)
                pending = np.empty(0, dtype=np.float32)
                sample_cursor = 0
                decoder = iter(container.decode(stream))
                while not stop.is_set():
                    position = max(0.0, float(self.position()))
                    play_sample = int(position * self.sample_rate)
                    # On large seeks, discard old audio and restart near the playhead.
                    with self._lock:
                        oldest = self._blocks[0][0] if self._blocks else None
                    # A decoder cursor ahead of playback is normal prebuffering,
                    # not a seek. Only restart if the playhead is outside the
                    # actual retained history or jumps beyond decoded audio.
                    if (sample_cursor is not None and (
                            (oldest is not None and play_sample < oldest - int(0.5 * self.sample_rate))
                            or play_sample > sample_cursor + int(2 * self.sample_rate))):
                        target = max(0, position - 0.25)
                        container.seek(int(target / stream.time_base), stream=stream,
                                       backward=True, any_frame=False)
                        decoder = iter(container.decode(stream))
                        resampler = av.audio.resampler.AudioResampler(
                            format='fltp', layout='mono', rate=self.sample_rate)
                        pending = np.empty(0, dtype=np.float32)
                        sample_cursor = None
                        with self._lock:
                            self._blocks.clear()
                    if sample_cursor is not None and sample_cursor > play_sample + int(self.ahead_seconds * self.sample_rate):
                        stop.wait(0.025)
                        continue
                    try:
                        frame = next(decoder)
                    except StopIteration:
                        break
                    # Use decoded frame PTS to anchor the PCM timeline (also after seeking).
                    if sample_cursor is None:
                        pts = float(frame.pts * frame.time_base) if frame.pts is not None else position
                        sample_cursor = max(0, round(pts * self.sample_rate))
                    for converted in resampler.resample(frame):
                        array = np.asarray(converted.to_ndarray(), dtype=np.float32).reshape(-1)
                        if not len(array):
                            continue
                        pending = np.concatenate((pending, array))
                        while len(pending) >= self.block_samples:
                            block = pending[:self.block_samples].copy()
                            pending = pending[self.block_samples:]
                            with self._lock:
                                self._blocks.append((sample_cursor, block))
                                cutoff = play_sample - int(self.history_seconds * self.sample_rate)
                                while self._blocks and self._blocks[0][0] + len(self._blocks[0][1]) < cutoff:
                                    self._blocks.popleft()
                            sample_cursor += len(block)
                # Tail is intentionally omitted; fixed-size analysis blocks only.
        except Exception as exc:
            self.error = f'{type(exc).__name__}: {exc}'
