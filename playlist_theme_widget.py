"""Theme-controlled playlist with persistent editing and local playback."""
import random
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QListWidget, QListWidgetItem, QPushButton, QMessageBox, QMenu, QFileDialog, QInputDialog,
    QAbstractItemView)
import spyder_engine as E
from music_library import add_audio_file, AUDIO_EXTENSIONS
from playlist_manager import (list_playlists, list_playlist_tracks, create_playlist,
    rename_playlist, duplicate_playlist, delete_playlist, add_track, remove_items,
    reorder_playlist, import_m3u, export_m3u8)


class PlaylistTrackList(QListWidget):
    filesDropped = Signal(list)
    orderChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            if paths:
                self.filesDropped.emit(paths)
                event.acceptProposedAction()
            return
        super().dropEvent(event)
        self.orderChanged.emit()


class PlaylistThemePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('playlist_panel')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._undo_order = None
        self._before_drag = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        header = QHBoxLayout()
        self.selector = QComboBox(self)
        self.selector.setObjectName('playlist_selector')
        self.selector.currentIndexChanged.connect(self.refresh_tracks)
        header.addWidget(self.selector, 1)
        self.shuffle = QPushButton('Shuffle', self)
        self.shuffle.setObjectName('playlist_shuffle')
        self.shuffle.setCheckable(True)
        self.shuffle.toggled.connect(self._shuffle_playback)
        header.addWidget(self.shuffle)
        self.menu_button = QPushButton('⋯', self)
        self.menu_button.setObjectName('playlist_refresh')  # Retain existing theme/QSS selector.
        self.menu_button.clicked.connect(self._playlist_menu)
        header.addWidget(self.menu_button)
        layout.addLayout(header)
        self.tracks = PlaylistTrackList(self)
        self.tracks.setObjectName('playlist_tracks')
        self.tracks.itemDoubleClicked.connect(self.play_selected)
        self.tracks.filesDropped.connect(self._add_paths)
        self.tracks.orderChanged.connect(self._save_drag_order)
        self.tracks.model().rowsAboutToBeMoved.connect(self._remember_drag_order)
        self.tracks.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tracks.customContextMenuRequested.connect(self._track_menu)
        layout.addWidget(self.tracks, 1)
        self.status = QLabel('', self)
        self.status.setObjectName('playlist_status')
        layout.addWidget(self.status)
        self.refresh_playlists()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.refresh_playlists)

    def _run(self, action):
        try:
            with E.db_connect() as conn:
                result = action(conn)
            return result
        except Exception as exc:
            QMessageBox.warning(self, 'Playlist', str(exc))
            return None

    def _pid(self):
        return self.selector.currentData()

    def _ids(self):
        return [int(item.data(Qt.UserRole)) for item in self.tracks.selectedItems()]

    def refresh_playlists(self, select=None):
        previous = self._pid() if select is None else select
        try:
            playlists = self._run(list_playlists)
            if playlists is None:
                return
            self.selector.blockSignals(True)
            self.selector.clear()
            for entry in playlists:
                self.selector.addItem(entry['name'], entry['id'])
            idx = self.selector.findData(previous)
            self.selector.setCurrentIndex(idx if idx >= 0 else (0 if playlists else -1))
            self.selector.blockSignals(False)
            self.refresh_tracks()
        except Exception as exc:
            self.status.setText(str(exc))

    def refresh_tracks(self, *_):
        self.tracks.clear()
        pid = self._pid()
        if pid is None:
            self.status.setText('No playlist selected')
            return
        rows = self._run(lambda c: list_playlist_tracks(c, pid))
        if rows is None:
            return
        for index, track in enumerate(rows):
            label = f"{track['artist'] or 'Unknown'} - {track['title']}"
            if not track['available']:
                label += ' [MISSING]'
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, track['item_id'])
            item.setData(Qt.UserRole + 1, index)
            self.tracks.addItem(item)
        self.status.setText(f'{len(rows)} tracks')

    def play_selected(self, *_):
        pid, item = self._pid(), self.tracks.currentItem()
        if pid is None or item is None:
            return
        try:
            E.play_local_playlist(int(pid), int(item.data(Qt.UserRole + 1)))
            E.get_offline_controller().set_shuffle(self.shuffle.isChecked())
        except Exception as exc:
            QMessageBox.warning(self, 'Playlist', str(exc))

    def _shuffle_playback(self, enabled):
        controller = E.get_offline_controller()
        controller.set_shuffle(bool(enabled))

    def _remember_drag_order(self, *_):
        self._before_drag = [self.tracks.item(i).data(Qt.UserRole) for i in range(self.tracks.count())]

    def _save_drag_order(self):
        pid = self._pid()
        if pid is None:
            return
        ids = [self.tracks.item(i).data(Qt.UserRole) for i in range(self.tracks.count())]
        self._undo_order = self._before_drag or None
        if self._run(lambda c: reorder_playlist(c, pid, ids)) is None:
            # reorder_playlist returns None on success, so refresh unconditionally.
            pass
        self.refresh_tracks()

    def _add_paths(self, paths):
        pid = self._pid()
        if pid is None:
            QMessageBox.information(self, 'Playlist', 'Create a playlist first.')
            return
        def operation(conn):
            added = 0
            for raw in paths:
                p = Path(raw)
                candidates = (p.rglob('*') if p.is_dir() else (p,))
                for file in candidates:
                    if file.is_file() and file.suffix.lower() in AUDIO_EXTENSIONS:
                        try:
                            audio_id = add_audio_file(conn, file, source='playlist_import')
                            add_track(conn, pid, audio_id)
                            added += 1
                        except (OSError, ValueError):
                            continue
            return added
        count = self._run(operation)
        if count is not None:
            self.refresh_tracks()
            self.status.setText(f'Added {count} tracks')

    def _track_menu(self, pos):
        item = self.tracks.itemAt(pos)
        if item and not item.isSelected():
            self.tracks.clearSelection()
            item.setSelected(True)
            self.tracks.setCurrentItem(item)
        menu = QMenu(self)
        menu.setObjectName('playlist_context_menu')
        if item:
            menu.addAction('Play Now', self.play_selected)
            menu.addAction('Play Next', lambda: self._enqueue(True))
            menu.addAction('Add to Local Queue', lambda: self._enqueue(False))
            menu.addSeparator()
            menu.addAction('Remove from Playlist', self._remove_selected)
            menu.addAction('Move Up', lambda: self._move(-1))
            menu.addAction('Move Down', lambda: self._move(1))
            menu.addAction('Track Info', self._track_info)
            menu.addSeparator()
        menu.addAction('Add Files…', self._add_files)
        menu.addAction('Add Folder…', self._add_folder)
        menu.addAction('New Playlist…', self._new_playlist)
        menu.addAction('Shuffle Playlist Order', self._shuffle_order)
        menu.exec(self.tracks.viewport().mapToGlobal(pos))

    def _enqueue(self, first):
        pid = self._pid()
        if pid is None:
            return
        rows = self._run(lambda c: list_playlist_tracks(c, pid))
        if rows is None:
            return
        selected = set(self._ids())
        queued = [dict(row) for row in rows if row['item_id'] in selected]
        E.get_offline_controller().enqueue_local(queued, first=first)
        self.status.setText(f'{len(queued)} tracks added to local queue')

    def _remove_selected(self):
        pid, ids = self._pid(), self._ids()
        if pid is None or not ids:
            return
        self._run(lambda c: remove_items(c, pid, ids))
        self.refresh_tracks()

    def _move(self, direction):
        pid = self._pid()
        if pid is None:
            return
        ids = [self.tracks.item(i).data(Qt.UserRole) for i in range(self.tracks.count())]
        selected = set(self._ids())
        self._undo_order = list(ids)
        positions = range(1, len(ids)) if direction < 0 else range(len(ids) - 2, -1, -1)
        for i in positions:
            a, b = (i, i - 1) if direction < 0 else (i, i + 1)
            if ids[a] in selected and ids[b] not in selected:
                ids[a], ids[b] = ids[b], ids[a]
        self._run(lambda c: reorder_playlist(c, pid, ids))
        self.refresh_tracks()

    def _track_info(self):
        pid = self._pid()
        rows = self._run(lambda c: list_playlist_tracks(c, pid)) if pid is not None else []
        ids = set(self._ids())
        if rows:
            track = next((t for t in rows if t['item_id'] in ids), None)
            if track:
                QMessageBox.information(self, 'Track Info', '\n'.join(
                    f'{k}: {track.get(k)}' for k in ('artist', 'title', 'duration', 'path', 'available')))

    def _add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, 'Add Audio Files', '', 'Audio Files (*.webm *.mp3 *.flac *.ogg *.opus *.wav *.m4a *.aac *.wma)')
        if files:
            self._add_paths(files)

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, 'Add Audio Folder')
        if folder:
            self._add_paths([folder])

    def _new_playlist(self):
        name, ok = QInputDialog.getText(self, 'New Playlist', 'Name:')
        if ok and name.strip():
            pid = self._run(lambda c: create_playlist(c, name))
            if pid is not None:
                self.refresh_playlists(pid)

    def _rename_playlist(self):
        pid = self._pid()
        if pid is None:
            return
        name, ok = QInputDialog.getText(self, 'Rename Playlist', 'Name:', text=self.selector.currentText())
        if ok and name.strip():
            self._run(lambda c: rename_playlist(c, pid, name))
            self.refresh_playlists(pid)

    def _duplicate_playlist(self):
        pid = self._pid()
        if pid is None:
            return
        name, ok = QInputDialog.getText(self, 'Duplicate Playlist', 'New name:', text=self.selector.currentText() + ' Copy')
        if ok and name.strip():
            new_id = self._run(lambda c: duplicate_playlist(c, pid, name))
            if new_id is not None:
                self.refresh_playlists(new_id)

    def _delete_playlist(self):
        pid = self._pid()
        if pid is None:
            return
        if QMessageBox.question(self, 'Delete Playlist',
                                f'Delete playlist "{self.selector.currentText()}"?\nAudio files will be kept.',
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._run(lambda c: delete_playlist(c, pid))
            self.refresh_playlists()

    def _shuffle_order(self):
        pid = self._pid()
        if pid is None:
            return
        ids = [self.tracks.item(i).data(Qt.UserRole) for i in range(self.tracks.count())]
        if len(ids) < 2:
            return
        self._undo_order = list(ids)
        random.shuffle(ids)
        self._run(lambda c: reorder_playlist(c, pid, ids))
        self.refresh_tracks()

    def _undo_sort(self):
        pid = self._pid()
        if pid is None or not self._undo_order:
            return
        current = [self.tracks.item(i).data(Qt.UserRole) for i in range(self.tracks.count())]
        if set(current) != set(self._undo_order):
            QMessageBox.information(self, 'Undo Sort', 'Playlist entries have changed; cannot restore the previous order.')
            return
        self._run(lambda c: reorder_playlist(c, pid, self._undo_order))
        self._undo_order = None
        self.refresh_tracks()

    def _import_m3u(self):
        file, _ = QFileDialog.getOpenFileName(self, 'Import Playlist', '', 'Playlists (*.m3u *.m3u8)')
        if file:
            result = self._run(lambda c: import_m3u(c, file))
            if result:
                self.refresh_playlists(result['playlist_id'])

    def _export_m3u(self):
        pid = self._pid()
        if pid is None:
            return
        file, _ = QFileDialog.getSaveFileName(self, 'Export Playlist', self.selector.currentText() + '.m3u8', 'M3U8 (*.m3u8)')
        if file:
            self._run(lambda c: export_m3u8(c, pid, file))

    def _playlist_menu(self):
        menu = QMenu(self)
        menu.setObjectName('playlist_context_menu')
        menu.addAction('New Playlist…', self._new_playlist)
        menu.addAction('Rename Playlist…', self._rename_playlist)
        menu.addAction('Duplicate Playlist…', self._duplicate_playlist)
        menu.addAction('Delete Playlist…', self._delete_playlist)
        menu.addSeparator()
        menu.addAction('Import M3U/M3U8…', self._import_m3u)
        menu.addAction('Export M3U8…', self._export_m3u)
        menu.addSeparator()
        menu.addAction('Shuffle Playlist Order', self._shuffle_order)
        menu.addAction('Undo Last Sort', self._undo_sort)
        menu.addAction('Refresh', self.refresh_playlists)
        menu.exec(self.menu_button.mapToGlobal(self.menu_button.rect().bottomLeft()))
