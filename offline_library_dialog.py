"""Theme-independent local library control panel; no changes to DJ queue."""
from pathlib import Path
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QListWidget, QInputDialog, QFileDialog, QMessageBox)
from PySide6.QtCore import Qt
import spyder_engine as E
from music_library import add_audio_file, scan_folder
from playlist_manager import (list_playlists, list_playlist_tracks, create_playlist,
    rename_playlist, duplicate_playlist, delete_playlist, add_track, remove_items,
    reorder_playlist, import_m3u, export_m3u8)

class OfflineLibraryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Local Music & Playlists')
        self.resize(780, 560)
        layout = QVBoxLayout(self)
        playlist_row = QHBoxLayout()
        self.playlists = QComboBox()
        self.playlists.currentIndexChanged.connect(self.refresh_tracks)
        playlist_row.addWidget(QLabel('Playlist:'))
        playlist_row.addWidget(self.playlists, 1)
        for name, handler in [('New', self.new_playlist), ('Rename', self.rename), ('Copy', self.duplicate), ('Delete', self.delete)]:
            b = QPushButton(name); b.clicked.connect(handler); playlist_row.addWidget(b)
        layout.addLayout(playlist_row)
        self.tracks = QListWidget()
        self.tracks.setSelectionMode(QListWidget.ExtendedSelection)
        self.tracks.itemDoubleClicked.connect(lambda _: self.play_selected())
        layout.addWidget(self.tracks, 1)
        actions = QHBoxLayout()
        for name, handler in [('Add files', self.add_files), ('Add folder', self.add_folder), ('Remove selected', self.remove_selected),
                              ('Move up', lambda: self.move(-1)), ('Move down', lambda: self.move(1))]:
            b = QPushButton(name); b.clicked.connect(handler); actions.addWidget(b)
        layout.addLayout(actions)
        actions2 = QHBoxLayout()
        for name, handler in [('Import M3U', self.import_playlist), ('Export M3U8', self.export_playlist),
                              ('Play selected', self.play_selected), ('Next', self.next_track), ('Stop', self.stop_track)]:
            b = QPushButton(name); b.clicked.connect(handler); actions2.addWidget(b)
        layout.addLayout(actions2)
        self.status = QLabel('Ready')
        layout.addWidget(self.status)
        self.refresh_playlists()

    def _error(self, exc):
        QMessageBox.warning(self, 'Local Music', str(exc))
        self.status.setText(str(exc))

    def _run(self, fn):
        try:
            result = fn()
            return result
        except Exception as exc:
            self._error(exc)
            return None

    def playlist_id(self):
        return self.playlists.currentData()

    def refresh_playlists(self, selected=None):
        if selected is None:
            selected = self.playlist_id()
        with E.db_connect() as conn:
            playlists = list_playlists(conn)
        self.playlists.blockSignals(True)
        self.playlists.clear()
        for p in playlists:
            self.playlists.addItem(f"{p['name']} ({p['track_count']})", p['id'])
        index = self.playlists.findData(selected)
        self.playlists.setCurrentIndex(index if index >= 0 else (0 if playlists else -1))
        self.playlists.blockSignals(False)
        self.refresh_tracks()

    def refresh_tracks(self, *_):
        self.tracks.clear()
        if self.playlist_id() is None:
            return
        with E.db_connect() as conn:
            tracks = list_playlist_tracks(conn, self.playlist_id())
        for track in tracks:
            item = __import__('PySide6.QtWidgets', fromlist=['QListWidgetItem']).QListWidgetItem(
                f"{track['artist'] or 'Unknown'} - {track['title']}" + ('' if Path(track['path']).is_file() else ' [MISSING]'))
            item.setData(Qt.UserRole, track)
            self.tracks.addItem(item)
        self.status.setText(f'{len(tracks)} tracks')

    def new_playlist(self):
        name, ok = QInputDialog.getText(self, 'New playlist', 'Name:')
        if ok and name.strip():
            def action():
                with E.db_connect() as conn: return create_playlist(conn, name)
            pid = self._run(action)
            if pid is not None: self.refresh_playlists(pid)

    def rename(self):
        if self.playlist_id() is None: return
        name, ok = QInputDialog.getText(self, 'Rename playlist', 'New name:')
        if ok and name.strip():
            def action():
                with E.db_connect() as conn: rename_playlist(conn, self.playlist_id(), name)
            self._run(action); self.refresh_playlists()

    def duplicate(self):
        if self.playlist_id() is None: return
        name, ok = QInputDialog.getText(self, 'Duplicate playlist', 'New name:')
        if ok and name.strip():
            def action():
                with E.db_connect() as conn: return duplicate_playlist(conn, self.playlist_id(), name)
            pid = self._run(action)
            if pid is not None: self.refresh_playlists(pid)

    def delete(self):
        if self.playlist_id() is None: return
        if QMessageBox.question(self, 'Delete playlist', 'Delete playlist? Audio files will be kept.') != QMessageBox.Yes: return
        def action():
            with E.db_connect() as conn: delete_playlist(conn, self.playlist_id())
        self._run(action); self.refresh_playlists()

    def add_files(self):
        if self.playlist_id() is None: return self._error('Create a playlist first.')
        files, _ = QFileDialog.getOpenFileNames(self, 'Add local audio files', '', 'Audio (*.webm *.mp3 *.flac *.opus *.ogg *.wav *.m4a *.aac);;All files (*)')
        if not files: return
        pid = self.playlist_id()
        def action():
            with E.db_connect() as conn:
                for file in files: add_track(conn, pid, add_audio_file(conn, file, source='playlist_import'))
        self._run(action); self.refresh_playlists(pid)

    def add_folder(self):
        if self.playlist_id() is None: return self._error('Create a playlist first.')
        folder = QFileDialog.getExistingDirectory(self, 'Import audio folder')
        if not folder: return
        pid = self.playlist_id()
        def action():
            with E.db_connect() as conn:
                result = scan_folder(conn, folder)
                rows = conn.execute('SELECT id,path FROM local_library WHERE available=1').fetchall()
                root = Path(folder).resolve()
                for audio_id, path in rows:
                    if Path(path).is_relative_to(root): add_track(conn, pid, audio_id)
                return result
        result = self._run(action)
        if result is not None:
            self.refresh_playlists(pid)
            self.status.setText(f"Indexed {result['registered']} files, {result['errors']} errors")

    def remove_selected(self):
        if self.playlist_id() is None: return
        ids = [i.data(Qt.UserRole)['item_id'] for i in self.tracks.selectedItems()]
        if not ids: return
        pid = self.playlist_id()
        def action():
            with E.db_connect() as conn: remove_items(conn, pid, ids)
        self._run(action); self.refresh_playlists(pid)

    def move(self, direction):
        pid = self.playlist_id()
        selected = self.tracks.currentRow()
        target = selected + direction
        if pid is None or selected < 0 or not (0 <= target < self.tracks.count()): return
        ids = [self.tracks.item(i).data(Qt.UserRole)['item_id'] for i in range(self.tracks.count())]
        ids[selected], ids[target] = ids[target], ids[selected]
        def action():
            with E.db_connect() as conn: reorder_playlist(conn, pid, ids)
        self._run(action); self.refresh_playlists(pid); self.tracks.setCurrentRow(target)

    def import_playlist(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Import playlist', '', 'Playlists (*.m3u *.m3u8)')
        if not path: return
        def action():
            with E.db_connect() as conn: return import_m3u(conn, path)
        result = self._run(action)
        if result: self.refresh_playlists(result['playlist_id']); self.status.setText(f"Imported {result['imported']}, skipped {result['skipped']}")

    def export_playlist(self):
        if self.playlist_id() is None: return
        path, _ = QFileDialog.getSaveFileName(self, 'Export playlist', '', 'M3U8 (*.m3u8)')
        if not path: return
        def action():
            with E.db_connect() as conn: return export_m3u8(conn, self.playlist_id(), path)
        result = self._run(action)
        if result is not None: self.status.setText(f'Exported {result} tracks')

    def play_selected(self):
        pid = self.playlist_id()
        if pid is None or self.tracks.count() == 0: return
        index = max(0, self.tracks.currentRow())
        def action():
            return E.play_local_playlist(pid, index)
        result = self._run(action)
        if result: self.status.setText(f"Playing: {result['artist']} - {result['title']}")

    def next_track(self):
        result = self._run(E.next_local_playlist_track)
        self.status.setText(f"Playing: {result['title']}" if result else 'Playlist finished')

    def stop_track(self):
        self._run(E.stop_local_playlist)
        self.status.setText('Stopped')
