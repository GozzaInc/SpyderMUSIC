"""Stage 16: non-destructive WYSIWYG editor for absolute-layout themes (FIXED GEOMETRY & MAPPING)."""
import copy
import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path
from qss_visual_studio import QssStudio
from PySide6.QtCore import Qt, QRect, QPoint, QSignalBlocker
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QWidget, QMenu, QColorDialog, QInputDialog, QFileDialog,
    QMessageBox, QFontDialog, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox, QDialogButtonBox,
    QPlainTextEdit, QTabWidget, QApplication, QSlider, QGroupBox, QFrame, QGraphicsDropShadowEffect, QListWidget, QListWidgetItem, QAbstractItemView, QGridLayout
)


class ElementGeometryDialog(QDialog):
    """Pixel-precise position and size editor in ThemeCanvas coordinates."""

    def __init__(self, parent, name, bounds, canvas_size, original_bounds=None):
        super().__init__(parent)
        self.setWindowTitle('Element Geometry')
        self.setMinimumWidth(290)
        self.setModal(True)
        self._canvas_width = max(1, canvas_size.width())
        self._canvas_height = max(1, canvas_size.height())
        x, y, w, h = bounds
        layout = QVBoxLayout(self)
        heading = QLabel('Element Geometry')
        heading.setStyleSheet('font-weight: 600; font-size: 14px;')
        layout.addWidget(heading)
        element_label = QLabel(str(name))
        element_label.setStyleSheet('color: #999999;')
        layout.addWidget(element_label)
        grid = QGridLayout()
        self.fields = {}
        for index, (key, title, initial, maximum) in enumerate((
            ('w', 'Width (B)', round(w * self._canvas_width / 1000), self._canvas_width),
            ('h', 'Height (H)', round(h * self._canvas_height / 1000), self._canvas_height),
            ('x', 'Position X', round(x * self._canvas_width / 1000), self._canvas_width),
            ('y', 'Position Y', round(y * self._canvas_height / 1000), self._canvas_height * 2),
        )):
            col, row = index % 2, index // 2
            label = QLabel(title)
            spin = QSpinBox()
            spin.setRange(1 if key in ('w', 'h') else 0, max(1, maximum))
            spin.setValue(int(initial))
            spin.setSuffix(' px')
            grid.addWidget(label, row * 2, col)
            grid.addWidget(spin, row * 2 + 1, col)
            self.fields[key] = spin
        layout.addLayout(grid)
        # Reference geometry is captured once per element, not on every edit.
        reference = original_bounds if original_bounds is not None else bounds
        self._original_width = max(1, round(reference[2] * self._canvas_width / 1000))
        self._original_height = max(1, round(reference[3] * self._canvas_height / 1000))
        self._locked_ratio = None
        controls = QHBoxLayout()
        self.ratio_lock = QPushButton('🔓 Lock proportions')
        self.ratio_lock.setCheckable(True)
        self.ratio_lock.toggled.connect(self._toggle_ratio_lock)
        controls.addWidget(self.ratio_lock)
        self.reset_size_button = QPushButton('Reset Size')
        self.reset_size_button.clicked.connect(self._reset_size)
        controls.addWidget(self.reset_size_button)
        self.original_ratio_button = QPushButton('Original Ratio')
        self.original_ratio_button.clicked.connect(self._restore_original_ratio)
        controls.addWidget(self.original_ratio_button)
        layout.addLayout(controls)
        self.fields['w'].valueChanged.connect(self._width_changed)
        self.fields['h'].valueChanged.connect(self._height_changed)
        note = QLabel('Coordinates relative to the theme canvas.')
        note.setStyleSheet('color: #999999;')
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('Apply')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _toggle_ratio_lock(self, enabled):
        if enabled:
            self._locked_ratio = self.fields['w'].value() / max(1, self.fields['h'].value())
        else:
            self._locked_ratio = None
        self.ratio_lock.setText('🔒 Unlock proportions' if enabled else '🔓 Lock proportions')

    def _width_changed(self, width):
        if self._locked_ratio is not None:
            with QSignalBlocker(self.fields['h']):
                self.fields['h'].setValue(max(1, round(width / self._locked_ratio)))

    def _height_changed(self, height):
        if self._locked_ratio is not None:
            with QSignalBlocker(self.fields['w']):
                self.fields['w'].setValue(max(1, round(height * self._locked_ratio)))

    def _set_size(self, width, height):
        with QSignalBlocker(self.fields['w']), QSignalBlocker(self.fields['h']):
            self.fields['w'].setValue(width)
            self.fields['h'].setValue(height)
        if self.ratio_lock.isChecked():
            self._locked_ratio = self.fields['w'].value() / max(1, self.fields['h'].value())

    def _reset_size(self):
        self._set_size(self._original_width, self._original_height)

    def _restore_original_ratio(self):
        # Keep current width and X/Y; correct height to the reference aspect.
        ratio = self._original_width / self._original_height
        width = self.fields['w'].value()
        self._set_size(width, max(1, round(width / ratio)))

    def normalized_bounds(self):
        values = {k: widget.value() for k, widget in self.fields.items()}
        return [round(values['x'] * 1000 / self._canvas_width),
                round(values['y'] * 1000 / self._canvas_height),
                max(1, round(values['w'] * 1000 / self._canvas_width)),
                max(1, round(values['h'] * 1000 / self._canvas_height))]


class GradientDialog(QDialog):
    def __init__(self, parent, initial=None):
        super().__init__(parent)
        self.setWindowTitle('Edit Gradient')
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Gradient Stops: Color, Opacity and Position'))
        self.direction = QComboBox()
        self.direction.addItems(['Left to Right', 'Right to Left', 'Top to Bottom', 'Bottom to Top'])
        layout.addWidget(self.direction)
        self.rows = []
        self.row_layout = QVBoxLayout()
        layout.addLayout(self.row_layout)
        add = QPushButton('+ Color Stop')
        add.clicked.connect(lambda: self.add_stop('#00f5ff', 1.0, 50))
        layout.addWidget(add)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        initial = initial or {}
        direction = initial.get('direction', 'horizontal')
        start = initial.get('start', initial.get('start_1', 'left'))
        self.direction.setCurrentIndex({
            ('horizontal', 'left'): 0, ('horizontal', 'right'): 1,
            ('vertical', 'top'): 2, ('vertical', 'bottom'): 3
        }.get((direction, start), 0))
        i = 1
        while 'color_' + str(i) in initial:
            rgba = [float(x.strip()) for x in str(initial['color_' + str(i)]).split(',')]
            if len(rgba) == 4:
                self.add_stop(QColor(int(rgba[0]), int(rgba[1]), int(rgba[2])).name(), rgba[3], initial.get('percent_' + str(i), 0))
            i += 1
        if not self.rows:
            self.add_stop('#ff175b', 1, 0)
            self.add_stop('#00f5ff', 1, 100)

    def add_stop(self, color, alpha, percent):
        if len(self.rows) >= 32:
            return
        row = QWidget(self)
        bar = QHBoxLayout(row)
        button = QPushButton('Color')
        button.setStyleSheet('background-color: ' + color + ';')
        button._color = color

        def choose():
            c = QColorDialog.getColor(QColor(button._color), self, 'Choose Color')
            if c.isValid():
                button._color = c.name()
                button.setStyleSheet('background-color: ' + c.name() + ';')

        button.clicked.connect(choose)
        opacity = QDoubleSpinBox()
        opacity.setRange(0, 100)
        opacity.setSuffix(' % Alpha')
        opacity.setValue(float(alpha) * 100)
        pos = QSpinBox()
        pos.setRange(0, 100)
        pos.setSuffix(' % Position')
        pos.setValue(int(percent))
        remove = QPushButton('✕')
        bar.addWidget(button)
        bar.addWidget(opacity)
        bar.addWidget(pos)
        bar.addWidget(remove)
        self.row_layout.addWidget(row)
        self.rows.append((row, button, opacity, pos))

        def delete():
            self.rows[:] = [entry for entry in self.rows if entry[0] is not row]
            row.deleteLater()

        remove.clicked.connect(delete)

    def result_gradient(self):
        index = self.direction.currentIndex()
        direction = 'horizontal' if index < 2 else 'vertical'
        start = ['left', 'right', 'top', 'bottom'][index]
        stops = sorted([(pos.value(), button._color, alpha.value() / 100) for _, button, alpha, pos in self.rows])
        result = {'direction': direction, 'start': start, 'start_1': start}
        for i, (p, c, a) in enumerate(stops, 1):
            qc = QColor(c)
            result['color_' + str(i)] = f'{qc.red()}, {qc.green()}, {qc.blue()}, {a:.3f}'
            result['percent_' + str(i)] = p
        return result


