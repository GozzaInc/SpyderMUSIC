"""Offline FFT analysis + SQLite cache for SpyderMUSIC.

Run analyze_pcm() in a worker, never on the GUI thread. Playback only calls
load()/frame_at() and applies its own smoothing. Audio must be mono float32 PCM
at the declared sample rate, aligned to track time zero.
"""
from __future__ import annotations
import hashlib
import io
import sqlite3
import threading
import zlib
from pathlib import Path
import numpy as np

VERSION = 1
SAMPLE_RATE = 48000
FFT_SAMPLES = 8192
HOP_SAMPLES = 2400  # 50 ms at 48 kHz, 20 frames/s
BANDS = ((20, 250), (250, 4000), (4000, 16000))


def analyze_pcm(pcm, sample_rate=SAMPLE_RATE, fft_samples=FFT_SAMPLES,
                hop_samples=HOP_SAMPLES, bars=64, waveform_points=256):
    """Return UNSMOOTHED arrays, one row per time step, including t=0.

    FFT window is centered on each frame time and zero-padded at edges.
    Processing in bounded batches prevents an oversized temporary array.
    """
    pcm = np.asarray(pcm, dtype=np.float32)
    if pcm.ndim != 1 or not np.all(np.isfinite(pcm)):
        raise ValueError('PCM must be finite mono float32 samples')
    if sample_rate <= 0 or fft_samples <= 0 or hop_samples <= 0:
        raise ValueError('Invalid audio geometry')
    if not 8 <= bars <= 128 or not 32 <= waveform_points <= 1024:
        raise ValueError('Invalid display geometry')
    if pcm.size == 0:
        raise ValueError('Empty PCM')
    times = np.arange(0, pcm.size, hop_samples, dtype=np.int64)
    window = np.hanning(fft_samples).astype(np.float32)
    norm = 2.0 / window.sum()
    freq = np.fft.rfftfreq(fft_samples, 1 / sample_rate)
    edges = np.geomspace(25, min(18000, sample_rate * .48), bars + 1)
    df = sample_rate / fft_samples
    low = np.maximum(0.0, freq - df * .5)
    high = freq + df * .5
    overlap = np.maximum(0., np.minimum(edges[1:, None], high[None, :]) -
                         np.maximum(edges[:-1, None], low[None, :]))
    weights = (overlap / np.maximum(overlap.sum(axis=1, keepdims=True), 1e-12)).astype(np.float32)
    masks = [(freq >= lo) & (freq < hi) for lo, hi in BANDS]
    spectrum = np.empty((len(times), bars), dtype=np.float32)
    bands = np.empty((len(times), 3), dtype=np.float32)
    waveform = np.empty((len(times), waveform_points), dtype=np.float32)
    offsets = np.arange(fft_samples, dtype=np.int64) - fft_samples // 2
    wave_offsets = np.linspace(0, fft_samples - 1, waveform_points)
    for start in range(0, len(times), 32):
        stop = min(start + 32, len(times))
        indices = times[start:stop, None] + offsets[None, :]
        samples = np.zeros(indices.shape, dtype=np.float32)
        valid = (indices >= 0) & (indices < pcm.size)
        samples[valid] = pcm[indices[valid]]
        magnitude = np.abs(np.fft.rfft(samples * window, axis=1)) * norm
        power = magnitude.astype(np.float32) ** 2
        amp = np.sqrt(power @ weights.T)
        spectrum[start:stop] = np.clip((20 * np.log10(np.maximum(amp, 1e-9)) + 75) / 60, 0, 1)
        for j, mask in enumerate(masks):
            rms = np.sqrt(np.mean(power[:, mask], axis=1)) if np.any(mask) else np.zeros(stop-start)
            bands[start:stop, j] = np.clip((20 * np.log10(np.maximum(rms, 1e-9)) + 65) / 45, 0, 1)
        x = np.arange(fft_samples)
        for k, row in enumerate(samples):
            waveform[start+k] = np.clip(np.interp(wave_offsets, x, row), -1, 1)
    return {'spectrum': spectrum, 'bands': bands, 'waveform': waveform,
            'sample_rate': sample_rate, 'fft_samples': fft_samples,
            'hop_samples': hop_samples, 'bars': bars, 'waveform_points': waveform_points}


def frame_at(data, seconds):
    """O(1) frame lookup. No FFT, disk I/O or interpolation."""
    index = max(0, min(len(data['bands']) - 1,
                       int(round(max(0., float(seconds)) * data['sample_rate'] / data['hop_samples']))))
    return {key: data[key][index].copy() for key in ('bands', 'spectrum', 'waveform')}


def file_identity(path):
    """Fast invalidation key, not a cryptographic content fingerprint."""
    p = Path(path).resolve()
    st = p.stat()
    return hashlib.sha256(f'{p}|{st.st_size}|{st.st_mtime_ns}'.encode()).hexdigest()


class FFTCache:
    def __init__(self, database_path):
        self.database_path = str(database_path)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS fft_analysis_cache (
                track_key TEXT PRIMARY KEY, version INTEGER NOT NULL,
                sample_rate INTEGER NOT NULL, fft_samples INTEGER NOT NULL,
                hop_samples INTEGER NOT NULL, bars INTEGER NOT NULL,
                waveform_points INTEGER NOT NULL, payload BLOB NOT NULL)''')

    def _connect(self):
        return sqlite3.connect(self.database_path, timeout=10)

    def save(self, track_key, data):
        """Atomic replace; call from a worker after complete offline analysis."""
        if not track_key:
            raise ValueError('Missing track key')
        buff = io.BytesIO()
        np.savez(buff, bands=data['bands'], spectrum=data['spectrum'], waveform=data['waveform'])
        payload = zlib.compress(buff.getvalue(), level=3)
        with self._lock, self._connect() as db:
            db.execute('''INSERT OR REPLACE INTO fft_analysis_cache VALUES (?,?,?,?,?,?,?,?)''',
                       (track_key, VERSION, int(data['sample_rate']), int(data['fft_samples']),
                        int(data['hop_samples']), int(data['bars']), int(data['waveform_points']), payload))

    def load(self, track_key, sample_rate=SAMPLE_RATE, fft_samples=FFT_SAMPLES,
             hop_samples=HOP_SAMPLES, bars=64, waveform_points=256):
        """Load once at track start in a worker; hold arrays in RAM during playback."""
        with self._lock, self._connect() as db:
            row = db.execute('''SELECT version,sample_rate,fft_samples,hop_samples,bars,
                                waveform_points,payload FROM fft_analysis_cache WHERE track_key=?''',
                             (track_key,)).fetchone()
        if not row or tuple(row[:6]) != (VERSION, sample_rate, fft_samples, hop_samples, bars, waveform_points):
            return None
        try:
            with np.load(io.BytesIO(zlib.decompress(row[6])), allow_pickle=False) as arrays:
                data = {key: arrays[key].astype(np.float32, copy=True)
                        for key in ('bands', 'spectrum', 'waveform')}
            if (data['bands'].ndim != 2 or data['bands'].shape[1] != 3 or
                data['spectrum'].ndim != 2 or data['spectrum'].shape[1] != bars or
                data['waveform'].ndim != 2 or data['waveform'].shape[1] != waveform_points or
                not len(data['bands']) or len(data['bands']) != len(data['spectrum']) or
                len(data['bands']) != len(data['waveform'])):
                return None
            data.update(sample_rate=sample_rate, fft_samples=fft_samples,
                        hop_samples=hop_samples, bars=bars, waveform_points=waveform_points)
            return data
        except (OSError, ValueError, KeyError, zlib.error):
            return None
