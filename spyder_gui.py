# SpyderMUSIC Qt6 GUI — extracted from Qt6 Alpha 20
# Presentation layer. The engine remains in spyder_engine.py.

from spyder_engine import *
from theme_api import ThemeManager, ThemeCanvas
from gpu_backend import QOpenGLWidget, paint_source_cover, clear_gpu_surface
from theme_editor import ThemeEditor
from waveform_rms import RMSWaveform
import json
from pathlib import Path
import ctypes
import spyder_engine as E
from spydertrack_control import SpyderTrackControl
from gui_frame_budget import GuiFrameBudget

THEME_DIR = os.path.join(BASE_DIR, "themes")
def _default_theme_qss():
    root = Path(THEME_DIR)
    direct = root / 'default.qss'
    if direct.is_file():
        return str(direct)
    matches = sorted(root.rglob('default.qss')) if root.is_dir() else []
    return str(matches[0]) if len(matches) == 1 else str(direct)

DEFAULT_THEME = _default_theme_qss()

def _load_theme_stylesheet(app, path=DEFAULT_THEME):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            app.setStyleSheet(fh.read())
        return True
    except Exception as exc:
        print(f"⚠️ Theme could not be loaded: {exc}")
        return False

# =============================================================================
last_user_interaction: float = 0
_seek_job = None

def do_seek(val):
    global last_user_interaction, _seek_job
    last_user_interaction = time.time()
    gui.last_user_interaction = last_user_interaction

    try:
        seconds = int(float(val))
        if _seek_job is not None:
            try:
                gui.root.after_cancel(_seek_job)
            except Exception:
                pass
        if gui.root is not None:
            _seek_job = gui.root.after(250, lambda: mpv_player.seek_absolute(seconds))
    except Exception as exc:
        print(f"Seek error: {exc}")


# =============================================================================
# GUI VISUAL UPDATER
# =============================================================================
def _append_to_history(track_str: str):
    gui.hist_box.insert(0, track_str)
    if gui.hist_box.size() > HISTORY_LIMIT:
        gui.hist_box.delete("end")


def _set_top_list(favs: list):
    # Avoid invalidating every row when the ranking has not changed.
    widget = gui.list_top
    if widget is None:
        return
    display = [f"{name} ({score})" for name, score in favs]
    if widget.size() == len(display) and all(
        widget.get(i) == value for i, value in enumerate(display)
    ):
        return
    widget.delete(0, "end")
    for value in display:
        widget.insert("end", value)


def _set_queue_list(rows: list):
    """Refresh the Qt queue with stable SQLite rowids (duplicate titles are valid)."""
    from PySide6.QtWidgets import QListWidgetItem
    from PySide6.QtCore import QSize
    widget = gui.queue_box.w if gui.queue_box is not None else None
    if widget is None:
        return
    # Skip the expensive clear/repopulate cycle if SQLite returned the same
    # ordered queue. Compare both row IDs and titles (duplicates are valid).
    desired = [(int(rowid), str(title)) for rowid, title in rows]
    if widget.count() == len(desired) and all(
        (int(widget.item(i).data(0x0100)), widget.item(i).text()) == item
        for i, item in enumerate(desired)
    ):
        return
    selected_ids = {i.data(0x0100) for i in widget.selectedItems()}
    widget.blockSignals(True)
    try:
        widget.clear()
        for rowid, title in desired:
            item = QListWidgetItem(title)
            item.setData(0x0100, rowid)  # Qt.UserRole
            item.setSizeHint(QSize(0, 18))
            widget.addItem(item)
            if rowid in selected_ids:
                item.setSelected(True)
    finally:
        widget.blockSignals(False)


def update_queue_ui():
    if gui.queue_box is None or gui.root is None:
        return
    def refresh():
        try:
            with db_connect() as conn:
                rows = conn.execute(
                    "SELECT rowid, item FROM queue ORDER BY timestamp ASC, rowid ASC"
                ).fetchall()
            _set_queue_list(rows)
        except Exception as exc:
            print(f"Queue UI update error: {exc}")
    # Read when callback runs, not before: queued callbacks must not restore stale order.
    gui.root.after(0, refresh)


def update_top_list_ui():
    try:
        with db_connect() as conn:
            favs = conn.execute(
                "SELECT item_name, score FROM preferences "
                "WHERE item_type = 'artist' AND score > 0 "
                f"ORDER BY score DESC LIMIT {cfg.TOP_FAV_ARTISTS_COUNT}"
            ).fetchall()
        if gui.list_top is not None and gui.root is not None:
            gui.root.after(0, lambda: _set_top_list(favs))
    except Exception as exc:
        print(f"Top list UI update error: {exc}")


def gui_visuals_updater():
    loc_hist: list = []
    last_track_str = ""
    last_clock = None
    last_seek = None

    # GUI visual updates are application-lifetime work, not Auto DJ work.
    # Local playlists and Hybrid mode must use the exact same adapters.
    while gui.root is not None and getattr(mpv_player, '_watchdog_running', False):
        try:
            elapsed = mpv_player.get_elapsed()
            total = mpv_player.current_duration
            artist = getattr(cfg, 'CURRENT_ARTIST', '?')
            title = getattr(cfg, 'CURRENT_TITLE', '?')
            track_str = f"{artist} - {title}"

            if track_str != last_track_str and artist not in ("", "?"):
                last_track_str = track_str

                if gui.lbl_track is not None and gui.root is not None:
                    gui.root.after(
                        0,
                        lambda a=artist, t=title: gui.lbl_track.configure(text=f"{a} - {t}"),
                    )

                if track_str not in loc_hist:
                    loc_hist.insert(0, track_str)
                        
                    if len(loc_hist) > HISTORY_LIMIT:
                        loc_hist.pop()
                            
                    if gui.hist_box is not None and gui.root is not None:
                        gui.root.after(0, lambda t=track_str: _append_to_history(t))

                cover_video_id = getattr(cfg, 'CURRENT_VIDEO_ID', '') or ''
                cover_artist = artist
                cover_title = title

                def _fetch_cover(vid=cover_video_id, artist_name=cover_artist, title_name=cover_title):
                    expected_track = f"{artist_name} - {title_name}"
                    original_url = None

                    # Prefer the local database. A track may later resolve to a
                    # different YouTube video ID, so fall back to the stable
                    # Artist - Title key before contacting YTMusic.
                    try:
                        with db_connect() as conn:
                            row = None
                            if vid:
                                row = conn.execute(
                                    "SELECT image_blob FROM preferences "
                                    "WHERE video_id = ? AND image_blob IS NOT NULL LIMIT 1",
                                    (vid,),
                                ).fetchone()
                            if not row:
                                row = conn.execute(
                                    "SELECT image_blob FROM preferences "
                                    "WHERE item_name = ? COLLATE NOCASE "
                                    "AND image_blob IS NOT NULL LIMIT 1",
                                    (expected_track,),
                                ).fetchone()

                        if row and row[0]:
                            with Image.open(BytesIO(row[0])) as img:
                                raw_img = img.convert("RGB")
                            blurred = raw_img.copy().filter(
                                ImageFilter.GaussianBlur(radius=COVER_BLUR_RADIUS)
                            )
                            bg_img_source = ImageEnhance.Brightness(blurred).enhance(COVER_BRIGHTNESS)
                            if gui.root is not None:
                                def apply_cached(img=raw_img, bg=bg_img_source, track=expected_track):
                                    if f"{cfg.CURRENT_ARTIST} - {cfg.CURRENT_TITLE}" == track:
                                        _apply_cover_image(img, bg)
                                gui.root.after(0, apply_cached)
                            return
                    except Exception as exc:
                        print(f"⚠ DB Cover Load Error: {exc}")

                    def extract_best_url(thumbnail_data):
                        if not thumbnail_data:
                            return None
                        if isinstance(thumbnail_data, list):
                            try:
                                if all(isinstance(t, dict) and 'width' in t for t in thumbnail_data):
                                    sorted_thumbnails = sorted(thumbnail_data, key=lambda x: x.get('width', 0))
                                    return sorted_thumbnails[-1]['url']
                                elif isinstance(thumbnail_data[-1], dict) and 'url' in thumbnail_data[-1]:
                                    return thumbnail_data[-1]['url']
                                elif isinstance(thumbnail_data[-1], str):
                                    return thumbnail_data[-1]
                            except Exception:
                                pass
                        if isinstance(thumbnail_data, str):
                            return thumbnail_data
                        return None

                    try:
                        if vid:
                            watch_data = ytmusic.get_watch_playlist(videoId=vid)
                            if watch_data.get('tracks'):
                                track = watch_data['tracks'][0]
                                if 'thumbnail' in track:
                                    original_url = extract_best_url(track['thumbnail'])
                                elif 'thumbnails' in track:
                                    original_url = extract_best_url(track['thumbnails'])
                    except Exception as exc:
                        print(f"⚠️ API ID Error: {exc}")

                    try:
                        if not original_url and artist_name and title_name:
                            results = ytmusic.search(f"{artist_name} {title_name}", filter="songs")
                            if results and results[0].get('thumbnails'):
                                original_url = extract_best_url(results[0]['thumbnails'])
                    except Exception as exc:
                        print(f"⚠️ API Search Error: {exc}")

                    if original_url and f"{cfg.CURRENT_ARTIST} - {cfg.CURRENT_TITLE}" == expected_track:
                        high_res_url = original_url
                        if "googleusercontent.com" in original_url:
                            high_res_url = (
                                original_url.split("=")[0] + "=w1000-h1000-l90-rj"
                                if "=" in original_url else original_url + "=w1000-h1000-l90-rj"
                            )
                        elif "i.ytimg.com" in original_url:
                            high_res_url = re.sub(
                                r'/(default|mqdefault|hqdefault|sddefault)\.jpg',
                                '/maxresdefault.jpg', original_url
                            )
                        download_cover(high_res_url, vid, fallback_url=original_url,
                                       expected_track=expected_track)

                start_thread(_fetch_cover)
                start_thread(update_top_list_ui)

            if gui.lbl_time is not None and gui.root is not None:
                c_str = time.strftime('%M:%S', time.gmtime(max(0, elapsed)))
                t_str = (time.strftime('%M:%S', time.gmtime(max(0, total)))
                         if total > 0 else '--:--')
                clock = f'{c_str} / {t_str}'
                if clock != last_clock:
                    last_clock = clock
                    gui.root.after(0, lambda text=clock: gui.lbl_time.configure(text=text))

            if gui.seek_bar is not None and gui.root is not None:
                if total > 0:
                    if time.time() - gui.last_user_interaction > 1.5:
                        seek = (round(elapsed, 1), round(total, 1))
                        if seek != last_seek:
                            last_seek = seek
                            gui.root.after(0, lambda lc=elapsed, tot=total: (
                                gui.seek_bar.configure(from_=0, to=tot), gui.seek_bar.set(lc)))
                elif last_seek != (0, 100):
                    last_seek = (0, 100)
                    gui.root.after(0, lambda: (gui.seek_bar.configure(from_=0, to=100), gui.seek_bar.set(0)))

        except Exception:
            pass

        time.sleep(0.5)


# Removed unused legacy Tk component: _apply_cover_image


