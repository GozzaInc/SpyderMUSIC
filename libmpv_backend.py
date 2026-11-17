"""In-process libmpv backend for SpyderMUSIC M4.

No mpv.exe, subprocess or named pipes. Uses the tested Windows x64 libmpv
C API; keep the DLL next to the program (or inside its PyInstaller bundle).
"""
import ctypes as C
import json
import math
import os
import sys
import threading
from pathlib import Path

MPV_FORMAT_STRING = 1
MPV_FORMAT_FLAG = 3
MPV_FORMAT_DOUBLE = 5


class LibMPVError(RuntimeError):
    pass


class LibMPV:
    def __init__(self, base_dir):
        # Source mode: DLL beside the .py files.
        # PyInstaller OneDir: sys._MEIPASS points at the runtime folder.
        # PyInstaller OneFile: sys._MEIPASS points at the extraction folder.
        # Prefer the bundled copy in frozen mode, not a stray DLL beside the EXE.
        filename = 'libmpv_windows-x86_64.dll'
        names = (filename, 'libmpv.dll')
        bundle = getattr(sys, '_MEIPASS', None)
        roots = []
        if getattr(sys, 'frozen', False) and bundle:
            roots.append(Path(bundle))
        roots.extend((Path(base_dir), Path(__file__).resolve().parent))
        candidates = []
        for root in roots:
            for name in names:
                candidate = root / name
                if candidate not in candidates:
                    candidates.append(candidate)
        dll_path = next((p for p in candidates if p.is_file()), None)
        if dll_path is None:
            raise LibMPVError('libmpv DLL is missing. Searched: ' + ', '.join(map(str, candidates)))
        self.dll_path = dll_path
        self.lib = C.CDLL(str(dll_path))
        self._bind('mpv_create', C.c_void_p)
        self._bind('mpv_initialize', C.c_int, C.c_void_p)
        self._bind('mpv_terminate_destroy', None, C.c_void_p)
        self._bind('mpv_set_option_string', C.c_int, C.c_void_p, C.c_char_p, C.c_char_p)
        self._bind('mpv_command', C.c_int, C.c_void_p, C.POINTER(C.c_char_p))
        self._bind('mpv_get_property', C.c_int, C.c_void_p, C.c_char_p, C.c_int, C.c_void_p)
        self._bind('mpv_set_property', C.c_int, C.c_void_p, C.c_char_p, C.c_int, C.c_void_p)
        self._bind('mpv_error_string', C.c_char_p, C.c_int)
        self._bind('mpv_free', None, C.c_void_p)

    def _bind(self, name, result, *args):
        fn = getattr(self.lib, name)
        fn.restype, fn.argtypes = result, list(args)

    def error(self, code):
        raw = self.lib.mpv_error_string(code)
        return raw.decode('utf-8', 'replace') if raw else str(code)

    def create_deck(self, name):
        return Deck(self, name)


class Deck:
    def __init__(self, backend, name):
        self.backend = backend
        self.name = name
        self.lock = threading.RLock()
        self.handle = backend.lib.mpv_create()
        if not self.handle:
            raise LibMPVError(f'Deck {name}: mpv_create failed')
        self.closed = False
        try:
            for key, value in [('vid', 'no'), ('audio-display', 'no'),
                               ('terminal', 'no'), ('idle', 'yes'),
                               ('volume', '0')]:
                rc = backend.lib.mpv_set_option_string(self.handle, key.encode(), value.encode())
                self._check(rc, 'option ' + key)
            self._check(backend.lib.mpv_initialize(self.handle), 'initialize')
            self.command('af', 'add', '@spydervu:lavfi=[astats=metadata=1:reset=1]')
        except Exception:
            self.close()
            raise

    def _check(self, rc, operation):
        if rc < 0:
            raise LibMPVError(f'Deck {self.name}: {operation}: {self.backend.error(rc)}')

    def command(self, *args):
        with self.lock:
            if self.closed:
                raise LibMPVError(f'Deck {self.name} already closed')
            argv = (C.c_char_p * (len(args) + 1))(*(str(a).encode('utf-8') for a in args), None)
            self._check(self.backend.lib.mpv_command(self.handle, argv), ' '.join(map(str, args)))

    def set(self, name, value):
        with self.lock:
            if isinstance(value, bool):
                data = C.c_int(int(value))
                fmt = MPV_FORMAT_FLAG
            elif isinstance(value, (int, float)):
                data = C.c_double(float(value))
                fmt = MPV_FORMAT_DOUBLE
            else:
                data = C.c_char_p(str(value).encode('utf-8'))
                fmt = MPV_FORMAT_STRING
            self._check(self.backend.lib.mpv_set_property(self.handle, name.encode(), fmt, C.byref(data)), 'set ' + name)

    def number(self, name):
        with self.lock:
            if self.closed:
                return None
            value = C.c_double()
            rc = self.backend.lib.mpv_get_property(self.handle, name.encode(), MPV_FORMAT_DOUBLE, C.byref(value))
            return value.value if rc >= 0 and math.isfinite(value.value) else None

    def string(self, name):
        with self.lock:
            if self.closed:
                return None
            p = C.c_char_p()
            rc = self.backend.lib.mpv_get_property(self.handle, name.encode(), MPV_FORMAT_STRING, C.byref(p))
            if rc < 0:
                return None
            try:
                return p.value.decode('utf-8', 'replace') if p.value else ''
            finally:
                self.backend.lib.mpv_free(C.cast(p, C.c_void_p))

    def flag(self, name):
        with self.lock:
            if self.closed:
                return None
            value = C.c_int()
            rc = self.backend.lib.mpv_get_property(self.handle, name.encode(), MPV_FORMAT_FLAG, C.byref(value))
            return bool(value.value) if rc >= 0 else None

    def peaks(self):
        raw = self.string('af-metadata/spydervu')
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return (float(data['lavfi.astats.1.Peak_level']),
                    float(data['lavfi.astats.2.Peak_level']))
        except (ValueError, TypeError, KeyError):
            return None

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.backend.lib.mpv_terminate_destroy(self.handle)
                self.handle = None
