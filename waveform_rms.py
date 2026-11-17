"""Isolated cached RMS waveform widget. Never touches MPV/PCM/FFT threads."""
import hashlib
import os
import sqlite3
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QImage
from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QWidget

from shared_audio_analysis import shared_analysis, rms_key

BINS = 2000
GAIN = 1.5




class RMSWaveform(QWidget):
    waveform_changed = Signal()
    """Second seek view: the existing QSlider and seek callback remain authoritative."""
    def __init__(self, slider, seek_callback, interaction_callback, cache_file, parent=None):
        super().__init__(parent)
        self.slider, self.seek_callback = slider, seek_callback
        self.interaction_callback = interaction_callback
        self.cache_file = str(cache_file)
        self.values = np.zeros(BINS, dtype=np.float32)
        self.ready = 0
        self._key = None
        self._worker = None
        self._workers = []
        self._analysis = shared_analysis(self.cache_file)
        self.setMinimumHeight(12)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.played_color = QColor('#ff465a')
        self.remaining_color = QColor('#f8b94f')
        self.cursor_color = QColor('#ffffff')
        self._waveform_version = 0
        self._cache_key = None
        self._cached_played = None
        self._cached_remaining = None
        self.setToolTip('RMS-Waveform: Click to seek (display x1.5)')
        self.slider.valueChanged.connect(self.update)
        self.slider.rangeChanged.connect(lambda *_: self.update())

    def set_theme_colors(self, played, remaining, cursor):
        self.played_color = QColor(played)
        self.remaining_color = QColor(remaining)
        self.cursor_color = QColor(cursor)
        self._cache_key = None
        self.update()

    def _db(self):
        Path(self.cache_file).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.cache_file, timeout=5)
        db.execute('CREATE TABLE IF NOT EXISTS waveform_rms (cache_key TEXT PRIMARY KEY, bins INTEGER NOT NULL, data BLOB NOT NULL)')
        return db

    def set_audio(self, filename):
        filename = str(filename or '')
        if not filename or not os.path.isfile(filename):
            if self._key is not None:
                self._key = None
                self.values.fill(0)
                self.ready = 0
                self._waveform_version += 1
                self.update()
            return
        key = rms_key(filename)
        if key == self._key:
            return
        self._key = key
        self.values.fill(0)
        self.ready = 0
        self._waveform_version += 1
        self.update()
        try:
            with self._db() as db:
                row = db.execute('SELECT data FROM waveform_rms WHERE cache_key=? AND bins=?', (key, BINS)).fetchone()
            if row:
                cached = np.frombuffer(row[0], dtype='<f4')
                if cached.size == BINS:
                    self.values[:] = cached
                    self.ready = BINS
                    self._waveform_version += 1
                    self.update()
                    return
        except (sqlite3.Error, ValueError) as exc:
            print('Waveform cache read:', exc)
        # Attach to the one shared worker; the engine's FFT request reuses it.
        worker, _ = self._analysis.request(filename)
        if worker is not None:
            self._worker = worker
            worker.chunk.connect(self._on_chunk)
            worker.done.connect(self._on_done)
            worker.failed.connect(self._on_error)

    def _on_chunk(self, key, start, values):
        if key != self._key:
            return
        end = min(BINS, start + len(values))
        self.values[start:end] = values[:end-start]
        self.ready = max(self.ready, end)
        self._waveform_version += 1
        self.update()
        self.waveform_changed.emit()

    def _on_done(self, key, values):
        if key != self._key:
            return
        self.values[:] = values
        self.ready = BINS
        self._waveform_version += 1
        self.update()
        self.waveform_changed.emit()
        # The shared worker commits RMS and FFT to the same database.

    def _on_error(self, key, message):
        if key == self._key:
            print('Waveform RMS analysis:', message)

    def _current_waveform(self):
        """Mirrors override this to share source data, not decoder work."""
        return self.values, self.ready, self._waveform_version

    def _waveform_textures(self, w, h, values, ready, revision):
        """Build two textures only when geometry, theme or waveform data change.

        Playback progress updates perform just two GPU texture draws and a
        vertical cursor instead of 1..2000 NumPy slices and drawLine calls.
        """
        key = (w, h, ready, revision, self.played_color.rgba(), self.remaining_color.rgba())
        if key == self._cache_key:
            return self._cached_played, self._cached_remaining
        self._cache_key = key
        self._cached_played = self._cached_remaining = None
        if w <= 0 or h <= 0 or ready <= 0:
            return None, None
        limit = min(w, int(np.ceil(w * ready / BINS)))
        mid = h / 2.0
        amplitudes = []
        for x in range(limit):
            a = int(x * BINS / w)
            b = min(ready, max(a+1, int((x+1) * BINS / w)))
            if a >= ready:
                break
            amplitudes.append(round(min(1.0, float(np.max(values[a:b])) * GAIN) * mid * .91))

        def make_layer(color):
            img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
            img.fill(Qt.transparent)
            painter = QPainter(img)
            painter.setPen(QPen(color, 1))
            cy = round(mid)
            for x, half in enumerate(amplitudes):
                painter.drawLine(x, round(mid-half), x, round(mid+half))
            painter.end()
            return QPixmap.fromImage(img)

        self._cached_played = make_layer(self.played_color)
        self._cached_remaining = make_layer(self.remaining_color)
        return self._cached_played, self._cached_remaining

    def paintEvent(self, event):
        self._render_waveform()

    def _render_waveform(self):
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        values, ready, revision = self._current_waveform()
        minimum, maximum = self.slider.minimum(), self.slider.maximum()
        progress = ((self.slider.value() - minimum) / (maximum - minimum)
                    if maximum > minimum else 0.0)
        progress_x = max(0, min(w-1, round(progress * max(0, w-1))))
        painter = QPainter(self)
        played, remaining = self._waveform_textures(w, h, values, ready, revision)
        if played is not None and remaining is not None:
            cut = progress_x + 1
            painter.drawPixmap(QRectF(0, 0, cut, h), played, QRectF(0, 0, cut, h))
            if cut < w:
                painter.drawPixmap(QRectF(cut, 0, w-cut, h), remaining,
                                   QRectF(cut, 0, w-cut, h))
        if maximum > minimum:
            painter.setPen(QPen(self.cursor_color, 1))
            painter.drawLine(progress_x, 0, progress_x, h)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._seek_at(event.position().x())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._seek_at(event.position().x())

    def _seek_at(self, x):
        self.interaction_callback()
        low, high = self.slider.minimum(), self.slider.maximum()
        if high <= low:
            return
        value = round(low + max(0, min(1, x/max(1, self.width()-1))) * (high-low))
        self.slider.setValue(value)
        self.seek_callback(value)

    def shutdown(self):
        # The shared service owns worker lifetime; no GUI-thread waits here.
        pass


class RMSWaveformMirror(RMSWaveform):
    """Extra RMS view shares the source decoder and its NumPy array, no worker."""
    def __init__(self, source, parent=None):
        # Do not invoke RMSWaveform.__init__: it would allocate a second cache/worker.
        QWidget.__init__(self, parent)
        self.source = source
        self.slider = source.slider
        self.seek_callback = source.seek_callback
        self.interaction_callback = source.interaction_callback
        self.values = source.values
        self.ready = source.ready
        self.played_color = QColor('#ff465a')
        self.remaining_color = QColor('#f8b94f')
        self.cursor_color = QColor('#ffffff')
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self._cache_key = None
        self._cached_played = None
        self._cached_remaining = None
        self.slider.valueChanged.connect(self.update)
        self.slider.rangeChanged.connect(lambda *_: self.update())
        source.waveform_changed.connect(self.refresh_source)

    def refresh_source(self):
        self.ready = self.source.ready
        self.update()

    def _current_waveform(self):
        return (self.source.values, self.source.ready,
                self.source._waveform_version)

    def set_audio(self, filename):
        pass  # Only the original RMSWaveform performs analysis.

    def shutdown(self):
        pass