def download_cover(url: str, vid: str, fallback_url: str = None,
                   expected_track: str = None):
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if response.status_code != 200 and fallback_url:
            response = requests.get(fallback_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if response.status_code != 200:
            return

        img_bytes = response.content
        with Image.open(BytesIO(img_bytes)) as source:
            raw_img = source.convert("RGB")

        if expected_track and f"{cfg.CURRENT_ARTIST} - {cfg.CURRENT_TITLE}" != expected_track:
            return

        if vid:
            try:
                with db_connect() as conn:
                    old_row = conn.execute(
                        "SELECT image_blob FROM preferences WHERE video_id = ? AND image_blob IS NOT NULL LIMIT 1",
                        (vid,),
                    ).fetchone()
                    should_save = True
                    if old_row and old_row[0]:
                        try:
                            with Image.open(BytesIO(old_row[0])) as older:
                                should_save = min(raw_img.size) > min(older.size)
                        except Exception:
                            pass
                    if should_save:
                        conn.execute(
                            "UPDATE preferences SET image_blob = ? WHERE video_id = ?",
                            (img_bytes, vid),
                        )
            except Exception as exc:
                print(f"⚠️ BLOB Save Error: {exc}")

        blurred = raw_img.copy().filter(ImageFilter.GaussianBlur(radius=COVER_BLUR_RADIUS))
        bg_img_source = ImageEnhance.Brightness(blurred).enhance(COVER_BRIGHTNESS)
        if gui.root is not None:
            def apply_if_current(img=raw_img, bg=bg_img_source, track=expected_track):
                if not track or f"{cfg.CURRENT_ARTIST} - {cfg.CURRENT_TITLE}" == track:
                    _apply_cover_image(img, bg)
            gui.root.after(0, apply_if_current)
    except Exception as exc:
        print(f"Cover download error: {exc}")


_shutdown_lock = threading.Lock()
_shutdown_started = False


def _shutdown_owned_resources(window=None):
    """One shutdown path for Qt close, aboutToQuit and legacy callbacks."""
    global _shutdown_started
    with _shutdown_lock:
        if _shutdown_started:
            return
        _shutdown_started = True

    cfg.RUNNING = False  # Stop producers before closing their resources.
    print('🛑 SpyderMUSIC Engine shutting down…')
    if window is not None:
        for timer_name in ('_spydertrack_timer', '_transport_state_timer'):
            timer = getattr(window, timer_name, None)
            if timer is not None:
                try:
                    timer.stop()
                except Exception as exc:
                    print(f'⚠️ Timer shutdown ({timer_name}): {exc}')
        for name in ('spydertrack_control', 'waveform_rms'):
            resource = getattr(window, name, None)
            if resource is not None:
                try:
                    resource.shutdown()
                except Exception as exc:
                    print(f'⚠️ {name} shutdown: {exc}')
    else:
        resource = getattr(gui, 'spydertrack_control', None)
        if resource is not None:
            try:
                resource.shutdown()
            except Exception as exc:
                print(f'⚠️ SpyderTRACK shutdown: {exc}')
    try:
        mpv_player.kill_all()
    except Exception as exc:
        print(f'⚠️ MPV kill error: {exc}')
    try:
        E.shutdown_chroma_service()
    except Exception as exc:
        print(f'⚠️ ChromaDB shutdown: {exc}')
    try:
        with db_connect() as conn:
            conn.execute('VACUUM')
        print('🧹 Database optimised (VACUUM).')
    except Exception as exc:
        print(f'⚠️ VACUUM failed: {exc}')


def on_closing():
    _shutdown_owned_resources()
    if gui.root is not None:
        gui.root.destroy()


# =============================================================================
# TREEVIEW COLUMN SORTING (numeric and alphabetical)
# =============================================================================
# Removed unused legacy Tk component: make_treeview_sortable


# =============================================================================
# VIBE BRAIN TAB - learned Track x Vibe experience
# =============================================================================
def refresh_vibe_scores_ui(current_track_only: bool | None = None):
    tree = gui.vibe_tree
    if tree is None:
        return
    if current_track_only is not None:
        gui.vibe_current_only = bool(current_track_only)
    current_track_only = getattr(gui, "vibe_current_only", False)
    try:
        for item in tree.get_children():
            tree.delete(item)
        current = f"{cfg.CURRENT_ARTIST} - {cfg.CURRENT_TITLE}".strip(" -")
        with db_connect() as conn:
            if current_track_only and current:
                rows = conn.execute(
                    "SELECT track_key,vibe_key,vibe_score,interactions,last_updated "
                    "FROM vibe_feedback WHERE track_key=? COLLATE NOCASE "
                    "ORDER BY vibe_key COLLATE NOCASE", (current,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT track_key,vibe_key,vibe_score,interactions,last_updated "
                    "FROM vibe_feedback ORDER BY last_updated DESC, track_key COLLATE NOCASE, vibe_key COLLATE NOCASE"
                ).fetchall()
        query = ""
        if getattr(gui, "vibe_search_var", None) is not None:
            query = gui.vibe_search_var.get().strip().casefold()
        visible = 0
        for track, vibe, score, interactions, updated in rows:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(updated)) if updated else "-"
            values = (track, vibe, f"{float(score):.0f}", int(interactions), stamp)
            if query and query not in " ".join(str(v) for v in values).casefold():
                continue
            tree.insert("", "end", values=values)
            visible += 1
        if hasattr(tree, "_apply_sort"):
            tree._apply_sort()
        if gui.vibe_filter_label is not None:
            mode = f"Current track: {current}" if current_track_only and current else "All learned Track × Vibe scores"
            suffix = f"  •  {visible}/{len(rows)} entries" if query else f"  •  {len(rows)} entries"
            gui.vibe_filter_label.configure(text=mode + suffix)
    except Exception as exc:
        log_to_gui(f"⚠️ Vibe score view: {exc}")


def reset_selected_vibe_score():
    tree = getattr(gui, "vibe_tree", None)
    if tree is None:
        return
    selected = tree.selection()
    if not selected:
        return
    reset = []
    now = time.time()
    with db_connect() as conn:
        for item in selected:
            values = tree.item(item, "values")
            if len(values) < 2:
                continue
            track, vibe = values[0], values[1]
            conn.execute(
                "UPDATE vibe_feedback SET vibe_score=50, interactions=0, last_updated=? "
                "WHERE track_key=? COLLATE NOCASE AND vibe_key=? COLLATE NOCASE",
                (now, track, vibe)
            )
            reset.append(f"{track} [{vibe}]")
    refresh_vibe_scores_ui()
    if reset:
        log_to_gui(f"♻ [Vibe Brain] Reset to neutral 50: {', '.join(reset)}")


# Removed unused legacy Tk component: build_vibe_scores_tab


# =============================================================================
# TRAINING TAB
# =============================================================================
# Removed unused legacy Tk component: build_training_tab


# =============================================================================
# UNIVERSAL CONTEXT MENUS (Cut, Copy, Paste, Select All / Copy Only)
# =============================================================================
# Removed unused legacy Tk component: add_context_menu


def _hist_double_click_handler(event):
    if not gui.hist_box.curselection():
        return
    track = gui.hist_box.get(gui.hist_box.curselection()[0])
    start_thread(_enqueue_track_by_name, track)


def _queue_double_click_handler(event):
    if not gui.queue_box.curselection():
        return
    track = gui.queue_box.get(gui.queue_box.curselection()[0])
    start_thread(remove_from_db_queue, track)


def _auto_update_music_libraries():
    """Update the two independent Python packages off the GUI thread.

    An already imported module is intentionally not hot-reloaded. Updates
    take effect on the next SpyderMUSIC launch.
    """
    from music_dependency_updates import update_music_libraries

    def report(message):
        log_to_gui(f"🔄 [Music Libraries] {message}")

    update_music_libraries(report=report)



# =============================================================================
# QT6 FULL MIGRATION – 1:1 CTK VISUAL CLONE & CLEAN MPV SHUTDOWN
# =============================================================================

# Engine callbacks that intentionally target the presentation layer.
# In the former monolith these names lived in one global namespace.
def _bind_engine_gui_callbacks():
    E.update_top_list_ui = update_top_list_ui
    E.update_queue_ui = update_queue_ui
    E.refresh_vibe_scores_ui = refresh_vibe_scores_ui

def start_qt6_full_gui():
    """Full Qt6 front end matching the CTk look & feel 1:1 while keeping REF35 engine intact."""
    _bind_engine_gui_callbacks()
    try:
        from PySide6.QtCore import QEvent, Qt, QTimer, QObject, Signal, QSize, QPoint, QRectF
        from PySide6.QtGui import QPixmap, QImage, QPainter, QColor, QPen, QFont, QIcon, QPainterPath, QBrush, QRegion
        from PySide6.QtWidgets import (
            QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
            QLabel, QPushButton, QLineEdit, QComboBox, QTabWidget, QPlainTextEdit,
            QListWidget, QListWidgetItem, QFrame, QSlider, QCheckBox, QMenu,
            QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
            QScrollArea, QFileDialog, QMessageBox, QDialog, QDialogButtonBox, QRadioButton, QButtonGroup, QInputDialog
        )
    except Exception as exc:
        print("PySide6 is missing. Install it with: pip install PySide6")
        print(f"Import error: {exc}")
        return

    if os.name == 'nt':
        try:
            myappid = 'gozzainc.spydermusic.v3.0'
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

    # ---------- Compatibility layer ----------
    class _Dispatcher(QObject):
        call = Signal(object, int)
        def __init__(self):
            super().__init__()
            self.call.connect(self._run)
        def _run(self, fn, ms):
            QTimer.singleShot(max(0, int(ms)), fn)

    dispatcher = _Dispatcher()

    class _QtLogBridge(QObject):
        main_log = Signal(str)
        ai_log = Signal(str)

    qt_log_bridge = _QtLogBridge()

    class QtRoot:
        def after(self, ms, fn):
            # Queue the callback on Qt's GUI thread; preserve the requested delay.
            dispatcher.call.emit(fn, max(0, int(ms)))
            return id(fn)
        def after_cancel(self, token):
            pass
        def destroy(self):
            cfg.RUNNING = False
            QApplication.quit()
        def clipboard_clear(self):
            QApplication.clipboard().clear()
        def clipboard_append(self, text):
            QApplication.clipboard().setText(str(text))
        def update_idletasks(self):
            QApplication.processEvents()

    class Var:
        def __init__(self, value="", callback=None):
            self.value = value
            self._cb = callback
        def get(self):
            return self.value
        def set(self, value):
            self.value = value
            if self._cb:
                self._cb(value)

    class LabelAdapter:
        def __init__(self, w):
            self.w = w
        def configure(self, **kw):
            if "text" in kw:
                value = str(kw["text"])
                if self.w.text() != value:
                    self.w.setText(value)
            if "state" in kw:
                enabled = kw["state"] != "disabled"
                if self.w.isEnabled() != enabled:
                    self.w.setEnabled(enabled)
            if "text_color" in kw:
                self.w.setStyleSheet(self.w.styleSheet() + f";color:{kw['text_color']};")
            if "fg_color" in kw:
                self.w.setStyleSheet(self.w.styleSheet() + f";background:{kw['fg_color']};")
        config = configure
        def winfo_exists(self):
            return True

    class ButtonAdapter(LabelAdapter):
        pass

    class TextAdapter:
        def __init__(self, w):
            self.w = w
        def insert(self, index, text):
            value = str(text)
            if value.endswith("\n"):
                value = value[:-1]
            if value:
                self.w.appendPlainText(value)
        def see(self, index):
            self.w.ensureCursorVisible()
        def delete(self, a, b=None):
            self.w.clear()
        def configure(self, **kw):
            if "state" in kw:
                self.w.setEnabled(kw["state"] != "disabled")
        def winfo_exists(self):
            return True

    class ListAdapter:
        def __init__(self, w):
            self.w = w
        def delete(self, a, b=None):
            if a == 0 and b == "end":
                self.w.clear()
                return
            if a == "end":
                if self.w.count():
                    self.w.takeItem(self.w.count() - 1)
                return
            try:
                self.w.takeItem(int(a))
            except Exception:
                pass
        def insert(self, index, text):
            item = QListWidgetItem(str(text))
            item.setSizeHint(QSize(0, 18))
            if index == 0:
                self.w.insertItem(0, item)
            else:
                self.w.addItem(item)
        def size(self):
            return self.w.count()
        def curselection(self):
            rows = sorted({i.row() for i in self.w.selectedIndexes()})
            return tuple(rows)
        def get(self, index):
            try:
                return self.w.item(int(index)).text()
            except Exception:
                return ""
        def winfo_exists(self):
            return True

    class SliderAdapter:
        def __init__(self, w):
            self.w = w
        def configure(self, **kw):
            if "from_" in kw:
                self.w.setMinimum(int(float(kw["from_"])))
            if "to" in kw:
                self.w.setMaximum(max(self.w.minimum() + 1, int(float(kw["to"]))))
        def set(self, v):
            # The playback updater deliberately suppresses slider signals.
            # Therefore valueChanged cannot update the floating linear seek.
            # Keep both views in sync explicitly, without emitting seek events.
            value = int(float(v))
            changed = self.w.value() != value
            if changed:
                previous = self.w.blockSignals(True)
                try:
                    self.w.setValue(value)
                finally:
                    self.w.blockSignals(previous)
            for mirror in getattr(self.w, '_floating_linear_mirrors', []):
                if mirror.value() != value:
                    previous = mirror.blockSignals(True)
                    try:
                        mirror.setValue(value)
                    finally:
                        mirror.blockSignals(previous)
            # Avoid a redundant paint on every poll when playback is paused,
            # stopped, or the integer seek position has not advanced.
            if changed:
                for view in getattr(self.w, '_seek_visual_mirrors', []):
                    view.update()

    class EntryAdapter:
        def __init__(self, w):
            self.w = w
        def get(self):
            return self.w.text()
        def delete(self, a, b=None):
            self.w.clear()
        def insert(self, index, text):
            self.w.setText(str(text))
        def configure(self, **kw):
            if "state" in kw:
                self.w.setEnabled(kw["state"] != "disabled")

    class ComboAdapter:
        def __init__(self, w):
            self.w = w
        def get(self):
            return self.w.currentText()
        def set(self, text):
            self.w.setCurrentText(str(text))
        def configure(self, **kw):
            if "values" in kw:
                cur = self.w.currentText()
                self.w.blockSignals(True)
                self.w.clear()
                self.w.addItems([str(x) for x in kw["values"]])
                self.w.setCurrentText(cur)
                self.w.blockSignals(False)
        def __setitem__(self, k, v):
            if k == "values":
                self.configure(values=v)
        def cget(self, k):
            if k == "values":
                return [self.w.itemText(i) for i in range(self.w.count())]
            return None

    class NumericTableItem(QTableWidgetItem):
        """Ensures numeric columns sort by float value instead of ASCII string."""
        def __lt__(self, other):
            try:
                return float(self.text()) < float(other.text())
            except (ValueError, TypeError):
                return super().__lt__(other)

    class TreeAdapter:
        def __init__(self, w, numeric_cols=()):
            self.w = w
            self.numeric_cols = set(numeric_cols)
        def get_children(self):
            self.w.setSortingEnabled(False)
            return list(range(self.w.rowCount() - 1, -1, -1))
        def delete(self, item):
            try:
                self.w.removeRow(int(item))
            except Exception:
                pass
        def insert(self, parent, index, values=()):
            was_sorting = self.w.isSortingEnabled()
            if was_sorting:
                self.w.setSortingEnabled(False)
            r = self.w.rowCount()
            self.w.insertRow(r)
            for c, v in enumerate(values):
                item = NumericTableItem(str(v)) if c in self.numeric_cols else QTableWidgetItem(str(v))
                if c in self.numeric_cols or c == 4:
                    item.setTextAlignment(Qt.AlignCenter)
                self.w.setItem(r, c, item)
            if was_sorting:
                self.w.setSortingEnabled(True)
            return r
        def selection(self):
            return sorted({i.row() for i in self.w.selectedIndexes()})
        def item(self, item, what=None):
            r = int(item)
            vals = tuple(
                self.w.item(r, c).text() if self.w.item(r, c) else ""
                for c in range(self.w.columnCount())
            )
            return vals if what == "values" else {"values": vals}
        def _apply_sort(self):
            self.w.setSortingEnabled(True)

    class QtVU(QWidget):
        """1:1 match to CTk StereoVUMeter including 0.65s peak hold."""
        def __init__(self, deck, accent):
            super().__init__()
            self.deck = deck
            self.accent = accent
            self.dl = self.dr = self.pl = self.pr = -90.0
            self.hl = self.hr = 0.0
            self.setMinimumHeight(18)
            self.setMinimumWidth(8)
            self.presentation = 'classic'
            self.vu_rotation = 0
            self.vu_flip_horizontal = False
            self.vu_flip_vertical = False
            self._glow_l = [0.0] * 38
            self._glow_r = [0.0] * 38
            self._glow_last = time.monotonic()
            # Cache the 38 fixed gradient colors instead of allocating them
            # twice per frame (once for each channel).
            self._classic_segment_colors = tuple(self._color((i + 1) / 38) for i in range(38))
            self._classic_off_color = QColor("#202020")
            self._classic_bg_color = QColor("#080808")
            self._classic_label_color = QColor("#B8B8B8")
            self._classic_peak_pen = QPen(QColor("#F2F2F2"), 1)

        def set_presentation(self, style):
            if style not in ('classic', 'phosphor', 'phosphor_mono'):
                raise ValueError('Unknown VU presentation')
            self.presentation = style
            self.update()

        def set_transform(self, rotation=0, flip_horizontal=False, flip_vertical=False):
            rotation = int(rotation)
            if rotation not in (0, 90, 180, 270):
                raise ValueError('VU rotation must be 0, 90, 180 or 270')
            self.vu_rotation = rotation
            self.vu_flip_horizontal = bool(flip_horizontal)
            self.vu_flip_vertical = bool(flip_vertical)
            self.update()

        def _glow_values(self, level, previous, dt):
            # Fast attack, slow decay; spatial alpha halo ahead of the signal.
            count = len(previous)
            position = self._norm(level) * count
            attack = min(1.0, dt / 0.035)
            release = min(1.0, dt / 0.22)
            result = []
            for i, old in enumerate(previous):
                distance = max(0.0, i - position)
                target = 1.0 if i < position else 0.5 ** (distance + 1.0)
                target = max(0.0, min(1.0, target))
                blend = attack if target >= old else release
                result.append(old + (target - old) * blend)
            return result

        def _phosphor_geometry(self):
            """Determine segment count from the visible long axis, never from
            a fixed 38-cell source or a pre-rotation reference dimension.

            The last cell ends exactly at the drawable boundary; all cells
            share any fractional remainder evenly.
            """
            mono = self.presentation == 'phosphor_mono'
            rotated = self.vu_rotation in (90, 270)
            # The painter swaps its logical axes for quarter turns. Its
            # horizontal axis is the actual height of a vertical meter.
            length = float(self.height() if rotated else self.width())
            left = 1.0 if mono else 22.0
            right = 1.0 if mono else 5.0
            available = max(1.0, length - left - right)
            gap = 1.0 if mono else 2.0
            # Deliberately independent of widget thickness: resizing only
            # the length must always add or remove segments.
            preferred_length = 19.0 if mono else 16.0
            count = max(1, int((available + gap) / (preferred_length + gap) + 0.5))
            gap = min(gap, available / max(1, count * 2))
            segment_length = max(0.01, (available - (count - 1) * gap) / count)
            return left, gap, count, segment_length

        @staticmethod
        def _resample_glow(values, count):
            """Preserve the spatial afterglow when resizing changes segment count."""
            if len(values) == count:
                return values
            if not values:
                return [0.0] * count
            old_count = len(values)
            result = []
            for i in range(count):
                pos = ((i + 0.5) / count) * old_count - 0.5
                lo = max(0, min(old_count - 1, int(pos // 1)))
                hi = min(old_count - 1, lo + 1)
                fraction = max(0.0, min(1.0, pos - lo))
                result.append(values[lo] * (1 - fraction) + values[hi] * fraction)
            return result

        def update_levels(self, l, r):
            now = time.time()
            for side, val in (("l", l), ("r", r)):
                d_attr, p_attr, h_attr = "d" + side, "p" + side, "h" + side
                old = getattr(self, d_attr)
                shown = val if val >= old else max(val, old - 2.2)
                setattr(self, d_attr, shown)
                if val >= getattr(self, p_attr) or now >= getattr(self, h_attr):
                    setattr(self, p_attr, val)
                    setattr(self, h_attr, now + 0.65)
                elif now > getattr(self, h_attr) - 0.25:
                    setattr(self, p_attr, max(val, getattr(self, p_attr) - 1.5))
            now_mono = time.monotonic()
            dt = max(0.0, min(0.1, now_mono - self._glow_last))
            self._glow_last = now_mono
            _, _, segment_count, _ = self._phosphor_geometry()
            self._glow_l = self._glow_values(l, self._resample_glow(self._glow_l, segment_count), dt)
            self._glow_r = self._glow_values(r, self._resample_glow(self._glow_r, segment_count), dt)
            self.update()
            # Audio-reactive effects are sampled by the GUI's own Qt timer.
            # Do not depend on widget parent/window lookup here.

        @staticmethod
        def _norm(db):
            return max(0.0, min(1.0, (db + 60.0) / 60.0))

        @staticmethod
        def _color(t):
            stops = ((0.0, (51, 255, 102)), (0.55, (255, 224, 0)), (0.78, (255, 138, 0)), (1.0, (249, 6, 27)))
            for (a, ca), (b, cb) in zip(stops, stops[1:]):
                if t <= b:
                    f = (t - a) / (b - a)
                    return QColor(*[round(ca[i] + (cb[i] - ca[i]) * f) for i in range(3)])
            return QColor(249, 6, 27)

        def paintEvent(self, e):
            self._render_vu()

        def _render_vu(self):
            p = QPainter(self)
            # Transform the whole VU drawing in logical coordinates. A quarter
            # turn swaps the logical width/height without resizing the widget.
            quarter_turn = self.vu_rotation in (90, 270)
            logical_w = self.height() if quarter_turn else self.width()
            logical_h = self.width() if quarter_turn else self.height()
            p.translate(self.width() / 2.0, self.height() / 2.0)
            p.rotate(self.vu_rotation)
            p.scale(-1 if self.vu_flip_horizontal else 1,
                    -1 if self.vu_flip_vertical else 1)
            p.translate(-logical_w / 2.0, -logical_h / 2.0)
            self._vu_paint_width = logical_w
            self._vu_paint_height = logical_h
            if self.presentation in ('phosphor', 'phosphor_mono'):
                self._paint_phosphor(p)
                p.end()
                return
            p.fillRect(0, 0, logical_w, logical_h, self._classic_bg_color)
            label_w, right_pad = 28, 5
            p.setFont(QFont("Consolas", 13, QFont.Bold))
            p.setPen(QColor(self.accent))
            p.drawText(5, 23, self.deck)
            p.setFont(QFont("Consolas", 8, QFont.Bold))
            x0, x1 = label_w, logical_w - right_pad
            segs, gap = 38, 2
            sw = max(2.0, (x1 - x0 - gap * (segs - 1)) / segs)
            rows = (("L", self.dl, self.pl, 4, 15), ("R", self.dr, self.pr, 19, 30))
            for lab, level, peak, y0, y1 in rows:
                p.setPen(self._classic_label_color)
                p.drawText(label_w - 10, y1 - 2, lab)
                active = int(round(self._norm(level) * segs))
                peak_idx = max(0, min(segs - 1, int(self._norm(peak) * segs) - 1))
                for i in range(segs):
                    sx = x0 + i * (sw + gap)
                    p.fillRect(int(sx), y0, int(sw), y1 - y0,
                               self._classic_segment_colors[i] if i < active else self._classic_off_color)
                if peak > -60.0:
                    sx = x0 + peak_idx * (sw + gap)
                    p.setPen(self._classic_peak_pen)
                    p.drawRect(int(sx), y0, int(sw), y1 - y0)
            p.end()

        def _paint_phosphor(self, p):
            # Draw in a local coordinate system so the editor may resize the widget.
            p.setRenderHint(QPainter.Antialiasing, True)
            p.fillRect(0, 0, self._vu_paint_width, self._vu_paint_height, QColor('#080e0b'))
            mono = self.presentation == 'phosphor_mono'
            left, gap, count, segment_w = self._phosphor_geometry()
            # Also handle a resize before the next incoming audio sample.
            self._glow_l = self._resample_glow(self._glow_l, count)
            self._glow_r = self._resample_glow(self._glow_r, count)
            row_h = max(1.0, self._vu_paint_height - 2) if mono else max(3.0, (self._vu_paint_height - 7) / 2.0)
            p.setFont(QFont('Consolas', 8, QFont.Bold))
            mono_values = [max(a, b) for a, b in zip(self._glow_l, self._glow_r)]
            rows = (('', mono_values),) if mono else (('L', self._glow_l), ('R', self._glow_r))
            for row, (name, values) in enumerate(rows):
                y = 1 if mono else 2 + row * (row_h + 3)
                if not mono:
                    p.setPen(QColor('#a4c9b0'))
                    p.drawText(4, int(y + row_h * .75), name)
                for i, alpha in enumerate(values):
                    x = left + i * (segment_w + gap)
                    # The same green phosphor color at every level: only alpha changes.
                    core = QColor(120, 255, 155, int(235 * alpha))
                    halo = QColor(36, 255, 110, int(80 * alpha))
                    p.fillRect(QRectF(x, y, segment_w, row_h), halo)
                    inset = max(1, int(row_h * .18))
                    if row_h > 5:
                        p.fillRect(QRectF(x, y + inset, segment_w,
                                          max(0.1, row_h - 2 * inset)), core)

    class ThresholdMarkers(QWidget):
        """Draws the red skip and green win markers underneath the seek bar."""
        def __init__(self, slider, parent=None):
            super().__init__(parent)
            self.slider = slider
            self.setFixedHeight(14)
            self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        def paintEvent(self, event):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, False)
            half_handle = 8
            usable = max(1, self.width() - 2 * half_handle)
            for percent, color in (
                (cfg.PENALTY_THRESHOLD_PERCENT, "#ff4444"),
                (cfg.SCORE_THRESHOLD_PERCENT, "#33FF66"),
            ):
                try:
                    fraction = max(0.0, min(1.0, float(percent) / 100.0))
                except (TypeError, ValueError):
                    continue
                x = round(half_handle + fraction * usable)
                p.setPen(QPen(QColor(color), 2))
                p.drawLine(x, 0, x, 8)

    class CircularSeek(QWidget):
        """Interactive radial representation of the existing seek slider.

        Uses the original slider's range and engine callbacks, no duplicate player logic.
        """
        def __init__(self, slider, seek_callback, interaction_callback, parent=None):
            super().__init__(parent)
            self.slider = slider
            self.seek_callback = seek_callback
            self.interaction_callback = interaction_callback
            self._dragging = False
            self._drag_value = None
            # Percentage is a separate mouse-transparent sibling, because a
            # ring mask would otherwise clip text painted inside the hole.
            self._centre_label = QLabel(parent)
            self._centre_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self._centre_label.setAttribute(Qt.WA_TranslucentBackground, True)
            self._centre_label.setAlignment(Qt.AlignCenter)
            self._centre_label.setStyleSheet('background: transparent; color: #f5f5f5;')
            self._centre_label.hide()
            self.setToolTip("Circular seek: drag or click the ring to seek")
            self.setMinimumSize(0, 0)
            slider.valueChanged.connect(self._refresh_centre)
            slider.rangeChanged.connect(lambda _a, _b: self._refresh_centre)
            self._refresh_centre()

        def _refresh_centre(self, *_args):
            minimum, maximum = self.slider.minimum(), self.slider.maximum()
            value = self._drag_value if self._drag_value is not None else self.slider.value()
            fraction = max(0., min(1., (value-minimum) / max(1, maximum-minimum)))
            percent = f'{round(fraction * 100)}%'
            if self._centre_label.text() != percent:
                self._centre_label.setText(percent)
            side = max(1, min(self.width(), self.height()))
            font_size = max(9, min(17, int(side / 8)))
            if getattr(self, '_centre_font_size', None) != font_size:
                self._centre_font_size = font_size
                self._centre_label.setFont(QFont('Consolas', font_size, QFont.Bold))
            # The ring must still track fractional progress even if its integer
            # percentage label did not change.
            self.update()

        def _sync_centre_geometry(self):
            self._centre_label.setGeometry(self.geometry())
            self._centre_label.raise_()

        def moveEvent(self, event):
            super().moveEvent(event)
            self._sync_centre_geometry()

        def showEvent(self, event):
            super().showEvent(event)
            self._sync_centre_geometry()
            self._centre_label.show()

        def hideEvent(self, event):
            self._centre_label.hide()
            super().hideEvent(event)

        def deleteLater(self):
            self._centre_label.deleteLater()
            super().deleteLater()

        def _geometry(self):
            # The arc now occupies the entire square widget instead of sitting
            # inside an unnecessarily large invisible margin.
            side = max(1, min(self.width(), self.height()))
            return ((self.width() - side) / 2, (self.height() - side) / 2, side)

        def _ring_dimensions(self):
            x, y, side = self._geometry()
            pen_width = max(5.0, min(14.0, side * 0.075))
            inset = max(3.0, pen_width / 2 + 1.0)
            radius = max(0.5, side / 2 - inset)
            tolerance = max(5.0, pen_width / 2 + 4.0)
            return x, y, side, inset, radius, pen_width, tolerance

        def _update_ring_mask(self):
            # A genuine donut-shaped widget region: clicks in the middle and
            # outside the ring reach lower layers, regardless of z-index.
            x, y, side, inset, radius, pen_width, tolerance = self._ring_dimensions()
            cx, cy = self.width() / 2, self.height() / 2
            outer = radius + tolerance
            inner = max(0.0, radius - tolerance)
            from PySide6.QtCore import QRect
            def ellipse(r):
                return QRegion(QRect(round(cx-r), round(cy-r),
                                     max(1, round(2*r)), max(1, round(2*r))),
                               QRegion.Ellipse)
            self.setMask(ellipse(outer).subtracted(ellipse(inner)))

        def resizeEvent(self, event):
            super().resizeEvent(event)
            self._update_ring_mask()
            self._sync_centre_geometry()
            self._refresh_centre()

        def paintEvent(self, event):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            x, y, side, inset, radius, pen_width, tolerance = self._ring_dimensions()
            r = QRectF(x + inset, y + inset,
                       max(1, side - 2 * inset), max(1, side - 2 * inset))
            p.setPen(QPen(QColor(getattr(self, 'remaining_color', '#303036')), pen_width))
            p.drawArc(r, 0, 360 * 16)
            minimum, maximum = self.slider.minimum(), self.slider.maximum()
            value = self._drag_value if self._drag_value is not None else self.slider.value()
            fraction = max(0., min(1., (value-minimum) / max(1, maximum-minimum)))
            p.setPen(QPen(QColor(getattr(self, 'played_color', '#F9061B')), pen_width))
            p.drawArc(r, 90 * 16, -round(fraction * 360 * 16))
            # The centre is intentionally click-through, so its percentage
            # label is drawn on a separate transparent painter layer below.
            p.end()

        def _is_on_ring(self, pos):
            import math
            x, y, side, inset, radius, pen_width, tolerance = self._ring_dimensions()
            cx, cy = self.width() / 2, self.height() / 2
            return abs(math.hypot(pos.x() - cx, pos.y() - cy) - radius) <= tolerance

        def _position_to_value(self, pos):
            import math
            cx, cy = self.width()/2, self.height()/2
            angle = math.degrees(math.atan2(pos.y()-cy, pos.x()-cx))
            # Clockwise, starting at twelve o'clock.
            fraction = ((angle + 90) % 360) / 360
            return round(self.slider.minimum() + fraction * (self.slider.maximum()-self.slider.minimum()))

        def mousePressEvent(self, event):
            if event.button() != Qt.LeftButton:
                return super().mousePressEvent(event)
            if not self._is_on_ring(event.position()):
                event.ignore()
                return
            self._dragging = True
            self.interaction_callback()
            self._drag_value = self._position_to_value(event.position())
            self._refresh_centre()
            event.accept()

        def mouseMoveEvent(self, event):
            if self._dragging:
                self._drag_value = self._position_to_value(event.position())
                self.interaction_callback()
                self._refresh_centre()
                event.accept()
            else:
                event.ignore()

        def mouseReleaseEvent(self, event):
            if self._dragging and event.button() == Qt.LeftButton:
                value = self._position_to_value(event.position())
                self._dragging = False
                self._drag_value = None
                self.slider.setValue(value)
                self.seek_callback(value)
                self.update()
                event.accept()
            else:
                super().mouseReleaseEvent(event)

    class RoundedCoverLabel(QLabel):
        """Cover surface that fills its panel and clips the artwork to rounded corners."""
        def __init__(self, text="", parent=None, radius=9):
            super().__init__(text, parent)
            self._fallback_text = str(text)
            self._cover_pixmap = None
            self._radius = radius
            self._cover_mask = 'rounded'
            self._spin_rpm = 0.0
            self._spin_when = 'playing'
            self._spin_angle = 0.0
            self._spin_last = time.monotonic()
            self._spin_timer = QTimer(self)
            self._spin_timer.setInterval(33)
            self._spin_timer.timeout.connect(self._advance_spin)
            self.setAlignment(Qt.AlignCenter)
            self.setMinimumSize(0, 0)

        def setText(self, text):
            self._fallback_text = str(text)
            QLabel.setText(self, str(text))

        def set_cover_presentation(self, config):
            self._cover_mask = config.get('mask', 'rounded')
            self._radius = config.get('radius', 9)
            self.update()

        def set_cover_animation(self, config):
            self._spin_rpm = float(config.get('rpm', 33.333)) if config.get('type') == 'spin' else 0.0
            self._spin_when = config.get('when', 'playing')
            self._spin_last = time.monotonic()
            if self._spin_rpm > 0 and self._cover_mask == 'vinyl':
                self._spin_timer.start()
            else:
                self._spin_timer.stop()
                self._spin_angle = 0.0
            self.update()

        def _advance_spin(self):
            now = time.monotonic()
            elapsed = min(0.2, max(0.0, now - self._spin_last))
            self._spin_last = now
            if self._spin_when == 'playing':
                try:
                    active = mpv_player.is_playing() and not mpv_player.is_paused
                except Exception:
                    active = False
                if not active:
                    return
            self._spin_angle = (self._spin_angle + elapsed * self._spin_rpm * 6.0) % 360.0
            self.update()

        def setPixmap(self, pixmap):
            # Keep the original artwork. Scaling/cropping is done at paint time so
            # resizing never creates a smaller square inside the cover panel.
            self._cover_pixmap = QPixmap(pixmap) if pixmap is not None else None
            self.setText("")
            self.update()

        def paintEvent(self, event):
            if not self._cover_pixmap or self._cover_pixmap.isNull():
                return QLabel.paintEvent(self, event)
            self._render_cover()

        def _render_cover(self):
            if not self._cover_pixmap or self._cover_pixmap.isNull():
                # Preserve the original QLabel fallback text.
                if self._fallback_text:
                    painter = QPainter(self)
                    painter.setPen(self.palette().windowText().color())
                    painter.drawText(self.rect(), Qt.AlignCenter, self._fallback_text)
                    painter.end()
                return
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

            target = self.rect()
            if self._cover_mask == 'vinyl':
                # The artwork is drawn only as a center label; the vinyl surface
                # and grooves are painted separately, without changing source art.
                side = min(target.width(), target.height())
                if side <= 0:
                    painter.end()
                    return
                painter.translate(target.center())
                painter.rotate(self._spin_angle)
                radius = side * 0.49
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor('#101014'))
                painter.drawEllipse(QRectF(-radius, -radius, 2*radius, 2*radius))
                painter.setBrush(Qt.NoBrush)
                for ratio in (0.68, 0.73, 0.78, 0.83, 0.88, 0.93, 0.97):
                    rr = radius * ratio
                    painter.setPen(QPen(QColor(52, 52, 60, 170), max(0.6, side/500)))
                    painter.drawEllipse(QRectF(-rr, -rr, rr*2, rr*2))
                label_radius = radius * 0.42
                painter.setPen(Qt.NoPen)
                clip = QPainterPath()
                clip.addEllipse(QRectF(-label_radius, -label_radius, 2*label_radius, 2*label_radius))
                painter.setClipPath(clip)
                label_square = QRectF(-label_radius, -label_radius, 2*label_radius, 2*label_radius)
                paint_source_cover(painter, label_square, self._cover_pixmap)
                painter.setClipping(False)
                painter.setBrush(QColor('#b6b6b8'))
                hole = max(1.5, radius*0.022)
                painter.drawEllipse(QRectF(-hole, -hole, 2*hole, 2*hole))
                painter.end()
                return
            path = QPainterPath()
            if self._cover_mask == 'circle':
                path.addEllipse(target)
            elif self._cover_mask == 'hexagon':
                from PySide6.QtCore import QPointF
                from PySide6.QtGui import QPolygonF
                x, y, w, h = target.x(), target.y(), target.width(), target.height()
                points = [(0.25, 0), (0.75, 0), (1, 0.5),
                          (0.75, 1), (0.25, 1), (0, 0.5)]
                path.addPolygon(QPolygonF([QPointF(x + u*w, y + v*h) for u, v in points]))
                path.closeSubpath()
            elif self._cover_mask == 'square':
                path.addRect(target)
            else:
                path.addRoundedRect(target, self._radius, self._radius)
            painter.setClipPath(path)

            # CSS background-size: cover + background-position: center center.
            # The image fills the complete cover surface; excess is cropped equally
            # on opposing sides instead of leaving an inner gap.
            paint_source_cover(painter, target, self._cover_pixmap)
            painter.end()

    class CoverBackdrop(QOpenGLWidget):
        """Paints the blurred cover across the ENTIRE window just like CTk bg_canvas."""
        def __init__(self):
            super().__init__()
            self._background = None
            self._editor_background = {'mode':'album'}
            self._editor_pixmap = None

        def set_editor_background(self, config):
            self._editor_background = config
            self._editor_pixmap = QPixmap(config.get('path','')) if config.get('mode') == 'image' else None
            self.update()

        def set_background(self, img):
            if img is None:
                return
            rgb = img.convert("RGB")
            qimg = QImage(
                rgb.tobytes("raw", "RGB"), rgb.width, rgb.height,
                rgb.width * 3, QImage.Format_RGB888
            ).copy()
            self._background = QPixmap.fromImage(qimg)
            self.update()

        def paintGL(self):
            began = time.perf_counter()
            try:
                manager = getattr(self, 'theme_manager', None)
                # Pre-render GPU effect masks BEFORE opening the backdrop
                # QPainter.  Nested QPainters on the same GL context would
                # corrupt Qt's active paint engine state.
                if manager is not None and manager._gpu_editor_fx_enabled:
                    effect_widgets = [w for w in manager._gpu_composited
                                      if (w.spec.get('editor_effect') or {}).get('enabled', False)
                                      and w.isVisible()]
                    if effect_widgets:
                        try:
                            from gpu_editor_effects import GPUVisualEffectRenderer
                            renderer = getattr(self, '_gpu_editor_effect_renderer', None)
                            if renderer is None or renderer.context != self.context():
                                renderer = GPUVisualEffectRenderer(self)
                                self._gpu_editor_effect_renderer = renderer
                            renderer.prepare(self, effect_widgets)
                        except Exception as exc:
                            self._gpu_editor_effect_renderer = None
                            manager._disable_gpu_editor_fx(exc)
                    else:
                        renderer = getattr(self, '_gpu_editor_effect_renderer', None)
                        if renderer is not None:
                            renderer.prepare(self, ())  # prune unused FBOs
                clear_gpu_surface(self)
                self._render_backdrop()
            finally:
                owner = self.window()
                monitor = getattr(owner, '_gui_frame_budget', None)
                if monitor is not None:
                    monitor.note_paint(time.perf_counter() - began)

        def _render_backdrop(self):
            painter = QPainter(self)
            cfg = self._editor_background
            painter.fillRect(self.rect(), QColor(cfg.get('color','#1a1a1a')))
            bg = self._editor_pixmap if cfg.get('mode') == 'image' else (self._background if cfg.get('mode') == 'album' else None)
            if bg and not bg.isNull():
                # Fill the complete backdrop while keeping the cover aspect ratio.
                # Crop symmetrically around the image centre instead of using the
                # old fixed vertical offset (-205 px). This keeps the artwork
                # vertically and horizontally centred at every window size.
                paint_source_cover(painter, self.rect(), bg)
            manager = getattr(self, 'theme_manager', None)
            if manager is not None and manager.full_canvas:
                from theme_api import make_audio_gradient
                from PySide6.QtGui import QLinearGradient, QRadialGradient, QPainterPath, QPen
                from PySide6.QtCore import QRectF
                bounds = QRectF(self.rect())
                painter.setRenderHint(QPainter.Antialiasing, True)
                for config in manager.color_gradients:
                    if config['direction'] == 'vertical':
                        gradient = QLinearGradient(0, self.height(), 0, 0) if config['start'] == 'bottom' else QLinearGradient(0, 0, 0, self.height())
                    else:
                        gradient = QLinearGradient(self.width(), 0, 0, 0) if config['start'] == 'right' else QLinearGradient(0, 0, self.width(), 0)
                    for stop in config['stops']:
                        r,g,b,a = stop['color']
                        gradient.setColorAt(stop['percent']/100.0, QColor.fromRgbF(r/255, g/255, b/255, a))
                    painter.fillRect(bounds, gradient)
                config = manager.audio_reactive
                if config.get('enabled'):
                    energy = manager.audio_frame.get(config.get('band', 'level'), manager.audio_energy)
                    if energy > 0.002:
                        if config.get('gradient'):
                            gradient = make_audio_gradient(config['gradient'], bounds, energy, config.get('intensity', 120)/120.0)
                        else:
                            gradient = QRadialGradient(bounds.center(), max(self.width(),self.height())*.67)
                            color = QColor(config.get('color', '#ff1744'))
                            color.setAlpha(round(min(235, energy*config.get('intensity',120)*1.3)))
                            gradient.setColorAt(0, color)
                            color.setAlpha(0)
                            gradient.setColorAt(1, color)
                        painter.fillRect(bounds, gradient)
            # Shared GPU compositor: all eligible image/FFT/wave/VU visual
            # layers are drawn in this single GL pass, under native widgets.
            if manager is not None:
                manager.draw_gpu_compositor(painter, self)
            painter.end()

    class QueueList(QListWidget):
        def dropEvent(self, event):
            if event.source() is not self:
                event.ignore()
                return
            super().dropEvent(event)
            if event.isAccepted() and callable(getattr(self, "persist_order", None)):
                self.persist_order()

        def keyPressEvent(self, event):
            if event.key() == Qt.Key_Delete:
                if callable(getattr(self, "delete_selected", None)):
                    self.delete_selected()
                event.accept()
                return
            super().keyPressEvent(event)

    # Windows native sizing: constrain the CLIENT AREA to the theme's authored
    # canvas aspect ratio. The individual widgets retain their existing scaling.
    if os.name == 'nt':
        from ctypes import wintypes as _win_types

        # Explicit Win32 MSG layout: ctypes.wintypes.MSG is not consistently
        # available/complete across Python environments.
        class _NativeMsg(ctypes.Structure):
            _fields_ = [('hwnd', _win_types.HWND),
                        ('message', _win_types.UINT),
                        ('wParam', _win_types.WPARAM),
                        ('lParam', _win_types.LPARAM),
                        ('time', _win_types.DWORD),
                        ('pt', _win_types.POINT)]

        class _SizingRect(ctypes.Structure):
            _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                        ('right', ctypes.c_long), ('bottom', ctypes.c_long)]

        _get_client_rect = ctypes.windll.user32.GetClientRect
        _get_client_rect.argtypes = [_win_types.HWND, ctypes.POINTER(_SizingRect)]
        _get_client_rect.restype = _win_types.BOOL
        _get_window_rect = ctypes.windll.user32.GetWindowRect
        _get_window_rect.argtypes = [_win_types.HWND, ctypes.POINTER(_SizingRect)]
        _get_window_rect.restype = _win_types.BOOL

    class Window(QMainWindow):
        def nativeEvent(self, event_type, message):
            # WM_SIZING (0x0214): change the proposed outer rectangle *before*
            # Windows paints it. This avoids recursive resizeEvent/setGeometry.
            if os.name != 'nt' or bytes(event_type) not in (b'windows_generic_MSG',
                                                            b'windows_dispatcher_MSG'):
                return super().nativeEvent(event_type, message)
            try:
                msg = _NativeMsg.from_address(int(message))
                if msg.message != 0x0214 or not msg.lParam:
                    return super().nativeEvent(event_type, message)

                ref = getattr(getattr(self, 'theme_manager', None),
                              'reference_canvas', None)
                if ref and ref[0] > 0 and ref[1] > 0:
                    ratio = ref[0] / ref[1]
                else:
                    # Fallback for themes without a reference_canvas entry.
                    ratio = getattr(self, '_initial_client_ratio', None)
                    if ratio is None:
                        ratio = max(1, self.width()) / max(1, self.height())
                        self._initial_client_ratio = ratio

                hwnd = msg.hwnd
                client = _SizingRect()
                outer = _SizingRect()
                if not (_get_client_rect(hwnd, ctypes.byref(client)) and
                        _get_window_rect(hwnd, ctypes.byref(outer))):
                    return super().nativeEvent(event_type, message)
                # Non-client width/height include titlebar, borders and shadows.
                extra_w = (outer.right - outer.left) - (client.right - client.left)
                extra_h = (outer.bottom - outer.top) - (client.bottom - client.top)
                rect = ctypes.cast(msg.lParam, ctypes.POINTER(_SizingRect)).contents
                proposed_w = max(1, rect.right - rect.left - extra_w)
                proposed_h = max(1, rect.bottom - rect.top - extra_h)
                edge = int(msg.wParam)  # WMSZ_LEFT=1 ... WMSZ_BOTTOMRIGHT=8
                if edge in (1, 2):  # Dragging a vertical edge: width drives height.
                    desired_w = proposed_w
                    desired_h = max(1, round(desired_w / ratio))
                elif edge in (3, 6):  # Dragging a horizontal edge: height drives width.
                    desired_h = proposed_h
                    desired_w = max(1, round(desired_h * ratio))
                else:  # Corner: use the dimension with the largest relative change.
                    old_w = max(1, client.right - client.left)
                    old_h = max(1, client.bottom - client.top)
                    if abs(proposed_w / old_w - 1) >= abs(proposed_h / old_h - 1):
                        desired_w = proposed_w
                        desired_h = max(1, round(desired_w / ratio))
                    else:
                        desired_h = proposed_h
                        desired_w = max(1, round(desired_h * ratio))

                outer_w, outer_h = desired_w + extra_w, desired_h + extra_h
                # Keep the dragged edge/corner under the mouse; opposite edge fixed.
                if edge in (1, 4, 7):
                    rect.left = rect.right - outer_w
                else:
                    rect.right = rect.left + outer_w
                if edge in (3, 4, 5):
                    rect.top = rect.bottom - outer_h
                else:
                    rect.bottom = rect.top + outer_h
                return True, 0
            except Exception as exc:
                print(f"WM_SIZING aspect lock error: {exc!r}")
                return super().nativeEvent(event_type, message)

        def __init__(self):
            super().__init__()
            self.setWindowTitle("SpyderMUSIC Control Center (ChromaDB + Last.fm)")
            self.resize(1298, 888)
            self.setMinimumSize(1060, 830)

            try:
                from assets import Icon_Image
                temp_ico_path = os.path.join(DATA_DIR, "icon.ico")
                if not os.path.exists(temp_ico_path):
                    with open(temp_ico_path, "wb") as f:
                        f.write(base64.b64decode(Icon_Image))
                self.setWindowIcon(QIcon(temp_ico_path))
            except Exception:
                pass

            # Full-window blurred cover backdrop
            self.backdrop = CoverBackdrop()
            self.setCentralWidget(self.backdrop)
            
            # The canvas is now the absolute root over the backdrop
            canvas_layout = QVBoxLayout(self.backdrop)
            canvas_layout.setContentsMargins(0, 0, 0, 0)
            self.canvas = ThemeCanvas(self.backdrop)
            canvas_layout.addWidget(self.canvas)
            
            # SYSTEM_TAB_FULL_CANVAS_V1
            self.root_layout = QVBoxLayout(self.canvas)
            self.root_layout.setContentsMargins(0, 0, 0, 0)
            self.root_layout.setSpacing(0)

            self.grid = QGridLayout()
            self.grid.setContentsMargins(0, 0, 0, 0)
            self.grid.setSpacing(10)
            
            # Setup theme manager early to register components
            self.theme_manager = ThemeManager(self, self.grid, THEME_DIR, self.canvas)
            self.backdrop.theme_manager = self.theme_manager

            # Tabs are now a child of the canvas inside the root layout
            self.tabs = QTabWidget(self.canvas)
            self.tabs.setObjectName("main_tabs")
            self.root_layout.addWidget(self.tabs, 1)

            self.live = QWidget() 
            self.expert = QWidget()
            self.training = QWidget()
            self.vibes = QWidget()
            self.banned = QWidget()
            self.about = QWidget()
            for w, n in (
                (self.live, "📡 Live Cockpit"),
                (self.expert, "⚙ Expert Settings"),
                (self.training, "🧠 AI Training"),
                (self.vibes, "🎚 Vibe Brain"),
                (self.banned, "🚫 Banned"),
                (self.about, "🎸 About"),
            ):
                self.tabs.addTab(w, n)

            # Hook tab changes to show/hide the absolute layout music components
            self.tabs.currentChanged.connect(self._on_tab_changed)

            self.raw_cover_pixmap = None
            self.setup_theme_menu()
            self.build_live()
            # Editor chrome is deliberately independent of the active theme's QSS.
            self._theme_editor_toolbar = QWidget(self.backdrop)
            self._theme_editor_toolbar.setObjectName('fixedThemeEditorToolbar')
            self._theme_editor_toolbar.setStyleSheet("""
                QWidget#fixedThemeEditorToolbar {
                    background: #20252c; border: 1px solid #00f5ff;
                    border-radius: 5px;
                }
                QPushButton#fixedThemeEditorAction {
                    background: #273441; color: #f5f7fa;
                    border: 1px solid #54788a; border-radius: 4px;
                    padding: 0px; font-family: 'Segoe UI';
                    font-size: 11px; font-weight: 600;
                }
                QPushButton#fixedThemeEditorAction:hover { background: #365367; border-color: #00f5ff; }
                QPushButton#fixedThemeEditorAction:pressed { background: #174457; }
            """)
            edit_bar = QHBoxLayout(self._theme_editor_toolbar)
            edit_bar.setContentsMargins(5, 4, 5, 4)
            edit_bar.setSpacing(4)
            for title, callback, width in [
                ('↶ Undo', lambda: self.theme_editor.undo(), 70),
                ('↷ Redo', lambda: self.theme_editor.redo(), 70),
                ('💾 Save', lambda: self.theme_editor.save(), 74),
                ('💾 Save As...', lambda: self.theme_editor.save_as(), 105),
                ('✕ Cancel', lambda: self.theme_editor.stop(), 78),
            ]:
                btn = QPushButton(title, self._theme_editor_toolbar)
                btn.setObjectName('fixedThemeEditorAction')
                btn.setFixedSize(width, 28)
                btn.clicked.connect(callback)
                edit_bar.addWidget(btn)
            self._theme_editor_toolbar.adjustSize()
            self._theme_editor_toolbar.move(12, 12)
            self._theme_editor_toolbar.hide()
            self.theme_editor = ThemeEditor(self)
            self.refresh_theme_menu()
            self.theme_manager.apply("default")
            self.build_expert()
            self.build_training()
            self.build_vibes()
            self.build_banned()
            self.build_about()
            # Clock typography is restored after the window is shown, once Qt
            # has completed its initial layout and applied the global QSS.

        def _on_tab_changed(self, index):
            QTimer.singleShot(0, self._enforce_system_layers)
            # Only show the absolute music components if Live Cockpit (index 0) is active
            visible = (index == 0)
            # The manager knows what is supposed to be visible in the theme
            for key, widget in self.theme_manager.panels.items():
                theme_visible = self.theme_manager.absolute_bounds.get(key) is not None
                # Check current theme manifest if it should be visible
                manifest_vis = True
                try:
                    data = json.loads(self.theme_manager.theme_file(self.theme_manager.active_theme, '.json').read_text(encoding='utf-8'))
                    manifest_vis = data.get('panels', {}).get(key, {}).get('visible', True)
                except Exception:
                    pass
                if key not in ('search_panel', 'quick_panel', 'tabs'): # Keep the base UI visible
                    widget.setVisible(visible and theme_visible and manifest_vis)
            
            # Only an explicit floating entry may show a canvas-only control.
            # Layout-less labels, clock and transport controls otherwise have
            # no valid placement and would appear at the canvas origin.
            try:
                manifest = json.loads(self.theme_manager.theme_file(
                    self.theme_manager.active_theme, '.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                manifest = {}
            floating_specs = manifest.get('floating_components', {})
            component_specs = manifest.get('components', {})
            for key, (widget, original_layout, _) in self.theme_manager.dock_slots.items():
                if key == 'tabs':
                    continue
                floating_spec = floating_specs.get(key)
                is_positioned = key in self.theme_manager.floating_bounds
                if original_layout is None:
                    # These controls have no fallback layout, regardless of
                    # any stale visibility flags from previous themes.
                    should_show = (is_positioned and isinstance(floating_spec, dict)
                                   and floating_spec.get('visible', True))
                elif is_positioned:
                    should_show = (isinstance(floating_spec, dict)
                                   and floating_spec.get('visible', True))
                else:
                    should_show = component_specs.get(key, {}).get('visible', True)
                widget.setVisible(bool(visible and should_show))

            for i, widget in enumerate(self.theme_manager.seek_widgets):
                manifest_vis = True
                try:
                    data = json.loads(self.theme_manager.theme_file(self.theme_manager.active_theme, '.json').read_text(encoding='utf-8'))
                    seekbars = data.get('seekbars', [])
                    if i < len(seekbars):
                        manifest_vis = seekbars[i].get('visible', True)
                except Exception:
                    pass
                widget.setVisible(visible and manifest_vis)
                
            for i, widget in enumerate(self.theme_manager.visual_widgets):
                manifest_vis = True
                try:
                    data = json.loads(self.theme_manager.theme_file(self.theme_manager.active_theme, '.json').read_text(encoding='utf-8'))
                    visuals = data.get('visualizations', [])
                    if i < len(visuals):
                        manifest_vis = visuals[i].get('visible', True)
                except Exception:
                    pass
                widget.setVisible(visible and manifest_vis)

        def setup_theme_menu(self):
            """Theme selection is presentation-only; engine callbacks stay untouched."""
            self.main_menu_button = QPushButton("☰")
            self.main_menu_button.setObjectName("mainMenuButton")
            self.main_menu_button.setToolTip("SpyderMUSIC Menu")
            self.main_menu_button.setAccessibleName("Main Menu")
            self.main_menu_button.setMinimumWidth(42)
            self.main_menu = QMenu(self.main_menu_button)
            self.theme_menu = self.main_menu.addMenu("🎨 Themes")
            self.main_menu.addSeparator()
            self.main_menu.addAction('♫ Local Music & Playlists…', self.open_local_music)
            self.playback_selection_menu = self.main_menu.addMenu('🎛 Player Mode')
            from playback_selection import OPTIONS
            self._playback_actions = {}
            for label, value in OPTIONS:
                action = self.playback_selection_menu.addAction(label)
                action.setCheckable(True)
                action.triggered.connect(lambda checked=False, v=value: self._change_playback_selection(v))
                self._playback_actions[value] = action
            self.playback_selection_menu.aboutToShow.connect(self._sync_playback_selection)

            self.main_menu.addSeparator()
            self.peq_menu_action = self.main_menu.addAction('🎛 Parametric EQ')
            self.peq_menu_action.setCheckable(True)
            self.peq_menu_action.setChecked(False)
            self.peq_menu_action.setToolTip('Enable or bypass PEQ independently of panel visibility')
            self.peq_menu_action.toggled.connect(self._set_peq_from_main_menu)
            self.main_menu.addSeparator()
            self.autodl_menu_action = self.main_menu.addAction("Auto-DL")
            self.autodl_menu_action.setCheckable(True)
            self.autodl_menu_action.setChecked(bool(getattr(cfg, "AUTO_DOWNLOAD", False)))
            self.autodl_menu_action.setToolTip("Keep externally downloaded tracks permanently in the local cache")
            self.autodl_menu_action.toggled.connect(self.set_autodl_from_menu)
            self.main_menu_button.setMenu(self.main_menu)
            # Reserve the original tab-bar space, but keep the actual button
            # outside QTabWidget so it can always be stacked above themes.
            self._menu_corner_spacer = QWidget(self.tabs)
            self._menu_corner_spacer.setFixedWidth(max(42, self.main_menu_button.sizeHint().width()))
            self._menu_corner_spacer.setFixedHeight(max(28, self.main_menu_button.sizeHint().height()))
            self.tabs.setCornerWidget(self._menu_corner_spacer, Qt.TopLeftCorner)
            self.tabs.tabBar().installEventFilter(self)
            self.main_menu_button.setParent(self.canvas)
            self.main_menu_button.resize(self._menu_corner_spacer.size())
            self._enforce_system_layers()

        def _set_peq_from_main_menu(self, enabled):
            panel = getattr(self, 'peq_panel', None)
            if panel is not None:
                panel.set_eq_enabled(bool(enabled))

        def _sync_peq_menu_action(self, enabled):
            action = getattr(self, 'peq_menu_action', None)
            if action is not None:
                from PySide6.QtCore import QSignalBlocker
                with QSignalBlocker(action):
                    action.setChecked(bool(enabled))

        def _change_playback_selection(self, value):
            try:
                E.set_playback_selection(value)
                self._sync_playback_selection()
            except Exception as exc:
                print(f'Playback selection error: {exc}')

        def _sync_playback_selection(self):
            selection = E.get_playback_selection()
            for key, action in getattr(self, '_playback_actions', {}).items():
                action.setChecked(key == selection)
        def _set_theme_tab_position(self, value):
            """Horizontal tab-bar offset (0..1000), independent of tab page geometry."""
            try:
                self._theme_tab_x = max(0, min(1000, int(value))) if value is not None else None
            except (ValueError, TypeError):
                self._theme_tab_x = None
            self._enforce_system_layers()
            # QTabWidget can re-layout its QTabBar after theme/QSS changes.
            # Re-apply the saved position once the event queue is drained.
            QTimer.singleShot(0, self._enforce_system_layers)

        def _enforce_system_layers(self):
            """Hard system boundaries: tabs below all theme widgets, menu above.

            This deliberately uses QWidget stacking, not a theme z-index. No
            fullscreen mouse-catching top overlay is created.
            """
            tabs = getattr(self, 'tabs', None)
            menu = getattr(self, 'main_menu_button', None)
            if tabs is not None:
                tabs.lower()
                # Only move the tab bar, never the QTabWidget or its pages.
                bar = tabs.tabBar()
                if bar is not None and bar.width() and getattr(self, '_theme_tab_x', None) is not None:
                    x = round(self._theme_tab_x * self.canvas.width() / 1000)
                    x = max(0, min(x, max(0, tabs.width() - bar.width())))
                    if bar.x() != x:
                        bar.move(x, bar.y())
            if menu is not None and menu.parentWidget() is self.canvas:
                corner = getattr(self, '_menu_corner_spacer', None)
                if corner is not None:
                    bar = tabs.tabBar() if tabs is not None else None
                    # Align the menu vertically with the actual QTabBar.
                    anchor = bar if bar is not None else corner
                    pos = self.canvas.mapFromGlobal(anchor.mapToGlobal(QPoint(0, 0)))
                    # Match the left/top outer spacing of the Snow-Ice tabs.
                    # Button padding and QSS styling remain customizable.
                    target_geometry = (4, pos.y() + 4,
                                            max(42, corner.width()), max(28, corner.height()))
                    if menu.geometry().getRect() != target_geometry:
                        menu.setGeometry(*target_geometry)
                menu.raise_()

        def open_local_music(self):
            from offline_library_dialog import OfflineLibraryDialog
            dialog = OfflineLibraryDialog(self)
            dialog.exec()

        def set_autodl_from_menu(self, enabled):
            """Change the global Auto-DL mode without coupling the menu to the engine."""
            enabled = bool(enabled)
            cfg.AUTO_DOWNLOAD = enabled
            if hasattr(self, "autodl") and self.autodl.isChecked() != enabled:
                self.autodl.setChecked(enabled)

        def refresh_theme_menu(self):
            self.theme_menu.clear()
            for slug, label in self.theme_manager.available_themes():
                action = self.theme_menu.addAction(("✓ " if slug == self.theme_manager.active_theme else "") + label)
                action.triggered.connect(lambda checked=False, name=slug: self.switch_theme(name))
            self.theme_menu.addSeparator()
            self.theme_menu.addAction('＋ Create New Theme…', self.create_theme_dialog)
            self.theme_menu.addAction('✏ Theme Studio (Editiermodus)', self.theme_editor.start)
            self.theme_menu.addAction("ZIP-Theme installieren…", self.install_theme_dialog)

        def create_theme_dialog(self):
            """Create an independent absolute-layout theme and open it in Studio."""
            import re
            dialog = QDialog(self)
            dialog.setWindowTitle('Create New Theme')
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel('What should your new theme be called?'))
            name_input = QLineEdit(dialog)
            name_input.setPlaceholderText('My New Theme')
            layout.addWidget(name_input)
            # New themes start without cockpit objects. The two protected
            # system layers are rendered by the editor itself.
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dialog)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() != QDialog.Accepted:
                return
            name = name_input.text().strip()
            if not name or name in ('.', '..') or any(c in name for c in '/\\:*?"<>|') or name.endswith(('.', ' ')):
                QMessageBox.warning(self, 'Invalid Name', 'Please enter a valid folder name.')
                return
            folder = self.theme_manager.theme_dir / name
            if folder.exists():
                QMessageBox.warning(self, 'Theme Already Exists', 'This theme folder already exists.')
                return
            # Die Vorlage ist als komprimierte Daten direkt im Programm enthalten.
            # No external ZIP/QSS/JSON/PNG template is read or copied.
            from theme_starter_data import starter_files
            from pathlib import Path
            try:
                template_files = starter_files()
                qss_bytes = template_files['Mad_Scientist_EMPTY.qss']
                data = json.loads(template_files['Mad_Scientist_EMPTY.json'].decode('utf-8-sig'))
                assets = [(asset_name, asset_bytes)
                          for asset_name, asset_bytes in template_files.items()
                          if asset_name.startswith('assets/')]
                data['id'] = name
                data['name'] = name
                self.theme_manager._validate_manifest(data)
                folder.mkdir(parents=False, exist_ok=False)
                try:
                    (folder / (name + '.json')).write_text(
                        json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                    (folder / (name + '.qss')).write_bytes(qss_bytes)
                    for asset_name, asset_bytes in assets:
                        # ZIP contents are restricted to the assets directory.
                        rel = Path(asset_name)
                        if rel.is_absolute() or '..' in rel.parts:
                            raise ValueError('Invalid asset path in theme template')
                        target = folder / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(asset_bytes)
                except Exception:
                    import shutil
                    shutil.rmtree(folder)
                    raise
                theme_id = (folder / name).relative_to(self.theme_manager.theme_dir).as_posix()
                self.switch_theme(theme_id)
                if self.theme_manager.active_theme != theme_id:
                    return
                self.theme_editor.start()
                self.theme_editor.new_theme_destination = theme_id
            except Exception as exc:
                QMessageBox.warning(self, 'Could not create theme', str(exc))

        def _apply_clock_typography(self, data):
            """Apply saved clock font via local QSS (QFont alone loses to global QSS)."""
            widget = self.theme_manager.components.get('clock')
            if widget is None:
                return
            spec = data.get('editor_fonts', {}).get('clock', {})
            color = data.get('editor_text_colors', {}).get('clock')
            font = widget.font()
            if spec:
                font.setFamily(str(spec.get('family', font.family())))
                font.setPointSize(max(1, int(spec.get('size', font.pointSize()))))
                font.setBold(bool(spec.get('bold', font.bold())))
                widget.setFont(font)
            rules = []
            if color:
                from PySide6.QtGui import QColor
                rules.append('color: %s' % QColor(color).name(QColor.HexArgb))
            if spec:
                family = font.family().replace('\\', '\\\\').replace('"', '\\"')
                rules.extend((f'font-family: "{family}"',
                              f'font-size: {font.pointSize()}pt',
                              f'font-weight: {"bold" if font.bold() else "normal"}'))
            widget.setStyleSheet('; '.join(rules) + ';' if rules else '')

        def _restore_initial_clock_typography(self):
            """Run after UI construction and the initial global QSS application."""
            try:
                name = self.theme_manager.active_theme
                path = self.theme_manager.theme_file(name, '.json')
                data = json.loads(path.read_text(encoding='utf-8'))
                self._apply_clock_typography(data)
            except Exception as exc:
                print(f'⚠️ Clock typography startup: {exc}')

        def switch_theme(self, name):
            previous = self.theme_manager.active_theme
            app = QApplication.instance()
            previous_style = app.styleSheet()
            try:
                # Validate before changing global stylesheet.
                data = json.loads((self.theme_manager.theme_file(name, '.json')).read_text(encoding='utf-8'))
                self.theme_manager._validate_manifest(data)
                new_style = self.theme_manager.stylesheet(name)
                self.theme_manager.apply(name)
                # Theme application can show/reparent floating cockpit bars.
                # Reapply the active tab's visibility after the theme settles.
                QTimer.singleShot(0, lambda: self._on_tab_changed(self.tabs.currentIndex()))
                self.backdrop.update()
                self.backdrop.set_editor_background(data.get('editor_background', {'mode':'album'}))
                for key, info in data.get('editor_fonts', {}).items():
                    widget = self.theme_manager.panels.get(key) or self.theme_manager.components.get(key)
                    if widget:
                        font = widget.font()
                        font.setFamily(info.get('family',font.family()))
                        font.setPointSize(max(1,int(info.get('size',font.pointSize()))))
                        font.setBold(bool(info.get('bold',False)))
                        widget.setFont(font)
                app.setStyleSheet(new_style)
                self._apply_clock_typography(data)
                self.refresh_theme_menu()
                QTimer.singleShot(0, self.update_cover_display)
                QTimer.singleShot(0, self._enforce_system_layers)
                QTimer.singleShot(200, self._enforce_system_layers)
                # Applying the app-wide QSS and reparenting widgets triggers deferred
                # Qt layout/resize events. Animate only once those have settled.
                QTimer.singleShot(150, lambda theme=name: self.theme_manager.start_pending_transitions(theme))
            except Exception as exc:
                try:
                    self.theme_manager.apply(previous)
                    app.setStyleSheet(previous_style)
                except Exception:
                    pass
                QMessageBox.warning(self, "Theme Error", str(exc))

        def install_theme_dialog(self):
            filename, _ = QFileDialog.getOpenFileName(self, "Select Theme ZIP", "", "ZIP-Archive (*.zip)")
            if not filename:
                return
            try:
                name = self.theme_manager.install_zip(filename)
                self.refresh_theme_menu()
                self.switch_theme(name)
            except Exception as exc:
                QMessageBox.warning(self, "Theme installation failed", str(exc))

        def panel(self, obj_name="panel"):
            f = QFrame(self.canvas)
            f.setObjectName(obj_name)
            f.setProperty("panelRole", True)
            return f

        def _create_standard_container(self, panel_id: str, header_id: str, header_text: str,
                                       content_widget: QWidget, content_id: str,
                                       footer_widget: QWidget = None) -> tuple[QFrame, QLabel]:
            """Creates a matching container for log and list boxes without inline QSS."""
            container = self.panel(panel_id)
            layout = QVBoxLayout(container)
            layout.setContentsMargins(6, 5, 6, 6)
            layout.setSpacing(4)

            header = QLabel(header_text)
            header.setObjectName(header_id)
            header.setAlignment(Qt.AlignCenter)
            layout.addWidget(header)

            content_widget.setObjectName(content_id)
            layout.addWidget(content_widget, 1)

            if footer_widget is not None:
                layout.addWidget(footer_widget)

            return container, header

        def section(self, title):
            f = QFrame()
            f.setObjectName("section")
            lay = QGridLayout(f)
            lay.setContentsMargins(15, 10, 15, 10)
            lay.setHorizontalSpacing(15)
            lay.setVerticalSpacing(8)
            # Keep the section title and all input rows together at the top.
            # Any extra height is absorbed by a trailing empty grid row.
            lay.setRowStretch(5, 1)
            # Keep the Expert Settings form anchored to the left. The spare
            # width belongs to a dedicated empty trailing column, not the fields.
            for column in range(4):
                lay.setColumnStretch(column, 0)
            lay.setColumnStretch(4, 1)
            h = QLabel(title)
            h.setStyleSheet("color:#00ff00;font-size:13px;font-weight:bold")
            lay.addWidget(h, 0, 0, 1, 4)
            return f, lay

        def context(self, w, editable=False):
            w.setContextMenuPolicy(Qt.CustomContextMenu)
            def pop(pos):
                m = QMenu(w)
                if editable and hasattr(w, "cut"):
                    m.addAction("Cut", w.cut)
                if hasattr(w, "copy"):
                    m.addAction("Copy", w.copy)
                if editable and hasattr(w, "paste"):
                    m.addAction("Paste", w.paste)
                if editable and hasattr(w, "selectAll"):
                    m.addSeparator()
                    m.addAction("Select All", w.selectAll)
                m.exec(w.mapToGlobal(pos))
            w.customContextMenuRequested.connect(pop)

        def table(self, headers, widths=None, sortable=True):
            t = QTableWidget(0, len(headers))
            t.setHorizontalHeaderLabels(headers)
            t.setSelectionBehavior(QAbstractItemView.SelectRows)
            t.setSelectionMode(QAbstractItemView.ExtendedSelection)
            t.setEditTriggers(QAbstractItemView.NoEditTriggers)
            t.verticalHeader().setVisible(False)
            t.verticalHeader().setDefaultSectionSize(25)
            t.horizontalHeader().setStretchLastSection(True)
            t.setSortingEnabled(sortable)
            if widths:
                for i, w in enumerate(widths):
                    t.setColumnWidth(i, w)
            return t

        def update_cover_display(self):
            """Keep the CTk proportions: cover column defines a square top row.

            Do not let the image size hint drive the grid's column widths.
            """
            if not hasattr(self, "cover_panel") or not hasattr(self, "cover"):
                return
            manager = getattr(self, "theme_manager", None)
            if manager is not None and manager.layout_mode == "absolute":
                # The layout engine owns panel geometry in absolute mode.
                # Only scale the artwork INSIDE its assigned rectangle.
                rect = manager.absolute_bounds.get("cover")
                if rect:
                    cw = max(1, round(manager.canvas.width() * rect[2] / 1000))
                    ch = max(1, round(manager.canvas.height() * rect[3] / 1000))
                    side = max(1, min(cw, ch))
                    if self.cover.width() != side or self.cover.height() != side:
                        self.cover.setFixedSize(side, side)
                    if self.raw_cover_pixmap and not self.raw_cover_pixmap.isNull():
                        self.cover.setPixmap(self.raw_cover_pixmap)
                return
            # CTK reference: ~212 px at 1300 px window width, not the
            # entire expanding grid column. Width and height must agree.
            available = max(1, self.live.width() - 20)
            settings = getattr(getattr(self, "theme_manager", None), "settings", {})
            base = settings.get("cover_base", 212)
            minimum = settings.get("cover_min", 110)
            reference = settings.get("cover_reference_width", 1280)
            side = max(minimum, round(available * base / reference))
            side = min(side, settings.get("cover_max", 4096), max(minimum, self.live.height() - 410))
            if self.cover_panel.width() != side:
                self.cover_panel.setFixedWidth(side)
            if self.cover_panel.height() != side:
                self.cover_panel.setFixedHeight(side)
            if hasattr(self, "brain_panel") and self.brain_panel.height() != side:
                self.brain_panel.setFixedHeight(side)
            art_side = side
            if self.cover.size().width() != art_side or self.cover.size().height() != art_side:
                self.cover.setFixedSize(art_side, art_side)
            if self.raw_cover_pixmap and not self.raw_cover_pixmap.isNull():
                self.cover.setPixmap(self.raw_cover_pixmap)

        def eventFilter(self, obj, event):
            # Do not reposition QTabBar from its own Move/Resize/Show events.
            # QTabWidget controls its internal tab bar layout. Reacting to a
            # Qt layout move by scheduling another move can create an event
            # feedback loop and flood the console with QTabBar warnings.
            # Explicit theme changes and window resize already synchronize
            # the requested tab offset via _enforce_system_layers().
            if hasattr(self, "cover_panel") and obj is self.cover_panel and event.type() == QEvent.Resize:
                QTimer.singleShot(0, self.update_cover_display)
            return super().eventFilter(obj, event)

        def resizeEvent(self, event):
            super().resizeEvent(event)
            QTimer.singleShot(0, self.update_cover_display)
            # Re-align once Qt has finished laying out the resized window.
            QTimer.singleShot(0, self.align_genres_to_search)
            QTimer.singleShot(0, self._enforce_system_layers)

        def align_genres_to_search(self):
            """Keep the Genres edit's right edge aligned with the Search combo."""
            if not hasattr(self, "genres") or not hasattr(self, "search"):
                return
            if not self.genres.isVisible() or not self.search.isVisible():
                return
            # Map both widgets to the same coordinate system; their layouts
            # have different margins and must not be compared locally.
            search_right = self.live.mapFromGlobal(
                self.search.mapToGlobal(QPoint(self.search.width(), 0))
            ).x()
            genres_left = self.live.mapFromGlobal(
                self.genres.mapToGlobal(QPoint(0, 0))
            ).x()
            desired = max(120, search_right - genres_left)
            if self.genres.width() != desired:
                self.genres.setFixedWidth(desired)

        def build_live(self):
            outer = QVBoxLayout(self.live)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)

            outer.addLayout(self.grid, 1)

            for col, weight, minimum in ((0, 0, 0), (1, 559, 230),
                                         (2, 216, 145), (3, 219, 145)):
                self.grid.setColumnStretch(col, weight)
                self.grid.setColumnMinimumWidth(col, minimum)
            self.grid.setRowStretch(0, 0)
            self.grid.setRowStretch(1, 1)

            # -----------------------------------------------------------------
            # Row 0, Col 0: Cover image
            # -----------------------------------------------------------------
            self.cover_panel = self.panel("cover_panel")
            cl = QVBoxLayout(self.cover_panel)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(0)
            self.cover = RoundedCoverLabel("Cover Art", radius=9)
            self.cover.setObjectName("cover_art")
            try:
                from assets import Start_Image
                pm = QPixmap()
                pm.loadFromData(base64.b64decode(Start_Image))
                self.raw_cover_pixmap = pm
                self.cover.setPixmap(pm)
            except Exception:
                self.cover.setText("Image load failed.")
            self.cover_panel.setFixedSize(212, 212)
            self.cover.setFixedSize(212, 212)
            cl.addWidget(self.cover, 0, Qt.AlignCenter)
            self.cover_panel.installEventFilter(self)
            self.grid.addWidget(self.cover_panel, 0, 0)
            self.theme_manager.register("cover", self.cover_panel, 0, 0)
            # Optional, independent PEQ panel. It is hidden for legacy themes
            # until placed explicitly through Theme Studio.
            from spyder_peq_embedded import SpyderPEQWidget
            self.peq_panel = SpyderPEQWidget(E.mpv_player, self.canvas)
            self.peq_panel.setObjectName("peq_panel")
            self.peq_panel.hide()
            self.theme_manager.register("peq", self.peq_panel, 0, 0)
            self.peq_panel.plot.eqToggled.connect(self._sync_peq_menu_action)
            self._sync_peq_menu_action(self.peq_panel.peq.enabled)


            # -----------------------------------------------------------------
            # 1. Container: Brain & Scout Log (Row 0, Col 1)
            # -----------------------------------------------------------------
            self.ai = QPlainTextEdit(self.canvas)
            self.ai.setReadOnly(True)
            self.context(self.ai, False)

            sf = QFrame(self.canvas)
            sf.setObjectName("scout_strip")
            sf.setFixedHeight(43)
            sl = QVBoxLayout(sf)
            sl.setContentsMargins(6, 2, 6, 2)
            sl.setSpacing(0)
            self.scout = QLabel("🔭 Scout: Waiting for discovery...")
            self.scout.setObjectName("scout_primary")
            self.scout2 = QLabel("")
            self.scout2.setObjectName("scout_secondary")
            sl.addWidget(self.scout)
            sl.addWidget(self.scout2)

            self.brain_panel, self.brain_header = self._create_standard_container(
                panel_id="brain_panel",
                header_id="brain_header",
                header_text="AI Judge & Vibe Selection",
                content_widget=self.ai,
                content_id="ai_log",
                footer_widget=sf
            )
            self.grid.addWidget(self.brain_panel, 0, 1)
            self.theme_manager.register("brain_log", self.brain_panel, 0, 1)

            # -----------------------------------------------------------------
            # 2. Container: Top Artists (Row 0..1, Col 2)
            # -----------------------------------------------------------------
            self.top = QListWidget(self.canvas)
            self.top.setSpacing(0)

            self.top_panel, self.toplabel = self._create_standard_container(
                panel_id="top_artists_panel",
                header_id="top_artists_header",
                header_text=f"Top {cfg.TOP_FAV_ARTISTS_COUNT}",
                content_widget=self.top,
                content_id="top_list",
                footer_widget=None
            )
            self.grid.addWidget(self.top_panel, 0, 2, 2, 1)
            self.theme_manager.register("top_artists", self.top_panel, 0, 2, 2, 1)

            # -----------------------------------------------------------------
            # 3. Container: Last Songs / History (Row 0, Col 3)
            # -----------------------------------------------------------------
            self.hist = QListWidget(self.canvas)
            self.hist.setSpacing(0)
            self.hist.itemDoubleClicked.connect(lambda item: start_thread(_enqueue_track_by_name, item.text()))

            self.history_panel, self.history_header = self._create_standard_container(
                panel_id="history_panel",
                header_id="history_header",
                header_text="Last Songs",
                content_widget=self.hist,
                content_id="history_list",
                footer_widget=None
            )
            self.grid.addWidget(self.history_panel, 0, 3)
            self.theme_manager.register("history", self.history_panel, 0, 3)

            # -----------------------------------------------------------------
            # 4. Container: Main Log + VU Strip (Row 1, Col 0..1)
            # -----------------------------------------------------------------
            self.log = QPlainTextEdit(self.canvas)
            self.log.setReadOnly(True)
            self.context(self.log, False)

            vu_strip = QFrame(self.canvas)
            vu_strip.setObjectName("vu_strip")
            vu_strip.setFixedHeight(38)
            vr = QHBoxLayout(vu_strip)
            vr.setContentsMargins(0, 0, 0, 0)
            vr.setSpacing(8)
            self.vua = QtVU("A", "#FF8A00")
            self.vua.setObjectName("vu_left")
            self.vub = QtVU("B", "#B84DFF")
            self.vub.setObjectName("vu_right")
            vr.addWidget(self.vua, 1)
            vr.addWidget(self.vub, 1)

            self.main_log_panel, self.track = self._create_standard_container(
                panel_id="main_log_panel",
                header_id="main_log_header",
                header_text="",
                content_widget=self.log,
                content_id="main_log_text",
                footer_widget=vu_strip
            )
            self.track.setObjectName("track_title")
            self.grid.addWidget(self.main_log_panel, 1, 0, 1, 2)
            self.theme_manager.register("main_log", self.main_log_panel, 1, 0, 1, 2)

            # -----------------------------------------------------------------
            # 5. Container: Queue (Row 1, Col 3)
            # -----------------------------------------------------------------
            self.queue = QueueList(self.canvas)
            self.queue.setSpacing(0)
            self.queue.setSelectionMode(QAbstractItemView.ExtendedSelection)
            self.queue.setDragDropMode(QAbstractItemView.InternalMove)
            self.queue.setDefaultDropAction(Qt.MoveAction)
            self.queue.persist_order = self.persist_queue_order
            self.queue.delete_selected = self.delete_queue_selection
            self.queue.itemDoubleClicked.connect(lambda item: self.delete_queue_rows([item]))

            clear_queue_btn = QPushButton("Clear Queue", self.canvas)
            clear_queue_btn.setObjectName("clear_queue_button")
            clear_queue_btn.clicked.connect(self.clear_queue_confirmed)

            self.queue_panel, self.queue_header = self._create_standard_container(
                panel_id="queue_panel",
                header_id="queue_header",
                header_text="Queue",
                content_widget=self.queue,
                content_id="queue_list",
                footer_widget=clear_queue_btn
            )
            self.grid.addWidget(self.queue_panel, 1, 3)
            self.theme_manager.register("queue", self.queue_panel, 1, 3)

            # Optional local playlist panel. Older themes do not include this key.
            from playlist_theme_widget import PlaylistThemePanel
            self.playlist_panel = PlaylistThemePanel(self.canvas)
            self.playlist_panel.hide()
            self.theme_manager.register('playlist', self.playlist_panel, 3, 0)
            # Row 2: Seek bar + threshold markers
            self.seek = QSlider(Qt.Horizontal, self.canvas)
            # Internal playback controller, never a visible theme element.
            self.seek.setObjectName("masterSeekController")
            self.seek.hide()
            self.seek.setRange(0, 100)
            self.seek.sliderPressed.connect(self._mark_seek_interaction)
            self.seek.sliderMoved.connect(lambda _: self._mark_seek_interaction())
            self.seek.sliderReleased.connect(lambda: do_seek(self.seek.value()))
            # Stage 17.6: independent RMS visual, no MPV/PCM/FFT changes.
            self.waveform_rms = RMSWaveform(
                self.seek, do_seek, self._mark_seek_interaction,
                os.path.join(DATA_DIR, 'waveform_rms.sqlite3'), parent=self.canvas)
            self.threshold_markers = ThresholdMarkers(self.seek, parent=self.canvas)
            # Markers belong to the hidden master, not the floating seek views.
            self.threshold_markers.hide()
            
            # A second *view* of the seek control, not a second player controller.
            self.circular_seek = CircularSeek(self.seek, do_seek, self._mark_seek_interaction, self.canvas)
            self.theme_manager.register_seek_visualization(self.circular_seek)
            # Separate floating linear view; the original slider remains the engine source.
            self.floating_linear_seek = QSlider(Qt.Horizontal, self.canvas)
            self.floating_linear_seek.setRange(self.seek.minimum(), self.seek.maximum())
            self.floating_linear_seek.setValue(self.seek.value())
            self.seek._floating_linear_mirrors = [self.floating_linear_seek]
            self.seek._seek_visual_mirrors = [self.circular_seek, self.waveform_rms]
            self.seek.rangeChanged.connect(self.floating_linear_seek.setRange)
            self.seek.valueChanged.connect(self.floating_linear_seek.setValue)
            self.floating_linear_seek.sliderPressed.connect(self._mark_seek_interaction)
            self.floating_linear_seek.sliderMoved.connect(lambda v: (self._mark_seek_interaction(), self.seek.setValue(v)))
            self.floating_linear_seek.sliderReleased.connect(lambda: do_seek(self.floating_linear_seek.value()))
            self.theme_manager.register_linear_seek(self.floating_linear_seek)

            self.waveform_rms.hide()
            self.theme_manager.register_waveform_seek(self.waveform_rms)
            # Every added seekbar is only a view onto the same slider/decoder.
            def make_seek_view(style):
                if style == 'linear':
                    view = QSlider(Qt.Horizontal, self.canvas)
                    view.setRange(self.seek.minimum(), self.seek.maximum())
                    view.setValue(self.seek.value())
                    self.seek._floating_linear_mirrors.append(view)
                    self.seek.rangeChanged.connect(view.setRange)
                    view.sliderPressed.connect(self._mark_seek_interaction)
                    view.sliderMoved.connect(lambda v: (self._mark_seek_interaction(), self.seek.setValue(v)))
                    view.sliderReleased.connect(lambda v=view: do_seek(v.value()))
                elif style == 'circular':
                    view = CircularSeek(self.seek, do_seek, self._mark_seek_interaction, self.canvas)
                    self.seek._seek_visual_mirrors.append(view)
                else:
                    from waveform_rms import RMSWaveformMirror
                    view = RMSWaveformMirror(self.waveform_rms, self.canvas)
                    self.seek._seek_visual_mirrors.append(view)
                view.hide()
                return view
            self.theme_manager.seek_widget_factory = make_seek_view


            # Transport controls (now floating, no fixed layout)
            self.transport_buttons = []
            for txt, act in (("⏮", "previous"), ("⏯", "toggle-play"), ("⏹", "stop"), ("⏭", "next")):
                b = QPushButton(txt, self.canvas)
                b.resize(50, 35)
                b.clicked.connect(lambda checked=False, a=act: start_thread(remote_control, a))
                self.transport_buttons.append(b)

            self.time = QLabel("00:00 / 00:00", self.canvas)
            self.time.setAlignment(Qt.AlignCenter)

            self.stop_after = QPushButton("🛑 STOP AFTER", self.canvas)
            self.stop_after.resize(120, 35)
            self.fade = QPushButton("📉 FADE STOP", self.canvas)
            self.fade.resize(110, 35)
            self.repeat = QPushButton("🔂 REPEAT 1", self.canvas)
            self.repeat.resize(100, 35)

            self.stop_after.clicked.connect(self.toggle_stop_after)
            self.fade.clicked.connect(self.fade_stop)
            self.repeat.clicked.connect(self.toggle_repeat)

            # Row 4: Search & rating bar
            sp = self.panel("search_panel")
            sr = QHBoxLayout(sp)
            sr.setContentsMargins(15, 10, 15, 10)
            lbl_s = QLabel("Search:")
            lbl_s.setStyleSheet("font-size:12px;border:0")
            sr.addWidget(lbl_s)
            self.search = QComboBox()
            self.search.setEditable(True)
            self.search.setMinimumWidth(520)
            self.context(self.search.lineEdit(), True)
            self.search.lineEdit().textEdited.connect(self.search_changed)
            sr.addWidget(self.search)

            qb = QPushButton("QUEUE")
            qb.setFixedWidth(90)
            qb.clicked.connect(self.queue_search)
            sr.addWidget(qb)
            sr.addStretch(1)

            bad = QPushButton("👎 −5")
            bad.setFixedWidth(55)
            bad.clicked.connect(lambda: manual_score_current(-5))
            sr.addWidget(bad)

            self.score = QLabel(f"{cfg.CURRENT_PENDING_SCORE} Pts")
            self.score.setFixedWidth(75)
            self.score.setAlignment(Qt.AlignCenter)
            sr.addWidget(self.score)

            good = QPushButton("👍 +5")
            good.setFixedWidth(55)
            good.clicked.connect(lambda: manual_score_current(5))
            sr.addWidget(good)

            self.ban_btn = QPushButton("🚫")
            self.ban_btn.setFixedWidth(35)
            self.ban_btn.setStyleSheet("QPushButton{background:#400;color:#ffcccc;font-size:14px}QPushButton:hover{background:#700}")
            self.ban_btn.clicked.connect(self.ban_menu)
            sr.addWidget(self.ban_btn)
            outer.addWidget(sp)

            # SpyderTRACK process belongs to this window and is stopped on exit.
            self.spydertrack_control = SpyderTrackControl(BASE_DIR)
            self._spydertrack_last_status = None

            # Row 5: Quick settings bar
            quick = self.panel("quick_panel")
            qr = QHBoxLayout(quick)
            qr.setContentsMargins(15, 8, 15, 8)
            def gl(t):
                x = QLabel(t)
                x.setObjectName("quickSettingLabel")
                return x

            self.spydertrack_led = QPushButton("")
            self.spydertrack_led.setObjectName("spydertrackLed")
            self.spydertrack_led.setFixedSize(20, 20)
            self.spydertrack_led.clicked.connect(self._toggle_spydertrack)
            qr.addWidget(self.spydertrack_led)
            self._spydertrack_timer = QTimer(self)
            self._spydertrack_timer.setInterval(2500)
            self._spydertrack_timer.timeout.connect(self._refresh_spydertrack_led)
            self._spydertrack_timer.start()
            self._refresh_spydertrack_led()

            qr.addWidget(gl("Preset:"))
            self.preset = QComboBox()
            self.preset.setFixedWidth(120)
            self.preset.addItem("Custom")
            for item in cfg.GENRE_PRESETS.split(" | "):
                if ":" in item:
                    self.preset.addItem(item.split(":", 1)[0].strip())
            self.preset.setCurrentText(cfg.SELECTED_GENRE_PRESET)
            self.preset.currentTextChanged.connect(self.preset_changed)
            qr.addWidget(self.preset)

            qr.addWidget(gl("Genres:"))
            start_preset = getattr(cfg, "SELECTED_GENRE_PRESET", "Custom")
            initial_genres = cfg.TARGET_GENRES
            if start_preset != "Custom":
                presets_dict = {
                    item.split(":", 1)[0].strip(): item.split(":", 1)[1].strip()
                    for item in getattr(cfg, "GENRE_PRESETS", "").split(" | ")
                    if ":" in item
                }
                initial_genres = presets_dict.get(start_preset, initial_genres)
            self.genres = QLineEdit(initial_genres)
            self.genres.setFixedWidth(270)  # Initial width; aligned after layout.
            self.context(self.genres, True)
            qr.addWidget(self.genres)
            # Distribute spare width across the bar instead of pushing all
            # controls to the far right. Keeps the quick settings airy.
            qr.addStretch(1)

            qr.addWidget(gl("Track Lock:"))
            self.lock = QLineEdit(str(cfg.TRACK_COOLDOWN_MINUTES))
            self.lock.setFixedWidth(60)
            self.context(self.lock, True)
            qr.addWidget(self.lock)

            qr.addWidget(gl("Artist Lock:"))
            self.artist_lock = QLineEdit(str(cfg.ARTIST_COOLDOWN_MINUTES))
            self.artist_lock.setFixedWidth(50)
            self.context(self.artist_lock, True)
            qr.addWidget(self.artist_lock)

            qr.addWidget(gl("Top:"))
            self.topn = QLineEdit(str(cfg.TOP_FAV_ARTISTS_COUNT))
            self.topn.setFixedWidth(60)
            self.context(self.topn, True)
            qr.addWidget(self.topn)
            qr.addStretch(1)

            # Auto-DL is a global operating mode, not a queue/preset option.
            # Keep it visually independent in the tab header at the top right.
            self.autodl = QCheckBox("Auto-DL")
            self.autodl.setChecked(bool(getattr(cfg, "AUTO_DOWNLOAD", False)))
            self.autodl.setToolTip("Keep externally fetched tracks permanently in the local cache")
            self.autodl.setStyleSheet(
                "QCheckBox{color:#00ff00;font-size:11px;font-weight:bold;"
                "padding:4px 12px 4px 8px;background:#242424;border-radius:5px;}"
                "QCheckBox::indicator{width:16px;height:16px;border-radius:4px;"
                "border:1px solid #555;background:#111;}"
                "QCheckBox::indicator:checked{background:#F9061B;border-color:#F9061B;}"
            )
            self.autodl.toggled.connect(lambda v: setattr(cfg, "AUTO_DOWNLOAD", bool(v)))
            self.autodl.toggled.connect(self.autodl_menu_action.setChecked)
            # Retain the existing checkbox for compatibility with the theme registry
            # and apply_quick(), but expose Auto-DL only through the main menu.
            self.autodl.hide()

            qr.addWidget(gl("Temp:"))
            self.temp = QSlider(Qt.Horizontal)
            self.temp.setRange(1, 10)
            self.temp.setValue(round(getattr(cfg, "TEMPERATURE", 0.3) * 10))
            self.temp.setFixedWidth(100)
            qr.addWidget(self.temp)

            self.tempv = QLabel(f"{getattr(cfg, 'TEMPERATURE', 0.3):.1f}")
            temp_colors = {
                1: "#FEDA04", 2: "#AAFF08", 3: "#33FF66", 4: "#71FF0E",
                5: "#AAFF08", 6: "#E4FF02", 7: "#FEDA04", 8: "#FC900C",
                9: "#FA4A13", 10: "#F9061B"
            }
            def update_qt_temperature(value):
                val = round(value / 10.0, 1)
                self.tempv.setText(f"{val:.1f}")
                self.tempv.setStyleSheet(
                    f"color:{temp_colors.get(value, '#FFFFFF')};"
                    "font-family:'Courier New',Consolas;font-size:13px;font-weight:bold;border:0"
                )
                cfg.TEMPERATURE = val
                cfg.save_to_ini()

            self.temp.valueChanged.connect(update_qt_temperature)
            update_qt_temperature(self.temp.value())
            qr.addWidget(self.tempv)

            ap = QPushButton("APPLY")
            ap.setFixedWidth(90)
            ap.clicked.connect(self.apply_quick)
            qr.addWidget(ap)
            outer.addWidget(quick)


            # Theme IDs expose presentation, never callbacks or engine state.
            for theme_id, widget in (
                ("cover_art", self.cover),
                ("brain_header", self.brain_header), ("ai_log", self.ai),
                ("scout_strip", sf), ("scout_primary", self.scout),
                ("scout_secondary", self.scout2), ("top_title", self.toplabel),
                ("top_list", self.top), ("history_header", self.history_header),
                ("history_list", self.hist), ("track_title", self.track),
                ("main_log_text", self.log), ("vu_strip", vu_strip),
                ("vu_left", self.vua), ("vu_right", self.vub),
                ("queue_header", self.queue_header), ("queue_list", self.queue),
                ("clear_queue_button", clear_queue_btn), ("seek", self.seek),
                ("waveform_rms", self.waveform_rms), ("seek_markers", self.threshold_markers),
                ("search_panel", sp), ("quick_panel", quick), ("tabs", self.tabs),
                ("queue_button", qb), ("negative_rating", bad),
                ("rating_score", self.score), ("positive_rating", good),
                ("ban_button", self.ban_btn), ("preset", self.preset),
                ("genres", self.genres), ("track_lock", self.lock),
                ("artist_lock", self.artist_lock), ("top_count", self.topn),
                ("autodl", self.autodl), ("temperature", self.temp),
                ("temperature_value", self.tempv), ("quick_apply", ap),
            ):
                self.theme_manager.register_component(theme_id, widget)

            # Each deck is a real dockable theme object. Legacy themes keep
            # both meters in the original strip until explicitly floated.
            self.theme_manager.register_dockable('vu_left', self.vua, vr, 0)
            self.theme_manager.register_dockable('vu_right', self.vub, vr, 1)

            self.theme_manager.register_dockable('search_panel', sp, outer, outer.indexOf(sp))
            self.theme_manager.register_dockable('quick_panel', quick, outer, outer.indexOf(quick))
            self.theme_manager.register_dockable('tabs', self.tabs, self.root_layout, self.root_layout.indexOf(self.tabs))
            
            for index, (name, button) in enumerate(zip(
                ('transport_previous', 'transport_play_pause', 'transport_stop', 'transport_next'),
                self.transport_buttons)):
                self.theme_manager.register_component(name, button)
                self.theme_manager.register_dockable(name, button, None, index)
            
            for index, (name, button) in enumerate((
                ('stop_after', self.stop_after),
                ('fade_stop', self.fade),
                ('repeat_one', self.repeat),
            ), start=1):
                self.theme_manager.register_component(name, button)
                self.theme_manager.register_dockable(name, button, None, index)
            
            self.theme_manager.register_component('clock', self.time)
            self.theme_manager.register_dockable('clock', self.time, None, 1)
            
            # Optional independent text overlays for custom themes.
            # These are not added to a visible layout; old themes stay unchanged.
            self.theme_track_labels = {}
            for theme_id in ('artist_title_text', 'artist_text', 'title_text'):
                label = QLabel(self.theme_manager.canvas)
                label.setObjectName(theme_id)
                label.setProperty('spyderThemeId', theme_id)
                label.setAlignment(Qt.AlignCenter)
                label.setStyleSheet('color: white; background: transparent;')
                label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                label.hide()
                self.theme_track_labels[theme_id] = label
                self.theme_manager.register_component(theme_id, label)
                self.theme_manager.register_dockable(theme_id, label, None, 0)


        def build_expert(self):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet("QScrollArea{background:transparent;border:0}")
            host = QWidget()
            lay = QVBoxLayout(host)
            lay.setSpacing(12)
            scroll.setWidget(host)
            root = QVBoxLayout(self.expert)
            root.addWidget(scroll)
            self.ex = {}

            def add_field(g, row, col, label, key, value, width=220, disabled=False):
                lbl = QLabel(label)
                lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                lbl.setStyleSheet("font-size:12px")
                g.addWidget(lbl, row, col * 2)
                e = QLineEdit(str(value))
                e.setFixedWidth(width)
                e.setDisabled(disabled)
                self.context(e, True)
                g.addWidget(e, row, col * 2 + 1, alignment=Qt.AlignLeft)
                self.ex[key] = e

            # 1. Last.fm & Scout
            f, g = self.section("Last.fm API & Autonomous Scout")
            add_field(g, 1, 0, "Last.fm API Key:", "lastfm", cfg.LASTFM_API_KEY, width=448)
            add_field(g, 3, 0, "Wikidata Contact Email:", "wikidata_email", cfg.WIKIDATA_CONTACT_EMAIL, width=448)
            self.ex["wikidata_email"].setToolTip("Required for Wikidata artist-name lookups. Sent to Wikimedia in the User-Agent header.")
            self.scoutcheck = QCheckBox("Scout Active (Discovers new Bands/Tracks)")
            self.scoutcheck.setChecked(getattr(cfg, "SCOUT_ENABLED", True))
            g.addWidget(self.scoutcheck, 1, 2, 1, 2, alignment=Qt.AlignLeft)

            lbl_db = QLabel("Database File:")
            lbl_db.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lbl_db.setStyleSheet("font-size:12px")
            g.addWidget(lbl_db, 2, 0)

            db_row = QHBoxLayout()
            existing_dbs = [f_name for f_name in os.listdir(BASE_DIR) if f_name.endswith('.db')]
            db_options = list(dict.fromkeys([cfg.DB_NAME] + existing_dbs))
            self.db_combo = QComboBox()
            self.db_combo.setEditable(True)
            self.db_combo.setFixedWidth(360)
            self.db_combo.addItems(db_options)
            self.db_combo.setCurrentText(cfg.DB_NAME)
            self.context(self.db_combo.lineEdit(), True)
            db_row.addWidget(self.db_combo)

            browse = QPushButton("📁 Browse")
            browse.setFixedWidth(80)
            browse.clicked.connect(self.browse_db)
            db_row.addWidget(browse)

            new_db_btn = QPushButton("✨ New DB")
            new_db_btn.setFixedWidth(80)
            new_db_btn.setStyleSheet("QPushButton{background:#004400}QPushButton:hover{background:#006600}")
            new_db_btn.clicked.connect(self.create_new_db_qt)
            db_row.addWidget(new_db_btn)
            db_row.addStretch(1)
            g.addLayout(db_row, 2, 1, 1, 3)
            lay.addWidget(f)

            # 2. DJ Behavior & Timers
            f, g = self.section("DJ Behavior & Timers")
            add_field(g, 1, 0, "Trigger Advance (sec):", "trig", cfg.TRIGGER_SECONDS_BEFORE_END, disabled=True)
            add_field(g, 2, 0, "Crossfade Duration (sec):", "fade", cfg.CROSSFADE_SEC)
            add_field(g, 3, 0, "Track Lock (min):", "tlock", cfg.TRACK_COOLDOWN_MINUTES)
            add_field(g, 4, 0, "Artist Lock (min):", "alock", cfg.ARTIST_COOLDOWN_MINUTES)
            lay.addWidget(f)

            # 3. ChromaDB Vibe & Target Genres
            f, g = self.section("ChromaDB Vibe & Target Genres")
            lbl_vibe = QLabel("Vibe / Theme:")
            lbl_vibe.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lbl_vibe.setStyleSheet("font-size:12px")
            g.addWidget(lbl_vibe, 1, 0)
            self.vibecb = QComboBox()
            self.vibecb.setEditable(True)
            self.vibecb.setFixedWidth(260)
            initial_vibes = [x.strip() for x in cfg.AI_PROMPT_LIST.split(",") if x.strip()]
            self.vibecb.addItems(list(dict.fromkeys(initial_vibes + list(VIBE_PRESETS))))
            self.vibecb.setCurrentText(cfg.SELECTED_PROMPT_PREF)
            self.context(self.vibecb.lineEdit(), True)
            g.addWidget(self.vibecb, 1, 1, alignment=Qt.AlignLeft)
            add_field(g, 2, 0, "Target Genres:", "genres", cfg.TARGET_GENRES, width=450)
            lay.addWidget(f)

            # 4. Rating System
            f, g = self.section("Rating System")
            add_field(g, 1, 0, "Top Artists Count:", "top", cfg.TOP_FAV_ARTISTS_COUNT)
            add_field(g, 2, 0, "Points for Win:", "win", cfg.POINTS_WIN)
            add_field(g, 3, 0, "Points for Skip:", "pen", cfg.POINTS_PENALTY)
            add_field(g, 1, 1, "Win Threshold (%):", "wth", cfg.SCORE_THRESHOLD_PERCENT, width=100)
            add_field(g, 2, 1, "Skip Threshold (%):", "pth", cfg.PENALTY_THRESHOLD_PERCENT, width=100)
            lay.addWidget(f)
            # Uniform compact spacing between the four settings sections;
            # unused vertical space goes below them rather than between
            # headings and their corresponding controls.
            lay.addStretch(1)

            save = QPushButton("💾 SAVE ALL SETTINGS TO INI")
            save.setMinimumHeight(50)
            save.setStyleSheet("QPushButton{background:#008800;font-size:14px;font-weight:bold}QPushButton:hover{background:#00aa00}")
            save.clicked.connect(self.save_expert)
            root.addWidget(save)

        def build_training(self):
            l = QVBoxLayout(self.training)
            l.setContentsMargins(15, 15, 15, 15)
            top = QHBoxLayout()
            self.playurl = QLineEdit()
            self.playurl.setPlaceholderText("YouTube Playlist URL…")
            self.context(self.playurl, True)
            scan = QPushButton("🔍 Scan")
            scan.setStyleSheet("QPushButton{background:#1f538d}QPushButton:hover{background:#2868b0}")
            scan.clicked.connect(self.scan_playlist)
            top.addWidget(self.playurl, 3)
            top.addWidget(scan, 2)
            l.addLayout(top)

            self.trainstatus = QLabel("Ready.")
            self.trainstatus.setAlignment(Qt.AlignCenter)
            self.trainstatus.setStyleSheet("background:#333333;color:gray;padding:7px;border-radius:6px")
            l.addWidget(self.trainstatus)

            self.traintable = self.table(["Artist", "Title"], [260, 600])
            l.addWidget(self.traintable, 1)

            bot = QHBoxLayout()
            self.traingenre = QLineEdit()
            self.traingenre.setPlaceholderText("Genre tag (e.g. NWOCR)…")
            self.context(self.traingenre, True)
            save = QPushButton("💾 Save to DB & Chroma")
            save.setStyleSheet("QPushButton{background:#8B0000}QPushButton:hover{background:#5c0000}")
            save.clicked.connect(self.save_training)
            bot.addWidget(self.traingenre, 3)
            bot.addWidget(save, 2)
            l.addLayout(bot)

        def build_vibes(self):
            l = QVBoxLayout(self.vibes)
            l.setContentsMargins(15, 15, 15, 15)
            h = QLabel("🎚 Vibe Brain")
            h.setStyleSheet("color:#FFB700;font-size:18px;font-weight:bold")
            l.addWidget(h)
            desc = QLabel("Global taste and situational vibe learning are separate. 50 = neutral; above 50 worked well in this vibe, below 50 did not.")
            desc.setStyleSheet("color:#aaaaaa")
            l.addWidget(desc)

            sr = QHBoxLayout()
            sr.addWidget(QLabel("Search:"))
            self.vibesearch = QLineEdit()
            self.vibesearch.setPlaceholderText("Track, vibe, score, date…")
            self.context(self.vibesearch, True)
            sr.addWidget(self.vibesearch, 1)
            l.addLayout(sr)

            self.vibefilter = QLabel("")
            self.vibefilter.setStyleSheet("color:#33FF66")
            l.addWidget(self.vibefilter)

            self.vibetable = self.table(["Track", "Vibe", "Vibe Score", "Experience", "Last update"], [390, 180, 90, 90, 145])
            l.addWidget(self.vibetable, 1)

            br = QHBoxLayout()
            allb = QPushButton("↻ Show all")
            curb = QPushButton("🎵 Current track")
            reset = QPushButton("♻ Reset selected to 50")
            allb.clicked.connect(lambda: refresh_vibe_scores_ui(False))
            curb.clicked.connect(lambda: refresh_vibe_scores_ui(True))
            reset.clicked.connect(reset_selected_vibe_score)
            for b in (allb, curb, reset):
                br.addWidget(b)
            br.addStretch()
            l.addLayout(br)
            self.vibesearch.textChanged.connect(lambda _: refresh_vibe_scores_ui())

        def build_banned(self):
            l = QVBoxLayout(self.banned)
            l.setContentsMargins(18, 18, 18, 18)
            h = QLabel("🚫 Banned Tracks & Artists")
            h.setAlignment(Qt.AlignCenter)
            h.setStyleSheet("font-size:20px;font-weight:bold")
            l.addWidget(h)
            sub = QLabel("Permanent bans are excluded from Judge and fallback selection.")
            sub.setAlignment(Qt.AlignCenter)
            sub.setStyleSheet("color:#aaaaaa")
            l.addWidget(sub)

            sr = QHBoxLayout()
            sr.addWidget(QLabel("Search:"))
            self.bansearch = QLineEdit()
            self.bansearch.setPlaceholderText("Type, artist / track, date…")
            self.context(self.bansearch, True)
            sr.addWidget(self.bansearch, 1)
            l.addLayout(sr)

            self.bantable = self.table(["Type", "Artist / Track", "Banned at"], [100, 650, 160])
            l.addWidget(self.bantable, 1)

            br = QHBoxLayout()
            u = QPushButton("♻ Unban selected")
            r = QPushButton("↻ Refresh")
            u.clicked.connect(unban_selected_items)
            r.clicked.connect(refresh_blacklist_ui)
            br.addWidget(u)
            br.addWidget(r)
            br.addStretch()
            l.addLayout(br)
            self.bansearch.textChanged.connect(lambda _: refresh_blacklist_ui())

        def build_about(self):
            l = QVBoxLayout(self.about)
            l.setContentsMargins(25, 15, 25, 15)
            title = QLabel("SpyderMUSIC v4")
            title.setAlignment(Qt.AlignCenter)
            title.setStyleSheet("color:#FFB700;font-size:32px;font-weight:bold")
            l.addWidget(title)

            sub = QLabel("The ChromaDB-Powered High-End DJ Engine")
            sub.setAlignment(Qt.AlignCenter)
            sub.setStyleSheet("color:#FFB700;font-size:16px;font-style:italic")
            l.addWidget(sub)

            d = QLabel(
                "SpyderMUSIC v4 is the result of a unique collaboration: a passionate developer's "
                "uncompromising vision for authentic, high-end audio, forged together with his AI "
                "coding partner. Built during countless night shifts, this engine proves what happens "
                "when human intuition and artificial intelligence work together on equal footing.\n\n"
                "No corporate chains, no sterile algorithms—just pure dedication to the raw, breathing "
                "sound of honest music. This project is a tribute to the open-source community, the "
                "audio enthusiasts, and everyone who refuses to compromise on quality."
            )
            d.setWordWrap(True)
            d.setAlignment(Qt.AlignCenter)
            d.setStyleSheet("color:#FFB700;font-size:14px;padding:10px 60px")
            l.addWidget(d)

            lic_hdr = QLabel("GNU Affero General Public License v3.0 (AGPL-3.0)")
            lic_hdr.setStyleSheet("color:#FFB700;font-size:14px;font-weight:bold")
            l.addWidget(lic_hdr)

            lic = QPlainTextEdit()
            lic.setReadOnly(True)
            lic.setStyleSheet("color:#FFB700;background:#1b1b1b;border:1px solid #FFB700;font-family:Fixedsys,Consolas;font-size:12px")
            lic.setPlainText(
                "SpyderMUSIC v4 - ChromaDB based DJ.\n"
                "Copyright (C) 2026  Gozza Inc.\n\n"
                "This program is free software: you can redistribute it and/or modify\n"
                "it under the terms of the GNU Affero General Public License as\n"
                "published by the Free Software Foundation, either version 3 of the\n"
                "License, or (at your option) any later version.\n\n"
                "This program is distributed in the hope that it will be useful,\n"
                "but WITHOUT ANY WARRANTY; without even the implied warranty of\n"
                "MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the\n"
                "GNU Affero General Public License for more details.\n\n"
                "You should have received a copy of the GNU Affero General Public License\n"
                "along with this program.  If not, see <https://www.gnu.org/licenses/>."
            )
            self.context(lic, False)
            l.addWidget(lic, 1)

            footer = QLabel(
                "This tool is Open Source and a statement against corporate greed. "
                "Whoever uses this code shares it under the same conditions. "
                "Free software for free music."
            )
            footer.setWordWrap(True)
            footer.setAlignment(Qt.AlignCenter)
            footer.setStyleSheet("color:#FFB700;font-size:13px;padding:8px")
            l.addWidget(footer)

        # ----- Callbacks -----
        def _mark_seek_interaction(self):
            global last_user_interaction
            last_user_interaction = time.time()
            gui.last_user_interaction = last_user_interaction

        def toggle_stop_after(self):
            cfg.STOP_AFTER_CURRENT = not cfg.STOP_AFTER_CURRENT
            self.stop_after.setText("🛑 STOP PENDING" if cfg.STOP_AFTER_CURRENT else "🛑 STOP AFTER")
            self.stop_after.setProperty('spyderActive', cfg.STOP_AFTER_CURRENT)
            self.stop_after.style().unpolish(self.stop_after)
            self.stop_after.style().polish(self.stop_after)
            log_to_gui("[System] Stop pending after current track." if cfg.STOP_AFTER_CURRENT else "[System] Stop after track canceled.")

        def fade_stop(self):
            log_to_gui("[System] Initiating manual fade out...")
            fade_stop_active_playback()
            self._sync_transport_button_states()

        def _sync_transport_button_states(self):
            """Keep theme PNG active variants in sync with actual engine state."""
            states = (
                (self.stop_after, bool(cfg.STOP_AFTER_CURRENT)),
                (self.fade, bool(getattr(mpv_player, 'fade_stop_active', False))),
                (self.repeat, bool(cfg.REPEAT_TRACK)),
            )
            for button, active in states:
                if button.property('spyderActive') != active:
                    button.setProperty('spyderActive', active)
                    button.style().unpolish(button)
                    button.style().polish(button)
                    button.update()
            expected = "🛑 STOP PENDING" if cfg.STOP_AFTER_CURRENT else "🛑 STOP AFTER"
            if self.stop_after.text() != expected:
                self.stop_after.setText(expected)

        def toggle_repeat(self):
            cfg.REPEAT_TRACK = not cfg.REPEAT_TRACK
            mpv_player.set_loop(cfg.REPEAT_TRACK)
            self.repeat.setProperty('spyderActive', cfg.REPEAT_TRACK)
            self.repeat.style().unpolish(self.repeat)
            self.repeat.style().polish(self.repeat)
            log_to_gui("🔂 [System] Native MPV Repeat ONE activated." if cfg.REPEAT_TRACK else "➡️ [System] Native MPV Repeat OFF.")

        def search_changed(self, text):
            if getattr(self, "_updating_search", False):
                return
            self._updating_search = True
            try:
                edit = self.search.lineEdit()
                cursor_pos = edit.cursorPosition()
                matches = get_db_suggestions(text.strip()) if len(text.strip()) > 1 else []
                combo_block = self.search.blockSignals(True)
                edit_block = edit.blockSignals(True)
                try:
                    self.search.clear()
                    self.search.addItems(matches)
                    self.search.setEditText(text)
                    edit.setCursorPosition(min(cursor_pos, len(text)))
                finally:
                    edit.blockSignals(edit_block)
                    self.search.blockSignals(combo_block)
            finally:
                self._updating_search = False

        def persist_queue_order(self):
            """Save displayed order atomically; preserve title and original source."""
            rowids = [self.queue.item(i).data(Qt.UserRole)
                      for i in range(self.queue.count())]
            if any(rowid is None for rowid in rowids) or len(set(rowids)) != len(rowids):
                QMessageBox.warning(self, "Queue", "Invalid queue entries; reload the queue.")
                update_queue_ui()
                return
            try:
                with db_connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    old = conn.execute("SELECT rowid, item, source FROM queue").fetchall()
                    by_id = {r[0]: (r[1], r[2]) for r in old}
                    # A playing deck may dequeue concurrently. Do not resurrect its title.
                    if set(rowids) != set(by_id):
                        raise RuntimeError("Queue changed concurrently; please reorder again.")
                    conn.execute("DELETE FROM queue")
                    conn.executemany(
                        "INSERT INTO queue (item, timestamp, source) VALUES (?, ?, ?)",
                        [(by_id[r][0], float(i + 1), by_id[r][1])
                         for i, r in enumerate(rowids)]
                    )
                update_queue_ui()
            except Exception as exc:
                QMessageBox.warning(self, "Queue", str(exc))
                update_queue_ui()

        def delete_queue_rows(self, items):
            ids = [i.data(Qt.UserRole) for i in items]
            if not ids:
                return
            try:
                with db_connect() as conn:
                    conn.executemany("DELETE FROM queue WHERE rowid = ?", [(i,) for i in ids])
                update_queue_ui()
            except Exception as exc:
                QMessageBox.warning(self, "Queue", str(exc))

        def delete_queue_selection(self):
            self.delete_queue_rows(self.queue.selectedItems())

        def clear_queue_confirmed(self):
            if not self.queue.count():
                return
            if QMessageBox.question(
                self, "Clear Queue", "Remove all queued tracks?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            ) != QMessageBox.Yes:
                return
            try:
                with db_connect() as conn:
                    conn.execute("DELETE FROM queue")
                update_queue_ui()
            except Exception as exc:
                QMessageBox.warning(self, "Queue", str(exc))

        def queue_search(self):
            t = self.search.currentText().strip()
            if t:
                add_to_db_queue(t)
                self.search.clear()
                self.search.setEditText("")
                log_to_gui(f"📌 '{t}' added to Queue!")
            else:
                log_to_gui("⚠️ Please enter a track title.")

        def ban_menu(self):
            m = QMenu(self)
            m.setStyleSheet(
                "QMenu{background:#1b1b1b;color:#fff;font-weight:bold;border:1px solid #444}"
                "QMenu::item:selected{background:#8B0000;color:#fff}"
            )
            cur_artist = (cfg.CURRENT_ARTIST or "Artist").strip()
            cur_title = (cfg.CURRENT_TITLE or "Track").strip()
            m.addAction(f"Ban Track: '{cur_title}'", ban_current_track)
            m.addAction(f"Ban Artist: '{cur_artist}'", ban_current_artist)
            m.exec(self.ban_btn.mapToGlobal(self.ban_btn.rect().bottomLeft()))

        def preset_changed(self, name):
            cfg.SELECTED_GENRE_PRESET = name
            if name == "Custom":
                self.genres.setText(cfg.TARGET_GENRES)
                return
            for item in cfg.GENRE_PRESETS.split(" | "):
                if ":" in item:
                    k, v = item.split(":", 1)
                    if k.strip() == name:
                        self.genres.setText(v.strip())
                        break

        def apply_quick(self):
            try:
                cfg.SELECTED_GENRE_PRESET = self.preset.currentText()
                if cfg.SELECTED_GENRE_PRESET == "Custom":
                    cfg.TARGET_GENRES = self.genres.text().strip()
                cfg.TRACK_COOLDOWN_MINUTES = int(self.lock.text())
                cfg.ARTIST_COOLDOWN_MINUTES = int(self.artist_lock.text())
                cfg.TOP_FAV_ARTISTS_COUNT = int(self.topn.text())
                cfg.AUTO_DOWNLOAD = self.autodl.isChecked()
                cfg.TEMPERATURE = round(self.temp.value() / 10.0, 1)
                cfg.save_to_ini()
                self.toplabel.setText(f"Top {cfg.TOP_FAV_ARTISTS_COUNT}")
                self.ex["tlock"].setText(str(cfg.TRACK_COOLDOWN_MINUTES))
                self.ex["alock"].setText(str(cfg.ARTIST_COOLDOWN_MINUTES))
                self.ex["top"].setText(str(cfg.TOP_FAV_ARTISTS_COUNT))
                self.ex["genres"].setText(cfg.TARGET_GENRES)
                start_thread(update_top_list_ui)
                log_to_gui("⚡ Quick settings saved!")
            except Exception as e:
                log_to_gui(f"⚠ Quick settings: {e}")

        def browse_db(self):
            filepath, _ = QFileDialog.getOpenFileName(self, "Select Database", BASE_DIR, "SQLite DB (*.db);;All Files (*.*)")
            if not filepath:
                return
            try:
                rel = os.path.relpath(filepath, BASE_DIR)
                final = os.path.join(".", rel) if not rel.startswith("..") and not os.path.isabs(rel) else filepath
            except ValueError:
                final = filepath
            if self.db_combo.findText(final) == -1:
                self.db_combo.addItem(final)
            self.db_combo.setCurrentText(final)

        def create_new_db_qt(self):
            filepath, _ = QFileDialog.getSaveFileName(self, "Create New Database", DATA_DIR, "SQLite DB (*.db)")
            if not filepath:
                return
            if not filepath.endswith(".db"):
                filepath += ".db"
            if os.path.exists(filepath):
                log_to_gui("⚠️ Database already exists; refusing to overwrite it.")
                return
            try:
                cfg.DB_NAME = os.path.abspath(filepath)
                setup_database()
                cfg.save_to_ini()
                if self.db_combo.findText(cfg.DB_NAME) == -1:
                    self.db_combo.addItem(cfg.DB_NAME)
                self.db_combo.setCurrentText(cfg.DB_NAME)
                start_thread(update_top_list_ui)
                log_to_gui(f"✨ New DB created & active: {os.path.basename(filepath)}")
            except Exception as exc:
                log_to_gui(f"❌ DB Error: {exc}")

        def save_expert(self):
            try:
                old = cfg.DB_NAME
                cfg.LASTFM_API_KEY = self.ex["lastfm"].text()
                cfg.WIKIDATA_CONTACT_EMAIL = self.ex["wikidata_email"].text().strip()
                cfg.SCOUT_ENABLED = self.scoutcheck.isChecked()
                new_db = self.db_combo.currentText().strip()
                if new_db and not new_db.endswith(".db"):
                    new_db += ".db"
                    self.db_combo.setCurrentText(new_db)
                cfg.DB_NAME = new_db

                cfg.CROSSFADE_SEC = int(self.ex["fade"].text())
                cfg.TRACK_COOLDOWN_MINUTES = int(self.ex["tlock"].text())
                cfg.ARTIST_COOLDOWN_MINUTES = int(self.ex["alock"].text())
                cfg.TOP_FAV_ARTISTS_COUNT = int(self.ex["top"].text())
                cfg.TARGET_GENRES = self.ex["genres"].text()
                cfg.POINTS_WIN = int(self.ex["win"].text())
                cfg.POINTS_PENALTY = int(self.ex["pen"].text())
                cfg.SCORE_THRESHOLD_PERCENT = int(self.ex["wth"].text())
                cfg.PENALTY_THRESHOLD_PERCENT = int(self.ex["pth"].text())

                new_pref = self.vibecb.currentText().strip()
                cfg.SELECTED_PROMPT_PREF = new_pref
                current_list = [x.strip() for x in cfg.AI_PROMPT_LIST.split(",") if x.strip()]
                if new_pref and new_pref not in current_list:
                    current_list.append(new_pref)
                    cfg.AI_PROMPT_LIST = " , " + ", ".join(current_list)
                    if self.vibecb.findText(new_pref) == -1:
                        self.vibecb.addItem(new_pref)

                cfg.save_to_ini()
                self.lock.setText(str(cfg.TRACK_COOLDOWN_MINUTES))
                self.artist_lock.setText(str(cfg.ARTIST_COOLDOWN_MINUTES))
                self.topn.setText(str(cfg.TOP_FAV_ARTISTS_COUNT))
                self.genres.setText(cfg.TARGET_GENRES)
                self.toplabel.setText(f"Top {cfg.TOP_FAV_ARTISTS_COUNT}")
                self.threshold_markers.update()

                if old != cfg.DB_NAME:
                    log_to_gui(f"🗄️ Database changed to: {cfg.DB_NAME}")
                    setup_database()
                    cfg.ORIGINAL_DB_SCORE = 0
                    cfg.CURRENT_PENDING_SCORE = cfg.POINTS_WIN
                    self.score.setText(f"{cfg.CURRENT_PENDING_SCORE} Pts")

                start_thread(update_top_list_ui)
                log_to_gui("\n✅ ALL SETTINGS SAVED!")
                self.tabs.setCurrentWidget(self.live)
            except Exception as e:
                log_to_gui(f"⚠ Save settings: {e}")

        def scan_playlist(self):
            url = self.playurl.text().strip()
            def worker():
                try:
                    dispatcher.call.emit(lambda: (
                        self.trainstatus.setText("Scanning playlist… ⏳"),
                        self.trainstatus.setStyleSheet("background:#333333;color:yellow;padding:7px;border-radius:6px")
                    ), 0)
                    if "list=" not in url:
                        dispatcher.call.emit(lambda: (
                            self.trainstatus.setText("❌ Error: Invalid URL"),
                            self.trainstatus.setStyleSheet("background:#333333;color:red;padding:7px;border-radius:6px")
                        ), 0)
                        return
                    pid = url.split("list=")[1].split("&")[0]
                    tracks = ytmusic.get_playlist(pid).get("tracks", [])
                    def fill():
                        self.traintable.setSortingEnabled(False)
                        self.traintable.setRowCount(0)
                        for tr in tracks:
                            r = self.traintable.rowCount()
                            self.traintable.insertRow(r)
                            artist = tr.get("artists", [{"name": "Unknown"}])[0].get("name", "Unknown") if tr.get("artists") else "Unknown"
                            self.traintable.setItem(r, 0, QTableWidgetItem(artist))
                            self.traintable.setItem(r, 1, QTableWidgetItem(tr.get("title", "Unknown")))
                        self.traintable.setSortingEnabled(True)
                        self.trainstatus.setText(f"✅ {len(tracks)} tracks found!")
                        entries = []
                        for tr in tracks:
                            artist_data = tr.get("artists") or []
                            artist = (artist_data[0].get("name") or "").strip() if artist_data else ""
                            title = (tr.get("title") or "").strip()
                            if title:
                                entries.append(f"{artist} - {title}" if artist else title)
                        if entries:
                            dialog = QMessageBox(self)
                            dialog.setWindowTitle("Playlist in Queue")
                            dialog.setText(f"{len(entries)} tracks to the queue?")
                            append_btn = dialog.addButton("Append", QMessageBox.AcceptRole)
                            replace_btn = dialog.addButton("Replace", QMessageBox.ActionRole)
                            dialog.addButton("Cancel", QMessageBox.RejectRole)
                            dialog.exec()
                            chosen = dialog.clickedButton()
                            if chosen in (append_btn, replace_btn):
                                try:
                                    with db_connect() as conn:
                                        conn.execute("BEGIN IMMEDIATE")
                                        if chosen is replace_btn:
                                            conn.execute("DELETE FROM queue")
                                        max_ts = conn.execute("SELECT MAX(timestamp) FROM queue").fetchone()[0]
                                        start_ts = max(time.time(), float(max_ts or 0) + 1)
                                        conn.executemany(
                                            "INSERT INTO queue (item, timestamp, source) VALUES (?, ?, ?)",
                                            [(entry, start_ts + i, "user_queue") for i, entry in enumerate(entries)]
                                        )
                                    update_queue_ui()
                                except Exception as exc:
                                    QMessageBox.warning(self, "Playlist-Import", str(exc))
                        self.trainstatus.setStyleSheet("background:#333333;color:#00FF00;padding:7px;border-radius:6px")
                    dispatcher.call.emit(fill, 0)
                except Exception as e:
                    dispatcher.call.emit(lambda e=e: (
                        self.trainstatus.setText(f"❌ Error: {e}"),
                        self.trainstatus.setStyleSheet("background:#333333;color:red;padding:7px;border-radius:6px")
                    ), 0)
            start_thread(worker)

        def save_training(self):
            rows = sorted({i.row() for i in self.traintable.selectedIndexes()})
            genre = self.traingenre.text().strip()
            if not rows or not genre:
                self.trainstatus.setText("⚠️ Selection or genre missing!")
                self.trainstatus.setStyleSheet("background:#333333;color:yellow;padding:7px;border-radius:6px")
                return
            def worker():
                try:
                    with db_connect() as conn:
                        for r in rows:
                            artist = self.traintable.item(r, 0).text()
                            title = self.traintable.item(r, 1).text()
                            for name, kind in ((artist.strip(), "artist"), (f"{artist} - {title}".strip(), "track")):
                                conn.execute(
                                    "INSERT INTO preferences (item_name, item_type, score, last_played, genre) "
                                    "VALUES (?, ?, ?, 0, ?) ON CONFLICT(item_name) DO UPDATE SET genre=excluded.genre",
                                    (name, kind, cfg.POINTS_WIN, genre)
                                )
                                existing = conn.execute(
                                    "SELECT item_type,score,last_played,genre,video_id,duration,year,description,"
                                    "listeners,tags,similar,track_tags FROM preferences WHERE item_name=?",
                                    (name,)
                                ).fetchone()
                                if existing:
                                    sync_track_to_chroma(name, *existing)
                    dispatcher.call.emit(lambda: (
                        self.trainstatus.setText(f"💾 {len(rows)} tracks saved as '{genre}'!"),
                        self.trainstatus.setStyleSheet("background:#333333;color:#00FF00;padding:7px;border-radius:6px")
                    ), 0)
                    start_thread(update_top_list_ui)
                except Exception as e:
                    dispatcher.call.emit(lambda e=e: self.trainstatus.setText(f"❌ Error: {e}"), 0)
            start_thread(worker)

        def _refresh_spydertrack_led(self):
            status = self.spydertrack_control.status()
            colors = {'missing': '#F13A3A', 'stopped': '#F2C438', 'running': '#2FE36C'}
            labels = {'missing': 'SpyderTRACK not found',
                      'stopped': 'SpyderTRACK ready - click to start',
                      'running': 'SpyderTRACK running - click to stop'}
            if status == self._spydertrack_last_status:
                return
            self.spydertrack_led.setStyleSheet(
                'QPushButton#spydertrackLed{background-color:%s;'
                'border:2px solid #777;border-radius:10px;padding:0px;}'
                'QPushButton#spydertrackLed:hover{border:2px solid #eee;}'
                'QPushButton#spydertrackLed:disabled{border:2px solid #555;}' % colors[status]
            )
            self.spydertrack_led.setEnabled(status != 'missing')
            self.spydertrack_led.setToolTip(
                labels[status] + (
                    '\nSearched: ' + self.spydertrack_control.searched_locations()
                    if status == 'missing' else '\n' + str(self.spydertrack_control.script)
                )
            )
            self.spydertrack_led.setAccessibleName(labels[status])
            self._spydertrack_last_status = status
            log_to_gui('SpyderTRACK: ' + labels[status])

        def _toggle_spydertrack(self):
            status = self.spydertrack_control.status()
            try:
                if status == 'stopped':
                    self.spydertrack_control.start()
                elif status == 'running':
                    if not self.spydertrack_control.stop():
                        log_to_gui('SpyderTRACK: already running externally; not owned by SpyderMUSIC, cannot stop safely.')
            except Exception as exc:
                log_to_gui(f'⚠️ SpyderTRACK process: {exc}')
            self._refresh_spydertrack_led()

        def _autostart_spydertrack(self):
            if self.spydertrack_control.status() == 'stopped':
                try:
                    self.spydertrack_control.start()
                except Exception as exc:
                    log_to_gui(f'⚠️ SpyderTRACK autostart: {exc}')
            self._refresh_spydertrack_led()

        def closeEvent(self, e):
            _shutdown_owned_resources(self)
            e.accept()

    app = QApplication.instance() or QApplication(sys.argv)
    print('[SpyderMUSIC GPU] Shared OpenGL backdrop + Qt alpha/layer compositor active')
    _load_theme_stylesheet(app)
    win = Window()
    gui.spydertrack_control = win.spydertrack_control
    # The companion must be closed even when exit bypasses Window.closeEvent.
    app.aboutToQuit.connect(lambda: _shutdown_owned_resources(win))

    # Center window like CTk
    screen_geo = app.primaryScreen().availableGeometry()
    win.move(
        (screen_geo.width() - 1298) // 2,
        max(0, (screen_geo.height() - 888) // 2)
    )

    # Bind the existing engine-facing gui object to Qt adapters.
    gui.root = QtRoot()
    gui.log_box = TextAdapter(win.log)
    gui.ai_box = TextAdapter(win.ai)
    gui.scout_status = LabelAdapter(win.scout)
    gui.scout_detail = LabelAdapter(win.scout2)
    gui.lbl_track = LabelAdapter(win.track)
    gui.lbl_time = LabelAdapter(win.time)
    gui.lbl_current_score = LabelAdapter(win.score)
    gui.lbl_cover = LabelAdapter(win.cover)
    gui.seek_bar = SliderAdapter(win.seek)
    gui.hist_box = ListAdapter(win.hist)
    gui.queue_box = ListAdapter(win.queue)
    gui.list_top = ListAdapter(win.top)
    gui.cb_search = ComboAdapter(win.search)
    gui.bg_canvas = None
    gui.db_var = Var(cfg.DB_NAME, callback=lambda v: win.db_combo.setCurrentText(str(v)))
    gui.btn_stop_after = ButtonAdapter(win.stop_after)
    gui.btn_repeat = ButtonAdapter(win.repeat)
    gui.btn_ban = ButtonAdapter(win.ban_btn)
    gui.top_x_label_var = Var(f"Top {cfg.TOP_FAV_ARTISTS_COUNT}", callback=lambda v: win.toplabel.setText(str(v)))
    gui.e_quick_top = EntryAdapter(win.topn)
    gui.e_expert_top = EntryAdapter(win.ex["top"])
    gui.vu_meter_a = win.vua
    gui.vu_meter_b = win.vub
    gui.vibe_tree = TreeAdapter(win.vibetable, numeric_cols=(2, 3))
    gui.vibe_filter_label = LabelAdapter(win.vibefilter)
    gui.vibe_search_var = Var("")
    gui.blacklist_tree = TreeAdapter(win.bantable)
    gui.blacklist_search_var = Var("")

    _log_section = [None]
    def _log_category(message):
        m = str(message).lower()
        if 'spydertrack' in m:
            return 'SpyderTRACK'
        if any(x in m for x in ('judge', 'vibe', 'candidate', 'selected track', 'track selected')):
            return 'Judge / Vibe'
        if any(x in m for x in ('scout', 'discovery', 'last.fm', 'lastfm', 'download')):
            return 'Scout / Discovery'
        if any(x in m for x in ('error', 'warning', '⚠', '❌', 'failed')):
            return 'Warnings'
        return 'General'

    def _append_main_log(message):
        msg = str(message).rstrip('\n')
        if not msg.strip():
            return
        section = _log_category(msg)
        if section != _log_section[0]:
            if _log_section[0] is not None:
                win.log.appendPlainText('')
            win.log.appendPlainText('──── ' + section + ' ────')
            _log_section[0] = section
        # Multi-line messages (e.g. track reports) stay intact.
        win.log.appendPlainText(msg)
        win.log.ensureCursorVisible()

    def _append_ai_log(message):
        win.ai.appendPlainText(str(message))
        win.ai.ensureCursorVisible()

    qt_log_bridge.main_log.connect(_append_main_log)
    qt_log_bridge.ai_log.connect(_append_ai_log)

    def _qt_log_to_gui(msg):
        print(msg)
        if getattr(cfg, "RUNNING", False):
            qt_log_bridge.main_log.emit(str(msg))

    def _qt_ai_log(msg):
        print(f"AI-Log: {msg}")
        if getattr(cfg, "RUNNING", False):
            qt_log_bridge.ai_log.emit(str(msg))

    globals()["log_to_gui"] = _qt_log_to_gui
    globals()["ai_log"] = _qt_ai_log
    E.log_to_gui = _qt_log_to_gui
    E.ai_log = _qt_ai_log

    win.vibesearch.textChanged.connect(lambda t: setattr(gui.vibe_search_var, "value", t))
    win.bansearch.textChanged.connect(lambda t: setattr(gui.blacklist_search_var, "value", t))

    def qt_apply_cover(raw_img, bg_img_source=None):
        try:
            img = raw_img.convert("RGB")
            data = img.tobytes("raw", "RGB")
            q = QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()
            win.raw_cover_pixmap = QPixmap.fromImage(q)
            win.update_cover_display()
            if bg_img_source is not None:
                win.backdrop.set_background(bg_img_source)
        except Exception as exc:
            print(f"Qt cover error: {exc}")

    globals()["_apply_cover_image"] = qt_apply_cover
    E._apply_cover_image = qt_apply_cover

    refresh_vibe_scores_ui(False)
    refresh_blacklist_ui()
    update_top_list_ui()
    update_queue_ui()

    QTimer.singleShot(500, lambda: start_thread(gui_visuals_updater))
    # One persistent Qt pulse for VU, theme audio and waveform visual data.
    # Other controls/threads stay independent; missed frames are not queued.
    win._gui_frame_budget = GuiFrameBudget()
    win._audio_last_change_ms = 40

    def _tick_audio_visuals():
        started = time.perf_counter()
        try:
            peaks_a = mpv_player.get_meter_peaks('A')
            peaks_b = mpv_player.get_meter_peaks('B')
            update_vu_meters(peaks_a, peaks_b)
            manager = getattr(win, 'theme_manager', None)
            if manager is not None:
                artist = str(getattr(cfg, 'CURRENT_ARTIST', '') or '')
                title = str(getattr(cfg, 'CURRENT_TITLE', '') or '')
                labels = getattr(win, 'theme_track_labels', {})
                values = {'artist_title_text': ' - '.join(x for x in (artist, title) if x),
                          'artist_text': artist, 'title_text': title}
                for key, value in values.items():
                    if key in labels and labels[key].text() != value:
                        labels[key].setText(value)
                manager.feed_audio_levels(*peaks_a, repaint=False)
                has_audio_visuals = any(v.spec.get('type') in ('spectrum', 'waveform')
                                        for v in manager.visual_widgets)
                band_visuals = any(v.spec.get('type') == 'vu' and
                                   v.spec.get('vu_band', 'full') != 'full'
                                   for v in manager.visual_widgets)
                needs_bands = (band_visuals or
                               (manager.audio_reactive.get('enabled') and
                                manager.audio_reactive.get('band', 'level') in ('bass', 'mids', 'highs')) or
                               any(o.spec.get('band') in ('bass', 'mids', 'highs')
                                   for o in manager.audio_overlays))
                if needs_bands:
                    manager.feed_frequency_bands(mpv_player.get_frequency_bands(), repaint=False)
                if has_audio_visuals:
                    manager.feed_visual_audio(mpv_player.get_visual_audio(), repaint=False)
                manager.repaint_audio_visuals(visual=has_audio_visuals)
        except Exception as exc:
            if not getattr(win, '_audio_reactive_warned', False):
                print(f'Theme audio feed warning: {exc}')
                win._audio_reactive_warned = True
        finally:
            changed, telemetry = win._gui_frame_budget.tick(started, time.perf_counter())
            # A/B test: adaptive visual throttling disabled. Keep telemetry,
            # but never change Qt timer intervals or compositor cadence.
            # `changed` is intentionally ignored in this test build.
            if telemetry is not None:
                print('[GUI PERF] pulse={pulse_ms}ms callback={mean_callback_ms:.1f}ms '
                      '(max {max_callback_ms:.1f}), GL paint={mean_paint_ms:.1f}ms '
                      '(max {max_paint_ms:.1f}), paint samples={paint_samples}'.format(**telemetry))

    win._audio_visual_timer = QTimer(win)
    win._audio_visual_timer.setTimerType(Qt.TimerType.PreciseTimer)
    win._audio_visual_timer.setInterval(40)
    win._audio_visual_timer.timeout.connect(_tick_audio_visuals)
    QTimer.singleShot(650, win._audio_visual_timer.start)

    # The active MPV deck already tracks its own local file. Only update the
    # RMS waveform when the source changes, not on every 300ms poll.
    def _tick_waveform():
        try:
            active = mpv_player.active_deck
            target = mpv_player._pcm_targets.get(active)
            if getattr(win, '_waveform_source', object()) != target:
                win._waveform_source = target
                win.waveform_rms.set_audio(target)
        except Exception as exc:
            if not getattr(win, '_waveform_warning', False):
                print('Waveform monitor warning:', exc)
                win._waveform_warning = True

    win._waveform_timer = QTimer(win)
    win._waveform_timer.setInterval(300)
    win._waveform_timer.timeout.connect(_tick_waveform)
    win._waveform_timer.start()

    # Themed active-state buttons still use their own inexpensive poll.
    win._transport_state_timer = QTimer(win)
    win._transport_state_timer.setInterval(200)
    win._transport_state_timer.timeout.connect(win._sync_transport_button_states)
    win._transport_state_timer.start()
    win._sync_transport_button_states()
    QTimer.singleShot(250, lambda: start_thread(run_dj_logic))
    QTimer.singleShot(500, lambda: start_thread(run_lastfm_scout))
    QTimer.singleShot(1000, lambda: start_thread(run_song_tag_enrichment))
    QTimer.singleShot(1500, win._autostart_spydertrack)
    threading.Thread(target=_auto_update_music_libraries, daemon=True).start()

    win.show()
    # The theme manager scales clock typography against the actual canvas size.
    # Do not restore the raw JSON point size here: that would overwrite scaling
    # and leave the clock clipped until the next window resize.
    QTimer.singleShot(0, win.theme_manager.reflow)
    QTimer.singleShot(200, win.theme_manager.reflow)
    # The initial logo was scaled before Qt had calculated the final cover
    # panel size. Refresh once the visible window has been laid out.
    QTimer.singleShot(0, win.update_cover_display)
    QTimer.singleShot(200, win.update_cover_display)
    QTimer.singleShot(0, win.align_genres_to_search)
    QTimer.singleShot(200, win.align_genres_to_search)
    try:
        rc = app.exec()
    finally:
        # Covers returns from the Qt event loop that bypass closeEvent().
        _shutdown_owned_resources(win)
    return rc
