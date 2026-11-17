"""Start and stop a companion SpyderTRACK instance owned by SpyderMUSIC.

The companion can be a PyInstaller one-file executable. Such executables may
have a bootloader parent *and* a worker child; terminating only Popen's PID
can leave the real application running.
"""

import os
import signal
import subprocess
import sys
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None


class _WindowsProcessJob:
    """Keep the complete Windows child-process tree in a kill-on-close job.

    A job also cleans up the companion if SpyderMUSIC exits unexpectedly. It
    avoids relying on psutil being bundled into a frozen release.
    """

    def __init__(self, process):
        import ctypes
        from ctypes import wintypes

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ('PerProcessUserTimeLimit', ctypes.c_int64),
                ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', wintypes.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t),
                ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ('BasicLimitInformation', _BasicLimit),
                ('IoInfo', _IoCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t),
            ]

        class _BasicAccounting(ctypes.Structure):
            _fields_ = [
                ('TotalUserTime', ctypes.c_int64),
                ('TotalKernelTime', ctypes.c_int64),
                ('ThisPeriodTotalUserTime', ctypes.c_int64),
                ('ThisPeriodTotalKernelTime', ctypes.c_int64),
                ('TotalPageFaultCount', wintypes.DWORD),
                ('TotalProcesses', wintypes.DWORD),
                ('ActiveProcesses', wintypes.DWORD),
                ('TotalTerminatedProcesses', wintypes.DWORD),
            ]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     ctypes.c_void_p, wintypes.DWORD,
                                                     ctypes.c_void_p]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL

        self._kernel = kernel
        self._ctypes = ctypes
        self._accounting_type = _BasicAccounting
        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = _ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(self._handle, 9,
                                                   ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.AssignProcessToJobObject(self._handle, process._handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def is_running(self):
        if not self._handle:
            return False
        info = self._accounting_type()
        if not self._kernel.QueryInformationJobObject(self._handle, 1,
            self._ctypes.byref(info), self._ctypes.sizeof(info), None):
            # A failed query should never be mistaken for proof of shutdown.
            return True
        return info.ActiveProcesses > 0

    def close(self):
        handle, self._handle = self._handle, None
        if handle:
            # KILL_ON_JOB_CLOSE stops the worker even if its bootloader
            # parent has already exited. TerminateJobObject expedites it.
            self._kernel.TerminateJobObject(handle, 1)
            self._kernel.CloseHandle(handle)


class SpyderTrackControl:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.child = None
        self._job = None
        self._process_group = None
        self.external_pid = None
        self.script = self._find_script()

    def _find_script(self):
        """Find companion in common source and packaged application layouts."""
        roots = [self.folder, Path(__file__).resolve().parent,
                 Path(sys.argv[0]).resolve().parent, Path.cwd(),
                 Path(sys.executable).resolve().parent]
        if getattr(sys, '_MEIPASS', None):
            roots.append(Path(sys._MEIPASS).resolve().parent)
        candidates = []
        seen = set()
        for root in roots:
            try:
                root = root.resolve()
                key = os.path.normcase(str(root))
                if not root.is_dir() or key in seen:
                    continue
                seen.add(key)
                folders = [root]
                for sub in ('SpyderTRACK', 'spydertrack', 'dist', 'bin'):
                    folder = root / sub
                    if folder.is_dir():
                        folders.append(folder)
                for folder in folders:
                    for file in folder.iterdir():
                        if file.is_file() and file.name.casefold() in ('spydertrack.exe', 'spydetrack.exe'):
                            candidates.append(file.resolve())
            except OSError:
                continue
        candidates.sort(key=lambda x: (x.suffix.lower() != '.exe', len(x.parts)))
        return candidates[0] if candidates else None

    def searched_locations(self):
        return ', '.join(str(p) for p in dict.fromkeys([
            self.folder, Path(__file__).resolve().parent,
            Path(sys.argv[0]).resolve().parent, Path.cwd(),
            Path(sys.executable).resolve().parent,
        ]))

    def _external_process(self):
        if psutil is None or self.script is None:
            return None
        target = os.path.normcase(os.path.abspath(str(self.script)))
        for proc in psutil.process_iter(['pid', 'exe', 'cmdline']):
            try:
                if proc.pid == os.getpid():
                    continue
                # Native Windows .exe processes put the executable in argv[0],
                # not argv[1]. Check exe() as well as the complete command line.
                paths = ([proc.info.get('exe')] if proc.info.get('exe') else [])
                paths.extend(proc.info.get('cmdline') or [])
                for arg in paths:
                    if not str(arg).lower().endswith(('spydertrack.exe', 'spydetrack.exe')):
                        continue
                    candidate = str(arg)
                    if not os.path.isabs(candidate):
                        try:
                            candidate = os.path.join(proc.cwd(), candidate)
                        except (OSError, psutil.Error):
                            continue
                    if os.path.normcase(os.path.abspath(candidate)) == target:
                        return proc
            except (OSError, psutil.Error):
                continue
        return None

    def refresh_script(self):
        """Explicit discovery for startup or a deliberate rescan, never GUI polling."""
        self.script = self._find_script()
        return self.script

    def _owned_running(self):
        if self._job is not None:
            return self._job.is_running()
        if self.child is not None and self.child.poll() is None:
            return True
        if self._process_group is not None and os.name != 'nt':
            try:
                os.killpg(self._process_group, 0)
                return True
            except ProcessLookupError:
                pass
        return False

    def status(self):
        # A periodic LED refresh must not scan directories or call os.stat().
        if self.script is None:
            return 'missing'
        if self._owned_running():
            self.external_pid = None
            return 'running'
        # Discard a completed job/group before discovering unrelated processes.
        self._release_handles()
        ext = self._external_process()
        self.external_pid = ext.pid if ext else None
        return 'running' if ext else 'stopped'

    def _release_handles(self):
        if self._job is not None:
            self._job.close()
            self._job = None
        self._process_group = None
        if self.child is not None:
            if self.child.poll() is not None:
                self.child = None

    def start(self):
        if self.status() != 'stopped':
            return self.status() == 'running'
        flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) if os.name == 'nt' else 0
        command = [str(self.script)]
        self.child = subprocess.Popen(
            command, cwd=str(self.script.parent), creationflags=flags,
            start_new_session=(os.name != 'nt'),
        )
        if os.name == 'nt':
            try:
                self._job = _WindowsProcessJob(self.child)
            except Exception as exc:
                # Still manageable via taskkill /T, without a new dependency.
                print(f'⚠️ SpyderTRACK process job unavailable; using taskkill fallback: {exc}')
        else:
            self._process_group = self.child.pid
        return self.status() == 'running'

    def stop(self):
        if not self._owned_running():
            self._release_handles()
            return False   # external process: do not touch
        if self._job is not None:
            self._job.close()
            self._job = None
        elif os.name == 'nt' and self.child is not None:
            # Kill the complete PyInstaller bootloader/worker tree if job
            # association was rejected by Windows (e.g. restrictive host job).
            try:
                subprocess.run(['taskkill', '/PID', str(self.child.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=3, check=False,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except (OSError, subprocess.TimeoutExpired):
                pass
        elif self._process_group is not None:
            try:
                os.killpg(self._process_group, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self.child is not None and self.child.poll() is None:
            try:
                self.child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.child.kill()
                try:
                    self.child.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
        self.child = None
        self._process_group = None
        return True

    def shutdown(self):
        """Idempotent: safe for closeEvent and QApplication.aboutToQuit."""
        self.stop()
