"""SpyderMUSIC declarative theme registry. No theme-provided executable code."""
import json
import os
import time
import re
import zipfile
from pathlib import Path
from PySide6.QtWidgets import QWidget, QGraphicsOpacityEffect, QGraphicsDropShadowEffect
# Shared GPU compositing is always active for eligible layers.
# Z-conflicting foreground widgets continue through the normal Qt path.

from PySide6.QtCore import QTimer, QPropertyAnimation, QEasingCurve, QParallelAnimationGroup, QRect, QRectF, QPoint, Qt, QObject, QEvent
from PySide6.QtGui import QPainter, QColor, QRadialGradient, QLinearGradient, QPen, QBrush, QFont, QPixmap, QMovie, QImageReader


def make_audio_gradient(config, rect, energy=1.0, intensity=1.0):
    """Shared RGBA gradient for audio-reactive fills and outlines.

    Coordinates follow the visible target rectangle; the audio envelope scales
    stop alpha without altering stop positions or colors.
    """
    if config['direction'] == 'vertical':
        x = rect.center().x()
        y0, y1 = (rect.bottom(), rect.top()) if config['start'] == 'bottom' else (rect.top(), rect.bottom())
        gradient = QLinearGradient(x, y0, x, y1)
    else:
        y = rect.center().y()
        x0, x1 = (rect.right(), rect.left()) if config['start'] == 'right' else (rect.left(), rect.right())
        gradient = QLinearGradient(x0, y, x1, y)
    for stop in config['stops']:
        r, g, b, a = stop['color']
        gradient.setColorAt(stop['percent'] / 100.0, QColor.fromRgbF(
            r / 255.0, g / 255.0, b / 255.0,
            max(0.0, min(1.0, a * energy * intensity))))
    return gradient


