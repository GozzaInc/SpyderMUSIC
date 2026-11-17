"""Single PyAV decode for seek RMS and cached FFT. No mpv/live-VU changes."""
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
import numpy as np
from PySide6.QtCore import QThread, Signal
from fft_cache import FFTCache, analyze_pcm, file_identity

BINS = 2000

def rms_key(filename):
    st = os.stat(filename)
    return hashlib.sha256(f'{os.path.normcase(os.path.abspath(filename))}|{st.st_size}|{st.st_mtime_ns}|rms-v1'.encode()).hexdigest()

class RMSWorker(QThread):
    chunk = Signal(str, int, object)
    done = Signal(str, object)
    failed = Signal(str, str)
    fft_done = Signal(str, object)

    def __init__(self, filename, key, db_path, fft_key, bins=BINS):
        super().__init__()
        self.filename, self.key, self.bins = filename, key, bins
        self.db_path, self.fft_key = db_path, fft_key
        self.fft_result = None
        self.cancelled = False

    def run(self):
        try:
            import av
            with av.open(self.filename) as container:
                stream = next((s for s in container.streams if s.type == 'audio'), None)
                if stream is None:
                    raise ValueError('No audio stream')
                rate = stream.codec_context.sample_rate
                duration = (float(stream.duration * stream.time_base)
                            if stream.duration is not None else
                            float(container.duration / av.time_base)
                            if container.duration is not None else None)
                if not rate or not duration or duration <= 0:
                    raise ValueError('Missing sample rate or duration')
                samples_per_bin = max(1, int(np.ceil(duration * rate / self.bins)))
                squares = np.zeros(self.bins, dtype=np.float64)
                counts = np.zeros(self.bins, dtype=np.int64)
                resampler = av.AudioResampler(format='fltp', layout='mono', rate=rate)
                fft_resampler = av.AudioResampler(format='fltp', layout='mono', rate=48000)
                fft_chunks = []
                processed, sent, last_emit = 0, 0, time.perf_counter()

                def consume(frame):
                    nonlocal processed
                    pcm = frame.to_ndarray().reshape(-1).astype(np.float32, copy=False)
                    pos = 0
                    while pos < pcm.size:
                        idx = processed // samples_per_bin
                        count = min(pcm.size - pos, samples_per_bin - processed % samples_per_bin)
                        if idx < self.bins:
                            segment = pcm[pos:pos + count]
                            squares[idx] += float(np.sum(segment * segment, dtype=np.float64))
                            counts[idx] += count
                        processed += count
                        pos += count

                def publish(force=False):
                    nonlocal sent, last_emit
                    ready = min(self.bins, (processed + samples_per_bin - 1) // samples_per_bin if force
                                else processed // samples_per_bin)
                    now = time.perf_counter()
                    if ready > sent and (force or now - last_emit >= 0.035):
                        values = np.sqrt(np.divide(squares[sent:ready], counts[sent:ready],
                                                  out=np.zeros(ready - sent), where=counts[sent:ready] > 0))
                        self.chunk.emit(self.key, sent, values.astype(np.float32))
                        sent, last_emit = ready, now

                for packet in container.demux(stream):
                    if self.cancelled:
                        return
                    for frame in packet.decode():
                        if self.cancelled:
                            return
                        for mono in resampler.resample(frame):
                            consume(mono)
                        for fft_frame in fft_resampler.resample(frame):
                            fft_chunks.append(np.asarray(fft_frame.to_ndarray(), dtype=np.float32).reshape(-1).copy())
                        publish()
                for mono in resampler.resample(None):
                    consume(mono)
                for fft_frame in fft_resampler.resample(None):
                    fft_chunks.append(np.asarray(fft_frame.to_ndarray(), dtype=np.float32).reshape(-1).copy())
                publish(True)
                all_values = np.sqrt(np.divide(squares, counts, out=np.zeros(self.bins), where=counts > 0))
                rms = all_values.astype(np.float32)
                # Publish the complete seek waveform BEFORE the potentially expensive FFT.
                self.done.emit(self.key, rms)
                if self.cancelled:
                    return
                with sqlite3.connect(self.db_path, timeout=10) as db:
                    db.execute('CREATE TABLE IF NOT EXISTS waveform_rms (cache_key TEXT PRIMARY KEY, bins INTEGER NOT NULL, data BLOB NOT NULL)')
                    db.execute('INSERT OR REPLACE INTO waveform_rms VALUES (?,?,?)',
                               (self.key, self.bins, rms.astype('<f4', copy=False).tobytes()))
                if fft_chunks and not self.cancelled:
                    result = analyze_pcm(np.concatenate(fft_chunks))
                    if not self.cancelled:
                        FFTCache(self.db_path).save(self.fft_key, result)
                        self.fft_result = result
                        self.fft_done.emit(self.fft_key, result)
        except Exception as exc:
            self.failed.emit(self.key, str(exc))


class SharedAudioAnalysis:
    """One in-flight decode per file, shared by the Qt seek view and engine FFT."""
    def __init__(self, database_path):
        self.database_path = str(database_path)
        self.lock = threading.RLock()
        self.workers = {}
        self.fft_data = {}
        self.fft_cache = None

    def request(self, filename):
        path = os.path.normcase(os.path.abspath(str(filename)))
        if not os.path.isfile(path):
            return None, None
        key = rms_key(path)
        fft_key = file_identity(path)
        with self.lock:
            worker = self.workers.get(fft_key)
            if worker is not None:
                if worker.isFinished() and worker.fft_result is None:
                    self.workers.pop(fft_key, None)
                else:
                    return worker, key
            # Reuse completed FFT without decoding, if both cache records exist.
            try:
                if self.fft_cache is None:
                    self.fft_cache = FFTCache(self.database_path)
                fft = self.fft_cache.load(fft_key)
                with sqlite3.connect(self.database_path, timeout=10) as db:
                    row = db.execute('SELECT data FROM waveform_rms WHERE cache_key=? AND bins=?', (key, BINS)).fetchone()
                if fft is not None and row and len(row[0]) == BINS * 4:
                    self.fft_data[fft_key] = fft
                    return None, key
            except sqlite3.Error:
                pass
            worker = RMSWorker(path, key, self.database_path, fft_key)
            self.workers[fft_key] = worker
            # No QObject slot/event-loop dependency: store FFT in worker before emitting.
            worker.start()
            return worker, key

    def fft_at(self, key, seconds):
        from fft_cache import frame_at
        if not key:
            return None
        with self.lock:
            data = self.fft_data.get(key)
            if data is None:
                worker = self.workers.get(key)
                if worker is not None:
                    data = worker.fft_result
                    if data is not None:
                        self.fft_data[key] = data
        if data is None:
            return None
        return frame_at(data, seconds)

    def shutdown(self):
        with self.lock:
            workers = list(self.workers.values())
        for worker in workers:
            worker.cancelled = True
        for worker in workers:
            worker.wait(3000)

_instances = {}
_instances_lock = threading.Lock()
def shared_analysis(database_path):
    key = os.path.normcase(os.path.abspath(str(database_path)))
    with _instances_lock:
        if key not in _instances:
            _instances[key] = SharedAudioAnalysis(key)
        return _instances[key]
