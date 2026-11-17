"""Prevent duplicate Windows instances and activate the existing window.

Uses named, per-logon-session Windows kernel objects. The mutex guarantees
there is only one primary process; an auto-reset event notifies the primary
when another launcher is opened. No daemon, ports or third-party modules.
"""

import ctypes
import sys

ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258

DEFAULT_NAME = r"Local\NotQuiteEverything"


class SingleInstance:
    def __init__(self, name: str = DEFAULT_NAME):
        self.mutex_name = name + ".mutex"
        self.event_name = name + ".activate"
        self._kernel32 = None
        self._mutex = None
        self._event = None
        self._primary = False

    def claim_or_activate(self) -> bool:
        """Return True for the primary process.

        A second process signals the existing primary and returns False.
        The event is created before the mutex so that a near-simultaneous
        second launch can signal even while the primary starts up.
        """
        if sys.platform != "win32":
            return True
        if self._mutex is not None:
            return self._primary

        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateEventW.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR
        ]
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.CreateMutexW.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR
        ]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.SetEvent.argtypes = [wintypes.HANDLE]
        kernel.SetEvent.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel32 = kernel

        try:
            self._event = kernel.CreateEventW(None, False, False, self.event_name)
            if not self._event:
                raise ctypes.WinError(ctypes.get_last_error())
            self._mutex = kernel.CreateMutexW(None, False, self.mutex_name)
            if not self._mutex:
                raise ctypes.WinError(ctypes.get_last_error())
            existing = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
            if existing:
                if not kernel.SetEvent(self._event):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.close()
                return False
            self._primary = True
            return True
        except BaseException:
            self.close()
            raise

    def activation_requested(self) -> bool:
        """Nonblocking poll; call periodically from the Tk main thread."""
        if not self._primary or not self._event:
            return False
        status = self._kernel32.WaitForSingleObject(self._event, 0)
        if status == WAIT_OBJECT_0:
            return True
        if status == WAIT_TIMEOUT:
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        """Release both kernel object handles; safe to call repeatedly."""
        if self._kernel32 is not None:
            if self._mutex:
                self._kernel32.CloseHandle(self._mutex)
            if self._event:
                self._kernel32.CloseHandle(self._event)
        self._mutex = None
        self._event = None
        self._primary = False