class ThemeCanvas(QWidget):
    """Resize-aware host for grid and absolute panel layouts."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme_manager = None
        self.setAutoFillBackground(False)

    def paintEvent(self, event):
        super().paintEvent(event)
        manager = self.theme_manager
        if manager is None:
            return
        if manager.color_gradients and not manager.full_canvas:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            bounds = self.rect()
            for config in manager.color_gradients:
                if config['direction'] == 'vertical':
                    height = max(1, bounds.height())
                    gradient = (QLinearGradient(0, height, 0, 0) if config['start'] == 'bottom'
                                else QLinearGradient(0, 0, 0, height))
                else:
                    width = max(1, bounds.width())
                    gradient = (QLinearGradient(width, 0, 0, 0) if config['start'] == 'right'
                                else QLinearGradient(0, 0, width, 0))
                for stop in config['stops']:
                    rgba = stop['color']
                    gradient.setColorAt(stop['percent'] / 100.0, QColor.fromRgbF(
                        rgba[0]/255.0, rgba[1]/255.0, rgba[2]/255.0, rgba[3]))
                painter.fillRect(bounds, gradient)
            painter.end()
        if manager.full_canvas or not manager.audio_reactive.get("enabled", False):
            return
        energy = manager.audio_frame.get(manager.audio_reactive.get('band', 'level'), manager.audio_energy)
        if energy <= 0.002:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        center = self.rect().center()
        radius = max(self.width(), self.height()) * 0.67
        if manager.audio_reactive.get('gradient'):
            gradient = make_audio_gradient(manager.audio_reactive['gradient'],
                                           QRectF(self.rect()), energy,
                                           manager.audio_reactive.get('intensity', 120) / 120.0)
        else:
            gradient = QRadialGradient(center, radius)
            color = QColor(manager.audio_reactive.get("color", "#ff1744"))
            # No permanent tint: intensity must follow the audio level visibly.
            color.setAlpha(round(min(235, energy * manager.audio_reactive.get("intensity", 120) * 1.3)))
            gradient.setColorAt(0.0, color)
            color.setAlpha(0)
            gradient.setColorAt(1.0, color)
        painter.fillRect(self.rect(), gradient)
        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.theme_manager is not None:
            self.theme_manager.reflow()


class AudioAccentOverlay(QWidget):
    """One transparent border per target, parented to the target itself.

    A sibling panel with a higher z-index therefore covers this accent just as
    it covers the target. No canvas-wide overlay can paint across that sibling.
    """
    def __init__(self, manager, target, spec):
        super().__init__(target)
        self.manager = manager
        self.target = target
        self.spec = spec
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setGeometry(target.rect())
        target.installEventFilter(self)
        self.show()
        self.raise_()

    def eventFilter(self, obj, event):
        if obj is self.target and event.type() == QEvent.Resize:
            self.setGeometry(self.target.rect())
        return False

    def paintEvent(self, event):
        energy = self.manager.audio_frame.get(self.spec.get('band', 'level'), self.manager.audio_energy)
        if energy < 0.008:
            return
        spec = self.spec
        stroke = float(spec['width'])
        # A Qt pen is centered on its path. Half the stroke width is the
        # only mandatory inset for a border flush with the target's outer edge.
        inset = float(spec['padding']) + stroke / 2.0
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        if rect.width() <= 0 or rect.height() <= 0:
            return

        # A vinyl/circular cover is a circular object inside its panel. Outline
        # the actual cover-art widget, not the rectangular panel surrounding it.
        mask = self.manager.cover_presentation.get('mask', 'rounded')
        circular_cover = (spec['target'] == 'cover' and mask in ('vinyl', 'circle', 'round'))
        if circular_cover:
            art = self.manager.components.get('cover_art')
            if art is not None and art.isVisible():
                origin = art.mapTo(self.target, art.rect().topLeft())
                rect = QRectF(art.rect().translated(origin)).adjusted(inset, inset, -inset, -inset)
                side = min(rect.width(), rect.height())
                rect = QRectF(rect.center().x() - side/2, rect.center().y() - side/2, side, side)
                if side <= 0:
                    return

        color = QColor(spec['color'])
        color.setAlpha(min(220, round(energy * spec['strength'])))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        if spec.get('gradient'):
            brush = QBrush(make_audio_gradient(spec['gradient'], rect, energy,
                                               spec['strength'] / 180.0))
            painter.setPen(QPen(brush, stroke))
        else:
            painter.setPen(QPen(color, stroke))
        painter.setBrush(Qt.NoBrush)
        if circular_cover:
            painter.drawEllipse(rect)
        else:
            radius = min(float(spec['radius']), rect.width()/2, rect.height()/2)
            painter.drawRoundedRect(rect, radius, radius)
        painter.end()

    def detach(self):
        self.target.removeEventFilter(self)
        self.hide()
        self.setParent(None)
        self.deleteLater()


class _AudioVisualizationLogic:
    """Declarative, mouse-transparent spectrum or oscilloscope widget."""
    def __init__(self, manager, spec):
        super().__init__(manager.canvas)
        self.manager, self.spec = manager, spec
        self._hovered = False
        self._pressed = False
        self._movie = None
        self._movie_path = None
        self._movie_speed = None
        self._static_pixmap = None
        self._asset_cache_key = None
        self._asset_cache_path = None
        self._gpu_effect_source_dirty = True
        self.setMouseTracking(True)
        self._configure_mouse()
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        if spec.get('type') == 'image':
            # Static assets need no periodic polling. QMovie.frameChanged drives
            # animated WebP frames; mouse handlers drive hover/press images.
            # Only linked transport buttons need external state polling.
            if spec.get('button_action'):
                self._state_timer = QTimer(self)
                self._state_timer.timeout.connect(self._refresh_state)
                self._state_timer.start(120)
            QTimer.singleShot(0, self._sync_image_frame)
        if spec.get('type') == 'vu':
            self._vu_timer = QTimer(self)
            self._vu_glow = []
            self._vu_last = time.monotonic()
            self._vu_timer.timeout.connect(self._update_vu)
            self._vu_timer.start(getattr(self.manager, 'gui_frame_interval_ms', 40))

    def _vu_signal(self):
        """Return (L dB, R dB, L peak dB, R peak dB, color source).

        A+B uses the louder channel of the two decks, not a sum: a
        crossfade cannot artificially double the meter reading.
        """
        deck = self.spec.get('source', 'A')
        a = self.manager.components.get('vu_left')
        b = self.manager.components.get('vu_right')
        sources = ([a, b] if deck == 'A+B' else [a if deck == 'A' else b])
        sources = [source for source in sources if source is not None]
        if not sources:
            return None
        gain = max(0.0, float(self.spec.get('vu_sensitivity', 100)) / 100.0)
        curve = float(self.spec.get('vu_curve', 1.0))
        band = self.spec.get('vu_band', 'full')
        if band != 'full':
            # Existing FFT band analyzer tracks the active playback deck.
            # For full-range level, A+B continues using both deck meters.
            level = max(0.0, min(1.0, float(self.manager.audio_frame.get(band, 0.0))))
            value = min(1.0, level * gain) ** curve
            db = -60.0 + 60.0 * value
            return (db, db, db, db, sources[0])
        def scaled(db):
            # Scale the displayed 0..1 range, not the logarithmic dB value.
            normalized = max(0.0, min(1.0, (db + 60.0) / 60.0))
            return -60.0 + 60.0 * (min(1.0, normalized * gain) ** curve)
        levels = [max(source.dl for source in sources),
                  max(source.dr for source in sources),
                  max(source.pl for source in sources),
                  max(source.pr for source in sources)]
        return (*[scaled(db) for db in levels], sources[0])

    def _update_vu(self):
        """Each meter owns its own geometry and phosphor envelope."""
        if self.spec.get('type') != 'vu':
            return
        signal = self._vu_signal()
        if signal is None:
            return
        quarter = self.spec.get('vu_rotation', 0) in (90, 270)
        length = self.height() if quarter else self.width()
        thickness = self.width() if quarter else self.height()
        gap = 1.0
        ideal = max(5.0, min(36.0, float(thickness) - 2.0))
        count = self.spec.get('vu_segments')
        if count is None:
            count = max(1, round((max(1.0, length - 2.0) + gap) / (ideal + gap)))
        # Re-sample the previous glow to this meter's own segment count.
        if len(self._vu_glow) != count:
            old = self._vu_glow
            self._vu_glow = [old[min(len(old)-1, int((i+.5)*len(old)/count))]
                             if old else 0.0 for i in range(count)]
        now = time.monotonic()
        dt = min(.1, max(0.0, now - self._vu_last))
        self._vu_last = now
        db = max(signal[0], signal[1])
        position = max(0.0, min(1.0, (db + 60.0) / 60.0)) * count
        attack = min(1.0, dt / .035)
        release = min(1.0, dt / .22)
        for i, previous in enumerate(self._vu_glow):
            target = 1.0 if i < position else .5 ** (max(0.0, i-position) + 1.0)
            blend = attack if target >= previous else release
            self._vu_glow[i] = previous + (target - previous) * blend
        self.update()

    def _configure_mouse(self):
        interactive = bool(self.spec.get('button_action'))
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not interactive)
        self.setCursor(Qt.PointingHandCursor if interactive else Qt.ArrowCursor)

    def _action_button(self):
        key = self.spec.get('button_action', '')
        return self.manager.components.get(key) if key else None

    def _active_state(self):
        button = self._action_button()
        if button is None:
            return False
        if self.spec.get('button_action') == 'transport_play_pause':
            # This button has a distinct playing/paused state.
            try:
                from spyder_engine import mpv_player
                return bool(mpv_player.is_playing() and not mpv_player.is_paused)
            except (ImportError, AttributeError, TypeError):
                return False
        return bool(button.property('spyderActive'))

    def _image_path(self):
        """Resolve image assets only when their state or theme changes."""
        relative = self.spec.get('asset', '')
        if self._pressed:
            relative = self.spec.get('asset_pressed') or relative
        elif self._hovered:
            relative = self.spec.get('asset_hover') or relative
        elif self._active_state():
            relative = self.spec.get('asset_active') or relative

        folder_value = getattr(self.manager, 'active_theme_folder',
                               self.manager.theme_dir)
        key = (str(folder_value), relative)
        if key == self._asset_cache_key:
            return self._asset_cache_path

        self._asset_cache_key = key
        self._asset_cache_path = None
        if not isinstance(relative, str) or not relative:
            return None

        folder_key = str(folder_value)
        if getattr(self.manager, '_resolved_image_theme_key', None) != folder_key:
            self.manager._resolved_image_theme_key = folder_key
            self.manager._resolved_image_theme_folder = Path(folder_value).resolve()
        folder = self.manager._resolved_image_theme_folder
        path = (folder / relative).resolve()
        if path.is_relative_to(folder) and path.is_file():
            self._asset_cache_path = path
        return self._asset_cache_path

    def _sync_image_frame(self):
        path = self._image_path()
        speed = self.spec.get('animation_speed', 100)
        if type(speed) is not int or not 25 <= speed <= 500:
            speed = 100
        if path == self._movie_path:
            if self._movie is not None and speed != self._movie_speed:
                self._movie.setSpeed(speed)
                self._movie_speed = speed
            return
        self._movie_speed = None
        if self._movie is not None:
            self._movie.stop()
            self._movie.deleteLater()
            self._movie = None
        self._movie_path = path
        self._static_pixmap = None
        if path is not None:
            reader = QImageReader(str(path))
            if reader.supportsAnimation():
                movie = QMovie(str(path), parent=self)
                if movie.isValid():
                    movie.frameChanged.connect(lambda _frame: self.update())
                    self._movie = movie
                    movie.setSpeed(speed)
                    self._movie_speed = speed
                    movie.start()
                else:
                    movie.deleteLater()
            if self._movie is None:
                self._static_pixmap = QPixmap(str(path))
        self.update()

    def _refresh_state(self):
        self._configure_mouse()
        self._sync_image_frame()
        state = (self._active_state(), self._hovered, self._pressed)
        if state != getattr(self, '_previous_state', None):
            self._previous_state = state
            self.update()

    def enterEvent(self, event):
        self._hovered = True
        self._sync_image_frame()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._pressed = False
        self._sync_image_frame()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if bool(self.spec.get('button_action')) and event.button() == Qt.LeftButton:
            self._pressed = True
            self._sync_image_frame()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if bool(self.spec.get('button_action')) and event.button() == Qt.LeftButton:
            was_pressed = self._pressed
            self._pressed = False
            self._sync_image_frame()
            if was_pressed and self.rect().contains(event.position().toPoint()):
                button = self._action_button()
                if button is not None:
                    button.click()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            self.paint_into(painter)
        finally:
            painter.end()

    def paint_into(self, painter):
        """Draw into a supplied painter, usable with QWidget or one GL canvas.

        The caller owns the QPainter and must save/restore its state between
        scene layers. Image alpha, transformations and VU settings are identical
        to those used in the original QWidget painter.
        """
        if self.spec.get('type') == 'image':
            pixmap = self._movie.currentPixmap() if self._movie is not None else self._static_pixmap
            if pixmap is None or pixmap.isNull():
                return
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            quarter = self.spec.get('rotation', 0) in (90, 270)
            w, h = (self.height(), self.width()) if quarter else (self.width(), self.height())
            painter.translate(self.width()/2, self.height()/2)
            painter.rotate(self.spec.get('rotation', 0))
            painter.scale(-1 if self.spec.get('flip_horizontal', False) else 1,
                          -1 if self.spec.get('flip_vertical', False) else 1)
            painter.drawPixmap(QRectF(-w/2, -h/2, w, h), pixmap, QRectF(pixmap.rect()))
            return
        if self.spec.get('type') == 'vu':
            signal = self._vu_signal()
            if signal is None:
                return
            left_db, right_db, left_peak, right_peak, source = signal
            quarter = self.spec.get('vu_rotation', 0) in (90, 270)
            lw, lh = (self.height(), self.width()) if quarter else (self.width(), self.height())
            painter.translate(self.width()/2, self.height()/2)
            painter.rotate(self.spec.get('vu_rotation', 0))
            painter.scale(-1 if self.spec.get('vu_flip_horizontal', False) else 1,
                          -1 if self.spec.get('vu_flip_vertical', False) else 1)
            painter.translate(-lw/2, -lh/2)
            painter.setRenderHint(QPainter.Antialiasing, True)
            mode = self.spec.get('vu_presentation', 'phosphor_mono')
            values = self._vu_glow
            phosphor = QColor(self.spec.get('vu_phosphor_color', '#41ff73'))
            rgb = (phosphor.red(), phosphor.green(), phosphor.blue())
            core_rgb = tuple(min(255, round(c * .55 + 255 * .45)) for c in rgb)
            painter.fillRect(0, 0, lw, lh, QColor('#080e0b'))
            count = len(values)
            gap = min(1.0, lw / max(1, count * 3.0))
            sw = max(.1, (lw - (count-1)*gap)/max(1, count))
            if mode == 'classic':
                # Match the built-in classic VU: L/R labels, green-to-red
                # scale, dark inactive cells, and independent peak markers.
                # Unlike the original, segment count follows THIS widget.
                label_w = min(28.0, max(0.0, lw * .12))
                right_pad = min(5.0, lw * .025)
                available = max(1.0, lw - label_w - right_pad)
                count = self.spec.get('vu_segments') or max(1, round((available + gap) / max(1.0, sw + gap)))
                gap = min(2.0, available / max(1, count * 3))
                sw = max(.1, (available - gap * (count - 1)) / count)
                label_size = max(5, min(10, round(lh * .21)))
                painter.setFont(QFont('Consolas', label_size, QFont.Bold))
                rows = (('L', left_db, left_peak), ('R', right_db, right_peak))
                for lane, (label, db, peak) in enumerate(rows):
                    y = lh * (.08 if lane == 0 else .54)
                    height = max(1.0, lh * .38)
                    if label_w >= 9:
                        painter.setPen(QColor('#B8B8B8'))
                        painter.drawText(QRectF(0, y, label_w - 2, height),
                                         Qt.AlignRight | Qt.AlignVCenter, label)
                    level = max(0.0, min(1.0, (db + 60.0) / 60.0))
                    active = round(level * count)
                    for i in range(count):
                        x = label_w + i * (sw + gap)
                        color = source._color((i + 1) / count) if i < active else QColor('#202020')
                        painter.fillRect(QRectF(x, y, sw, height), color)
                    if peak > -60:
                        idx = max(0, min(count - 1, int(((peak + 60.0) / 60.0) * count) - 1))
                        x = label_w + idx * (sw + gap)
                        painter.setPen(QPen(QColor('#F2F2F2'), 1))
                        painter.drawRect(QRectF(x, y, sw, height))
            elif mode == 'phosphor':
                # Stereo lanes, each using the independent meter's phosphor
                # envelope with per-channel amplitude weighting.
                for lane, db in enumerate((left_db, right_db)):
                    level = max(0.0, min(1.0, (db + 60.0) / 60.0)) * count
                    y = lh * (0.05 if lane == 0 else 0.53)
                    height = lh * .42
                    for i, glow in enumerate(values):
                        lane_glow = max(glow * .35, min(glow, 1.0 if i < level else .5 ** (i-level+1)))
                        x = i*(sw+gap)
                        painter.fillRect(QRectF(x, y, sw, height),
                                         QColor(*rgb, round(85*lane_glow)))
                        painter.fillRect(QRectF(x, y+height*.16, sw, height*.68),
                                         QColor(*core_rgb, round(220*lane_glow)))
            else:
                # Original mono phosphor appearance, unchanged.
                for i, alpha in enumerate(values):
                    x = i*(sw+gap)
                    halo = QColor(*rgb, round(80*alpha))
                    core = QColor(*core_rgb, round(235*alpha))
                    painter.fillRect(QRectF(x, 0, sw, lh), halo)
                    painter.fillRect(QRectF(x, lh*.18, sw, lh*.64), core)
            return
        values = self.manager.visual_frame.get(self.spec['type'], ())
        if len(values) < 2:
            return
        painter.setRenderHint(QPainter.Antialiasing, True)
        color = QColor(self.spec.get('color', '#00f5ff'))
        color.setAlphaF(float(self.spec.get('opacity', 1.0)))
        painter.setPen(QPen(color, self.spec.get('line_width', 2)))
        rect = self.rect().adjusted(2, 2, -2, -2)
        if self.spec['type'] == 'spectrum':
            count = min(self.spec.get('bars', 64), len(values))
            step = rect.width() / max(1, count)
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            for i in range(count):
                height = max(0, min(1, float(values[i]))) * rect.height()
                painter.drawRect(QRectF(rect.left()+i*step, rect.bottom()-height,
                                        max(1, step * .78), height))
        else:
            from PySide6.QtGui import QPainterPath
            path = QPainterPath()
            mid = rect.center().y()
            gain = self.spec.get('gain', 1.0)
            for i, value in enumerate(values):
                x = rect.left() + i * rect.width() / max(1, len(values)-1)
                y = mid - max(-1, min(1, float(value)*gain)) * rect.height() * .48
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            painter.drawPath(path)



class AudioVisualization(_AudioVisualizationLogic, QWidget):
    """An original Qt layer or a nonpainting data/input host for the GPU scene."""

    def update(self, *args):
        if getattr(self, '_gpu_composited', False):
            self._gpu_effect_source_dirty = True
            self.manager.request_gpu_compositor_frame()
            return
        return QWidget.update(self, *args)

    def paintEvent(self, event):
        if getattr(self, '_gpu_composited', False):
            return  # one GPU pass draws this layer; never rasterize it twice
        _AudioVisualizationLogic.paintEvent(self, event)



class ThemeManager:
    """Register GUI widgets without transferring their behavior to themes.

    API v1 remains grid based. Components can be styled and hidden separately;
    grid relocation is limited to top-level panels. Absolute positioning,
    overlays and animations are deliberately NOT claimed as supported.
    """
    def __init__(self, window, grid, theme_dir, canvas=None):
        self.window, self.grid, self.theme_dir = window, grid, Path(theme_dir)
        self.canvas = canvas
        if canvas is not None:
            canvas.theme_manager = self
        self.layout_mode = "grid"
        self.absolute_bounds = {}
        self.absolute_layers = {}
        self.floating_bounds = {}
        self.floating_layers = {}
        self.dock_slots = {}
        self._floating = set()
        self._original_sizes = {}
        self._hidden_internal_components = {"autodl"}
        self.panels = {}
        self.positions = {}
        self.components = {}
        self.settings = {}
        self.active_theme = "default"
        self.cover_presentation = {"mask": "rounded", "radius": 9}
        self.seek_presentation = {"style": "linear"}
        self.seek_visualization = None
        self.waveform_seek = None
        self.linear_seek = None
        self.seek_visual_bounds = None
        self.seek_visual_z = 70
        self.seek_instances = []
        self.seek_widgets = []
        self.seek_widget_factory = None
        self._transition_group = None
        self._transition_effects = {}
        self._transition_targets = {}
        self._in_transition = False
        self._pending_transitions = {}
        self.audio_reactive = {"enabled": False}
        self.audio_energy = 0.0
        self.audio_bindings = []
        self.color_gradients = []
        self.audio_frame = {'level': 0.0, 'left_db': -120.0, 'right_db': -120.0, 'bass': 0.0, 'mids': 0.0, 'highs': 0.0}
        self.audio_overlays = []
        self.visual_frame = {"spectrum": (), "waveform": ()}
        self.visual_widgets = []
        self._gpu_composited = ()
        self._gpu_editor_fx_enabled = True  # becomes False on GL shader failure
        self._gpu_frame_queued = False
        self._gpu_last_frame_at = 0.0
        self.gui_frame_interval_ms = 40
        self._gpu_transition_suspended = False
        self.full_canvas = False
        self.reference_canvas = None  # Pixel dimensions of the canvas when the theme was authored.
        self.theme_fonts = {}

    def set_gui_frame_interval(self, milliseconds):
        """Share one render budget with Qt audio timer and per-theme VUs."""
        self.gui_frame_interval_ms = max(33, min(80, int(milliseconds)))
        for visual in self.visual_widgets:
            timer = getattr(visual, '_vu_timer', None)
            if timer is not None:
                timer.setInterval(self.gui_frame_interval_ms)

    def repaint_audio_visuals(self, visual=True):
        """Coalesce peak, FFT bands and waveform into one invalidation pass."""
        # Static gradients and image layers do not need a full redraw on each
        # audio sample. Only an enabled reactive canvas changes with the pulse.
        reactive = self.audio_reactive.get('enabled', False)
        if reactive and not self.full_canvas and self.canvas is not None:
            self.canvas.update()
        for overlay in self.audio_overlays:
            overlay.update()
        if visual:
            for widget in self.visual_widgets:
                if widget.spec.get('type') in ('spectrum', 'waveform'):
                    widget.update()
        if reactive and self.full_canvas and hasattr(self.window, 'backdrop'):
            self.window.backdrop.update()

    def feed_audio_levels(self, left_db, right_db, repaint=True):
        """GUI-thread VU meter feed, not frequency-specific PCM analysis."""
        import math
        try:
            peak = max(float(left_db), float(right_db))
            target = max(0.0, min(1.0, (peak + 55.0) / 55.0)) if math.isfinite(peak) else 0.0
        except (TypeError, ValueError):
            target = 0.0
        coefficient = 0.55 if target > self.audio_energy else 0.20
        self.audio_energy += (target - self.audio_energy) * coefficient
        self.audio_frame.update({'level': self.audio_energy, 'left_db': left_db, 'right_db': right_db})
        if repaint:
            self.repaint_audio_visuals(visual=False)

    def feed_frequency_bands(self, bands, repaint=True):
        """Update frequency data without affecting VU-derived level."""
        for key in ('bass', 'mids', 'highs'):
            self.audio_frame[key] = max(0.0, min(1.0, float(bands.get(key, 0.0))))
        if repaint:
            self.repaint_audio_visuals(visual=False)

    def request_gpu_compositor_frame(self):
        """Coalesce GPU layers; never queue more than one pending visual frame."""
        if not self._gpu_composited:
            return
        diag = getattr(self.window, '_frame_path_diag', None)
        if diag is not None:
            diag.requested(self._gpu_frame_queued)
        if self._gpu_frame_queued:
            return
        self._gpu_frame_queued = True
        # A/B test: no artificial frame interval on the GPU compositor.
        # Defer to the next event-loop pass only, so concurrent invalidations
        # coalesce without imposing the previous 40/50/66 ms deadline.
        QTimer.singleShot(0, self._flush_gpu_compositor_frame)

    def _flush_gpu_compositor_frame(self):
        diag = getattr(self.window, '_frame_path_diag', None)
        if diag is not None:
            diag.flushed()
        self._gpu_frame_queued = False
        self._gpu_last_frame_at = time.monotonic()
        backdrop = getattr(self.window, 'backdrop', None)
        if backdrop is not None and self._gpu_composited:
            backdrop.update()

    def draw_gpu_compositor(self, painter, backdrop):
        """Paint every selected scene layer into ONE OpenGL-backed framebuffer.

        This is called from CoverBackdrop._render_backdrop while its GL painter
        is active. The source widget is a nonpainting data holder; it remains
        parented to the original canvas for geometry and animation lifecycle.
        """
        if not self._gpu_composited or self.canvas is None:
            return
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        renderer = getattr(backdrop, '_gpu_editor_effect_renderer', None)
        for widget in self._gpu_composited:
            if not widget.isVisible() or widget.width() <= 0 or widget.height() <= 0:
                continue
            effect = widget.spec.get('editor_effect') or {}
            # Paint the shader halo below the unchanged full-res original.
            # beginNativePainting() preserves the QPainter/GL interoperability.
            if renderer is not None and effect.get('enabled', False):
                try:
                    renderer.composite(widget, painter, backdrop)
                except Exception as exc:
                    self._disable_gpu_editor_fx(exc)
                    renderer = None  # Qt effect will be restored on next loop
            origin = widget.mapTo(backdrop, QPoint(0, 0))
            painter.save()
            painter.translate(origin)
            painter.setClipRect(QRectF(0, 0, widget.width(), widget.height()))
            widget.paint_into(painter)
            painter.restore()

    def _choose_gpu_compositor_layers(self):
        """Only move layers whose painter order remains exact with Qt siblings.

        All GPU layers share the bottom GL surface. A visual above an
        overlapping *lower* native sibling would no longer appear above that
        widget, so it MUST remain raster-backed. Likewise an overlying GPU
        visual cannot be placed above an overlapping lower raster visual.
        We re-evaluate on every responsive reflow and theme switch.
        """
        old = tuple(getattr(self, '_gpu_composited', ()))
        if (self._gpu_transition_suspended
                or self.layout_mode != 'absolute'
                or self.canvas is None or not hasattr(self.window, 'backdrop')
                or (self.color_gradients and not self.full_canvas)
                or (self.audio_reactive.get('enabled') and not self.full_canvas)):
            chosen = ()
        else:
            native = []
            for name, panel in self.panels.items():
                if name in self.absolute_bounds:
                    native.append((self.absolute_layers.get(name, 0), name, panel))
            for name, entry in self.dock_slots.items():
                widget = entry[0]
                if name in self.floating_bounds:
                    native.append((self.floating_layers.get(name, 50), name, widget))
            for index, (spec, widget) in enumerate(zip(self.seek_instances, self.seek_widgets)):
                if widget.width() and widget.height():
                    native.append((spec.get('z_index', 70), 'seekbar_%02d' % index, widget))
            known = {w for _, _, w in native}
            known.add(getattr(self.window, 'tabs', None))
            known.add(getattr(self.window, 'main_menu_button', None))
            known.update(self.visual_widgets)
            # Unregistered direct-canvas widgets are treated as lower
            # foreground blockers; never silently draw over their content.
            for child in self.canvas.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
                if child not in known and not child.isHidden() and child.width() > 0 and child.height() > 0:
                    native.append((-10**9, 'unregistered_widget', child))
            visual_sorted = sorted(enumerate(self.visual_widgets),
                                   key=lambda p: (p[1].spec.get('z_index', 80), 'visual_'+str(p[0])))
            available = []
            for i, widget in visual_sorted:
                spec = widget.spec
                effect = spec.get('editor_effect') or {}
                if (spec.get('button_action')
                        or (effect.get('enabled', False) and not self._gpu_editor_fx_enabled)
                        or widget.isHidden() or not widget.width() or not widget.height()
                        or not isinstance(widget, AudioVisualization)):
                    continue
                # The GPU halo extends outside the source bounds.  Count that
                # extension when enforcing the original native-widget order.
                bounds = widget.geometry()
                if effect.get('enabled', False):
                    from gpu_editor_effects import _effect_geometry
                    pad, dx, dy = _effect_geometry(widget, effect)
                    bounds = bounds.adjusted(-pad + min(0, int(dx)),
                                             -pad + min(0, int(dy)),
                                             pad + max(0, int(dx)),
                                             pad + max(0, int(dy)))
                visual_order = (spec.get('z_index', 80), 'visual_'+str(i))
                if any((z, name) < visual_order and bounds.intersects(other.geometry())
                       for z, name, other in native if other is not widget):
                    continue
                available.append((i, widget))
            # Mixed GPU/raster visualizations can overlap. In that case a
            # lower native visual cannot be painted behind a higher GPU visual.
            selected = {id(w) for _, w in available}
            while True:
                demote = []
                for i, visual in available:
                    if id(visual) not in selected:
                        continue
                    vo = (visual.spec.get('z_index', 80), 'visual_'+str(i))
                    for j, other in visual_sorted:
                        if id(other) in selected or other.isHidden():
                            continue
                        if ((other.spec.get('z_index', 80), 'visual_'+str(j)) < vo
                                and visual.geometry().intersects(other.geometry())):
                            demote.append(id(visual))
                            break
                if not demote:
                    break
                selected.difference_update(demote)
            chosen = tuple(w for _, w in available if id(w) in selected)
        if chosen != old:
            self._gpu_composited = chosen
            # Render original widget when not composited, otherwise draw from
            # the source data inside the shared GPU backdrop.
            needs_native_effect_refresh = False
            for widget in self.visual_widgets:
                state = widget in chosen
                was_gpu = getattr(widget, '_gpu_composited', False)
                if was_gpu != state:
                    widget._gpu_composited = state
                    widget._gpu_effect_source_dirty = True
                    QWidget.update(widget)  # clear previous raster pixels on either switch
                    if state and widget.property('spyder_editor_effect'):
                        widget.setGraphicsEffect(None)  # promote: remove CPU blur
                        widget.setProperty('spyder_editor_effect', False)
                    elif was_gpu and not state and (widget.spec.get('editor_effect') or {}).get('enabled', False):
                        needs_native_effect_refresh = True
            if needs_native_effect_refresh and getattr(self, '_editor_effect_theme_data', None):
                # Native painting was restored on reflow or a z-order change.
                QTimer.singleShot(0, lambda: self.apply_editor_effects(self._editor_effect_theme_data))
            backdrop = getattr(self.window, 'backdrop', None)
            if backdrop is not None:
                backdrop.update()
        elif chosen:
            self.request_gpu_compositor_frame()

    def feed_visual_audio(self, data, repaint=True):
        self.visual_frame = data
        if repaint:
            self.repaint_audio_visuals(visual=True)

    @staticmethod
    def _normalize_image_assets(data, theme_folder):
        """Store image references as assets/<file>.png regardless of old path style.

        Only accept an existing PNG in the theme's own assets folder. Never
        allow arbitrary absolute paths or traversal outside that folder.
        """
        base = Path(theme_folder).resolve()
        assets = (base / 'assets').resolve()
        for spec in data.get('visualizations', []):
            if not isinstance(spec, dict) or spec.get('type') != 'image':
                continue
            for field in ('asset', 'asset_hover', 'asset_pressed', 'asset_active'):
                asset = spec.get(field)
                if not isinstance(asset, str) or not asset:
                    continue
                normalized = asset.replace('\\', '/')
                candidate = Path(normalized)
                resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
                if resolved.parent == assets and resolved.suffix.lower() in ('.png', '.webp'):
                    spec[field] = 'assets/' + resolved.name

    @staticmethod
    def _validate_visualizations(specs, mode):
        if not isinstance(specs, list):
            raise ValueError('visualizations must be a list')
        if specs and mode != 'absolute':
            raise ValueError('visualizations require absolute layout')
        for spec in specs:
            ThemeManager._validate_editor_effect(spec, 'visualization')
            if not isinstance(spec, dict) or set(spec) - {'type','bounds','color','bars','line_width','gain','z_index','visible','opacity','editor_effect','editor_layer','editor_inner_z','source','vu_rotation','vu_flip_horizontal','vu_flip_vertical','vu_presentation','vu_sensitivity','vu_segments','vu_phosphor_color','vu_curve','vu_band','asset','asset_hover','asset_pressed','asset_active','mouse_mode','button_action','rotation','flip_horizontal','flip_vertical','original_geometry_bounds','animation_speed'}:
                raise ValueError('Invalid visualization specification')
            original = spec.get('original_geometry_bounds')
            if original is not None:
                if (not isinstance(original, list) or len(original) != 4
                        or any(type(v) is not int for v in original)
                        or not (0 <= original[0] <= 1000 and 0 <= original[1] <= 1000
                                and 1 <= original[2] <= 1000 and 1 <= original[3] <= 1000
                                and original[0] + original[2] <= 1000
                                and original[1] + original[3] <= 1000)):
                    raise ValueError('Invalid original geometry bounds')
            if type(spec.get('visible', True)) is not bool:
                raise ValueError('Invalid visualization visibility')
            if spec.get('type') not in ('spectrum', 'waveform', 'vu', 'image'):
                raise ValueError('Invalid visualization type')
            if spec.get('type') == 'image':
                for field in ('asset', 'asset_hover', 'asset_pressed', 'asset_active'):
                    asset = spec.get(field, '')
                    if not asset and field != 'asset':
                        continue
                    if (not isinstance(asset, str) or not asset.startswith('assets/')
                            or len(asset.split('/')) != 2 or Path(asset).suffix.lower() not in ('.png', '.webp')
                            or asset.split('/')[1] in ('', '.', '..') or '\\' in asset
                            or ':' in asset or '\x00' in asset):
                        raise ValueError(f'Invalid image asset path: {asset!r} (expected assets/filename.png or .webp)')
                speed = spec.get('animation_speed', 100)
                if type(speed) is not int or not 25 <= speed <= 500:
                    raise ValueError('Image animation speed must be an integer from 25 to 500 percent')
                if spec.get('mouse_mode', 'pass') not in ('pass', 'block', 'action'):
                    raise ValueError('Invalid PNG mouse mode')
                if spec.get('button_action', '') not in ('', 'transport_previous', 'transport_play_pause', 'transport_stop', 'transport_next', 'stop_after', 'fade_stop', 'repeat_one'):
                    raise ValueError('Invalid PNG button action')
                if spec.get('rotation', 0) not in (0, 90, 180, 270):
                    raise ValueError('Invalid image rotation')
                if any(type(spec.get(k, False)) is not bool for k in ('flip_horizontal', 'flip_vertical')):
                    raise ValueError('Invalid image flip')
            if spec.get('type') == 'vu':
                if spec.get('vu_presentation', 'phosphor_mono') not in ('classic', 'phosphor', 'phosphor_mono'):
                    raise ValueError('Invalid VU presentation')
                if spec.get('source', 'A') not in ('A', 'B', 'A+B'):
                    raise ValueError('Invalid VU source')
                if type(spec.get('vu_sensitivity', 100)) not in (int, float) or not 10 <= spec.get('vu_sensitivity', 100) <= 200:
                    raise ValueError('VU sensitivity must be 10..200 percent')
                if not isinstance(spec.get('vu_phosphor_color', '#41ff73'), str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', spec.get('vu_phosphor_color', '#41ff73')):
                    raise ValueError('VU phosphor color must be #RRGGBB')
                if spec.get('vu_band', 'full') not in ('full', 'bass', 'mids', 'highs'):
                    raise ValueError('Invalid VU frequency band')
                if type(spec.get('vu_curve', 1.0)) not in (int, float) or not 1.0 <= spec.get('vu_curve', 1.0) <= 5.0:
                    raise ValueError('VU curve must be 1.0..5.0')
                segments = spec.get('vu_segments')
                if segments is not None and (type(segments) is not int or not 1 <= segments <= 128):
                    raise ValueError('VU segments must be 1..128 or automatic')
                if spec.get('vu_rotation', 0) not in (0, 90, 180, 270):
                    raise ValueError('Invalid VU rotation')
                if any(type(spec.get(k, False)) is not bool for k in ('vu_flip_horizontal', 'vu_flip_vertical')):
                    raise ValueError('Invalid VU flip')
            bounds = spec.get('bounds')
            if not isinstance(bounds, list) or len(bounds) != 4 or any(type(v) is not int or v < 0 or v > 1000 for v in bounds) or bounds[2] < 1 or bounds[3] < 1:
                raise ValueError('Visualization bounds must be [x,y,w,h] in 0..1000')
            if not re.fullmatch(r'#[0-9a-fA-F]{6}', spec.get('color', '#00f5ff')):
                raise ValueError('Invalid visualization color')
            if type(spec.get('opacity', 1.0)) not in (int, float) or not 0 <= spec.get('opacity', 1.0) <= 1:
                raise ValueError('Visualization opacity must be 0..1')
            if type(spec.get('bars', 64)) is not int or not 8 <= spec.get('bars', 64) <= 128:
                raise ValueError('Visualization bars must be 8..128')
            if type(spec.get('line_width', 2)) is not int or not 1 <= spec.get('line_width', 2) <= 8:
                raise ValueError('Visualization line_width must be 1..8')
            if type(spec.get('gain', 1.0)) not in (int,float) or not 0.1 <= spec.get('gain', 1.0) <= 20:
                raise ValueError('Visualization gain must be 0.1..20')
            if type(spec.get('z_index', 80)) is not int or not -100 <= spec.get('z_index', 80) <= 200:
                raise ValueError('Visualization z_index must be -100..200')

    @staticmethod
    def _parse_color_gradients(config):
        if config is None:
            return []
        if not isinstance(config, list) or len(config) > 8:
            raise ValueError('color_gradients must be a list (max 8 layers)')
        normalized = []
        for layer in config:
            if not isinstance(layer, dict):
                raise ValueError('Gradient layer must be an object')
            direction = layer.get('direction', layer.get('direction_1'))
            if direction not in ('vertical', 'horizontal'):
                raise ValueError('Gradient direction must be vertical or horizontal')
            start = layer.get('start', layer.get('start_1', 'top' if direction == 'vertical' else 'left'))
            allowed_starts = ('top', 'bottom') if direction == 'vertical' else ('left', 'right')
            if start not in allowed_starts:
                raise ValueError('Gradient start must be ' + ' or '.join(allowed_starts))
            if 'stops' in layer:
                if set(layer) - {'direction', 'direction_1', 'start', 'start_1', 'stops'}:
                    raise ValueError('Unknown gradient layer property')
                stops = layer['stops']
            else:
                keys = set(layer) - {'direction', 'direction_1', 'start', 'start_1'}
                if any(not re.fullmatch(r'(color|percent)_[1-9][0-9]*', key) for key in keys):
                    raise ValueError('Unknown gradient layer property')
                indices = sorted({int(k.split('_')[1]) for k in keys})
                if indices != list(range(1, len(indices)+1)):
                    raise ValueError('Gradient stop indices must be consecutive from 1')
                if any(f'color_{i}' not in layer or f'percent_{i}' not in layer for i in indices):
                    raise ValueError('Every gradient color needs a percent')
                stops = [{'color': layer[f'color_{i}'], 'percent': layer[f'percent_{i}']} for i in indices]
            if not isinstance(stops, list) or not 2 <= len(stops) <= 32:
                raise ValueError('Gradient needs 2..32 stops')
            parsed = []
            for stop in stops:
                if not isinstance(stop, dict) or set(stop) != {'color', 'percent'}:
                    raise ValueError('Invalid gradient stop')
                value = stop['color']
                if isinstance(value, str):
                    parts = [part.strip() for part in value.split(',')]
                elif isinstance(value, (list, tuple)):
                    parts = value
                else:
                    raise ValueError('Gradient color must be RGBA')
                if len(parts) != 4:
                    raise ValueError('Gradient color needs R,G,B,alpha')
                try:
                    rgba = [float(v) for v in parts]
                except (TypeError, ValueError):
                    raise ValueError('Invalid RGBA color') from None
                import math
                if not all(math.isfinite(v) for v in rgba):
                    raise ValueError('Non-finite RGBA color')
                if any(not 0 <= v <= 255 or int(v) != v for v in rgba[:3]) or not 0 <= rgba[3] <= 1:
                    raise ValueError('RGBA must be 0..255, 0..255, 0..255, alpha 0..1')
                percent = stop['percent']
                if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100:
                    raise ValueError('Gradient percent must be 0..100')
                parsed.append({'percent': float(percent), 'color': rgba})
            parsed.sort(key=lambda item: item['percent'])
            if len({stop['percent'] for stop in parsed}) != len(parsed):
                raise ValueError('Gradient stop positions must be distinct')
            normalized.append({'direction': direction, 'start': start, 'stops': parsed})
        return normalized

    @staticmethod
    def _parse_single_audio_gradient(config):
        if not isinstance(config, dict):
            raise ValueError('Audio gradient must be an object')
        return ThemeManager._parse_color_gradients([config])[0]

    @staticmethod
    def _validate_audio_bindings(bindings, panels, components):
        if not isinstance(bindings, list) or len(bindings) > 32:
            raise ValueError('audio_bindings must be a list of at most 32 entries')
        for spec in bindings:
            if not isinstance(spec, dict) or set(spec) - {'target','effect','color','strength','padding','width','radius','gradient','band'}:
                raise ValueError('Invalid audio binding')
            if spec.get('target') not in set(panels) | set(components):
                raise ValueError('Unknown audio binding target')
            if spec.get('band', 'level') not in ('level', 'bass', 'mids', 'highs'):
                raise ValueError('Invalid audio binding band')
            if spec.get('effect') != 'outline':
                raise ValueError('Unsupported audio binding effect')
            if 'gradient' in spec:
                ThemeManager._parse_single_audio_gradient(spec['gradient'])
            if not isinstance(spec.get('color','#ff175b'),str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',spec.get('color','#ff175b')):
                raise ValueError('Invalid audio binding color')
            for key, lo, hi in [('strength',0,255),('padding',0,32),('width',1,12),('radius',0,100)]:
                value=spec.get(key, {'strength':180,'padding':0,'width':3,'radius':8}[key])
                if type(value) is not int or not lo <= value <= hi:
                    raise ValueError('Invalid audio binding '+key)

    @staticmethod
    def _validate_audio_reactive(config):
        if not isinstance(config, dict) or set(config) - {"enabled", "color", "intensity", "gradient", "band"}:
            raise ValueError("Invalid audio_reactive")
        if config.get('band', 'level') not in ('level', 'bass', 'mids', 'highs'):
            raise ValueError('Invalid audio reactive band')
        if 'gradient' in config:
            ThemeManager._parse_single_audio_gradient(config['gradient'])
        if "enabled" in config and type(config["enabled"]) is not bool:
            raise ValueError("audio_reactive.enabled must be boolean")
        if "color" in config and (not isinstance(config["color"], str) or not __import__("re").fullmatch(r"#[0-9a-fA-F]{6}", config["color"])):
            raise ValueError("audio_reactive.color must be #RRGGBB")
        if "intensity" in config and (type(config["intensity"]) is not int or not 0 <= config["intensity"] <= 200):
            raise ValueError("audio_reactive.intensity must be 0..200")

    OPTIONAL_PANELS = frozenset({'playlist', 'peq'})

    def register(self, name, widget, row, column, rowspan=1, colspan=1):
        if name in self.panels or name in self.components:
            raise ValueError(f'Duplicate theme ID: {name}')
        widget.setProperty('spyderThemeId', name)
        self.panels[name] = widget
        self.positions[name] = (row, column, rowspan, colspan)

    def register_component(self, name, widget):
        if name in self.panels or name in self.components:
            raise ValueError(f'Duplicate theme ID: {name}')
        widget.setProperty('spyderThemeId', name)
        self.components[name] = widget
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def register_dockable(self, name, widget, layout, index):
        """A real control, moved between its original layout and ThemeCanvas."""
        if name not in self.components:
            raise ValueError(f'Component must be registered first: {name}')
        self.dock_slots[name] = (widget, layout, index)

    def register_seek_visualization(self, widget):
        self.seek_visualization = widget
        widget.hide()

    def register_waveform_seek(self, widget):
        self.waveform_seek = widget
        widget.hide()

    def register_linear_seek(self, widget):
        self.linear_seek = widget
        widget.hide()

    def active_seek_widget(self, style=None):
        style = style or self.seek_presentation.get('style', 'linear')
        return {'linear': self.linear_seek, 'circular': self.seek_visualization,
                'waveform': self.waveform_seek}.get(style)

    @staticmethod
    def resolved_seekbars(data):
        if 'seekbars' in data:
            return data['seekbars']
        old = data.get('seek_presentation', {})
        return [dict(old)] if isinstance(old, dict) and 'bounds' in old else []

    @staticmethod
    def _validate_seekbars(data, mode):
        specs = ThemeManager.resolved_seekbars(data)
        if not isinstance(specs, list) or len(specs) > 16:
            raise ValueError('Seekbars must be a list (max 16)')
        for spec in specs:
            ThemeManager._validate_seek_presentation(spec, mode)
            if 'bounds' not in spec:
                raise ValueError('Each seekbar needs bounds')

    def sync_seek_widgets(self, specs):
        from PySide6.QtWidgets import QSlider
        bases = {'linear': self.linear_seek, 'circular': self.seek_visualization,
                 'waveform': self.waveform_seek}
        needed = {k: 0 for k in bases}
        new_widgets = []
        old_extras = [w for w in self.seek_widgets if w not in tuple(bases.values())]
        for spec in specs:
            style = spec.get('style', 'linear')
            if needed[style] == 0:
                widget = bases[style]
            else:
                widget = self.seek_widget_factory(style) if self.seek_widget_factory else None
            needed[style] += 1
            if widget is not None:
                new_widgets.append(widget)
        for widget in old_extras:
            widget.hide()
            if widget in getattr(self.window.seek, '_floating_linear_mirrors', []):
                self.window.seek._floating_linear_mirrors.remove(widget)
            if widget in getattr(self.window.seek, '_seek_visual_mirrors', []):
                self.window.seek._seek_visual_mirrors.remove(widget)
            widget.deleteLater()
        self.seek_instances = list(specs)
        self.seek_widgets = new_widgets
        for base in bases.values():
            if base is not None and base not in new_widgets:
                base.hide()
        self.style_seek_widgets()

    def style_seek_widgets(self):
        for spec, widget in zip(self.seek_instances, self.seek_widgets):
            style = spec.get('style', 'linear')
            played = spec.get('played_color', '#F9061B')
            remaining = spec.get('remaining_color', '#303036')
            cursor = spec.get('cursor_color', '#ffffff')
            if style == 'linear':
                widget.setStyleSheet('QSlider::groove:horizontal {height: 7px; background: %s; border-radius: 3px;} QSlider::sub-page:horizontal {background: %s; border-radius: 3px;} QSlider::handle:horizontal {background: %s; width: 13px; margin: -5px 0; border-radius: 6px;}' % (remaining, played, cursor))
            elif style == 'circular':
                widget.played_color, widget.remaining_color, widget.cursor_color = played, remaining, cursor
                widget.update()
            elif style == 'waveform':
                widget.set_theme_colors(played, remaining, cursor)
            widget.setVisible(bool(spec.get('visible', True)))

    @staticmethod
    def _validate_seek_presentation(config, mode):
        # Seek presentation has no component key; VU transforms are
        # validated separately in _validate_floating(key, config).
        if not isinstance(config, dict):
            raise ValueError('Invalid seek_presentation')
        ThemeManager._validate_editor_effect(config, 'seekbar')
        allowed = {'style', 'bounds', 'z_index', 'hide_linear', 'visible',
                   'played_color', 'remaining_color', 'cursor_color',
                   'editor_effect', 'editor_layer', 'editor_inner_z'}
        if set(config) - allowed:
            raise ValueError('Invalid seek_presentation')
        if type(config.get('visible', True)) is not bool:
            raise ValueError('Invalid seek visibility')
        style = config.get('style', 'linear')
        if style not in ('linear', 'circular', 'waveform'):
            raise ValueError('Unsupported seek presentation')
        if 'bounds' in config:
            if mode != 'absolute':
                raise ValueError('Floating seek requires absolute layout')
            ThemeManager._validate_floating('circular_seek', {
                'bounds': config.get('bounds'), 'z_index': config.get('z_index', 70)})
            if 'hide_linear' in config and type(config['hide_linear']) is not bool:
                raise ValueError('Invalid hide_linear')
            for name in ('played_color', 'remaining_color', 'cursor_color'):
                if name in config and not QColor(config[name]).isValid():
                    raise ValueError('Invalid seek color: ' + name)
        elif set(config) - {'style', 'editor_effect'}:
            raise ValueError('Seek without bounds accepts only style')

    def _restore_docked(self):
        for key in sorted(self._floating, key=lambda k: self.dock_slots[k][2]):
            widget, layout, index = self.dock_slots[key]
            widget.hide()
            if key in ('artist_title_text', 'artist_text', 'title_text'):
                widget.setParent(self.canvas)
                widget.hide()
                continue
            if layout is None:
                widget.setParent(self.canvas)
                continue
            # Restore to the owner of the ORIGINAL layout, never blindly to
            # the Live tab. In particular, `tabs` is a QTabWidget whose Live
            # page is its own descendant: parenting tabs to Live creates a
            # circular QWidget hierarchy and can freeze Qt during theme save.
            original_parent = layout.parentWidget()
            if original_parent is None:
                raise RuntimeError(f'Original layout has no parent for {key!r}')
            widget.setParent(original_parent)
            if key in ('search_panel', 'quick_panel', 'tabs'):
                layout.insertWidget(index, widget)
            elif key == 'clock':
                layout.addWidget(widget, 0, 1)
                layout.setAlignment(widget, __import__('PySide6.QtCore', fromlist=['Qt']).Qt.AlignCenter)
            else:
                layout.insertWidget(index, widget)
            widget.show()
        self._floating.clear()

    @staticmethod
    def _validate_editor_effect(config, key):
        if not isinstance(config, dict) or 'editor_effect' not in config:
            return
        fx = config['editor_effect']
        if fx == {}:
            return
        if not isinstance(fx, dict) or set(fx) - {'enabled', 'type', 'color', 'blur', 'distance', 'angle', 'opacity'}:
            raise ValueError(f'Invalid editor effect: {key}')
        if type(fx.get('enabled', True)) is not bool or fx.get('type', 'shadow') not in ('shadow', 'glow'):
            raise ValueError(f'Invalid editor effect mode: {key}')
        if not isinstance(fx.get('color', '#000000'), str) or not QColor(fx.get('color', '#000000')).isValid():
            raise ValueError(f'Invalid editor effect color: {key}')
        for field, low, high in (('blur', 0, 100), ('distance', 0, 80), ('angle', 0, 360), ('opacity', 0, 100)):
            value = fx.get(field, low)
            if type(value) not in (int, float) or not low <= value <= high:
                raise ValueError(f'Invalid editor effect {field}: {key}')

    ACTION_KEYS = frozenset(('stop_after', 'fade_stop', 'repeat_one', 'transport_previous', 'transport_play_pause', 'transport_stop', 'transport_next'))

    @staticmethod
    def _validate_floating(key, config):
        ThemeManager._validate_editor_effect(config, key)
        allowed = {'bounds', 'z_index', 'visible', 'editor_effect', 'editor_layer', 'editor_inner_z'}
        if key in ('vu_left', 'vu_right'):
            allowed |= {'vu_rotation', 'vu_flip_horizontal', 'vu_flip_vertical'}
        if not isinstance(config, dict) or set(config) - allowed:
            raise ValueError(f'Invalid floating component: {key}')
        if key in ('vu_left', 'vu_right'):
            if type(config.get('vu_rotation', 0)) is not int or config.get('vu_rotation', 0) not in (0, 90, 180, 270):
                raise ValueError(f'Invalid VU rotation: {key}')
            if any(type(config.get(prop, False)) is not bool for prop in ('vu_flip_horizontal', 'vu_flip_vertical')):
                raise ValueError(f'Invalid VU mirror flag: {key}')
        if type(config.get('visible', True)) is not bool:
            raise ValueError(f'Invalid floating visibility: {key}')
        bounds = config.get('bounds')
        y_limit = 1500 if key in ThemeManager.ACTION_KEYS else 1000
        x_limit = 10000 if key in ThemeManager.ACTION_KEYS else 1000
        if not (isinstance(bounds, list) and len(bounds) == 4 and
                all(type(v) is int for v in bounds) and
                0 <= bounds[0] <= x_limit and 0 <= bounds[1] <= y_limit and
                1 <= bounds[2] <= 1000 and 1 <= bounds[3] <= y_limit and
                bounds[0] + bounds[2] <= x_limit and
                bounds[1] + bounds[3] <= y_limit):
            raise ValueError(f'Invalid floating bounds: {key}')

    @staticmethod
    def _validate_config(key, config, allow_grid):
        ThemeManager._validate_editor_effect(config, key)
        if not isinstance(config, dict):
            raise ValueError(f'Invalid theme entry: {key}')
        if 'editor_layer' in config and (not isinstance(config['editor_layer'], str) or not config['editor_layer'].strip()):
            raise ValueError(f'Invalid editor layer: {key}')
        if 'editor_inner_z' in config and type(config['editor_inner_z']) is not int:
            raise ValueError(f'Invalid inner z_index: {key}')
        if 'visible' in config and type(config['visible']) is not bool:
            raise ValueError(f'Invalid visibility: {key}')
        if 'grid' in config:
            if not allow_grid:
                raise ValueError(f'Grid is only valid for panels: {key}')
            pos = config['grid']
            if not (isinstance(pos, list) and len(pos) == 4 and
                    all(type(v) is int for v in pos) and
                    min(pos[:2]) >= 0 and min(pos[2:]) >= 1):
                raise ValueError(f'Invalid grid: {key}')
        if 'z_index' in config:
            if not allow_grid or type(config['z_index']) is not int or not -100 <= config['z_index'] <= 100:
                raise ValueError(f'Invalid z_index: {key}')
        if 'bounds' in config:
            if not allow_grid:
                raise ValueError(f'Absolute bounds only valid for panels: {key}')
            bounds = config['bounds']
            if not (isinstance(bounds, list) and len(bounds) == 4 and
                    all(type(v) is int for v in bounds) and
                    0 <= bounds[0] <= 1000 and 0 <= bounds[1] <= 1000 and
                    1 <= bounds[2] <= 1000 and 1 <= bounds[3] <= 1000 and
                    bounds[0] + bounds[2] <= 1000 and bounds[1] + bounds[3] <= 1000):
                raise ValueError(f'Invalid normalized bounds: {key}')
        for dim in ('width', 'height', 'min_width', 'min_height'):
            if dim in config and (type(config[dim]) is not int or not 0 <= config[dim] <= 4096):
                raise ValueError(f'Invalid {dim}: {key}')

    @staticmethod
    def _validate_cover_presentation(config):
        if not isinstance(config, dict) or set(config) - {'mask', 'radius'}:
            raise ValueError('Invalid cover presentation')
        if config.get('mask', 'rounded') not in ('rounded', 'circle', 'square', 'hexagon', 'vinyl'):
            raise ValueError('Unsupported cover mask')
        if 'radius' in config and (type(config['radius']) is not int or not 0 <= config['radius'] <= 100):
            raise ValueError('Invalid cover radius')

    @staticmethod
    def _validate_cover_animation(config):
        if not isinstance(config, dict) or set(config) - {'type', 'rpm', 'when'}:
            raise ValueError('Invalid cover animation')
        if config.get('type', 'none') not in ('none', 'spin'):
            raise ValueError('Unsupported cover animation')
        rpm = config.get('rpm', 33.333)
        if type(rpm) not in (int, float) or not 0 <= rpm <= 120:
            raise ValueError('Invalid cover rotation speed')
        if config.get('when', 'playing') not in ('playing', 'always'):
            raise ValueError('Invalid cover animation condition')

    @staticmethod
    def _validate_transitions(config, mode, panels, floating):
        if not isinstance(config, dict) or set(config) - {'duration_ms', 'easing', 'entries'}:
            raise ValueError('Invalid transitions')
        if not config:
            return
        if mode != 'absolute':
            raise ValueError('Transitions require absolute layout')
        duration = config.get('duration_ms', 350)
        if type(duration) is not int or not 0 <= duration <= 2000:
            raise ValueError('Invalid transition duration')
        if config.get('easing', 'out_cubic') not in ('linear', 'out_cubic', 'in_out_quad'):
            raise ValueError('Invalid transition easing')
        entries = config.get('entries', {})
        if not isinstance(entries, dict) or len(entries) > 32:
            raise ValueError('Invalid transition entries')
        for key, spec in entries.items():
            if key not in panels and key not in floating and key != 'circular_seek':
                raise ValueError(f'Unknown transition target: {key}')
            if not isinstance(spec, dict) or set(spec) - {'effect', 'offset_x', 'offset_y'}:
                raise ValueError(f'Invalid transition entry: {key}')
            if spec.get('effect', 'fade_slide') not in ('fade', 'slide', 'fade_slide'):
                raise ValueError('Unsupported transition effect')
            for axis in ('offset_x', 'offset_y'):
                value = spec.get(axis, 0)
                if type(value) is not int or not -500 <= value <= 500:
                    raise ValueError('Invalid transition offset')

    def _stop_transitions(self):
        if self._transition_group is not None:
            self._transition_group.stop()
            self._transition_group = None
        for widget, rect in self._transition_targets.values():
            widget.setGeometry(rect)
        self._transition_targets.clear()
        had_fade_effects = bool(self._transition_effects)
        for widget, effect in self._transition_effects.values():
            if widget.graphicsEffect() is effect:
                widget.setGraphicsEffect(None)
        self._transition_effects.clear()
        self._in_transition = False
        self._gpu_transition_suspended = False
        if had_fade_effects and getattr(self, '_editor_effect_theme_data', None):
            self.apply_editor_effects(self._editor_effect_theme_data)
        if self.visual_widgets:
            self._choose_gpu_compositor_layers()

    def start_pending_transitions(self, expected_theme=None):
        if expected_theme is not None and self.active_theme != expected_theme:
            return
        config = self._pending_transitions
        self._pending_transitions = {}
        if not config:
            return
        self._stop_transitions()
        self._start_transitions(config)

    def _start_transitions(self, config):
        entries = config.get('entries', {})
        if not entries or not config.get('duration_ms', 350):
            return
        easing = {'linear': QEasingCurve.Linear,
                  'out_cubic': QEasingCurve.OutCubic,
                  'in_out_quad': QEasingCurve.InOutQuad}[config.get('easing', 'out_cubic')]
        group = QParallelAnimationGroup(self.canvas)
        width, height = max(1, self.canvas.width()), max(1, self.canvas.height())
        for key, spec in entries.items():
            widget = (self.panels.get(key) or
                      (self.dock_slots[key][0] if key in self.floating_bounds else None) or
                      (self.active_seek_widget() if key in ('circular_seek', 'waveform_seek') and self.seek_visual_bounds else None))
            if widget is None or not widget.isVisible():
                continue
            effect_type = spec.get('effect', 'fade_slide')
            if 'slide' in effect_type:
                end = widget.geometry()
                start = QRect(end)
                start.translate(round(width * spec.get('offset_x', 0) / 1000),
                                round(height * spec.get('offset_y', 0) / 1000))
                self._transition_targets[key] = (widget, end)
                widget.setGeometry(start)
                animation = QPropertyAnimation(widget, b'geometry', group)
                animation.setStartValue(start)
                animation.setEndValue(end)
                animation.setDuration(config.get('duration_ms', 350))
                animation.setEasingCurve(easing)
                group.addAnimation(animation)
            if 'fade' in effect_type:
                effect = QGraphicsOpacityEffect(widget)
                widget.setGraphicsEffect(effect)
                self._transition_effects[key] = (widget, effect)
                animation = QPropertyAnimation(effect, b'opacity', group)
                animation.setStartValue(0.0)
                animation.setEndValue(1.0)
                animation.setDuration(config.get('duration_ms', 350))
                animation.setEasingCurve(easing)
                group.addAnimation(animation)
        if group.animationCount() == 0:
            group.deleteLater()
            return
        self._transition_group = group
        self._in_transition = True
        # A sliding native widget may cross GPU layers while the animation
        # progresses. Keep every visualization in its faithful QWidget layer
        # during that short transition; return to the GPU after finished().
        self._gpu_transition_suspended = True
        self._choose_gpu_compositor_layers()
        group.finished.connect(self._stop_transitions)
        group.start()

    def theme_file(self, name, extension):
        if not isinstance(name, str) or not name or any(
                not re.fullmatch(r'[\w -]+', part, flags=re.UNICODE)
                for part in name.split('/')):
            raise ValueError('Invalid theme name')
        if extension not in ('.json', '.qss'):
            raise ValueError('Invalid theme extension')
        direct = self.theme_dir.joinpath(*name.split('/')).with_suffix(extension)
        if direct.is_file() or '/' in name:
            return direct
        matches = sorted(p for p in self.theme_dir.rglob(name + extension)
                         if p.stem == name)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError('Theme name is ambiguous; use its relative path: ' + name)
        return direct

    def apply(self, name='default'):
        path = self.theme_file(name, '.json')
        if not path.is_file():
            raise FileNotFoundError(path)
        data = json.loads(path.read_text(encoding='utf-8'))
        self.active_theme_folder = path.parent
        self._normalize_image_assets(data, path.parent)
        if data.get('api_version') != 1:
            raise ValueError('Unsupported theme API version')
        ref = data.get('reference_canvas')
        if ref is not None and (not isinstance(ref, dict) or
                                any(type(ref.get(k)) is not int or ref[k] < 1 for k in ('width', 'height'))):
            raise ValueError('reference_canvas requires positive integer width and height')
        # Apply title settings after every theme switch, including defaults.
        title_defaults = {
            'brain': ('brain_header', 'AI Judge & Vibe Selection'),
            'main': ('track', self.window.track.text()),
            'top': ('toplabel', None),
            'queue': ('queue_header', 'Queue'),
            'history': ('history_header', 'Last Songs'),
        }
        title_settings = data.get('panel_titles', {})
        if not isinstance(title_settings, dict):
            raise ValueError('panel_titles must be an object')
        for title_key, (attr, default) in title_defaults.items():
            setting = title_settings.get(title_key, {})
            if not isinstance(setting, dict):
                raise ValueError('Invalid panel title settings: ' + title_key)
            if 'text' in setting and not isinstance(setting['text'], str):
                raise ValueError('Panel title text must be a string')
            if 'visible' in setting and type(setting['visible']) is not bool:
                raise ValueError('Panel title visible must be boolean')
            label = getattr(self.window, attr, None)
            if label is None:
                continue
            if default is None:
                # Preserve the configurable Top X count unless explicitly overridden.
                from spyder_engine import cfg
                default = f"Top {cfg.TOP_FAV_ARTISTS_COUNT}"
            label.setText(setting.get('text', default))
            label.setVisible(setting.get('visible', True))
        self._panel_title_settings = title_settings
        panels, components = data.get('panels', {}), data.get('components', {})
        floating = data.get('floating_components', {})
        mode = data.get('layout_mode', 'grid')
        explicit_visibility = data.get('theme_visibility_policy') == 'explicit'
        if mode not in ('grid', 'absolute') or (mode == 'absolute' and self.canvas is None):
            raise ValueError('Unsupported layout mode')
        if not isinstance(panels, dict) or not isinstance(components, dict) or not isinstance(floating, dict):
            raise ValueError('Invalid theme manifest')
        for key, config in panels.items():
            if key not in self.panels:
                raise ValueError(f'Unknown panel: {key}')
            self._validate_config(key, config, True)
        for key, config in components.items():
            if key not in self.components:
                raise ValueError(f'Unknown component: {key}')
            if key in ('vu_left', 'vu_right') and 'vu_presentation' in config:
                if config['vu_presentation'] not in ('classic', 'phosphor', 'phosphor_mono'):
                    raise ValueError('Invalid VU presentation')
                config = {k: v for k, v in config.items() if k != 'vu_presentation'}
            self._validate_config(key, config, False)
        if floating and mode != 'absolute':
            raise ValueError('Floating components require absolute layout mode')
        for key, config in floating.items():
            if key not in self.dock_slots:
                raise ValueError(f'Component cannot be floated: {key}')
            self._validate_floating(key, config)
            if components.get(key, {}).get('visible') is False:
                raise ValueError(f'Floating component cannot be hidden: {key}')
        seek_presentation = data.get('seek_presentation', {})
        self._validate_seek_presentation(seek_presentation, mode)
        self._validate_seekbars(data, mode)
        cover_presentation = data.get('cover_presentation', {})
        self._validate_cover_presentation(cover_presentation)
        cover_animation = data.get('cover_animation', {})
        self._validate_cover_animation(cover_animation)
        self._validate_audio_reactive(data.get("audio_reactive", {}))
        self._validate_audio_bindings(data.get("audio_bindings", []), self.panels, self.components)
        self._normalize_image_assets(data, getattr(self, "active_theme_folder", self.theme_dir))
        self._validate_visualizations(data.get("visualizations", []), mode)
        self._parse_color_gradients(data.get('color_gradients', []))
        if type(data.get("full_canvas", mode == "absolute")) is not bool:
            raise ValueError("full_canvas must be boolean")
        transitions = data.get('transitions', {})
        self._validate_transitions(transitions, mode, self.panels, floating)
        settings = data.get('settings', {})
        if not isinstance(settings, dict):
            raise ValueError('Invalid theme settings')
        allowed = {'cover_base', 'cover_min', 'cover_max', 'cover_reference_width'}
        if set(settings) - allowed or any(type(v) is not int or not 1 <= v <= 4096 for v in settings.values()):
            raise ValueError('Invalid theme settings')
        positions = {k: tuple(panels.get(k, {}).get('grid', v)) for k, v in self.positions.items()}
        occupied = set()
        for key, (r, c, rs, cs) in positions.items():
            if panels.get(key, {}).get('visible', True) is False:
                continue
            cells = {(y, x) for y in range(r, r + rs) for x in range(c, c + cs)}
            if mode == 'grid' and cells & occupied:
                raise ValueError(f'Overlapping panel: {key}')
            occupied |= cells
        if mode == 'absolute':
            if (set(panels) - set(self.panels) or
                    any('bounds' not in panels[k] for k in set(self.panels) - self.OPTIONAL_PANELS)):
                raise ValueError('Absolute themes require bounds for all required panels')
        
        if hasattr(self.window, '_set_theme_tab_position'):
            self.window._set_theme_tab_position(data.get('system_tab_x'))
        self._pending_transitions = {}
        self._stop_transitions()
        self._restore_docked()
        
        if self.linear_seek is not None:
            self.linear_seek.hide()
        if self.seek_visualization is not None:
            self.seek_visualization.hide()
        if self.waveform_seek is not None:
            self.waveform_seek.hide()

        self.floating_bounds = {}
        self.floating_layers = {}
        
        # FIX: HIDE ALL FLOATING COMPONENTS UNLESS THEY ARE IN THE THEME!
        for key, (widget, _, _) in self.dock_slots.items():
            if explicit_visibility or key not in ('search_panel', 'quick_panel', 'tabs'):
                widget.hide()

        if mode != self.layout_mode:
            if mode == 'absolute':
                for key, widget in self.panels.items():
                    self._original_sizes[key] = (widget.minimumSize(), widget.maximumSize())
                    self.grid.removeWidget(widget)
                    widget.setParent(self.canvas)
                self.layout_mode = 'absolute'
            else:
                for key, widget in self.panels.items():
                    widget.setParent(self.canvas)
                    original = self._original_sizes.get(key)
                    if original:
                        widget.setMinimumSize(original[0])
                        widget.setMaximumSize(original[1])
                    if key not in self.OPTIONAL_PANELS:
                        self.grid.addWidget(widget, *positions[key])
                self.layout_mode = 'grid'
        for key, widget in self.panels.items():
            if key in self.OPTIONAL_PANELS and key not in panels:
                widget.hide()
                continue
            if mode == "grid" and positions[key] != self.positions[key]:
                self.grid.removeWidget(widget)
                self.grid.addWidget(widget, *positions[key])
            self._apply_widget(widget, panels.get(key, {}))
        for key, widget in self.components.items():
            if key in self._hidden_internal_components:
                widget.hide()
                continue
            # Optional track-text overlays must be explicitly configured.
            # A missing entry is not permission to display an unpositioned
            # QLabel at the top-left corner of the full-window canvas.
            if key in ('artist_title_text', 'artist_text', 'title_text'):
                if key not in components and key not in floating:
                    widget.hide()
                    continue
                # Floating visibility is applied below from its own spec.
                if key in floating:
                    widget.hide()
                    continue
            if explicit_visibility and key not in components and key not in floating:
                widget.hide()
                continue
            self._apply_widget(widget, {k: v for k, v in components.get(key, {}).items()
                                        if k != 'vu_presentation'})
            if key in ('vu_left', 'vu_right') and hasattr(widget, 'set_presentation'):
                widget.set_presentation(components.get(key, {}).get('vu_presentation', 'classic'))
                vu_spec = floating.get(key, {})
                if hasattr(widget, 'set_transform'):
                    widget.set_transform(vu_spec.get('vu_rotation', 0),
                                         vu_spec.get('vu_flip_horizontal', False),
                                         vu_spec.get('vu_flip_vertical', False))
            
        for key in ('artist_title_text', 'artist_text', 'title_text'):
            spec = data.get('editor_fonts', {}).get(key, {})
            widget = self.components.get(key)
            if widget is not None:
                font = widget.font()
                font.setFamily(str(spec.get('family', 'Sans Serif')))
                font.setPointSize(max(1, int(spec.get('size', 20))))
                font.setBold(bool(spec.get('bold', True)))
                widget.setFont(font)
                widget.setStyleSheet(
                    f'color: {QColor(data.get("editor_text_colors", {}).get(key, "#ffffff")).name(QColor.HexArgb)}; background: transparent; '
                    f'font-size: {max(1, int(spec.get("size", 20)))}pt; '
                    f'font-weight: {"bold" if spec.get("bold", True) else "normal"};'
                )

        if mode == 'absolute':
            for key, config in floating.items():
                if key not in self.dock_slots:
                    continue
                widget, layout, index = self.dock_slots[key]
                if layout is not None:
                    layout.removeWidget(widget)
                widget.setParent(self.canvas)
                self._floating.add(key)
                self.floating_bounds[key] = tuple(config['bounds'])
                self.floating_layers[key] = config.get('z_index', 50)
                widget.setVisible(config.get('visible', True))

        # Canvas-only controls have no original Qt layout. Never let a
        # previous theme or _apply_widget() expose them at (0, 0).
        # A valid floating placement is the sole opt-in for their visibility.
        for key, (widget, original_layout, _) in self.dock_slots.items():
            if original_layout is None and key not in floating:
                widget.hide()

        # The old VU footer must not occupy empty space when both decks float.
        strip = self.components.get('vu_strip')
        if strip is not None:
            strip.setVisible(not {'vu_left', 'vu_right'}.issubset(self._floating))

        self.reference_canvas = (ref['width'], ref['height']) if ref else None
        self.theme_fonts = dict(data.get('editor_fonts', {}))
        self.absolute_bounds = {key: tuple(spec["bounds"]) for key, spec in panels.items() if key in self.panels and "bounds" in spec} if mode == "absolute" else {}
        self.absolute_layers = {key: spec.get("z_index", 0) for key, spec in panels.items() if key in self.absolute_bounds} if mode == "absolute" else {}
        self.seek_presentation = {'style': 'linear', **seek_presentation}
        self.seek_visual_bounds = None
        self.seek_visual_z = 70
        self.sync_seek_widgets(self.resolved_seekbars(data))
        self.reflow()
        
        # Master slider and its marker overlay are internal controllers only.
        # Never resurrect them when a theme is applied; visible seekbars are
        # the independent views created by sync_seek_widgets().
        for internal_id in ('seek', 'seek_markers'):
            internal_widget = self.components.get(internal_id)
            if internal_widget is not None:
                internal_widget.hide()
        self.settings = settings.copy()
        self.cover_presentation = {'mask': 'rounded', 'radius': 9, **cover_presentation}
        cover = self.components.get('cover_art')
        if cover is not None and hasattr(cover, 'set_cover_presentation'):
            cover.set_cover_presentation(self.cover_presentation)
            if hasattr(cover, 'set_cover_animation'):
                cover.set_cover_animation(cover_animation)
        self.full_canvas = bool(data.get("full_canvas", mode == "absolute"))
        self.color_gradients = self._parse_color_gradients(data.get('color_gradients', []))
        self.audio_reactive = {"enabled": False, **data.get("audio_reactive", {})}
        if 'gradient' in self.audio_reactive:
            self.audio_reactive['gradient'] = self._parse_single_audio_gradient(self.audio_reactive['gradient'])
        self.audio_bindings = [{**dict(color='#ff175b', strength=180, padding=0, width=3, radius=8), **spec}
                               for spec in data.get('audio_bindings', [])]
        for spec in self.audio_bindings:
            if 'gradient' in spec:
                spec['gradient'] = self._parse_single_audio_gradient(spec['gradient'])
        self.audio_energy = 0.0
        self.audio_frame = {'level': 0.0, 'left_db': -120.0, 'right_db': -120.0, 'bass': 0.0, 'mids': 0.0, 'highs': 0.0}
        
        for overlay in self.audio_overlays:
            overlay.detach()
        self.audio_overlays.clear()
        for spec in self.audio_bindings:
            target = self.components.get(spec['target']) or self.panels.get(spec['target'])
            if target is not None:
                self.audio_overlays.append(AudioAccentOverlay(self, target, spec))
        self._gpu_composited = ()
        for widget in self.visual_widgets:
            widget.hide()
            widget.deleteLater()
        self.visual_widgets = []
        self.visual_frame = {'spectrum': (), 'waveform': ()}
        # Visuals retain their QWidget for geometry and hit testing. The
        # shared GL compositor selects compatible layers automatically; any
        # visual requiring a higher Qt z-index renders in its QWidget instead.
        for spec in data.get('visualizations', []):
            widget = AudioVisualization(self, spec)
            self.visual_widgets.append(widget)
            widget.setVisible(spec.get('visible', True))
        if self.canvas is not None:
            self.canvas.update()
        self.active_theme = name
        if self.visual_widgets:
            self.reflow()
        self._editor_effect_theme_data = data
        self.apply_editor_effects(data)
        self._pending_transitions = transitions
        if hasattr(self.window, 'update_cover_display'):
            self.window.update_cover_display()
        if hasattr(self.window, '_enforce_system_layers'):
            self.window._enforce_system_layers()

    def _disable_gpu_editor_fx(self, reason):
        """Fail closed: restore Qt effects if GPU shader creation/painting fails."""
        if not self._gpu_editor_fx_enabled:
            return
        self._gpu_editor_fx_enabled = False
        print('[SpyderMUSIC GPU FX] Falling back to Qt glow/shadow: ' + str(reason), flush=True)
        # Do not mutate or reinstall graphics effects in the GL paint callback.
        data = getattr(self, '_editor_effect_theme_data', None)
        if data is not None:
            QTimer.singleShot(0, lambda: self.apply_editor_effects(data))

    def apply_editor_effects(self, theme_data):
        # Reassess the compositor whenever the editor changes Shadow/Glow/Off.
        # Without this the previous QGraphicsDropShadowEffect blocks GPU paint.
        self._choose_gpu_compositor_layers()
        targets = []
        for category, collection in (("panels", self.panels),
                                     ("floating_components", {k: v[0] for k, v in self.dock_slots.items()})):
            for key, widget in collection.items():
                targets.append((category, key, widget))
        targets.extend(("seekbars", i, w) for i, w in enumerate(self.seek_widgets))
        targets.extend(("visualizations", i, w) for i, w in enumerate(self.visual_widgets))
        for category, key, widget in targets:
            if widget is None:
                continue
            specs = theme_data.get(category, {})
            if isinstance(specs, list):
                config = specs[key] if isinstance(key, int) and key < len(specs) else {}
            else:
                config = specs.get(key, {})
            effect_spec = config.get("editor_effect", {}) if isinstance(config, dict) else {}
            # GPU-composited visualizations must not ALSO carry a Qt raster
            # graphics effect (which would trigger an expensive second pass).
            if category == "visualizations" and widget in self._gpu_composited:
                if widget.property("spyder_editor_effect"):
                    widget.setGraphicsEffect(None)
                    widget.setProperty("spyder_editor_effect", False)
                continue
            if effect_spec.get("type") not in ("shadow", "glow") or not effect_spec.get("enabled", False):
                if widget.property("spyder_editor_effect"):
                    widget.setGraphicsEffect(None)
                    widget.setProperty("spyder_editor_effect", False)
                continue
            fx = widget.graphicsEffect()
            if not (widget.property("spyder_editor_effect") and isinstance(fx, QGraphicsDropShadowEffect)):
                if fx is not None:
                    continue
                fx = QGraphicsDropShadowEffect(widget)
                widget.setGraphicsEffect(fx)
                widget.setProperty("spyder_editor_effect", True)
            col = QColor(effect_spec.get("color", "#000000"))
            col.setAlpha(round(255 * max(0, min(100, int(effect_spec.get("opacity", 65)))) / 100))
            fx.setColor(col)
            fx.setBlurRadius(max(0, min(100, float(effect_spec.get("blur", 24)))))
            if effect_spec["type"] == "glow":
                fx.setOffset(0, 0)
            else:
                import math
                angle = math.radians(float(effect_spec.get("angle", 45)))
                distance = float(effect_spec.get("distance", 12))
                fx.setOffset(math.cos(angle) * distance, math.sin(angle) * distance)
        if self._gpu_composited:
            self.request_gpu_compositor_frame()

    def font_scale(self):
        if not self.reference_canvas or self.canvas is None:
            return 1.0
        rw, rh = self.reference_canvas
        return min(self.canvas.width()/rw, self.canvas.height()/rh)

    def scale_theme_fonts(self):
        for key, spec in self.theme_fonts.items():
            if key not in ('clock', 'artist_title_text', 'artist_text', 'title_text'):
                continue
            widget = self.components.get(key)
            if widget is None or not isinstance(spec, dict) or 'size' not in spec:
                continue
            size = max(1, round(float(spec['size']) * self.font_scale()))
            font = widget.font()
            font.setFamily(str(spec.get('family', font.family())))
            font.setPointSize(size)
            font.setBold(bool(spec.get('bold', font.bold())))
            widget.setFont(font)
            css = widget.styleSheet()
            import re
            if re.search(r'font-size\s*:', css):
                css = re.sub(r'font-size\s*:\s*[^;]+', f'font-size: {size}pt', css)
            else:
                css += f'; font-size: {size}pt;'
            widget.setStyleSheet(css)

    def reflow(self):
        if self.layout_mode != 'absolute' or self.canvas is None:
            return
        if self._in_transition:
            self._stop_transitions()
        width, height = max(1, self.canvas.width()), max(1, self.canvas.height())
        for key, (x, y, w, h) in self.absolute_bounds.items():
            widget = self.panels[key]
            widget.setMinimumSize(0, 0)
            widget.setMaximumSize(16777215, 16777215)
            pw = max(1, round(width*w/1000))
            ph = max(1, round(height*h/1000))
            if key == 'cover':
                pw = ph = min(pw, ph)
            widget.setGeometry(round(width*x/1000), round(height*y/1000), pw, ph)
        for key, (x, y, w, h) in self.floating_bounds.items():
            widget = self.dock_slots[key][0]
            # SYSTEM_TABS_CANVAS_GEOMETRY_V2
            # Ignore theme JSON bounds for tabs: the system container fills
            # the entire inner canvas, including when the window is resized.
            if key == 'tabs':
                widget.setMinimumSize(0, 0)
                widget.setMaximumSize(16777215, 16777215)
                widget.setGeometry(0, 0, width, height)
                continue
            widget.setGeometry(round(width*x/1000), round(height*y/1000),
                               max(1, round(width*w/1000)), max(1, round(height*h/1000)))
        for spec, widget in zip(self.seek_instances, self.seek_widgets):
            x, y, w, h = spec['bounds']
            widget.setGeometry(round(width*x/1000), round(height*y/1000),
                               max(1, round(width*w/1000)), max(1, round(height*h/1000)))
        cover_bounds = self.absolute_bounds.get('cover')
        cover_widget = self.panels.get('cover')
        for widget in self.visual_widgets:
            widget.setVisible(widget.spec.get("visible", True))
            x, y, w, h = widget.spec['bounds']
            geometry = (round(width*x/1000), round(height*y/1000),
                        max(1, round(width*w/1000)), max(1, round(height*h/1000)))
            if (self.reference_canvas and cover_bounds and cover_widget and
                    widget.spec.get('type') == 'waveform'):
                cx, cy, cw, ch = cover_bounds
                if abs(x-cx) <= 3 and abs(w-cw) <= 3 and cy <= y <= cy+ch:
                    rw, rh = self.reference_canvas
                    ref_cover_side = max(1, min(rw*cw/1000, rh*ch/1000))
                    current_side = max(1, cover_widget.width())
                    factor = current_side/ref_cover_side
                    geometry = (cover_widget.x()+round((x-cx)*rw/1000*factor),
                                cover_widget.y()+round((y-cy)*rh/1000*factor),
                                max(1, round(w*rw/1000*factor)),
                                max(1, round(h*rh/1000*factor)))
            widget.setGeometry(*geometry)
        self.scale_theme_fonts()
        layers = [(self.absolute_layers.get(key, 0), key, self.panels[key])
                  for key in self.absolute_bounds]
        layers += [(self.floating_layers.get(key, 50), key, self.dock_slots[key][0])
                   for key in self.floating_bounds]
        layers += [(spec.get('z_index', 70), 'seekbar_%02d' % i, widget)
                   for i, (spec, widget) in enumerate(zip(self.seek_instances, self.seek_widgets))]
        layers += [(widget.spec.get('z_index', 80), 'visual_'+str(i), widget)
                   for i, widget in enumerate(self.visual_widgets)]
        # 'tabs' is a protected system widget, even if an old theme supplies
        # z_index=+100 or places it inside an arbitrary editor layer.
        tabs = getattr(self.window, 'tabs', None)
        for _, _, widget in sorted(layers, key=lambda entry: (entry[0], entry[1])):
            if widget is not tabs:
                widget.raise_()
        if hasattr(self.window, '_enforce_system_layers'):
            self.window._enforce_system_layers()
        self._choose_gpu_compositor_layers()

    @staticmethod
    def _apply_widget(widget, config):
        for dim, method in (('min_width', 'setMinimumWidth'), ('min_height', 'setMinimumHeight'),
                            ('width', 'setFixedWidth'), ('height', 'setFixedHeight')):
            if dim in config:
                getattr(widget, method)(config[dim])
        widget.setVisible(config.get('visible', True))

    def set_panel_visible(self, name, visible):
        self.panels[name].setVisible(bool(visible))
        self._choose_gpu_compositor_layers()

    def set_component_visible(self, name, visible):
        if name in self._hidden_internal_components:
            self.components[name].hide()
            return
        self.components[name].setVisible(bool(visible))
        if name in self.floating_bounds:
            self._choose_gpu_compositor_layers()

    def available_themes(self):
        names = []
        for path in sorted(self.theme_dir.rglob('*.json')):
            if not re.fullmatch(r'[\w -]+', path.stem, flags=re.UNICODE):
                continue
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if data.get('api_version') == 1 and path.with_suffix('.qss').is_file():
                    theme_id = path.relative_to(self.theme_dir).with_suffix('').as_posix()
                    names.append((theme_id, str(data.get('name', path.stem))))
            except (OSError, ValueError, TypeError):
                continue
        return names

    def stylesheet(self, name):
        path = self.theme_file(name, '.qss')
        return path.read_text(encoding='utf-8') + (
            "\n/* SpyderMUSIC protected system container */\n"
            "QTabWidget#main_tabs { background: transparent; }\n"
            "QTabWidget#main_tabs::pane { background: transparent; background-color: transparent; }\n"
        )

    def install_zip(self, archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            entries = {info.filename: info for info in archive.infolist() if not info.is_dir()}
            if set(entries) != {'theme.json', 'theme.qss'}:
                raise ValueError('ZIP must contain only theme.json and theme.qss')
            if any(info.file_size > 512 * 1024 or info.compress_size > 512 * 1024
                   for info in entries.values()):
                raise ValueError('Theme package exceeds size limits')
            manifest_raw = archive.read('theme.json')
            qss_raw = archive.read('theme.qss')
        data = json.loads(manifest_raw.decode('utf-8'))
        if not isinstance(data, dict) or data.get('api_version') != 1:
            raise ValueError('Unsupported theme manifest')
        slug = data.get('id')
        if not isinstance(slug, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', slug):
            raise ValueError('Theme requires a safe lowercase id')
        if slug == 'default':
            raise ValueError('Default theme cannot be overwritten')
        manifest_path = self.theme_dir / (slug + '.json')
        style_path = self.theme_dir / (slug + '.qss')
        if manifest_path.exists() or style_path.exists():
            raise FileExistsError(f'Theme already installed: {slug}')
        qss = qss_raw.decode('utf-8')
        if re.search(r'url\s*\(', qss, flags=re.I) or '@import' in qss.lower():
            raise ValueError('External resources are not supported in stage 2 themes')
        self._validate_manifest(data)
        self.theme_dir.mkdir(parents=True, exist_ok=True)
        style_path.write_text(qss, encoding='utf-8')
        try:
            manifest_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        except Exception:
            style_path.unlink(missing_ok=True)
            raise
        return slug

    def _validate_manifest(self, data):
        if data.get('theme_visibility_policy', 'legacy') not in ('legacy', 'explicit'):
            raise ValueError('Invalid theme visibility policy')
        panels, components = data.get('panels', {}), data.get('components', {})
        floating = data.get('floating_components', {})
        mode = data.get('layout_mode', 'grid')
        if mode not in ('grid', 'absolute'):
            raise ValueError('Unsupported layout mode')
        if mode == 'absolute' and (set(panels) - set(self.panels) or any('bounds' not in panels[k] for k in set(self.panels) - self.OPTIONAL_PANELS)):
            raise ValueError('Absolute themes require bounds for all panels')
        if not isinstance(panels, dict) or not isinstance(components, dict) or not isinstance(floating, dict):
            raise ValueError('Invalid theme manifest')
        for key, config in panels.items():
            if key not in self.panels:
                raise ValueError(f'Unknown panel: {key}')
            self._validate_config(key, config, True)
        for key, config in components.items():
            if key not in self.components:
                raise ValueError(f'Unknown component: {key}')
            if key in ('vu_left', 'vu_right') and 'vu_presentation' in config:
                if config['vu_presentation'] not in ('classic', 'phosphor', 'phosphor_mono'):
                    raise ValueError('Invalid VU presentation')
                config = {k: v for k, v in config.items() if k != 'vu_presentation'}
            self._validate_config(key, config, False)
        if floating and mode != 'absolute':
            raise ValueError('Floating components require absolute layout mode')
        for key, config in floating.items():
            if key not in self.dock_slots:
                raise ValueError(f'Component cannot be floated: {key}')
            self._validate_floating(key, config)
            if components.get(key, {}).get('visible') is False:
                raise ValueError(f'Floating component cannot be hidden: {key}')
        self._validate_audio_reactive(data.get("audio_reactive", {}))
        self._validate_audio_bindings(data.get("audio_bindings", []), self.panels, self.components)
        self._validate_visualizations(data.get("visualizations", []), mode)
        self._parse_color_gradients(data.get('color_gradients', []))
        self._validate_transitions(data.get('transitions', {}), mode, self.panels, floating)
        self._validate_cover_presentation(data.get('cover_presentation', {}))
        self._validate_cover_animation(data.get('cover_animation', {}))
        self._validate_seek_presentation(data.get('seek_presentation', {}), mode)
        self._validate_seekbars(data, mode)
        settings = data.get('settings', {})
        if not isinstance(settings, dict):
            raise ValueError('Invalid theme settings')
        allowed = {'cover_base', 'cover_min', 'cover_max', 'cover_reference_width'}
        if set(settings) - allowed or any(type(v) is not int or not 1 <= v <= 4096 for v in settings.values()):
            raise ValueError('Invalid theme settings')
        positions = {k: tuple(panels.get(k, {}).get('grid', v)) for k, v in self.positions.items()}
        occupied = set()
        for key, (r, c, rs, cs) in positions.items():
            if panels.get(key, {}).get('visible', True) is False:
                continue
            cells = {(y, x) for y in range(r, r + rs) for x in range(c, c + cs)}
            if mode == 'grid' and cells & occupied:
                raise ValueError(f'Overlapping panel: {key}')
            occupied |= cells
