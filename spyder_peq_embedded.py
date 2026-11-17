"""SpyderMUSIC PEQ bridge: independent, theme-placeable panel and dual-deck DSP."""
from pathlib import Path
import json
from PySide6.QtCore import Qt, QTimer, QSignalBlocker
from spyder_peq_compact_filters import Window


class DualDeckProxy:
    """Send the same PEQ filter commands to both live libmpv decks."""
    def __init__(self, engine):
        self.engine = engine

    def command(self, *args):
        errors = []
        for name in ('A', 'B'):
            try:
                self.engine.decks[name]['player'].command(*args)
            except Exception as exc:
                errors.append(f'{name}: {exc}')
        if errors:
            raise RuntimeError('PEQ update: ' + '; '.join(errors))

    def close(self):
        # The host engine owns the decks. Never close them from a widget.
        pass


class SpyderPEQWidget(Window):
    """A QWidget-compatible QMainWindow child registered as optional theme panel.

    Geometry and z-order belong to ThemeManager, not to the PEQ.
    EQ state is independent of themes and shared across both decks.
    """
    def __init__(self, engine, parent=None):
        self._state_path = Path(__file__).resolve().parent / 'data' / 'peq_state.json'
        super().__init__(DualDeckProxy(engine))
        self.setParent(parent)
        self.setObjectName('peq_panel')
        # Avoid the standalone widget stylesheet overriding the active QSS theme.
        self.setStyleSheet('')
        self.setWindowFlags(Qt.Widget)
        self.setMinimumSize(0, 0)
        self.editor.hide()  # In-theme presentation defaults to compact Mini.
        self.display_mode = 'mini'
        self.mode_action.setText('Max mode')
        # Load the complete previous session, including DSP enable and edit lock.
        # Keep live deck changes deferred until Qt finishes constructing the UI.
        self.timer.stop()
        locked = False
        if self._state_path.exists():
            try:
                state = json.loads(self._state_path.read_text(encoding='utf-8'))
                self.peq.load(self._state_path)
                locked = bool(state.get('edit_locked', False))
            except Exception as exc:
                self.status.setText('PEQ preset load failed: ' + str(exc))
                self.status.show()
                self.peq.enabled = False
        self.plot.set_eq_enabled(self.peq.enabled)
        self.select_band(self.selected)
        self.plot.set_data(self.peq.bands, self.peq.preamp, self.peq.enabled)
        with QSignalBlocker(self.lock_action):
            self.lock_action.setChecked(locked)
        super().set_edit_locked(locked)
        self.timer.stop()
        # Apply restored enable/bypass to both decks once the main window is ready.
        QTimer.singleShot(0, self.apply)

    def _save_session_state(self):
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        state = dict(bands=self.peq.bands, preamp=self.peq.preamp,
                     enabled=self.peq.enabled, edit_locked=self.plot.edit_locked)
        self._state_path.write_text(json.dumps(state, indent=2), encoding='utf-8')

    def set_edit_locked(self, locked):
        super().set_edit_locked(locked)
        try:
            self._save_session_state()
        except OSError as exc:
            self.status.setText('PEQ state save failed: ' + str(exc))
            self.status.show()

    def toggle_mode(self):
        # ThemeManager owns geometry. Never resize or move a themed panel.
        if self.display_mode == 'mini':
            self.display_mode = 'max'
            self.editor.show()
            self.mode_action.setText('Mini mode')
        else:
            self.display_mode = 'mini'
            self.editor.hide()
            self.mode_action.setText('Max mode')
        self.plot.update()

    def apply(self):
        try:
            super().apply()
        except Exception as exc:
            # Roll back the PEQ and keep the original @spydervu chain intact.
            self.peq.enabled = False
            self.plot.set_eq_enabled(False)
            try: self.peq.apply()
            except Exception: pass
            self.status.setText('PEQ disabled after filter error: ' + str(exc))
            self.status.show()
            return
        try:
            self._save_session_state()
        except OSError as exc:
            self.status.setText('PEQ state save failed: ' + str(exc))
            self.status.show()

    def closeEvent(self, event):
        self.timer.stop()
        event.accept()
