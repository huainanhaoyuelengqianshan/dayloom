"""Small process-level facts: pid file, pause flag, "is it running"."""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

from . import paths

#: A daemon that has not touched its heartbeat for this long is presumed dead.
STALE_HEARTBEAT_SECONDS = 180.0


def write_pid(home: Path, pid: int | None = None) -> None:
    paths.pid_file(home).write_text(str(pid or os.getpid()), encoding="ascii")


def read_pid(home: Path) -> int | None:
    try:
        raw = paths.pid_file(home).read_text(encoding="ascii").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def clear_pid(home: Path) -> None:
    with contextlib.suppress(OSError):
        paths.pid_file(home).unlink()


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def running_pid(home: Path) -> int | None:
    """Pid of a *live* daemon, or ``None``.

    A bare pid check is not enough on Windows: a crashed daemon leaves the pid
    file behind and the number can be reused by an unrelated process, which
    made dayloom think it was recording when it was not. The daemon also
    touches a heartbeat file; a stale heartbeat means "not really running".
    """
    pid = read_pid(home)
    if not pid or not pid_alive(pid):
        return None
    age = heartbeat_age(home)
    if age is not None and age > STALE_HEARTBEAT_SECONDS:
        return None
    return pid


def touch_heartbeat(home: Path) -> None:
    with contextlib.suppress(OSError):
        paths.heartbeat_file(home).write_text(str(int(time.time())), encoding="ascii")


def heartbeat_age(home: Path) -> float | None:
    """Seconds since the last heartbeat, or ``None`` if there is no file."""
    try:
        raw = paths.heartbeat_file(home).read_text(encoding="ascii").strip()
        return max(0.0, time.time() - float(raw))
    except (OSError, ValueError):
        return None


def is_paused(home: Path) -> bool:
    return paths.paused_flag(home).exists()


def set_paused(home: Path, paused: bool) -> None:
    flag = paths.paused_flag(home)
    if paused:
        flag.write_text("paused\n", encoding="utf-8")
    else:
        with contextlib.suppress(OSError):
            flag.unlink()


def acquire_instance_lock(home: Path) -> object | None:
    """Take an exclusive lock on the GUI instance file.

    Returns an open handle (keep it alive for the process lifetime) or ``None``
    when another instance already holds it. Uses an OS-level lock rather than a
    pid file so a crashed instance cannot lock the user out.
    """
    handle = open(home / ".gui.lock", "a+b")  # noqa: SIM115 - held for process life
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - Windows is the target platform
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle
