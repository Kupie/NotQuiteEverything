"""Windows system-tray integration using only the Python standard library.

The icon owns a hidden native message window on a dedicated thread. No Tk
calls are made from that thread: actions are delivered to a thread-safe queue
that the Tk main loop polls. This avoids subclassing Tk's window procedure.
"""

import ctypes
from ctypes import wintypes
import logging
import os
import queue
import sys
import threading

logger = logging.getLogger(__name__)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_NULL = 0x0000
WM_APP = 0x8000
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
CALLBACK_MESSAGE = WM_APP + 1

NIF_MESSAGE = 0x0001
NIF_ICON = 0x0002
NIF_TIP = 0x0004
NIM_ADD = 0
NIM_DELETE = 2
MF_STRING = 0
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
IDI_APPLICATION = 32512
MENU_RESTORE = 101
MENU_EXIT = 102


class WindowsTrayIcon:
    """A tray icon with Restore and Exit actions.

    read events from .events (values: "restore", "exit", ("error", message)).
    is_available indicates whether a tray icon was successfully registered.
    """

    def __init__(self, title: str = "NotQuiteEverything"):
        self.title = title
        self.events: queue.Queue = queue.Queue()
        self._available = threading.Event()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self._nid = None
        self._wndproc_callback = None
        self._user32 = None
        self._shell32 = None

    @property
    def is_available(self) -> bool:
        return self._available.is_set()

    def start(self) -> None:
        if sys.platform != "win32" or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="NQETray", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_requested.set()
        hwnd = self._hwnd
        if hwnd and self._user32 is not None:
            try:
                self._user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except (OSError, ValueError):
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        try:
            self._message_loop()
        except Exception as exc:
            logger.exception("Unable to create system tray icon")
            self.events.put(("error", "Tray icon unavailable: " + str(exc)))
        finally:
            self._available.clear()
            self._hwnd = None
            self._wndproc_callback = None

    def _message_loop(self) -> None:
        from ctypes import wintypes

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
            ctypes.c_size_t, ctypes.c_ssize_t
        )

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HCURSOR),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeoutOrVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID),
                ("hBalloonIcon", wintypes.HICON),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._user32, self._shell32 = user32, shell32

        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.WORD
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t
        ]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t
        ]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
        ]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        user32.LoadIconW.restype = wintypes.HICON
        user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterWindowMessageW.restype = wintypes.UINT
        user32.CreatePopupMenu.argtypes = []
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, ctypes.c_void_p
        ]
        user32.TrackPopupMenu.restype = wintypes.UINT
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

        taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

        @WNDPROC
        def wndproc(hwnd, message, wparam, lparam):
            if message == CALLBACK_MESSAGE:
                if lparam == WM_LBUTTONDBLCLK:
                    self.events.put("restore")
                elif lparam == WM_RBUTTONUP:
                    self._show_menu()
                return 0
            if message == taskbar_created:
                self._add_icon()
                return 0
            if message == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                self._remove_icon()
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._wndproc_callback = wndproc
        instance = kernel32.GetModuleHandleW(None)
        classname = "NQETrayWindow_%s_%s" % (os.getpid(), id(self))
        klass = WNDCLASSW()
        klass.hInstance = instance
        klass.lpszClassName = classname
        klass.lpfnWndProc = wndproc
        if not user32.RegisterClassW(ctypes.byref(klass)):
            raise ctypes.WinError(ctypes.get_last_error())

        hwnd = user32.CreateWindowExW(
            0, classname, self.title, 0, 0, 0, 0, 0, None, None, instance, None
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hwnd = hwnd

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = CALLBACK_MESSAGE
        nid.szTip = self.title[:127]
        nid.hIcon = user32.LoadIconW(
            None, ctypes.cast(ctypes.c_void_p(IDI_APPLICATION), wintypes.LPCWSTR)
        )
        if not nid.hIcon:
            user32.DestroyWindow(hwnd)
            raise ctypes.WinError(ctypes.get_last_error())
        self._nid = nid
        if not self._add_icon():
            user32.DestroyWindow(hwnd)
            raise ctypes.WinError(ctypes.get_last_error())
        if self._stop_requested.is_set():
            user32.DestroyWindow(hwnd)
            return

        msg = wintypes.MSG()
        while not self._stop_requested.is_set():
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result <= 0:
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _add_icon(self) -> bool:
        if self._nid is None:
            return False
        success = bool(self._shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid)))
        if success:
            self._available.set()
        else:
            self._available.clear()
        return success

    def _remove_icon(self) -> None:
        self._available.clear()
        if self._nid is not None and self._shell32 is not None:
            self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))

    def _show_menu(self) -> None:
        user32 = self._user32
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(menu, MF_STRING, MENU_RESTORE, "Restore NotQuiteEverything")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, MENU_EXIT, "Exit")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self._hwnd)
            selected = user32.TrackPopupMenu(
                menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                point.x, point.y, 0, self._hwnd, None
            )
            if selected == MENU_RESTORE:
                self.events.put("restore")
            elif selected == MENU_EXIT:
                self.events.put("exit")
            user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)
