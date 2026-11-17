"""Unprivileged Windows recursive file notifications using only ctypes.

ReadDirectoryChangesW watches each configured root with bWatchSubtree=TRUE.
No NTFS raw-volume access or administrator permissions are needed, though
directory permissions and network shares can prevent particular watches.
Periodic full reconciliation remains enabled as a reliability fallback.
"""

import ctypes
import logging
import os
import struct
import sys
import threading

log = logging.getLogger(__name__)

FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x0001
FILE_SHARE_WRITE = 0x0002
FILE_SHARE_DELETE = 0x0004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
NOTIFY_FILTER = 0x0000005F  # names, attrs, sizes, writes, creation
ERROR_OPERATION_ABORTED = 995
BUFFER_SIZE = 64 * 1024


class WindowsDirectoryWatcher:
    def __init__(self, roots: list[str], on_changes, on_reconcile=None, on_error=None):
        self.roots = list(roots)
        self.on_changes = on_changes
        self.on_reconcile = on_reconcile or (lambda root: None)
        self.on_error = on_error or (lambda message: None)
        self._stop = threading.Event()
        self._handles = {}
        self._lock = threading.Lock()
        self._threads = []

    def start(self) -> None:
        if sys.platform != "win32":
            self.on_error("Live monitoring requires Windows; periodic scans remain enabled.")
            return
        for root in self.roots:
            thread = threading.Thread(target=self._watch, args=(root,), daemon=True,
                                      name="DirectoryWatcher:" + root)
            self._threads.append(thread)
            thread.start()

    def _watch(self, root: str) -> None:
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.ReadDirectoryChangesW.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p, ctypes.c_void_p
        ]
        kernel.ReadDirectoryChangesW.restype = wintypes.BOOL
        kernel.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel.CancelIoEx.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL

        handle = kernel.CreateFileW(
            root, FILE_LIST_DIRECTORY,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None
        )
        if handle == ctypes.c_void_p(-1).value or handle is None:
            self.on_error("Cannot monitor %s: Windows error %s" % (root, ctypes.get_last_error()))
            return
        with self._lock:
            self._handles[root] = (kernel, handle)
        buffer = ctypes.create_string_buffer(BUFFER_SIZE)
        received = wintypes.DWORD()
        try:
            while not self._stop.is_set():
                success = kernel.ReadDirectoryChangesW(
                    handle, buffer, BUFFER_SIZE, True, NOTIFY_FILTER,
                    ctypes.byref(received), None, None
                )
                if self._stop.is_set():
                    break
                if not success:
                    error = ctypes.get_last_error()
                    if error != ERROR_OPERATION_ABORTED:
                        self.on_error("Live watch stopped for %s: Windows error %s" % (root, error))
                        self.on_reconcile(root)
                    break
                if not received.value:
                    # Buffer overflow: notifications lost; fall back to a full reconciliation.
                    self.on_reconcile(root)
                    continue
                paths = []
                offset = 0
                while offset + 12 <= received.value:
                    next_offset, action, length = struct.unpack_from("<III", buffer, offset)
                    if length % 2 or offset + 12 + length > received.value:
                        self.on_reconcile(root)
                        break
                    relative = buffer.raw[offset + 12:offset + 12 + length].decode(
                        "utf-16-le", errors="replace"
                    )
                    paths.append(os.path.normpath(os.path.join(root, relative)))
                    if not next_offset:
                        break
                    offset += next_offset
                if paths:
                    self.on_changes(paths)
        except Exception:
            log.exception("Live filesystem watcher failed for %s", root)
            if not self._stop.is_set():
                self.on_reconcile(root)
        finally:
            with self._lock:
                self._handles.pop(root, None)
            kernel.CloseHandle(handle)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            handles = list(self._handles.values())
        for kernel, handle in handles:
            try:
                kernel.CancelIoEx(handle, None)
            except OSError:
                pass
