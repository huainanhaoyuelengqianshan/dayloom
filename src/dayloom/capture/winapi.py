"""Windows-specific plumbing: foreground window, DPI, idle and lock state.

Everything here is ctypes-only on purpose. Shelling out to PowerShell every
second would cost more CPU than the screenshots themselves, and a GUI
automation dependency (pywin32) is exactly the kind of thing antivirus
products like to quarantine.

Two details that are easy to get wrong on Windows and are handled here:

* **DPI.** On a mixed-DPI setup (this machine: 250% + 180%) a DPI-unaware
  process gets virtualized coordinates and captures the wrong rectangle.
  We ask for PER_MONITOR_AWARE_V2 before touching any window API.
* **Frame bounds.** ``GetWindowRect`` includes invisible resize borders
  (typically ~8px of transparent margin) on Windows 10/11;
  ``DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`` does not.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

WINDOWS = sys.platform == "win32"

if WINDOWS:  # pragma: no cover - exercised on Windows only
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    user32.GetLastInputInfo.restype = wintypes.BOOL
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetTickCount.restype = wintypes.DWORD

    dwmapi.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DESKTOP_SWITCHDESKTOP = 0x0100
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

_dpi_mode: str | None = None


class CaptureUnsupported(RuntimeError):
    pass


def _require_windows() -> None:
    if not WINDOWS:
        raise CaptureUnsupported("dayloom 的采集模块目前只支持 Windows")


def enable_dpi_awareness() -> str:
    """Make this process per-monitor DPI aware. Idempotent; safe to call twice."""
    global _dpi_mode
    if _dpi_mode is not None:
        return _dpi_mode
    if not WINDOWS:
        _dpi_mode = "unsupported"
        return _dpi_mode
    try:
        ctx = ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        if user32.SetProcessDpiAwarenessContext(ctx):
            _dpi_mode = "per-monitor-v2"
            return _dpi_mode
    except (AttributeError, OSError):
        pass
    try:
        shcore = ctypes.WinDLL("shcore")
        if shcore.SetProcessDpiAwareness(2) == 0:
            _dpi_mode = "per-monitor-v1"
            return _dpi_mode
    except (AttributeError, OSError):
        pass
    _dpi_mode = "system"
    return _dpi_mode


@dataclass(slots=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    exe: str
    app: str
    rect: tuple[int, int, int, int] | None

    def key(self) -> tuple[str, str]:
        return (self.app, self.title)


def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _process_exe(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        capacity = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(capacity.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(capacity)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
    )
    if hr != 0 and not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return (rect.left, rect.top, width, height)


def foreground_window() -> WindowInfo | None:
    """The window the user is looking at, or None (desktop / lock screen)."""
    _require_windows()
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    if not user32.IsWindowVisible(hwnd):
        return None
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = _process_exe(pid.value)
    app = Path(exe).stem.lower() if exe else ""
    return WindowInfo(
        hwnd=int(hwnd),
        title=_window_text(hwnd),
        pid=int(pid.value),
        exe=exe,
        app=app,
        rect=_window_rect(hwnd),
    )


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input (session-wide)."""
    _require_windows()
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    tick = kernel32.GetTickCount()
    # dwTime is a 32-bit tick count, so wrap the subtraction the same way.
    return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000.0


def is_locked() -> bool:
    """True when the input desktop is not ours (lock screen / UAC secure desktop).

    ``OpenInputDesktop`` fails while the workstation is locked because the
    input desktop is Winlogon's. A handle means we can see the user's desktop.
    False positives would stop capture entirely, which ``dayloom doctor``
    reports, so this is checked there before anyone trusts it.
    """
    _require_windows()
    handle = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not handle:
        return True
    user32.CloseDesktop(handle)
    return False


def screen_metrics() -> dict[str, int]:
    """Virtual screen metrics in physical pixels (after DPI awareness)."""
    _require_windows()
    gsm = user32.GetSystemMetrics
    return {
        "x": int(gsm(76)),  # SM_XVIRTUALSCREEN
        "y": int(gsm(77)),  # SM_YVIRTUALSCREEN
        "width": int(gsm(78)),  # SM_CXVIRTUALSCREEN
        "height": int(gsm(79)),  # SM_CYVIRTUALSCREEN
        "monitors": int(gsm(80)),  # SM_CMONITORS
    }