class MovableLayerPanel(QWidget):
    """Floating layer palette; drag using its title strip, not the layer list."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_start = None
        self._drag_position = None
        self._title = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._title is not None and event.position().y() <= self._title.height() + 14:
            self._drag_start = event.globalPosition().toPoint()
            self._drag_position = self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start is not None and event.buttons() & Qt.LeftButton:
            delta = event.globalPosition().toPoint() - self._drag_start
            pos = self._drag_position + delta
            parent = self.parentWidget()
            if parent is not None:
                pos.setX(max(0, min(pos.x(), max(0, parent.width() - self.width()))))
                pos.setY(max(0, min(pos.y(), max(0, parent.height() - self.height()))))
            self.move(pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_start = None
        self._drag_position = None
        super().mouseReleaseEvent(event)


class ThemeEditor(QWidget):
    """Transparent canvas overlay. Edits a working JSON copy, never playback widgets."""
    def __init__(self, window):
        self.window = window
        self.manager = window.theme_manager
        super().__init__(window.backdrop)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.selected = None
        self.multi_selected = set()
        self.group_initial = {}
        self.drag_origin = None
        self._system_tab_drag = None
        self.initial_bounds = None
        self.resize_mode = False
        self.resize_handle = None
        self.snap_locks = {"x": None, "y": None}
        self.snap_guides = []
        self.data = {}
        self.qss_draft = None
        self.undo_stack = []
        self.redo_stack = []
        self.setGeometry(window.backdrop.rect())
        self._layer_panel = MovableLayerPanel(window.backdrop)
        self._layer_panel.setObjectName('spyder_layer_panel')
        self._layer_panel.setStyleSheet("""#spyder_layer_panel {background:#20252c; border:1px solid #00f5ff; border-radius:5px;}
            #spyder_layer_panel QLabel {color:#e5faff; border:0;}
            #spyder_layer_panel QListWidget {background:#121920; color:#e5faff; border:1px solid #405560;}
            #spyder_layer_panel QPushButton {background:#293b48; color:white; padding:4px; border:1px solid #49616d;}""")
        panel_layout = QVBoxLayout(self._layer_panel)
        panel_layout.setContentsMargins(7, 7, 7, 7)
        panel_layout.setSpacing(5)
        layer_title = QLabel('☰  LAYERS  ·  Drag to Reorder')
        layer_title.setCursor(Qt.OpenHandCursor)
        panel_layout.addWidget(layer_title)
        self._layer_panel._title = layer_title
        # Forward title dragging to the floating panel (labels normally consume mouse input).
        layer_title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._layer_list = QListWidget()
        self._layer_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        panel_layout.addWidget(self._layer_list)
        controls = QHBoxLayout()
        for label, fn in [('👁', self._toggle_layer_visibility), ('🔒', self._toggle_layer_lock)]:
            button = QPushButton(label)
            button.clicked.connect(fn)
            controls.addWidget(button)
        panel_layout.addLayout(controls)
        manage_row = QHBoxLayout()
        for label, callback, tooltip in [
            ('＋ Layer', self._create_empty_layer, 'Create Empty Layer'),
            ('－ Layer', self._delete_empty_layers, 'Delete Selected Empty Layers'),
        ]:
            button = QPushButton(label)
            button.setToolTip(tooltip)
            button.clicked.connect(callback)
            manage_row.addWidget(button)
        panel_layout.addLayout(manage_row)
        self._layer_panel.setGeometry(12, 55, 252, 460)
        self._layer_panel.hide()
        self._layer_refreshing = False
        self._layer_list.itemSelectionChanged.connect(self._select_from_layers)
        self._layer_list.itemDoubleClicked.connect(self._rename_layer)
        self._layer_clipboard = None
        self._layer_clipboard_cut = False
        self._paste_destination = None  # Independent of canvas selection and right-clicks.
        edit_row = QHBoxLayout()
        for label, callback, tip in [
            ('✂', self._cut_layer_objects, 'Cut Selection'),
            ('⧉', self._copy_layer_objects, 'Copy Selection'),
            ('📋', self._paste_layer_objects, 'Paste into Selected Layer'),
            ('↑', lambda: self.layer(1), 'Move Layer Forward'),
            ('↓', lambda: self.layer(-1), 'Move Layer Backward'),
        ]:
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(callback)
            edit_row.addWidget(button)
        panel_layout.addLayout(edit_row)

        window.backdrop.installEventFilter(self)
        self.hide()

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self.window.backdrop and event.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
        return False

    def start(self):
        # Always switch to the Live Cockpit tab first so canvas geometry is valid and visible
        if hasattr(self.window, 'tabs') and hasattr(self.window, 'live'):
            self.window.tabs.setCurrentWidget(self.window.live)
            QApplication.processEvents()

        name = self.manager.active_theme
        path = self.manager.theme_file(name, '.json')
        self.data = json.loads(path.read_text(encoding='utf-8'))
        self.manager._normalize_image_assets(self.data, path.parent)
        self.original_theme = name
        source = path.with_suffix('.qss')
        self.qss_draft = source.read_text(encoding="utf-8") if source.exists() else ""
        if self.data.get('layout_mode') != 'absolute':
            QMessageBox.information(self, 'Theme Studio', 'The editor requires a theme with absolute layout. Please select Stage 15.')
            return

        # Stop any running slide/fade transition before reading or editing geometries!
        self.manager._stop_transitions()

        self.undo_stack = []
        self.redo_stack = []
        self.selected = None
        self.multi_selected.clear()
        self._paste_destination = None
        self._layer_clipboard = None
        self._layer_clipboard_cut = False
        self.group_initial = {}
        self.snap_locks = {"x": None, "y": None}
        self.snap_guides = []
        # Migrate old single seekbar into the new editable instance list.
        if 'seekbars' not in self.data:
            legacy = self.data.get('seek_presentation', {})
            self.data['seekbars'] = [copy.deepcopy(legacy)] if 'bounds' in legacy else []
        # Older themes predate the fourth transport control. Register it as a
        # normal editable floating component so visibility, layers and Save work.
        floating = self.data.setdefault('floating_components', {})
        if 'transport_stop' not in floating:
            sibling = floating.get('transport_next') or floating.get('transport_play_pause')
            if sibling and 'bounds' in sibling:
                spec = copy.deepcopy(sibling)
                x, y, w, h = spec['bounds']
                # Put Stop next to the other transport controls by default.
                spec['bounds'] = [max(0, min(1000 - w, x - w - 6)), y, w, h]
                spec['visible'] = True
                floating['transport_stop'] = spec
        self._init_layer_groups()
        self.repair_bounds()
        self._refresh_layers()
        self.setGeometry(self.window.backdrop.rect())
        self.apply_preview()
        self.show()
        self.raise_()
        self.setFocus()
        self.window._theme_editor_toolbar.show()
        self.window._theme_editor_toolbar.raise_()
        self._layer_panel.show()
        self._layer_panel.raise_()
        self.window._theme_editor_toolbar.raise_()
        self.update()

    def stop(self, revert=True):
        self.hide()
        self._layer_panel.hide()
        self.window._theme_editor_toolbar.hide()
        if revert:
            self.window.switch_theme(self.original_theme)

    def snapshot(self):
        self.undo_stack.append(copy.deepcopy(self.data))
        self.undo_stack = self.undo_stack[-60:]
        self.redo_stack.clear()

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(copy.deepcopy(self.data))
            self.data = self.undo_stack.pop()
            self.apply_preview()

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(copy.deepcopy(self.data))
            self.data = self.redo_stack.pop()
            self.apply_preview()

    def _layer_id(self, category, key):
        return f'{category}:{key}'

    def _init_layer_groups(self):
        """Migrate object-based Stage 19 layers without changing draw order."""
        layers = self.data.setdefault('editor_layer_groups', {})
        order = self.data.setdefault('editor_layer_order', [])
        old = self.data.get('editor_layers', {})
        for cat, key, spec in sorted(self.targets(), key=lambda t: int(t[2].get('z_index', 0))):
            legacy = old.get(self._layer_id(cat, key), {})
            name = spec.get('editor_layer') or legacy.get('name') or f'{cat} / {key}'
            spec['editor_layer'] = name
            spec.setdefault('editor_inner_z', int(spec.get('z_index', 0)))
            if name not in layers:
                layers[name] = {'locked': bool(legacy.get('locked', False))}
            if name not in order:
                order.append(name)
        # Keep empty layers available for future pasting.
        for name in layers:
            if name not in order:
                order.append(name)
        self._rebuild_layer_z()

    def _group(self, cat, key):
        for c, k, spec in self.targets():
            if (c, k) == (cat, key):
                return spec.get('editor_layer', f'{cat} / {key}')
        return None

    def _layer_meta(self, category, key):
        return self.data.setdefault('editor_layer_groups', {}).setdefault(self._group(category, key), {})

    def _is_locked(self, category, key):
        group = self._group(category, key)
        return bool(self.data.get('editor_layer_groups', {}).get(group, {}).get('locked', False))

    def _rebuild_layer_z(self):
        """Flatten group order + within-group order to the existing renderer's z_index."""
        order = self.data.get('editor_layer_order', [])
        objects = self.targets()
        objects.sort(key=lambda t: (order.index(t[2].get('editor_layer')) if t[2].get('editor_layer') in order else len(order),
                                    int(t[2].get('editor_inner_z', t[2].get('z_index', 0)))))
        for i, (_, _, spec) in enumerate(objects):
            spec['z_index'] = -90 + round(180 * i / max(1, len(objects) - 1))

    def _inner_z_members(self, group):
        """Members in front-to-back order within one named editor layer."""
        members = [t for t in self.targets() if t[2].get('editor_layer') == group]
        return sorted(members, key=lambda t: (int(t[2].get('editor_inner_z', 0)),
                                                     str(t[0]), str(t[1])))

    def _inner_z_controls(self, rect):
        """Overlay-only hit areas: do not modify the underlying object z-order."""
        from PySide6.QtCore import QRect
        right = min(self.width() - 2, rect.right())
        top = max(2, min(self.height() - 44, rect.top() + 3))
        return (QRect(right - 42, top, 19, 19), QRect(right - 21, top, 19, 19))

    def _change_inner_z(self, direction):
        """Swap adjacent members only; other layers remain untouched."""
        if not self.selected or self._is_locked(*self.selected):
            return
        group = self._group(*self.selected)
        members = self._inner_z_members(group)
        index = next((i for i, (cat, key, _) in enumerate(members)
                      if (cat, key) == self.selected), -1)
        other = index + direction
        if index < 0 or other < 0 or other >= len(members):
            return
        self.snapshot()
        # Normalize first to handle duplicate / legacy inner-z values.
        for i, (_, _, spec) in enumerate(members):
            spec['editor_inner_z'] = i
        members[index][2]['editor_inner_z'], members[other][2]['editor_inner_z'] = (
            members[other][2]['editor_inner_z'], members[index][2]['editor_inner_z'])
        self._rebuild_layer_z()
        self.apply_preview()
        self.update()

    def _refresh_layers(self):
        if not hasattr(self, '_layer_list'):
            return
        selected_groups = {item.data(Qt.UserRole) for item in self._layer_list.selectedItems()}
        self._layer_refreshing = True
        try:
            self._layer_list.clear()
            top = QListWidgetItem('🔒 SYSTEM TOP · Hamburger Menu')
            top.setFlags(Qt.NoItemFlags)
            self._layer_list.addItem(top)
            order = self.data.get('editor_layer_order', [])
            groups = self.data.get('editor_layer_groups', {})
            members = {}
            for cat, key, spec in self.targets():
                members.setdefault(spec.get('editor_layer'), []).append((cat, key, spec))
            for name in reversed(order):
                objects = members.get(name, [])
                meta = groups.get(name, {})
                visible = bool(objects) and any(s.get('visible', True) for _, _, s in objects)
                led1 = '🟢' if visible else '🔴'
                led2 = '🔴' if meta.get('locked', False) else '🟢'
                item = QListWidgetItem(f'{led1} {led2}  {name} ({len(objects)})')
                item.setData(Qt.UserRole, name)
                item.setToolTip('Visibility / Editing Lock')
                self._layer_list.addItem(item)
                if name in selected_groups or name == self._paste_destination or (not selected_groups and self.selected and self._group(*self.selected) == name):
                    item.setSelected(True)
            bottom = QListWidgetItem('🔒 SYSTEM BOTTOM · Tabs / Container')
            bottom.setFlags(Qt.NoItemFlags)
            self._layer_list.addItem(bottom)
        finally:
            self._layer_refreshing = False

    def _sync_layer_selection(self):
        if not hasattr(self, '_layer_list') or self._layer_refreshing:
            return
        names = {self._group(*t) for t in self.multi_selected}
        if self.selected:
            names.add(self._group(*self.selected))
        self._layer_refreshing = True
        try:
            for i in range(self._layer_list.count()):
                item = self._layer_list.item(i)
                item.setSelected(item.data(Qt.UserRole) in names)
        finally:
            self._layer_refreshing = False

    def _select_from_layers(self):
        if self._layer_refreshing:
            return
        names = [item.data(Qt.UserRole) for item in self._layer_list.selectedItems()]
        self._paste_destination = names[-1] if names else None
        selected = [(cat, key) for cat, key, spec in self.targets() if spec.get('editor_layer') in names]
        self.multi_selected = set(selected)
        self.selected = selected[-1] if selected else None
        self.update()

    def _selected_layer_targets(self):
        return [item.data(Qt.UserRole) for item in self._layer_list.selectedItems()]

    def _copy_layer_objects(self):
        selected = list(self.multi_selected) or ([self.selected] if self.selected else [])
        available = {(cat, key): spec for cat, key, spec in self.targets()}
        selected = [t for t in selected if t in available and not self._is_locked(*t)]
        if not selected:
            return
        self._layer_clipboard = [(cat, key, copy.deepcopy(available[cat, key])) for cat, key in selected]
        self._layer_clipboard_cut = False

    def _cut_layer_objects(self):
        self._copy_layer_objects()
        if not self._layer_clipboard:
            return
        self.snapshot()
        self._layer_clipboard_cut = True
        for cat, key, _ in self._layer_clipboard:
            spec = next((s for c, k, s in self.targets() if (c, k) == (cat, key)), None)
            if spec is not None:
                spec['visible'] = False
        self.multi_selected.clear()
        self.selected = None
        self.apply_preview()

    def _editable_destination_layer(self):
        """Only an explicitly selected, unlocked layer may receive objects."""
        if not hasattr(self, '_layer_list'):
            return None
        names = self._selected_layer_targets()
        if not names:
            return None
        destination = names[-1]
        groups = self.data.get('editor_layer_groups', {})
        if destination not in groups or groups[destination].get('locked', False):
            return None
        return destination

    def _place_new_element_in_layer(self, spec, destination):
        """Attach a new element to the layer and put it on top within it."""
        members = [s for _, _, s in self.targets()
                   if s is not spec and s.get('editor_layer') == destination]
        spec['editor_layer'] = destination
        spec['editor_inner_z'] = max(
            (int(s.get('editor_inner_z', 0)) for s in members), default=-1) + 1
        self._rebuild_layer_z()
        self._paste_destination = destination

    def _paste_layer_objects(self, centered=False):
        if not self._layer_clipboard:
            return
        destination = self._editable_destination_layer()
        if destination is None:
            return
        if not self._layer_clipboard_cut:
            if any(cat not in ('seekbars', 'visualizations') or (cat == 'visualizations' and spec.get('type') == 'vu') for cat, key, spec in self._layer_clipboard):
                QMessageBox.information(self, 'Copy', 'Unique player elements cannot be duplicated.')
                return
            for cat, key, _ in self._layer_clipboard:
                target_cat = cat
                count = sum(1 for c, k, _ in self._layer_clipboard if c == target_cat)
                if len(self.data.get(target_cat, [])) + count > (16 if target_cat == 'seekbars' else 8):
                    QMessageBox.information(self, 'Layers', 'Maximum number of seekbars or visualizations reached.')
                    return
        self.snapshot()
        pasted = []
        if self._layer_clipboard_cut:
            for cat, key, original in self._layer_clipboard:
                spec = next((s for c, k, s in self.targets() if (c, k) == (cat, key)), None)
                if spec is not None:
                    spec['editor_layer'] = destination
                    spec['visible'] = original.get('visible', True)
                    pasted.append((cat, key))
        else:
            for cat, key, original in self._layer_clipboard:
                spec = copy.deepcopy(original)
                spec['editor_layer'] = destination
                self.data.setdefault(cat, []).append(spec)
                pasted.append((cat, len(self.data[cat]) - 1))
        if centered and pasted:
            bounds = [spec.get('bounds') for _, _, spec in self._layer_clipboard]
            bounds = [b for b in bounds if b and len(b) == 4]
            if bounds:
                left = min(b[0] for b in bounds)
                top = min(b[1] for b in bounds)
                right = max(b[0] + b[2] for b in bounds)
                bottom = max(b[1] + b[3] for b in bounds)
                dx, dy = round(500 - (left + right) / 2), round(500 - (top + bottom) / 2)
                for cat, key in pasted:
                    spec = next((s for c, k, s in self.targets() if (c, k) == (cat, key)), None)
                    if spec is not None and 'bounds' in spec:
                        b = spec['bounds']
                        spec['bounds'] = [b[0] + dx, b[1] + dy, b[2], b[3]]
        members = [s for _, _, s in self.targets() if s.get('editor_layer') == destination]
        highest = max((int(s.get('editor_inner_z', 0)) for s in members if (s.get('editor_layer') == destination)), default=0)
        for i, (cat, key) in enumerate(pasted, 1):
            spec = next((s for c, k, s in self.targets() if (c, k) == (cat, key)), None)
            if spec is not None:
                spec['editor_inner_z'] = highest + i
        self._rebuild_layer_z()
        self.multi_selected = set(pasted)
        self.selected = pasted[-1] if pasted else None
        self._layer_clipboard_cut = False
        self._layer_clipboard = None
        self._paste_destination = destination
        self.apply_preview()

    def _rename_layer(self, item):
        old = item.data(Qt.UserRole)
        name, ok = QInputDialog.getText(self, 'Rename Layer', 'Name:', text=old)
        if not ok or not name.strip() or name.strip() == old:
            return
        name = name.strip()
        if name in self.data.get('editor_layer_groups', {}):
            QMessageBox.information(self, 'Layers', 'This layer name is already in use.')
            return
        self.snapshot()
        groups = self.data['editor_layer_groups']
        groups[name] = groups.pop(old)
        self.data['editor_layer_order'] = [name if x == old else x for x in self.data['editor_layer_order']]
        for _, _, spec in self.targets():
            if spec.get('editor_layer') == old:
                spec['editor_layer'] = name
        if self._paste_destination == old:
            self._paste_destination = name
        self._refresh_layers()

    def _create_empty_layer(self):
        groups = self.data.setdefault('editor_layer_groups', {})
        order = self.data.setdefault('editor_layer_order', [])
        index = 1
        while f'Layer {index}' in groups:
            index += 1
        proposed = f'Layer {index}'
        name, ok = QInputDialog.getText(self, 'New Layer', 'Layer Name:', text=proposed)
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in groups:
            QMessageBox.information(self, 'Layers', 'This layer name already exists.')
            return
        self.snapshot()
        groups[name] = {'locked': False}
        # A new empty group is a valid destination even before it contains objects.
        order.append(name)
        self._paste_destination = name
        self._refresh_layers()
        self._layer_refreshing = True
        try:
            self._layer_list.clearSelection()
            for i in range(self._layer_list.count()):
                item = self._layer_list.item(i)
                if item.data(Qt.UserRole) == name:
                    item.setSelected(True)
                    self._layer_list.scrollToItem(item)
                    break
        finally:
            self._layer_refreshing = False

    def _delete_empty_layers(self):
        names = self._selected_layer_targets()
        if not names:
            return
        occupied = {spec.get('editor_layer') for _, _, spec in self.targets()}
        nonempty = [name for name in names if name in occupied]
        if nonempty:
            QMessageBox.information(self, 'Layers',
                'Layers containing objects cannot be deleted. Move their objects first.')
            return
        self.snapshot()
        groups = self.data.setdefault('editor_layer_groups', {})
        for name in names:
            groups.pop(name, None)
        self.data['editor_layer_order'] = [name for name in self.data.get('editor_layer_order', []) if name not in names]
        if self._paste_destination in names:
            self._paste_destination = None
        self._refresh_layers()

    def _toggle_layer_visibility(self):
        names = self._selected_layer_targets()
        if not names:
            return
        self.snapshot()
        members = [spec for _, _, spec in self.targets() if spec.get('editor_layer') in names]
        new_state = not all(spec.get('visible', True) for spec in members)
        for spec in members:
            spec['visible'] = new_state
        self.apply_preview()

    def _toggle_layer_lock(self):
        names = self._selected_layer_targets()
        if not names:
            return
        self.snapshot()
        groups = self.data.setdefault('editor_layer_groups', {})
        lock = not all(groups.get(name, {}).get('locked', False) for name in names)
        for name in names:
            groups.setdefault(name, {})['locked'] = lock
        self._refresh_layers()

    def targets(self):
        targets = []
        for key, spec in self.data.get('panels', {}).items():
            if 'bounds' in spec:
                targets.append(('panels', key, spec))
        for key, spec in self.data.get('floating_components', {}).items():
            # Existing theme JSON stays compatible, but tabs are a protected
            # system layer and no longer enter editable theme z-order.
            if key != 'tabs' and 'bounds' in spec:
                targets.append(('floating_components', key, spec))
        for i, spec in enumerate(self.data.get('seekbars', [])):
            targets.append(('seekbars', i, spec))
        for i, spec in enumerate(self.data.get('visualizations', [])):
            targets.append(('visualizations', i, spec))
        return targets

    def _horizontal_limit(self, category, key):
        """All editor targets share the same full-window canvas coordinates."""
        return 1000

    def repair_bounds(self):
        """Keep every rectangle within the theme's normalized 0..1000 canvas without distorting existing aspect ratios."""
        cw = max(1, self.manager.canvas.width())
        ch = max(1, self.manager.canvas.height())
        for category, key, spec in self.targets():
            x, y, w, h = (int(round(float(v))) for v in spec['bounds'])
            w = max(1, min(1000, w))
            h = max(1, min(1000, h))
            if ((category, key) == ('panels', 'cover') or
                    (category == 'seekbars' and spec.get('style') == 'circular')):
                # Match ThemeManager.reflow() exactly: pw = ph = min(pw, ph)
                side = max(1, min(round(w * cw / 1000), round(h * ch / 1000), cw, ch))
                w = max(1, min(1000, round(side * 1000 / cw)))
                h = max(1, min(1000, round(side * 1000 / ch)))
            x = max(0, min(x, self._horizontal_limit(category, key) - w))
            # The three action buttons can occupy the strip below ThemeCanvas.
            # Keep all legacy objects constrained to their original 0..1000 area.
            limit = 1500 if (category == 'floating_components' and key in
                             ('stop_after', 'fade_stop', 'repeat_one')) else 1000
            y = max(0, min(y, limit - h))
            spec['bounds'] = [x, y, w, h]

    def sync_visual_widgets(self):
        """Rebuild only visualization widgets when their instance count changes."""
        from theme_api import AudioVisualization
        m = self.manager
        if len(m.visual_widgets) == len(self.data.get('visualizations', [])):
            return
        for widget in m.visual_widgets:
            widget.hide()
            widget.deleteLater()
        m.visual_widgets = []
        for spec in self.data.get('visualizations', []):
            widget = AudioVisualization(m, spec)
            m.visual_widgets.append(widget)
            widget.setVisible(spec.get('visible', True))

    def import_png(self):
        """Copy a PNG into the active theme's assets and create a movable image."""
        destination = self._editable_destination_layer()
        if destination is None:
            return
        visuals = self.data.setdefault('visualizations', [])
        filename, _ = QFileDialog.getOpenFileName(self, 'Bild importieren', '', 'Bilder (*.png *.webp)')
        if not filename:
            return
        from PySide6.QtGui import QImageReader
        reader = QImageReader(filename)
        if bytes(reader.format()).lower() not in (b'png', b'webp') or not reader.canRead():
            QMessageBox.warning(self, 'PNG-Import', 'This file is not a readable PNG/WebP.')
            return
        source = Path(filename)
        if source.stat().st_size > 20 * 1024 * 1024:
            QMessageBox.warning(self, 'PNG-Import', 'Image exceeds 20 MB.')
            return
        # Keep assets alongside the theme JSON, including themes in subfolders.
        theme_file = self.manager.theme_file(self.original_theme, '.json')
        asset_dir = theme_file.parent / 'assets'
        asset_dir.mkdir(parents=True, exist_ok=True)
        # Content-addressed reuse: identical PNG bytes need only one file.
        digest = self._png_sha256(source)
        target = next((p for p in asset_dir.iterdir()
                       if p.is_file() and p.suffix.lower() == source.suffix.lower()
                       and self._png_sha256(p) == digest), None)
        if target is None:
            unique = source.stem[:60] + '_' + digest[:12] + source.suffix.lower()
            target = asset_dir / unique
            if source.resolve() != target.resolve():
                shutil.copy2(source, target)
        size = reader.size()
        canvas = self.manager.canvas
        w = min(600, max(40, round(size.width() / max(1, canvas.width()) * 1000)))
        h = min(600, max(40, round(size.height() / max(1, canvas.height()) * 1000)))
        self.snapshot()
        spec = {'type': 'image', 'asset': target.relative_to(theme_file.parent).as_posix(),
                'bounds': [max(0, (1000-w)//2), max(0, (1000-h)//2), w, h],
                'z_index': 90, 'visible': True, 'rotation': 0,
                'flip_horizontal': False, 'flip_vertical': False}
        visuals.append(spec)
        self._place_new_element_in_layer(spec, destination)
        self.selected = ('visualizations', len(visuals)-1)
        self.apply_preview()
        self.update()

    def transform_image(self, index, operation):
        spec = self.data.get('visualizations', [])[index]
        if spec.get('type') != 'image' or self._is_locked('visualizations', index):
            return
        self.snapshot()
        if operation == 'clockwise':
            spec['rotation'] = (spec.get('rotation', 0) + 90) % 360
        elif operation == 'counterclockwise':
            spec['rotation'] = (spec.get('rotation', 0) - 90) % 360
        elif operation == 'horizontal':
            spec['flip_horizontal'] = not spec.get('flip_horizontal', False)
        elif operation == 'vertical':
            spec['flip_vertical'] = not spec.get('flip_vertical', False)
        if operation in ('clockwise', 'counterclockwise'):
            x,y,w,h = spec['bounds']
            spec['bounds'] = [max(0, round(x+(w-h)/2)), max(0, round(y+(h-w)/2)), h, w]
        self.apply_preview()
        self.update()

    @staticmethod
    def _png_sha256(path):
        digest = hashlib.sha256()
        with Path(path).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def configure_png(self, index, field, value):
        if not 0 <= index < len(self.data.get('visualizations', [])):
            return
        self.snapshot()
        spec = self.data['visualizations'][index]
        if value is None:
            spec.pop(field, None)
        else:
            spec[field] = value
        if field == 'button_action':
            # Keep older theme files compatible, but never let an old
            # mouse_mode override the automatically derived interaction.
            spec.pop('mouse_mode', None)
        self.apply_preview()
        self.update()

    def image_animation_speed(self, index):
        """Live speed preview for animated WebP, with Cancel restoring the old value."""
        visuals = self.data.get('visualizations', [])
        if not 0 <= index < len(visuals) or self._is_locked('visualizations', index):
            return
        spec = visuals[index]
        if spec.get('type') != 'image':
            return
        original = int(spec.get('animation_speed', 100))
        dialog = QDialog(self)
        dialog.setWindowTitle('WebP Animation Speed')
        dialog.setMinimumWidth(340)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('Animation speed (25–500 %). 100 % = original speed.'))
        row = QHBoxLayout()
        slider = QSlider(Qt.Horizontal)
        slider.setRange(25, 500)
        slider.setSingleStep(5)
        spin = QSpinBox()
        spin.setRange(25, 500)
        spin.setSuffix(' %')
        slider.setValue(original)
        spin.setValue(original)
        row.addWidget(slider, 1)
        row.addWidget(spin)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)

        def preview(value):
            spec['animation_speed'] = value
            if index < len(self.manager.visual_widgets):
                widget = self.manager.visual_widgets[index]
                widget.spec['animation_speed'] = value
                if hasattr(widget, '_sync_image_frame'):
                    widget._sync_image_frame()

        spin.valueChanged.connect(preview)
        accepted = dialog.exec() == QDialog.Accepted
        chosen = spin.value()
        preview(original)
        if accepted and chosen != original:
            self.snapshot()
            spec['animation_speed'] = chosen
            self.apply_preview()
            self.update()

    def choose_png_state(self, index, field):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose image state', '', 'Images (*.png *.webp)')
        if not path:
            return
        source = Path(path)
        from PySide6.QtGui import QImageReader
        reader = QImageReader(str(source))
        if bytes(reader.format()).lower() not in (b'png', b'webp') or not reader.canRead() or source.stat().st_size > 20 * 1024 * 1024:
            QMessageBox.warning(self, 'PNG', 'Invalid PNG/WebP or file larger than 20 MB.')
            return
        folder = self.manager.theme_file(self.original_theme, '.json').parent
        assets = folder / 'assets'
        assets.mkdir(parents=True, exist_ok=True)
        digest = self._png_sha256(source)
        target = assets / (source.stem[:60] + '_' + digest[:12] + source.suffix.lower())
        if source.resolve() != target.resolve() and not target.exists():
            shutil.copy2(source, target)
        self.configure_png(index, field, 'assets/' + target.name)

    def _copy_image_assets_for_save(self, data, dest):
        """Copy only referenced images, reusing identical content in target assets."""
        origin = self.manager.theme_file(self.original_theme, '.json').parent.resolve()
        self.manager._normalize_image_assets(data, origin)
        asset_dir = dest.parent / 'assets'
        for spec in data.get('visualizations', []):
            if spec.get('type') != 'image':
                continue
            for field in ('asset', 'asset_hover', 'asset_pressed', 'asset_active'):
                rel = spec.get(field, '')
                if not rel:
                    continue
                source = (origin / rel).resolve()
                if (source.parent != (origin / 'assets').resolve()
                        or not source.is_file() or source.suffix.lower() not in ('.png', '.webp')):
                    raise FileNotFoundError('Theme image missing: ' + str(rel))
                asset_dir.mkdir(parents=True, exist_ok=True)
                digest = self._png_sha256(source)
                target = next((p for p in asset_dir.iterdir()
                               if p.is_file() and p.suffix.lower() == source.suffix.lower()
                               and self._png_sha256(p) == digest), None)
                if target is None:
                    target = asset_dir / (source.stem[:60] + '_' + digest[:12] + source.suffix.lower())
                    if source != target.resolve():
                        shutil.copy2(source, target)
                spec[field] = 'assets/' + target.name

    @staticmethod
    def _cleanup_unused_png_assets(theme_folder):
        """Remove unreferenced PNGs only when every sibling theme JSON is readable.

        Never remove other file types or images used by a different theme JSON.
        """
        folder = Path(theme_folder)
        assets = folder / 'assets'
        if not assets.is_dir():
            return
        referenced = set()
        for json_path in folder.glob('*.json'):
            try:
                document = json.loads(json_path.read_text(encoding='utf-8'))
                if not isinstance(document, dict):
                    return
                visuals = document.get('visualizations', [])
                if not isinstance(visuals, list):
                    return
                for spec in visuals:
                    if not isinstance(spec, dict):
                        return
                    if spec.get('type') == 'image':
                        for field in ('asset', 'asset_hover', 'asset_pressed', 'asset_active'):
                            asset = spec.get(field, '')
                            if not asset:
                                continue
                            if not isinstance(asset, str):
                                return
                            candidate = Path(asset.replace('\\', '/'))
                            if candidate.is_absolute():
                                resolved = candidate.resolve()
                            else:
                                resolved = (folder / candidate).resolve()
                            if resolved.parent == assets.resolve():
                                referenced.add(resolved.name)
                            else:
                                return
            except (OSError, ValueError, TypeError):
                return
        for file in assets.iterdir():
            if file.is_file() and file.suffix.lower() in ('.png', '.webp') and file.name not in referenced:
                file.unlink()

    def add_visualization(self, kind):
        destination = self._editable_destination_layer()
        if destination is None:
            return
        visuals = self.data.setdefault('visualizations', [])
        self.snapshot()
        number = len(visuals) + 1
        spec = {
            'type': kind,
            'bounds': [120 + number * 12, 180 + number * 12, 400, 120],
            'color': '#00f5ff' if kind == 'spectrum' else '#ff175b',
            'z_index': 80
        }
        if kind == 'vu':
            spec.update({'bounds': [160 + number * 12, 200 + number * 12, 260, 24],
                         'source': 'A', 'vu_rotation': 0,
                         'vu_flip_horizontal': False, 'vu_flip_vertical': False})
        visuals.append(spec)
        self._place_new_element_in_layer(spec, destination)
        self.selected = ('visualizations', len(visuals) - 1)
        self.repair_bounds()
        self.apply_preview()

    def add_playlist_panel(self):
        """Opt in to the new panel; legacy theme manifests remain untouched."""
        destination = self._editable_destination_layer()
        if destination is None:
            return
        panels = self.data.setdefault('panels', {})
        if 'playlist' in panels:
            self.selected = ('panels', 'playlist')
            self.update()
            return
        self.snapshot()
        spec = {'bounds': [170, 220, 420, 420], 'z_index': 45, 'visible': True}
        panels['playlist'] = spec
        self._place_new_element_in_layer(spec, destination)
        self.selected = ('panels', 'playlist')
        self.reload_preview()
        self.apply_preview()

    def add_track_text(self, kind):
        destination = self._editable_destination_layer()
        if destination is None:
            return
        floating = self.data.setdefault('floating_components', {})
        if kind in floating:
            self.selected = ('floating_components', kind)
            self.update()
            return
        self.snapshot()
        floating[kind] = {'bounds': [240, 110 + 65 * len([k for k in floating if k.endswith('_text')]), 520, 55], 'z_index': 85, 'visible': True}
        self._place_new_element_in_layer(floating[kind], destination)
        self.data.setdefault('components', {}).setdefault(kind, {})['visible'] = True
        self.selected = ('floating_components', kind)
        self.apply_preview()

    def add_action_buttons(self):
        """Expose the existing playback action buttons as regular layer objects."""
        destination = self._editable_destination_layer()
        if destination is None:
            QMessageBox.information(self, 'Controls',
                'Select an unlocked destination layer in the layer list first.')
            return
        keys = ('stop_after', 'fade_stop', 'repeat_one')
        floating = self.data.setdefault('floating_components', {})
        missing = [key for key in keys if key not in floating]
        if not missing:
            self.selected = ('floating_components', 'stop_after')
            self.update()
            return
        self.snapshot()
        for key in missing:
            index = keys.index(key)
            spec = {'bounds': [620 + index * 125, 1040, 115, 45],
                    'z_index': 85, 'visible': True}
            floating[key] = spec
            self._place_new_element_in_layer(spec, destination)
        # Re-apply the manifest: preview alone cannot undock a real QWidget.
        # ThemeManager.apply() reparents it, then apply_preview() updates bounds.
        self.reload_preview()
        self.selected = ('floating_components', 'stop_after')
        self.update()

    def remove_track_text(self):
        if not self.selected or self.selected[0] != 'floating_components' or self.selected[1] not in ('artist_title_text', 'artist_text', 'title_text'):
            return
        self.snapshot()
        key = self.selected[1]
        self.data.get('floating_components', {}).pop(key, None)
        self.manager.floating_bounds.pop(key, None)
        self.manager.floating_layers.pop(key, None)
        self.manager.components[key].hide()
        self.selected = None
        self.apply_preview()

    def remove_visualization(self):
        if not self.selected or self.selected[0] != 'visualizations':
            return
        self.snapshot()
        del self.data['visualizations'][self.selected[1]]
        self.selected = None
        self.apply_preview()

    def _canvas_origin_in_editor(self) -> QPoint:
        """
        Correctly map the top-left corner of ThemeCanvas into ThemeEditor coordinates.
        Never use self.mapFrom(canvas, ...) because canvas is not a direct parent of self!
        """
        canvas = self.manager.canvas
        if canvas is None:
            return QPoint(0, 0)
        global_top_left = canvas.mapToGlobal(QPoint(0, 0))
        return self.mapFromGlobal(global_top_left)

    def _live_widget_for(self, category, key):
        """Return the actual live QWidget for a target if one exists."""
        m = self.manager
        if category == 'panels':
            return m.panels.get(key)
        if category == 'floating_components' and key in m.dock_slots:
            return m.dock_slots[key][0]
        if category == 'seekbars' and isinstance(key, int) and key < len(m.seek_widgets):
            return m.seek_widgets[key]
        if category == 'visualizations' and isinstance(key, int) and key < len(m.visual_widgets):
            return m.visual_widgets[key]
        return None

    def rect_for(self, spec, category=None, key=None):
        """
        Compute the exact editor rectangle in ThemeEditor coordinates.
        If the live widget is visible, map its real global screen position directly
        so the selection frame is guaranteed to match 1:1 even under DPI scaling.
        """
        if category is not None and key is not None:
            widget = self._live_widget_for(category, key)
            if widget is not None and widget.isVisible():
                top_left = self.mapFromGlobal(widget.mapToGlobal(QPoint(0, 0)))
                return QRect(top_left, widget.size())

        x, y, w, h = spec['bounds']
        canvas = self.manager.canvas
        offset = self._canvas_origin_in_editor()
        pw = round(w * canvas.width() / 1000)
        ph = round(h * canvas.height() / 1000)
        if (category, key) == ('panels', 'cover'):
            pw = ph = min(pw, ph)
        return QRect(
            offset.x() + round(x * canvas.width() / 1000),
            offset.y() + round(y * canvas.height() / 1000),
            pw,
            ph
        )

    def hit(self, point):
        ordered = sorted(
            self.targets(),
            key=lambda t: t[2].get('z_index', 50 if t[0] == 'floating_components' else 80 if t[0] == 'visualizations' else 0),
            reverse=True
        )
        for category, key, spec in ordered:
            if spec.get('visible', True) is False or self._is_locked(category, key):
                continue
            if self.rect_for(spec, category, key).contains(point):
                return (category, key)
        return None

    def selected_spec(self):
        for category, key, spec in self.targets():
            if (category, key) == self.selected:
                return spec
        return None

    def _magnet_targets(self):
        """Global editor guide coordinates in logical Qt pixels."""
        canvas = self.manager.canvas
        origin = self._canvas_origin_in_editor()
        xs = [origin.x(), origin.x() + canvas.width() / 2, origin.x() + canvas.width()]
        ys = [origin.y(), origin.y() + canvas.height() / 2, origin.y() + canvas.height()]
        for category, key, spec in self.targets():
            if (category, key) in self.multi_selected or (category, key) == self.selected or spec.get('visible', True) is False:
                continue
            rect = self.rect_for(spec, category, key)
            xs.extend([rect.left(), rect.left() + rect.width() / 2, rect.left() + rect.width()])
            ys.extend([rect.top(), rect.top() + rect.height() / 2, rect.top() + rect.height()])
        return {'x': xs, 'y': ys}

    def _snap_axis(self, axis, raw_edges, targets, alt):
        """6 px capture; 15 px cursor displacement releases the lock immediately."""
        if alt:
            self.snap_locks[axis] = None
            return 0, None
        lock = self.snap_locks[axis]
        if lock is not None:
            edge_index, guide = lock
            # The unsnapped edge follows the real cursor throughout the drag.
            if abs(raw_edges[edge_index] - guide) < 15:
                return guide - raw_edges[edge_index], guide
            self.snap_locks[axis] = None
            # No immediate recapture in the same event: jump to real cursor.
            return 0, None
        choices = [(abs(guide - edge), guide - edge, index, guide)
                   for index, edge in enumerate(raw_edges) for guide in targets]
        if choices:
            distance, correction, index, guide = min(choices, key=lambda item: item[0])
            if distance <= 6:
                self.snap_locks[axis] = (index, guide)
                return correction, guide
        return 0, None

    def _resize_handles(self, rect):
        """Eight handles in editor coordinates; corners take priority."""
        left, right = rect.left(), rect.right()
        top, bottom = rect.top(), rect.bottom()
        mid_x = (left + right) // 2
        mid_y = (top + bottom) // 2
        return {
            'nw': QPoint(left, top), 'ne': QPoint(right, top),
            'sw': QPoint(left, bottom), 'se': QPoint(right, bottom),
            'n': QPoint(mid_x, top), 's': QPoint(mid_x, bottom),
            'w': QPoint(left, mid_y), 'e': QPoint(right, mid_y),
        }

    def _handle_at(self, point, rect):
        # Only the visible grip itself starts a resize; avoid stealing drags
        # from small elements or adjacent objects.
        for name, center in self._resize_handles(rect).items():
            if abs(point.x() - center.x()) <= 7 and abs(point.y() - center.y()) <= 7:
                return name
        return None

    def mousePressEvent(self, event):
        point = event.position().toPoint()
        target = self.hit(point)
        if event.button() == Qt.RightButton:
            # Context menus are non-selecting. Never change either canvas
            # selection or the explicit destination in the layers palette.
            self.context(event.globalPosition().toPoint())
            return
        if event.button() != Qt.LeftButton:
            return
        # Protected bottom layer: draggable horizontally, never part of theme z-order.
        bar = self.window.tabs.tabBar()
        if bar is not None and bar.isVisible():
            bar_origin = self.mapFromGlobal(bar.mapToGlobal(QPoint(0, 0)))
            tab_rect = QRect(bar_origin, bar.size())
            if tab_rect.contains(point):
                self._system_tab_drag = (point.x(), int(self.data.get('system_tab_x', round(bar.mapTo(self.manager.canvas, QPoint(0, 0)).x() * 1000 / max(1, self.manager.canvas.width())))))
                self.snapshot()
                self.update()
                return
        # Selection controls live in the editor overlay, above all widgets.
        # Test them before hit-testing any underlying overlapping object.
        if self.selected and len(self.multi_selected) <= 1:
            spec = self.selected_spec()
            if spec is not None and spec.get('visible', True) and not self._is_locked(*self.selected):
                rect = self.rect_for(spec, *self.selected)
                up, down = self._inner_z_controls(rect)
                if up.contains(point):
                    self._change_inner_z(+1)
                    return
                if down.contains(point):
                    self._change_inner_z(-1)
                    return
        # Existing selection handles win over hit-testing overlapping objects.
        active_handle = None
        if self.selected and len(self.multi_selected) <= 1:
            selected_spec = self.selected_spec()
            if selected_spec is not None and selected_spec.get('visible', True) and not self._is_locked(*self.selected):
                active_handle = self._handle_at(point, self.rect_for(selected_spec, *self.selected))
                if active_handle:
                    target = self.selected
        self.drag_origin = None
        self.resize_handle = None
        self.group_initial = {}
        self.snap_locks = {'x': None, 'y': None}
        self.snap_guides = []
        if event.modifiers() & Qt.ControlModifier:
            # Ctrl-click toggles membership; no accidental movement on selection clicks.
            if target is not None:
                if target in self.multi_selected:
                    self.multi_selected.remove(target)
                    if self.selected == target:
                        self.selected = next(iter(self.multi_selected), None)
                else:
                    self.multi_selected.add(target)
                    self.selected = target
            self._sync_layer_selection()
            self.update()
            return
        if target is None:
            self.multi_selected.clear()
            self.selected = None
            self._sync_layer_selection()
            self.update()
            return
        if target not in self.multi_selected:
            self.multi_selected = {target}
        self.selected = target
        spec = self.selected_spec()
        if spec is None:
            return
        rect = self.rect_for(spec, target[0], target[1])
        # Group dragging never resizes an individual member accidentally.
        self.resize_handle = active_handle if len(self.multi_selected) == 1 else None
        self.resize_mode = bool(self.resize_handle)
        self.drag_origin = point
        self.initial_bounds = list(spec['bounds'])
        self.group_initial = {(cat, key): list(item['bounds']) for cat, key, item in self.targets()
                              if (cat, key) in self.multi_selected and item.get('visible', True) and not self._is_locked(cat, key)}
        self.snapshot()
        self._sync_layer_selection()
        self.update()

    def mouseMoveEvent(self, event):
        if self._system_tab_drag is not None:
            start_x, initial = self._system_tab_drag
            delta = event.position().toPoint().x() - start_x
            x = max(0, min(1000, initial + round(delta * 1000 / max(1, self.manager.canvas.width()))))
            self.data['system_tab_x'] = x
            self.window._set_theme_tab_position(x)
            self.update()
            return
        if self.drag_origin is None or not self.selected:
            return
        spec = self.selected_spec()
        if spec is None:
            return
        delta = event.position().toPoint() - self.drag_origin
        if len(self.group_initial) > 1:
            self._move_selection(delta, event.modifiers())
            return
        base = self.manager.canvas
        bw, bh = max(1, base.width()), max(1, base.height())
        dx = round(delta.x() * 1000 / bw)
        dy = round(delta.y() * 1000 / bh)
        x, y, w, h = self.initial_bounds
        handle = self.resize_handle
        proportional = (self.selected == ('panels', 'cover') or
                        (self.selected[0] == 'seekbars' and spec.get('style') == 'circular'))
        if handle:
            # Work with four edges so north/west grips keep their opposite
            # edges fixed. A side grip never modifies the other dimension.
            l, t, r, b = x, y, x + w, y + h
            if 'w' in handle:
                l = min(r - 15, l + dx)
            if 'e' in handle:
                r = max(l + 15, r + dx)
            if 'n' in handle:
                t = min(b - 15, t + dy)
            if 's' in handle:
                b = max(t + 15, b + dy)
            # Cover and circular seekbar are square in actual canvas pixels.
            # Side grips adjust the opposite dimension symmetrically around
            # its center; corners keep the opposite corner anchored.
            if proportional:
                orig_l, orig_t, orig_r, orig_b = x, y, x + w, y + h
                if handle in ('n', 's'):
                    side_px = (b - t) * bh / 1000
                elif handle in ('e', 'w'):
                    side_px = (r - l) * bw / 1000
                else:
                    side_px = max((r - l) * bw / 1000, (b - t) * bh / 1000)
                new_w = max(15, round(side_px * 1000 / bw))
                new_h = max(15, round(side_px * 1000 / bh))
                if 'w' in handle:
                    l, r = orig_r - new_w, orig_r
                elif 'e' in handle:
                    l, r = orig_l, orig_l + new_w
                else:
                    cx = (orig_l + orig_r) / 2
                    l, r = round(cx - new_w / 2), round(cx + new_w / 2)
                if 'n' in handle:
                    t, b = orig_b - new_h, orig_b
                elif 's' in handle:
                    t, b = orig_t, orig_t + new_h
                else:
                    cy = (orig_t + orig_b) / 2
                    t, b = round(cy - new_h / 2), round(cy + new_h / 2)
            x, y, w, h = l, t, r - l, b - t
        else:
            x += dx
            y += dy
        origin = self._canvas_origin_in_editor()
        left = origin.x() + x * bw / 1000
        top = origin.y() + y * bh / 1000
        right = left + w * bw / 1000
        bottom = top + h * bh / 1000
        targets = self._magnet_targets()
        alt = bool(event.modifiers() & Qt.AltModifier)
        # Only moving edges may snap during a resize.
        edges_x = ([left] if 'w' in handle else [right] if 'e' in handle else []) if handle else [left, (left + right) / 2, right]
        edges_y = ([top] if 'n' in handle else [bottom] if 's' in handle else []) if handle else [top, (top + bottom) / 2, bottom]
        sx, gx = self._snap_axis('x', edges_x, targets['x'], alt) if edges_x and not (handle and proportional) else (0, None)
        sy, gy = self._snap_axis('y', edges_y, targets['y'], alt) if edges_y and not (handle and proportional) else (0, None)
        if handle:
            adjust_x = round(sx * 1000 / bw)
            adjust_y = round(sy * 1000 / bh)
            if 'w' in handle:
                x += adjust_x
                w -= adjust_x
            elif 'e' in handle:
                w += adjust_x
            if 'n' in handle:
                y += adjust_y
                h -= adjust_y
            elif 's' in handle:
                h += adjust_y
            w, h = max(15, w), max(15, h)
        else:
            x += round(sx * 1000 / bw)
            y += round(sy * 1000 / bh)
        spec['bounds'] = [x, y, w, h]
        self.snap_guides = ([('x', gx)] if gx is not None else []) + ([('y', gy)] if gy is not None else [])
        self.repair_bounds()
        self.apply_preview()
        self.update()

    def _move_selection(self, delta, modifiers):
        """Translate all members by one shared delta, snapping the group envelope."""
        canvas = self.manager.canvas
        bw, bh = max(1, canvas.width()), max(1, canvas.height())
        dx = round(delta.x() * 1000 / bw)
        dy = round(delta.y() * 1000 / bh)
        bounds = list(self.group_initial.values())
        min_x = min(b[0] for b in bounds)
        min_y = min(b[1] for b in bounds)
        max_x = max(b[0] + b[2] for b in bounds)
        max_y = max(b[1] + b[3] for b in bounds)
        # Clamp once for the whole group, never independently per widget.
        x_limit = min(self._horizontal_limit(cat, key) for cat, key in self.group_initial)
        dx = max(-min_x, min(x_limit - max_x, dx))
        y_limit = 1500 if all(cat == 'floating_components' and key in
            ('stop_after', 'fade_stop', 'repeat_one') for cat, key in self.group_initial) else 1000
        dy = max(-min_y, min(y_limit - max_y, dy))
        origin = self._canvas_origin_in_editor()
        left = origin.x() + (min_x + dx) * bw / 1000
        right = origin.x() + (max_x + dx) * bw / 1000
        top = origin.y() + (min_y + dy) * bh / 1000
        bottom = origin.y() + (max_y + dy) * bh / 1000
        targets = self._magnet_targets()
        alt = bool(modifiers & Qt.AltModifier)
        sx, gx = self._snap_axis('x', [left, (left + right) / 2, right], targets['x'], alt)
        sy, gy = self._snap_axis('y', [top, (top + bottom) / 2, bottom], targets['y'], alt)
        dx = max(-min_x, min(x_limit - max_x, dx + round(sx * 1000 / bw)))
        dy = max(-min_y, min(y_limit - max_y, dy + round(sy * 1000 / bh)))
        for category, key, spec in self.targets():
            original = self.group_initial.get((category, key))
            if original is not None:
                spec['bounds'] = [original[0] + dx, original[1] + dy, original[2], original[3]]
        self.snap_guides = ([('x', gx)] if gx is not None else []) + ([('y', gy)] if gy is not None else [])
        self.apply_preview()
        self.update()

    def mouseReleaseEvent(self, event):
        if self._system_tab_drag is not None:
            self._system_tab_drag = None
            # Keep the live preview and the serialized manifest in sync.
            self.window._set_theme_tab_position(self.data.get('system_tab_x'))
            self.update()
            return

        self.drag_origin = None
        self.resize_handle = None
        self.resize_mode = False
        self.group_initial = {}
        self.snap_locks = {'x': None, 'y': None}
        self.snap_guides = []
        self.update()

    def mouseDoubleClickEvent(self, event):
        self.selected = self.hit(event.position().toPoint())
        self.multi_selected = {self.selected} if self.selected is not None else set()
        self._sync_layer_selection()
        self.context(event.globalPosition().toPoint())

    def keyPressEvent(self, event):
        if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_C:
            self._copy_layer_objects()
            return
        if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_X:
            self._cut_layer_objects()
            return
        if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_V:
            self._paste_layer_objects()
            return
        if event.key() == Qt.Key_Escape:
            self.stop()
            return
        if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_Z:
            self.undo()
            return
        if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_Y:
            self.redo()
            return
        arrows = {
            Qt.Key_Left: (-1, 0), Qt.Key_Right: (1, 0),
            Qt.Key_Up: (0, -1), Qt.Key_Down: (0, 1)
        }
        if event.key() in arrows and self.selected_spec() is not None and not self._is_locked(*self.selected):
            if not (event.modifiers() & Qt.AltModifier):
                dx, dy = arrows[event.key()]
                step = 10 if event.modifiers() & Qt.ShiftModifier else 1
                self.nudge(dx * step, dy * step)
                event.accept()
                return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        for category, key, spec in self.targets():
            if spec.get('visible', True) is False:
                continue
            rect = self.rect_for(spec, category, key)
            active = (category, key) in self.multi_selected or (category, key) == self.selected
            pen = QPen(
                QColor('#ffcc00' if active else '#00f5ff'),
                2 if active else 1,
                Qt.SolidLine if active else Qt.DashLine
            )
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect)
            # The index is local to this layer, not the flattened renderer z-index.
            group = spec.get('editor_layer')
            members = self._inner_z_members(group)
            inner_rank = next((i for i, (cat, k, _) in enumerate(members)
                               if (cat, k) == (category, key)), 0)
            label = f'Z:{inner_rank}'
            label_width = max(34, p.fontMetrics().horizontalAdvance(label) + 10)
            tag = QRect(rect.left() + 3, rect.top() + 3, label_width, 19)
            p.fillRect(tag, QColor('#172630'))
            p.setPen(QColor('#ffffff'))
            p.drawText(tag, Qt.AlignCenter, label)
            if active:
                if (category, key) == self.selected and len(self.multi_selected) <= 1 and not self._is_locked(category, key):
                    for center in self._resize_handles(rect).values():
                        grip = QRect(center.x() - 5, center.y() - 5, 11, 11)
                        p.fillRect(grip, QColor('#ffcc00'))
                        p.setPen(QPen(QColor('#141414'), 1))
                        p.drawRect(grip)
                # Keep existing object name visible beneath the z badge.
                p.drawText(rect.topLeft() + QPoint(5, 39), str(key))
        # Draw handles last so they remain on top of overlapping outlines.
        if self.selected and len(self.multi_selected) <= 1:
            spec = self.selected_spec()
            if spec is not None and spec.get('visible', True) and not self._is_locked(*self.selected):
                rect = self.rect_for(spec, *self.selected)
                up, down = self._inner_z_controls(rect)
                for button, symbol in ((up, '▲'), (down, '▼')):
                    p.fillRect(button, QColor('#173e52'))
                    p.setPen(QPen(QColor('#ffcc00'), 1))
                    p.drawRect(button)
                    p.setPen(QColor('#ffffff'))
                    p.drawText(button, Qt.AlignCenter, symbol)
        p.setPen(QPen(QColor('#ff45c8'), 1, Qt.DashLine))
        for axis, position in self.snap_guides:
            if axis == 'x':
                p.drawLine(round(position), 0, round(position), self.height())
            else:
                p.drawLine(0, round(position), self.width(), round(position))
        p.end()

    def apply_preview(self):
        if hasattr(self.window, '_set_theme_tab_position'):
            self.window._set_theme_tab_position(self.data.get('system_tab_x'))
        self.repair_bounds()
        self.sync_visual_widgets()
        m = self.manager
        for category, key, spec in self.targets():
            b = spec['bounds']
            if category == 'panels':
                m.absolute_bounds[key] = tuple(b)
                m.absolute_layers[key] = spec.get('z_index', 0)
            elif category == 'floating_components':
                m.floating_bounds[key] = tuple(b)
                m.floating_layers[key] = spec.get('z_index', 50)
            elif category == 'seekbars':
                pass  # synced as a complete instance list below
            elif category == 'visualizations' and key < len(m.visual_widgets):
                m.visual_widgets[key].spec.update(spec)
        for key in ('artist_title_text', 'artist_text', 'title_text'):
            if key in m.components:
                m.components[key].setVisible(key in self.data.get('floating_components', {}) and
                                             self.data['floating_components'][key].get('visible', True))
        m.sync_seek_widgets(self.data.get('seekbars', [])) if len(m.seek_widgets) != len(self.data.get('seekbars', [])) or [x.get('style') for x in m.seek_instances] != [x.get('style') for x in self.data.get('seekbars', [])] else None
        m.seek_instances = self.data.get('seekbars', [])
        m.style_seek_widgets()
        m.reflow()
        if 'playlist' not in self.data.get('panels', {}):
            m.panels['playlist'].hide()
        for key in ('vu_left', 'vu_right'):
            spec = self.data.get('floating_components', {}).get(key, {})
            widget = m.components.get(key)
            if widget is not None and hasattr(widget, 'set_transform'):
                widget.set_transform(spec.get('vu_rotation', 0),
                                     spec.get('vu_flip_horizontal', False),
                                     spec.get('vu_flip_vertical', False))
        if hasattr(self.window, 'update_cover_display'):
            self.window.update_cover_display()
        if m.full_canvas:
            self.window.backdrop.update()
        self.apply_visibility()
        m.apply_editor_effects(self.data)
        self.apply_clock_color()
        m.reference_canvas = ((self.data['reference_canvas']['width'], self.data['reference_canvas']['height'])
                              if self.data.get('reference_canvas') else None)
        m.theme_fonts = dict(self.data.get('editor_fonts', {}))
        for key in ('artist_title_text', 'artist_text', 'title_text'):
            spec = self.data.get('editor_fonts', {}).get(key)
            widget = m.components.get(key)
            if widget is not None:
                spec = spec or {}
                font = widget.font()
                font.setFamily(str(spec.get('family', 'Sans Serif')))
                font.setPointSize(max(1, int(spec.get('size', 20))))
                font.setBold(bool(spec.get('bold', True)))
                widget.setFont(font)
                widget.setStyleSheet(
                    f'color: {QColor(self.data.get("editor_text_colors", {}).get(key, "#ffffff")).name(QColor.HexArgb)}; background: transparent; '
                    f'font-size: {max(1, int(spec.get("size", 20)))}pt; '
                    f'font-weight: {"bold" if spec.get("bold", True) else "normal"};'
                )
        m.scale_theme_fonts()
        self.manager.audio_reactive = self._parsed_audio_background(self.data.get('audio_reactive', {'enabled': False}))
        self.manager.canvas.update()
        self.window.backdrop.update()
        self.raise_()
        self.window._theme_editor_toolbar.raise_()
        self._layer_panel.raise_()
        self._refresh_layers()
        self.update()

    def apply_visibility(self):
        """Apply draft visibility without restarting playback or the theme."""
        m = self.manager
        for category, key, spec in self.targets():
            visible = spec.get('visible', True)
            if category == 'panels' and key in m.panels:
                m.panels[key].setVisible(visible)
            elif category == 'floating_components' and key in m.dock_slots:
                m.dock_slots[key][0].setVisible(visible)
            elif category == 'seekbars' and key < len(m.seek_widgets):
                m.seek_widgets[key].setVisible(visible)
            elif category == 'visualizations' and key < len(m.visual_widgets):
                m.visual_widgets[key].setVisible(visible)
                if m.full_canvas:
                    self.window.backdrop.update()

    def set_visibility(self, category, key, visible):
        for cat, item, spec in self.targets():
            if (cat, item) == (category, key):
                if spec.get('visible', True) == visible:
                    return
                self.snapshot()
                spec['visible'] = bool(visible)
                if not visible and self.selected == (category, key):
                    self.selected = None
                self.apply_preview()
                return

    def set_audio_background_visible(self, visible):
        config = self.data.setdefault('audio_reactive', {})
        if bool(config.get('enabled', False)) == bool(visible):
            return
        self.snapshot()
        config['enabled'] = bool(visible)
        self.manager.audio_reactive = self._parsed_audio_background(config)
        self.manager.canvas.update()
        self.apply_preview()

    def add_peq_panel(self):
        """Opt-in PEQ placement. Legacy themes remain unchanged."""
        panels = self.data.setdefault('panels', {})
        if 'peq' not in panels:
            self.snapshot()
            panels['peq'] = {'bounds': [300, 400, 400, 260],
                             'z_index': 65, 'visible': True}
        elif not panels['peq'].get('visible', True):
            self.snapshot()
            panels['peq']['visible'] = True
        self.apply_preview()

    def visibility_menu(self, parent):
        submenu = parent.addMenu('Show / Hide Elements')
        submenu.setMinimumWidth(285)
        audio_action = submenu.addAction('Audio-Reactive Background')
        audio_action.setCheckable(True)
        audio_action.setChecked(bool(self.data.get('audio_reactive', {}).get('enabled', False)))
        audio_action.toggled.connect(self.set_audio_background_visible)
        submenu.addSeparator()
        add_peq = submenu.addAction('Add / Place Parametric EQ')
        add_peq.triggered.connect(self.add_peq_panel)
        submenu.addSeparator()
        labels = {
            'cover': 'Cover', 'peq': 'Parametric EQ', 'brain': 'Brain-Log', 'queue': 'Queue', 'playlist': 'Playlist',
            'circular_seek': 'Circulare Seekbar', 'waveform_seek': 'RMS-Waveform-Seekbar'
        }
        for category, key, spec in self.targets():
            title = (f"Seekbar {key + 1}: {spec.get('style', 'linear')}" if category == 'seekbars' else labels.get(key, str(key)) if category != 'visualizations' else f"{spec.get('type', 'Visualisierung').title()} {key + 1}")
            action = submenu.addAction(title)
            action.setCheckable(True)
            action.setChecked(spec.get('visible', True))
            action.toggled.connect(lambda checked, c=category, k=key: self.set_visibility(c, k, checked))
        return submenu

    def _delete_selected_element(self):
        """Delete only instance types that can safely be removed from the theme."""
        if not self.selected or self._is_locked(*self.selected):
            return
        category, key = self.selected
        if category == 'seekbars':
            self.remove_seekbar()
        elif category == 'visualizations':
            self.remove_visualization()
        elif category == 'floating_components' and key in ('artist_title_text', 'artist_text', 'title_text'):
            self.remove_track_text()

    def set_vu_presentation(self, key, style):
        if key not in ('vu_left', 'vu_right'):
            return
        self.snapshot()
        self.data.setdefault('components', {}).setdefault(key, {})['vu_presentation'] = style
        widget = self.manager.components.get(key)
        if widget is not None and hasattr(widget, 'set_presentation'):
            widget.set_presentation(style)

    def set_independent_vu_presentation(self, index, style):
        if style not in ('classic', 'phosphor', 'phosphor_mono'):
            return
        visuals = self.data.get('visualizations', [])
        if not 0 <= index < len(visuals) or visuals[index].get('type') != 'vu':
            return
        if self._is_locked('visualizations', index):
            return
        self.snapshot()
        visuals[index]['vu_presentation'] = style
        self.apply_preview()

    def set_vu_phosphor_color(self, index):
        visuals = self.data.get('visualizations', [])
        if not 0 <= index < len(visuals) or visuals[index].get('type') != 'vu':
            return
        if self._is_locked('visualizations', index):
            return
        current = QColor(visuals[index].get('vu_phosphor_color', '#41ff73'))
        chosen = QColorDialog.getColor(current, self, 'Choose VU Phosphor Color')
        if not chosen.isValid():
            return
        value = chosen.name(QColor.HexRgb)
        if value == current.name(QColor.HexRgb):
            return
        self.snapshot()
        visuals[index]['vu_phosphor_color'] = value
        self.apply_preview()

    def set_vu_copy_source(self, index, deck):
        if deck not in ('A', 'B', 'A+B') or self._is_locked('visualizations', index):
            return
        self.snapshot()
        self.data['visualizations'][index]['source'] = deck
        self.apply_preview()

    def set_vu_sensitivity(self, index):
        visuals = self.data.get('visualizations', [])
        if not 0 <= index < len(visuals) or visuals[index].get('type') != 'vu':
            return
        if self._is_locked('visualizations', index):
            return
        old = int(visuals[index].get('vu_sensitivity', 100))
        value, ok = QInputDialog.getInt(self, 'VU-Empfindlichkeit',
                                         'Pegel-Empfindlichkeit in % (100 = Original):',
                                         old, 10, 200, 5)
        if ok and value != old:
            self.snapshot()
            visuals[index]['vu_sensitivity'] = value
            self.apply_preview()

    def set_vu_band(self, index, band):
        visuals = self.data.get('visualizations', [])
        if not 0 <= index < len(visuals) or visuals[index].get('type') != 'vu':
            return
        if self._is_locked('visualizations', index) or band not in ('full', 'bass', 'mids', 'highs'):
            return
        if visuals[index].get('vu_band', 'full') == band:
            return
        self.snapshot()
        visuals[index]['vu_band'] = band
        self.apply_preview()

    def set_vu_curve(self, index):
        visuals = self.data.get('visualizations', [])
        if not 0 <= index < len(visuals) or visuals[index].get('type') != 'vu':
            return
        if self._is_locked('visualizations', index):
            return
        old = float(visuals[index].get('vu_curve', 1.0))
        value, ok = QInputDialog.getDouble(self, 'VU-Pegelkennlinie',
            'Exponent (1,0 = linear; 2,0 = quadratisch; 3,0 = stark):',
            old, 1.0, 5.0, 2)
        if ok and value != old:
            self.snapshot()
            visuals[index]['vu_curve'] = value
            self.apply_preview()

    def set_vu_segments(self, index, count):
        visuals = self.data.get('visualizations', [])
        if not 0 <= index < len(visuals) or visuals[index].get('type') != 'vu':
            return
        if self._is_locked('visualizations', index):
            return
        previous = visuals[index].get('vu_segments')
        if previous == count:
            return
        self.snapshot()
        if count is None:
            visuals[index].pop('vu_segments', None)
        else:
            visuals[index]['vu_segments'] = count
        self.apply_preview()

    def prompt_vu_segments(self, index):
        visuals = self.data.get('visualizations', [])
        if not 0 <= index < len(visuals) or visuals[index].get('type') != 'vu':
            return
        if self._is_locked('visualizations', index):
            return
        old = visuals[index].get('vu_segments') or 6
        value, ok = QInputDialog.getInt(self, 'VU-Segment Count',
                                         'Anzahl der Segmente (1–128):', old, 1, 128, 1)
        if ok:
            self.set_vu_segments(index, value)

    def transform_vu(self, key, operation):
        if isinstance(key, int):
            spec = self.data.get('visualizations', [])[key]
            if spec.get('type') != 'vu' or self._is_locked('visualizations', key):
                return
        else:
            if key not in ('vu_left', 'vu_right'):
                return
            spec = self.data.get('floating_components', {}).get(key)
        if spec is None or self._is_locked('visualizations' if isinstance(key, int) else 'floating_components', key):
            return
        self.snapshot()
        rotation = int(spec.get('vu_rotation', 0))
        if operation == 'clockwise':
            spec['vu_rotation'] = (rotation + 90) % 360
        elif operation == 'counterclockwise':
            spec['vu_rotation'] = (rotation - 90) % 360
        elif operation == 'horizontal':
            spec['vu_flip_horizontal'] = not spec.get('vu_flip_horizontal', False)
        elif operation == 'vertical':
            spec['vu_flip_vertical'] = not spec.get('vu_flip_vertical', False)
        else:
            return
        if operation in ('clockwise', 'counterclockwise'):
            # Keep the rotated VU's original aspect ratio and center.
            x, y, w, h = spec['bounds']
            spec['bounds'] = [round(x + (w-h)/2), round(y + (h-w)/2), h, w]
        self.apply_preview()
        self.update()

    def edit_element_geometry(self, category, key):
        """Edit the selected element in canvas pixels, keeping theme storage normalized."""
        spec = next((item for c, k, item in self.targets()
                     if (c, k) == (category, key)), None)
        if spec is None or self._is_locked(category, key):
            return
        dialog = ElementGeometryDialog(self, key, spec['bounds'], self.manager.canvas.size(),
                                       spec.get('original_geometry_bounds'))
        if dialog.exec() != QDialog.Accepted:
            return
        new_bounds = dialog.normalized_bounds()
        if new_bounds == list(spec['bounds']):
            return
        self.snapshot()
        spec.setdefault('original_geometry_bounds', list(spec['bounds']))
        spec['bounds'] = new_bounds
        self.apply_preview()  # includes repair_bounds() and widget synchronization
        self.update()

    def context(self, point):
        """Compact Flash-inspired menu; keep object-specific tools in Effects."""
        menu = QMenu(self)
        spec = self.selected_spec()
        if spec:
            category, key = self.selected
            menu.addAction(f'Element: {key}').setEnabled(False)
            geometry_action = menu.addAction('Edit Geometry…', lambda checked=False, c=category, k=key: self.edit_element_geometry(c, k))
            geometry_action.setEnabled(not self._is_locked(category, key))
            menu.addSeparator()
            if category == 'floating_components' and key in ('vu_left', 'vu_right'):
                presentation = menu.addMenu('Appearance')
                for label, style in (('Classic', 'classic'), ('Phosphor with Attack / Decay', 'phosphor'), ('Mono Phosphor', 'phosphor_mono')):
                    action = presentation.addAction(label)
                    action.setCheckable(True)
                    action.setChecked(self.data.get('components', {}).get(key, {}).get('vu_presentation', 'classic') == style)
                    action.triggered.connect(lambda checked=False, k=key, st=style: self.set_vu_presentation(k, st))
                transform = menu.addMenu('Transform')
                for title, op in (('Rotate 90° Clockwise', 'clockwise'),
                                  ('Rotate 90° Counterclockwise', 'counterclockwise'),
                                  ('Flip Horizontally', 'horizontal'),
                                  ('Flip Vertically', 'vertical')):
                    action = transform.addAction(title)
                    action.triggered.connect(lambda checked=False, k=key, operation=op: self.transform_vu(k, operation))

            if category == 'visualizations' and spec.get('type') == 'image':
                functions = menu.addMenu('Image Player Function')
                for label, function in (('None', ''), ('Previous', 'transport_previous'),
                                        ('Play / Pause', 'transport_play_pause'),
                                        ('Stop', 'transport_stop'), ('Next', 'transport_next'),
                                        ('Repeat One', 'repeat_one'), ('Fade Stop', 'fade_stop'),
                                        ('Stop After', 'stop_after')):
                    action = functions.addAction(label)
                    action.setCheckable(True)
                    action.setChecked(spec.get('button_action', '') == function)
                    action.triggered.connect(lambda checked=False, idx=key, fn=function:
                                             self.configure_png(idx, 'button_action', fn))
                states = menu.addMenu('Image Button States')
                for label, field in (('Normal', 'asset'), ('Mouseover', 'asset_hover'),
                                     ('Pressed', 'asset_pressed'), ('Active / Playing', 'asset_active')):
                    sub = states.addMenu(label)
                    sub.addAction('Choose Image…', lambda checked=False, idx=key, f=field:
                                  self.choose_png_state(idx, f))
                    if field != 'asset':
                        sub.addAction('Use normal image', lambda checked=False, idx=key, f=field:
                                      self.configure_png(idx, f, None))
                # Only animated WebP images need a playback-speed control.
                from PySide6.QtGui import QImageReader
                theme_folder = self.manager.theme_file(self.original_theme, '.json').parent
                image_rel = spec.get('asset', '')
                if isinstance(image_rel, str) and image_rel.lower().endswith('.webp'):
                    image_file = (theme_folder / image_rel).resolve()
                    if image_file.is_file() and QImageReader(str(image_file)).supportsAnimation():
                        speed_action = menu.addAction(
                            f"Animation Speed: {spec.get('animation_speed', 100)} %…")
                        speed_action.triggered.connect(
                            lambda checked=False, idx=key: self.image_animation_speed(idx))
                transform = menu.addMenu('Transform Image')
                for title, op in (('Rotate 90° Clockwise', 'clockwise'),
                                  ('Rotate 90° Counterclockwise', 'counterclockwise'),
                                  ('Flip Horizontally', 'horizontal'),
                                  ('Flip Vertically', 'vertical')):
                    transform.addAction(title, lambda checked=False, idx=key, operation=op: self.transform_image(idx, operation))

            if category == 'visualizations' and spec.get('type') == 'vu':
                presentation = menu.addMenu('Appearance')
                for label, style in (('Classic', 'classic'),
                                     ('Phosphor with Attack / Decay', 'phosphor'),
                                     ('Mono Phosphor', 'phosphor_mono')):
                    action = presentation.addAction(label)
                    action.setCheckable(True)
                    action.setChecked(spec.get('vu_presentation', 'phosphor_mono') == style)
                    action.triggered.connect(
                        lambda checked=False, idx=key, st=style:
                            self.set_independent_vu_presentation(idx, st))
                presentation.addSeparator()
                presentation.addAction('Phosphor Color…', lambda checked=False, idx=key: self.set_vu_phosphor_color(idx))
                source_menu = menu.addMenu('Audio Source')
                for deck in ('A', 'B', 'A+B'):
                    action = source_menu.addAction('Deck ' + deck)
                    action.setCheckable(True)
                    action.setChecked(spec.get('source', 'A') == deck)
                    action.triggered.connect(lambda checked=False, idx=key, d=deck: self.set_vu_copy_source(idx, d))
                menu.addAction(f"Sensitivity: {spec.get('vu_sensitivity', 100):g} %…",
                               lambda checked=False, idx=key: self.set_vu_sensitivity(idx))
                band_menu = menu.addMenu('Frequency Band')
                for label, band in (('Full Level (Deck A/B/A+B)', 'full'),
                                    ('Bass (Active Deck)', 'bass'),
                                    ('Mids (Active Deck)', 'mids'),
                                    ('Highs (Active Deck)', 'highs')):
                    action = band_menu.addAction(label)
                    action.setCheckable(True)
                    action.setChecked(spec.get('vu_band', 'full') == band)
                    action.triggered.connect(lambda checked=False, idx=key, b=band: self.set_vu_band(idx, b))
                menu.addAction(f"Level Curve: Exponent {spec.get('vu_curve', 1.0):.2f}…",
                               lambda checked=False, idx=key: self.set_vu_curve(idx))
                segments_menu = menu.addMenu('Segment Count')
                auto_action = segments_menu.addAction('Automatic')
                auto_action.setCheckable(True)
                auto_action.setChecked(spec.get('vu_segments') is None)
                auto_action.triggered.connect(lambda checked=False, idx=key: self.set_vu_segments(idx, None))
                manual_action = segments_menu.addAction(
                    f"Manual… ({spec['vu_segments']})" if spec.get('vu_segments') is not None else 'Manual…')
                manual_action.triggered.connect(lambda checked=False, idx=key: self.prompt_vu_segments(idx))
                transform = menu.addMenu('Transform')
                for title, op in (('Rotate 90° Clockwise', 'clockwise'),
                                  ('Rotate 90° Counterclockwise', 'counterclockwise'),
                                  ('Flip Horizontally', 'horizontal'),
                                  ('Flip Vertically', 'vertical')):
                    transform.addAction(title, lambda checked=False, idx=key, operation=op: self.transform_vu(idx, operation))

            toggle = menu.addAction('Show Element')
            toggle.setCheckable(True)
            toggle.setChecked(spec.get('visible', True))
            toggle.toggled.connect(lambda checked, c=category, k=key: self.set_visibility(c, k, checked))
            menu.addAction('Cut', self._cut_layer_objects)
            menu.addAction('Copy', self._copy_layer_objects)
            delete_action = menu.addAction('Delete', self._delete_selected_element)
            delete_action.setEnabled(not self._is_locked(category, key) and
                (category in ('seekbars', 'visualizations') or
                 (category == 'floating_components' and key in ('artist_title_text', 'artist_text', 'title_text'))))
        else:
            menu.addAction('No Element Selected').setEnabled(False)

        menu.addSeparator()
        centered = menu.addAction('Paste Centered', lambda: self._paste_layer_objects(centered=True))
        positioned = menu.addAction('Paste at Position', self._paste_layer_objects)
        can_insert = self._editable_destination_layer() is not None
        centered.setEnabled(bool(self._layer_clipboard) and can_insert)
        positioned.setEnabled(bool(self._layer_clipboard) and can_insert)

        menu.addSeparator()
        effects = menu.addMenu('Effects')
        if spec:
            if category == 'visualizations':
                effects.addAction('Color…', self.visual_color)
            elif category == 'seekbars':
                effects.addAction('Color…', self.waveform_colors)
            elif category == 'floating_components' and key in ('artist_title_text', 'artist_text', 'title_text'):
                effects.addAction('Color…', self.track_text_color)
            elif (category, key) == ('floating_components', 'clock'):
                effects.addAction('Color…', self.clock_color)
            effects.addAction('Font…', self.font_properties)
            effects.addAction('Shadow / Glow…', self.effect_properties)
            effects.addAction('Frame / Audio Reactivity…', self.frame_properties)
            if category == 'visualizations':
                effects.addAction('Visualization Properties…', self.visual_properties)
            if category == 'seekbars':
                self.seek_style_menu(effects)
            if (category, key) == ('panels', 'cover'):
                shapes = effects.addMenu('Cover Shape')
                for title, mask in [('Vinyl Record', 'vinyl'), ('Circular Cover', 'circle'),
                                    ('Rounded Corners', 'rounded'), ('Square Cover', 'square')]:
                    shapes.addAction(title, lambda v=mask: self.cover_shape(v))
                effects.addAction('Corner Radius…', self.cover_radius)
        else:
            effects.addAction('Background…', self.background_properties)
            effects.addAction('Background Gradient…', self.background_gradient)
        # Global audio background is independent of the selected object.
        # Keep it at the top level, separate from object-specific Effects.
        menu.addAction('Audio-Reactive Background', self.audio_background_properties)

        menu.addAction('QSS Studio', self.qss_properties)
        menu.addSeparator()
        add_menu = menu.addMenu('Add Element')
        add_menu.setEnabled(can_insert)
        add_menu.addAction('Playlist', self.add_playlist_panel)
        add_menu.addAction('FFT Spectrum', lambda: self.add_visualization('spectrum'))
        add_menu.addAction('Waveform', lambda: self.add_visualization('waveform'))
        add_menu.addAction('VU Meter', lambda: self.add_visualization('vu'))
        add_menu.addAction('Import Image (PNG/WebP)…', self.import_png)
        seek_add = add_menu.addMenu('Seekbar')
        seek_add.addAction('Linear', lambda: self.add_seekbar('linear'))
        seek_add.addAction('Circular', lambda: self.add_seekbar('circular'))
        seek_add.addAction('RMS-Waveform', lambda: self.add_seekbar('waveform'))
        add_menu.addSeparator()
        add_menu.addAction('Artist - Title', lambda: self.add_track_text('artist_title_text'))
        add_menu.addAction('Artist', lambda: self.add_track_text('artist_text'))
        add_menu.addAction('Title', lambda: self.add_track_text('title_text'))
        self.visibility_menu(menu)
        menu.exec(point)

    def seek_style_menu(self, parent):
        menu = parent.addMenu('Seekbar Appearance')
        spec = self.selected_spec() or {}
        current = spec.get('style', 'linear')
        for label, style in [('Long (Linear)', 'linear'), ('Circular (1:1)', 'circular'), ('RMS-Waveform', 'waveform')]:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(current == style)
            action.triggered.connect(lambda checked=False, value=style: self.set_seek_style(value))
        return menu

    def add_seekbar(self, style):
        destination = self._editable_destination_layer()
        if destination is None:
            return
        specs = self.data.setdefault('seekbars', [])
        if len(specs) >= 16:
            QMessageBox.information(self, 'Seekbars', 'Maximum of 16 seekbars per theme.')
            return
        self.snapshot()
        index = len(specs)
        bounds = ([140, 700, 550, 38] if style == 'linear' else
                  [680, 560, 175, 175] if style == 'circular' else [80, 790, 700, 65])
        bounds[0] = min(950 - bounds[2], bounds[0] + index * 18)
        bounds[1] = min(950 - bounds[3], bounds[1] + index * 18)
        specs.append({'style': style, 'bounds': bounds, 'z_index': 70 + index,
                      'visible': True, 'hide_linear': True,
                      'played_color': '#F9061B', 'remaining_color': '#303036',
                      'cursor_color': '#ffffff'})
        self._place_new_element_in_layer(specs[-1], destination)
        self.selected = ('seekbars', index)
        self.reload_preview()

    def remove_seekbar(self):
        if not self.selected or self.selected[0] != 'seekbars':
            return
        self.snapshot()
        self.data['seekbars'].pop(self.selected[1])
        self.selected = None
        self.reload_preview()

    def waveform_colors(self):
        if not self.selected or self.selected[0] != 'seekbars':
            return
        spec = self.selected_spec()
        options = [('played_color', 'Played section', '#ff465a'),
                   ('remaining_color', 'Remaining section', '#f8b94f'),
                   ('cursor_color', 'Position line', '#ffffff')]
        changed = False
        for key, label, default in options:
            color = QColorDialog.getColor(QColor(spec.get(key, default)), self, label)
            if color.isValid():
                if not changed:
                    self.snapshot()
                changed = True
                spec[key] = color.name()
        if changed:
            self.reload_preview()

    def set_seek_style(self, style):
        if not self.selected or self.selected[0] != 'seekbars':
            return
        spec = self.selected_spec()
        if spec.get('style') == style:
            return
        self.snapshot()
        spec['style'] = style
        if style == 'circular':
            cw = max(1, self.manager.canvas.width())
            ch = max(1, self.manager.canvas.height())
            side = min(spec['bounds'][2] * cw / 1000, spec['bounds'][3] * ch / 1000)
            spec['bounds'][2] = max(15, round(side * 1000 / cw))
            spec['bounds'][3] = max(15, round(side * 1000 / ch))
        self.reload_preview()

    def normalize_layers(self, ordered=None):
        if 'editor_layer_order' in self.data:
            self._rebuild_layer_z()
            return
        items = list(self.targets())
        if ordered is None:
            defaults = {'panels': 0, 'floating_components': 50, 'seekbars': 70, 'visualizations': 80}
            items.sort(key=lambda item: int(item[2].get('z_index', defaults[item[0]])))
        else:
            items = ordered
        for index, (_, _, spec) in enumerate(items):
            spec['z_index'] = -90 + round(180 * index / max(1, len(items) - 1))

    def layer(self, delta):
        name = self._paste_destination or (self._group(*self.selected) if self.selected else None)
        order = self.data.get('editor_layer_order', [])
        if name not in order or self.data.get('editor_layer_groups', {}).get(name, {}).get('locked'):
            return
        old = order.index(name)
        new = max(0, min(len(order) - 1, old + delta))
        if old == new:
            return
        self.snapshot()
        order.insert(new, order.pop(old))
        self._rebuild_layer_z()
        self.apply_preview()

    def nudge(self, dx, dy):
        """Move the whole selection by one shared delta, preserving spacing."""
        selected = self.multi_selected or ({self.selected} if self.selected else set())
        members = [(cat, key, spec) for cat, key, spec in self.targets()
                   if (cat, key) in selected and spec.get('visible', True) is not False and not self._is_locked(cat, key)]
        if not members:
            return
        canvas = self.manager.canvas
        cw, ch = max(1, canvas.width()), max(1, canvas.height())
        signature = frozenset((cat, key) for cat, key, _ in members)
        if getattr(self, '_nudge_target', None) != signature:
            self._nudge_target = signature
            self._nudge_residual = [0.0, 0.0]
        rx = self._nudge_residual[0] + dx * 1000 / cw
        ry = self._nudge_residual[1] + dy * 1000 / ch
        mx = int(rx) if rx >= 0 else -int(-rx)
        my = int(ry) if ry >= 0 else -int(-ry)
        self._nudge_residual = [rx - mx, ry - my]
        bounds = [spec['bounds'] for _, _, spec in members]
        min_x = min(v[0] for v in bounds)
        min_y = min(v[1] for v in bounds)
        max_x = max(v[0] + v[2] for v in bounds)
        max_y = max(v[1] + v[3] for v in bounds)
        mx = max(-min_x, min(1000 - max_x, mx))
        my = max(-min_y, min(1000 - max_y, my))
        if mx or my:
            self.snapshot()
            for _, _, spec in members:
                x, y, w, h = spec['bounds']
                spec['bounds'] = [x + mx, y + my, w, h]
            self.apply_preview()
            self.update()

    def cover_shape(self, mask):
        self.snapshot()
        self.data.setdefault('cover_presentation', {})['mask'] = mask
        self.window.cover.set_cover_presentation(self.data['cover_presentation'])
        self.window.update_cover_display()
        self.update()

    def cover_radius(self):
        old = self.data.get('cover_presentation', {}).get('radius', 9)
        value, ok = QInputDialog.getInt(self, 'Eckenradius', 'Radius (Pixel)', old, 0, 100)
        if ok:
            self.snapshot()
            self.data.setdefault('cover_presentation', {}).update(mask='rounded', radius=value)
            self.window.cover.set_cover_presentation(self.data['cover_presentation'])
            self.window.update_cover_display()

    def visual_qcolor(self, spec):
        color = QColor(spec.get('color', '#00f5ff'))
        color.setAlphaF(float(spec.get('opacity', 1.0)))
        return color

    def track_text_color(self):
        if not self.selected or self.selected[0] != 'floating_components':
            return
        key = self.selected[1]
        if key not in ('artist_title_text', 'artist_text', 'title_text'):
            return
        current = self.data.get('editor_text_colors', {}).get(key, '#ffffff')
        color = QColorDialog.getColor(QColor(current), self, 'Text Color', QColorDialog.ShowAlphaChannel)
        if not color.isValid():
            return
        self.snapshot()
        self.data.setdefault('editor_text_colors', {})[key] = color.name(QColor.HexArgb)
        self.apply_preview()

    def clock_color(self):
        current = self.data.get('editor_text_colors', {}).get('clock', '#ffffff')
        color = QColorDialog.getColor(QColor(current), self, 'Time Display Text Color', QColorDialog.ShowAlphaChannel)
        if not color.isValid():
            return
        self.snapshot()
        self.data.setdefault('editor_text_colors', {})['clock'] = color.name(QColor.HexArgb)
        self.apply_clock_color()

    def apply_clock_color(self):
        widget = self.manager.components.get('clock')
        if widget is None:
            return
        spec = self.data.get('editor_fonts', {}).get('clock', {})
        color = self.data.get('editor_text_colors', {}).get('clock')
        font = widget.font()
        if spec:
            font.setFamily(str(spec.get('family', font.family())))
            font.setPointSize(max(1, int(spec.get('size', font.pointSize()))))
            font.setBold(bool(spec.get('bold', font.bold())))
            widget.setFont(font)
        rules = []
        if color:
            rules.append('color: %s' % QColor(color).name(QColor.HexArgb))
        if spec:
            family = font.family().replace('\\', '\\\\').replace('"', '\\"')
            rules.extend((f'font-family: "{family}"',
                          f'font-size: {font.pointSize()}pt',
                          f'font-weight: {"bold" if font.bold() else "normal"}'))
        widget.setStyleSheet('; '.join(rules) + ';' if rules else '')

    def visual_color(self):
        spec = self.selected_spec()
        color = QColorDialog.getColor(self.visual_qcolor(spec), self, 'Choose Color', QColorDialog.ShowAlphaChannel)
        if color.isValid():
            self.snapshot()
            spec['color'] = color.name()
            spec['opacity'] = round(color.alphaF(), 3)
            self.apply_preview()

    def visual_properties(self):
        spec = self.selected_spec()
        if spec['type'] == 'spectrum':
            val, ok = QInputDialog.getInt(self, 'FFT', 'Anzahl Balken', spec.get('bars', 64), 8, 128)
            if ok:
                self.snapshot()
                spec['bars'] = val
                self.apply_preview()
        else:
            val, ok = QInputDialog.getDouble(self, 'Waveform', 'Gain', spec.get('gain', 1.0), 0.1, 10, 2)
            if ok:
                self.snapshot()
                spec['gain'] = val
                self.apply_preview()

    def font_properties(self):
        if not self.selected:
            return
        category, key = self.selected
        widget = self.manager.panels.get(key) or self.manager.components.get(key)
        if widget is None:
            QMessageBox.information(self, 'Font', 'This element has no individual text font.')
            return
        # PySide6 versions/bindings may expose QFontDialog.getFont() tuple
        # ordering differently. Identify the QFont by type, never by position.
        from PySide6.QtGui import QFont
        result = QFontDialog.getFont(widget.font(), self, 'Choose Font')
        if not isinstance(result, tuple) or len(result) != 2:
            return
        font = next((item for item in result if isinstance(item, QFont)), None)
        accepted = next((item for item in result if isinstance(item, bool)), False)
        if not accepted or font is None:
            return
        # Raster fonts such as Fixedsys advertise only one native size in
        # QFontDialog. Ask for an independent numeric size after font selection.
        # Theme fonts are stored in points for backward compatibility.
        current = self.data.get('editor_fonts', {}).get(key, {})
        default_size = int(current.get('size') or (font.pointSize() if font.pointSize() > 0 else 11))
        size, size_ok = QInputDialog.getInt(
            self, 'Font Size', 'Font size (pt):', max(1, default_size), 1, 256, 1
        )
        if not size_ok:
            return
        font.setPointSize(size)
        self.snapshot()
        self.data.setdefault('editor_fonts', {})[key] = {
            'family': font.family(), 'size': size, 'bold': font.bold()
        }
        if key in ('artist_title_text', 'artist_text', 'title_text', 'clock'):
            # Reflow may replace styles; apply persisted font *after* reflow.
            self.apply_preview()
        else:
            widget.setFont(font)

    def effect_properties(self):
        """Visual effect designer; preview uses the real widget, Cancel restores original."""
        spec = self.selected_spec()
        if spec is None:
            return
        old = copy.deepcopy(spec.get('editor_effect', {}))
        values = {"enabled": True, "type": "shadow", "color": "#000000",
                  "blur": 24, "distance": 12, "angle": 45, "opacity": 65}
        values.update(old)
        dlg = QDialog(self)
        dlg.setWindowTitle('Shadow & Glow Designer · ' + str(self.selected[1]))
        dlg.setMinimumWidth(465)
        root = QVBoxLayout(dlg)
        root.setSpacing(10)
        mode = QComboBox()
        mode.addItems(['Shadow', 'Glow', 'No Effect'])
        mode.setCurrentIndex(0 if values.get('enabled') and values['type'] == 'shadow' else
                             1 if values.get('enabled') and values['type'] == 'glow' else 2)
        root.addWidget(QLabel('Effect'))
        root.addWidget(mode)
        # Preview is rendered by the actual Qt graphics-effect engine.
        preview = QFrame()
        preview.setMinimumHeight(145)
        preview.setStyleSheet('QFrame {background: #171923; border-radius: 12px;}')
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(85, 35, 85, 35)
        sample = QLabel('SpyderMUSIC')
        sample.setAlignment(Qt.AlignCenter)
        sample.setMinimumHeight(65)
        sample.setStyleSheet('background: #34394b; color: white; border: 1px solid #7e879c; border-radius: 12px; font: bold 18px Arial;')
        preview_layout.addWidget(sample)
        sample_fx = QGraphicsDropShadowEffect(sample)
        sample.setGraphicsEffect(sample_fx)
        root.addWidget(preview)
        color = [QColor(values['color'])]
        color_button = QPushButton('🎨 Choose Effect Color…')
        root.addWidget(color_button)
        def choose_color():
            picked = QColorDialog.getColor(color[0], dlg, 'Effektfarbe', QColorDialog.ShowAlphaChannel)
            if picked.isValid():
                color[0] = picked
                values['color'] = picked.name(QColor.HexRgb)
                values['opacity'] = round(picked.alphaF() * 100)
                sliders['opacity'][0].setValue(values['opacity'])
                refresh()
        color_button.clicked.connect(choose_color)
        sliders = {}
        for key, title, maximum in [('blur', 'Softness / Blur', 100),
                                    ('distance', 'Shadow Distance', 80),
                                    ('angle', 'Richtung', 360),
                                    ('opacity', 'Intensity / Opacity', 100)]:
            line = QHBoxLayout()
            label = QLabel(title)
            number = QLabel()
            number.setMinimumWidth(52)
            number.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            line.addWidget(label)
            line.addWidget(number)
            root.addLayout(line)
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, maximum)
            slider.setValue(int(values[key]))
            root.addWidget(slider)
            sliders[key] = (slider, number, label)
        def refresh():
            values['enabled'] = mode.currentIndex() != 2
            values['type'] = 'glow' if mode.currentIndex() == 1 else 'shadow'
            for key, (slider, number, _) in sliders.items():
                values[key] = slider.value()
                number.setText(str(slider.value()) + ('°' if key == 'angle' else '%' if key == 'opacity' else ' px'))
            for key in ('distance', 'angle'):
                slider, _, label = sliders[key]
                slider.setEnabled(mode.currentIndex() == 0)
                label.setEnabled(mode.currentIndex() == 0)
            color_button.setStyleSheet('background-color: ' + color[0].name() + ';')
            import math
            qcolor = QColor(color[0])
            qcolor.setAlpha(round(255 * values['opacity'] / 100))
            sample_fx.setColor(qcolor)
            sample_fx.setBlurRadius(values['blur'])
            distance = values['distance'] if values['type'] == 'shadow' else 0
            angle = math.radians(values['angle'])
            sample_fx.setOffset(math.cos(angle) * distance, math.sin(angle) * distance)
            sample_fx.setEnabled(values['enabled'])
            spec['editor_effect'] = copy.deepcopy(values) if values['enabled'] else {}
            self.manager.apply_editor_effects(self.data)
        for slider, _, _ in sliders.values():
            slider.valueChanged.connect(refresh)
        mode.currentIndexChanged.connect(refresh)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        root.addWidget(buttons)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        refresh()
        if dlg.exec() == QDialog.Accepted:
            if spec.get('editor_effect', {}) != old:
                self.undo_stack.append(copy.deepcopy({**self.data, self.selected[0]:
                    (copy.deepcopy(self.data[self.selected[0]]) if self.selected[0] not in ('seekbars', 'visualizations') else
                     copy.deepcopy(self.data[self.selected[0]]))}))
                # Undo snapshot must contain the previous effect, not the live preview.
                collection = self.undo_stack[-1][self.selected[0]]
                item = collection[self.selected[1]]
                if old:
                    item['editor_effect'] = old
                else:
                    item.pop('editor_effect', None)
                self.undo_stack[:] = self.undo_stack[-60:]
                self.redo_stack.clear()
        else:
            if old:
                spec['editor_effect'] = old
            else:
                spec.pop('editor_effect', None)
            self.manager.apply_editor_effects(self.data)

    def frame_properties(self):
        if not self.selected:
            return
        key = self.selected[1]
        if self.selected[0] == 'visualizations':
            QMessageBox.information(self, 'Border', 'Visualizations currently have no dedicated border container.')
            return
        bindings = self.data.setdefault('audio_bindings', [])
        current = next((b for b in bindings if b.get('target') == key), None)
        dialog = QDialog(self)
        dialog.setWindowTitle('Frame and Audio Reactivity: ' + str(key))
        layout = QVBoxLayout(dialog)
        enabled = QCheckBox('Audio-Reactive Border')
        enabled.setChecked(current is not None)
        layout.addWidget(enabled)
        band = QComboBox()
        band.addItems(['level', 'bass', 'mids', 'highs'])
        band.setCurrentText((current or {}).get('band', 'level'))
        layout.addWidget(band)
        width = QSpinBox()
        width.setRange(1, 30)
        width.setValue((current or {}).get('width', 3))
        width.setPrefix('Border Width: ')
        layout.addWidget(width)
        strength = QSpinBox()
        strength.setRange(1, 255)
        strength.setValue((current or {}).get('strength', 180))
        strength.setPrefix('Intensity: ')
        layout.addWidget(strength)
        color = QColor((current or {}).get('color', '#00f5ff'))
        choose = QPushButton('Border Color…')
        layout.addWidget(choose)

        def pick():
            nonlocal color
            c = QColorDialog.getColor(color, dialog, 'Border Color', QColorDialog.ShowAlphaChannel)
            if c.isValid():
                color = c

        choose.clicked.connect(pick)
        gradient = [copy.deepcopy((current or {}).get('gradient'))]
        change = QPushButton('Edit Gradient…')
        layout.addWidget(change)

        def edit_gradient():
            dlg = GradientDialog(dialog, gradient[0])
            if dlg.exec():
                gradient[0] = dlg.result_gradient()

        change.clicked.connect(edit_gradient)
        clear = QPushButton('Remove Gradient')
        layout.addWidget(clear)
        clear.clicked.connect(lambda: gradient.__setitem__(0, None))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec():
            self.snapshot()
            bindings[:] = [b for b in bindings if b.get('target') != key]
            if enabled.isChecked():
                entry = {
                    'target': key, 'effect': 'outline', 'band': band.currentText(),
                    'width': width.value(), 'strength': strength.value(),
                    'color': color.name(), 'radius': 10, 'padding': 0
                }
                if gradient[0]:
                    entry['gradient'] = gradient[0]
                bindings.append(entry)
            self.reload_preview()

    def qss_properties(self):
        # Resolve the stylesheet alongside the manifest, not via a legacy root path.
        # Preserve unsaved QSS edits when reopening Studio.
        if self.qss_draft is None:
            manifest = self.manager.theme_file(self.original_theme, '.json')
            qss_path = manifest.with_suffix('.qss')
            self.qss_draft = qss_path.read_text(encoding='utf-8') if qss_path.is_file() else ''
        titles = copy.deepcopy(self.data.get("panel_titles", {}))
        if "top" not in titles:
            titles["top"] = {"text": self.window.toplabel.text(), "visible": True}
        dlg = QssStudio(self, self.qss_draft, titles)
        if dlg.exec() == QDialog.Accepted:
            self.qss_draft = dlg.result_qss()
            self.data["panel_titles"] = dlg.result_panel_titles()
            QApplication.instance().setStyleSheet(self.qss_draft)
            # Update the actual headers immediately. Saving the theme only
            # persists the already-visible changes; it is not a preview trigger.
            header_widgets = {
                "brain": self.window.brain_header,
                "main": self.window.track,
                "top": self.window.toplabel,
                "queue": self.window.queue_header,
                "history": self.window.history_header,
            }
            for key, label in header_widgets.items():
                settings = self.data["panel_titles"].get(key, {})
                if "text" in settings:
                    label.setText(settings["text"])
                label.setVisible(settings.get("visible", True))
            # Keep the preview in sync without reloading the entire theme.
            self.manager._panel_title_settings = copy.deepcopy(self.data["panel_titles"])

    def audio_background_properties(self):
        current = copy.deepcopy(self.data.get('audio_reactive', {}))
        dialog = QDialog(self)
        dialog.setWindowTitle('Audio-Reactive Background')
        layout = QVBoxLayout(dialog)
        enabled = QCheckBox('Show Audio-Reactive Background')
        enabled.setChecked(bool(current.get('enabled', False)))
        layout.addWidget(enabled)
        layout.addWidget(QLabel('Audio Source'))
        band = QComboBox()
        band.addItems(['level', 'bass', 'mids', 'highs'])
        band.setCurrentText(current.get('band', 'level'))
        layout.addWidget(band)
        intensity = QSpinBox()
        intensity.setRange(0, 200)
        intensity.setValue(int(current.get('intensity', 120)))
        intensity.setPrefix('Intensity: ')
        layout.addWidget(intensity)
        color = [QColor(current.get('color', '#ff175b'))]
        choose = QPushButton('Choose Base Color…')
        layout.addWidget(choose)

        def pick_color():
            picked = QColorDialog.getColor(color[0], dialog, 'Choose Base Color')
            if picked.isValid():
                color[0] = picked
                choose.setStyleSheet('background-color: ' + picked.name() + ';')

        choose.clicked.connect(pick_color)
        gradient = [copy.deepcopy(current.get('gradient'))]
        edit = QPushButton('Edit Gradient…')
        layout.addWidget(edit)

        def edit_gradient():
            editor = GradientDialog(dialog, gradient[0])
            if editor.exec():
                gradient[0] = editor.result_gradient()

        edit.clicked.connect(edit_gradient)
        remove = QPushButton('Remove Gradient (Use Base Color)')
        layout.addWidget(remove)
        remove.clicked.connect(lambda: gradient.__setitem__(0, None))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec():
            updated = {
                'enabled': enabled.isChecked(), 'band': band.currentText(),
                'intensity': intensity.value(), 'color': color[0].name()
            }
            if gradient[0]:
                updated['gradient'] = gradient[0]
            if updated != current:
                self.snapshot()
                self.data['audio_reactive'] = updated
                self.manager.audio_reactive = self._parsed_audio_background(updated)
                self.manager.canvas.update()
                self.reload_preview()

    def _parsed_audio_background(self, config):
        parsed = dict(config)
        if parsed.get('gradient'):
            parsed['gradient'] = self.manager._parse_single_audio_gradient(parsed['gradient'])
        return parsed

    def background_gradient(self):
        existing = self.data.get('color_gradients', [])
        dlg = GradientDialog(self, existing[0] if existing else None)
        if dlg.exec():
            self.snapshot()
            self.data['color_gradients'] = [dlg.result_gradient()]
            self.reload_preview()

    def background_properties(self):
        choices = ['Blurred Album Cover', 'Solid Color', 'Custom Image', 'No Image']
        choice, ok = QInputDialog.getItem(self, 'Background', 'Background Type', choices, 0, False)
        if not ok:
            return
        cfg = {'mode': 'album'}
        if choice == choices[1]:
            color = QColorDialog.getColor(QColor('#10151e'), self, 'Background Color')
            if not color.isValid():
                return
            cfg = {'mode': 'color', 'color': color.name()}
        elif choice == choices[2]:
            path, _ = QFileDialog.getOpenFileName(self, 'Background Image', '', 'Images (*.png *.jpg *.jpeg *.webp)')
            if not path:
                return
            cfg = {'mode': 'image', 'path': path}
        elif choice == choices[3]:
            cfg = {'mode': 'none'}
        self.snapshot()
        self.data['editor_background'] = cfg
        self.window.backdrop.set_editor_background(cfg)

    def reload_preview(self):
        path = self.manager.theme_file(self.original_theme, '.json').parent / '_stage16_preview.json'
        self.repair_bounds()
        path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding='utf-8')
        try:
            self.manager.apply(self.manager.theme_file(self.original_theme, '.json').parent.relative_to(self.manager.theme_dir).joinpath('_stage16_preview').as_posix())
            self.manager.active_theme = self.original_theme
            self.apply_preview()
        except Exception as exc:
            QMessageBox.warning(self, 'Preview error', str(exc))
        finally:
            path.unlink(missing_ok=True)

    def save(self):
        """Save changes to the currently edited theme, without renaming it."""
        self.repair_bounds()
        data = copy.deepcopy(self.data)
        canvas = self.manager.canvas
        data['reference_canvas'] = {
            'width': max(1, canvas.width()),
            'height': max(1, canvas.height()),
        }
        dest = self.manager.theme_file(self.original_theme, '.json')
        self._copy_image_assets_for_save(data, dest)
        dest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        dest.with_suffix('.qss').write_text(self.qss_draft or '', encoding='utf-8')
        self._cleanup_unused_png_assets(dest.parent)
        self.new_theme_destination = None
        self.stop(revert=False)
        self.window.refresh_theme_menu()
        self.window.switch_theme(self.original_theme)

    def save_as(self):
        """Create a separately named theme, preserving the original."""
        name, ok = QInputDialog.getText(self, 'Save Theme As', 'New theme name (filename without extension):', text=self.manager.active_theme.split('/')[-1] + '_custom')
        if not ok:
            return
        slug = re.sub('[^a-zA-Z0-9_-]', '_', name.strip())
        if not slug or slug == 'default':
            QMessageBox.warning(self, 'Invalid Name', 'Please choose another name.')
            return
        dest = self.manager.theme_file(self.original_theme, '.json').parent / (slug + '.json')
        if dest.exists() and QMessageBox.question(self, 'Overwrite Theme?', f'{slug} already exists. Overwrite it?') != QMessageBox.Yes:
            return
        self.repair_bounds()
        data = copy.deepcopy(self.data)
        # Record the real canvas, not the screen resolution or the editor overlay.
        canvas = self.manager.canvas
        data['reference_canvas'] = {'width': max(1, canvas.width()),
                                    'height': max(1, canvas.height())}
        data['id'] = slug
        data['name'] = name
        bg = data.get('editor_background', {})
        if bg.get('mode') == 'image' and Path(bg.get('path', '')).is_file():
            import shutil
            src = Path(bg['path'])
            target = dest.parent / (slug + '_background' + src.suffix.lower())
            if src.resolve() != target.resolve():
                shutil.copyfile(src, target)
            bg['path'] = str(target)
        self._copy_image_assets_for_save(data, dest)
        dest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        source = self.manager.theme_file(self.original_theme, '.json').with_suffix('.qss')
        dest.with_suffix('.qss').write_text(
            self.qss_draft if self.qss_draft is not None else source.read_text(encoding='utf-8'),
            encoding='utf-8'
        )
        self._cleanup_unused_png_assets(dest.parent)
        self.stop(revert=False)
        self.window.refresh_theme_menu()
        self.window.switch_theme(dest.relative_to(self.manager.theme_dir).with_suffix('').as_posix())
