"""Windows shell shortcuts: the Start Menu entry (and an optional desktop icon).

No admin rights, no installer: a per-user ``.lnk`` in the Start Menu folder is
enough for the app to show up in the search box, and it is the same file the
user would delete to "uninstall" it.

The shortcut target differs by layout: a source checkout launches
``pythonw -m dayloom.gui``, a frozen build launches ``dayloom-gui.exe`` next to
it. ``gui_command`` is the single place that knows the difference.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import paths

SHORTCUT_NAME = "dayloom.lnk"
DESKTOP_NAME = "dayloom.lnk"
CREATE_NO_WINDOW = 0x08000000


@dataclass(slots=True)
class ShortcutStatus:
    start_menu: bool
    desktop: bool
    target: str = ""

    @property
    def installed(self) -> bool:
        return self.start_menu or self.desktop


def _base(kind: str) -> Path:
    appdata = os.environ.get("APPDATA")
    roaming = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    if kind == "start":
        return roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def start_menu_path() -> Path:
    return _base("start") / SHORTCUT_NAME


def desktop_path() -> Path:
    return _base("desktop") / DESKTOP_NAME


def pythonw() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        windowless = exe.with_name("pythonw.exe")
        if windowless.exists():
            return str(windowless)
    return str(exe)


def gui_command() -> list[str]:
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        windowed = exe.with_name("dayloom-gui.exe")
        return [str(windowed if windowed.exists() else exe)]
    return [pythonw(), "-m", "dayloom.gui"]


def icon_location() -> str:
    found = paths.asset("dayloom.ico")
    return str(found) if found else ""


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def create_shortcut(
    link: Path,
    *,
    target: str,
    arguments: str = "",
    workdir: Path | None = None,
    icon: str = "",
    description: str = "dayloom screen activity diary",
) -> bool:
    """Create a .lnk through WScript.Shell (no pywin32 dependency)."""
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$shell = New-Object -ComObject WScript.Shell",
        f"$link = $shell.CreateShortcut({_ps_quote(str(link))})",
        f"$link.TargetPath = {_ps_quote(target)}",
        f"$link.Arguments = {_ps_quote(arguments)}",
        f"$link.WorkingDirectory = {_ps_quote(str(workdir or Path.home()))}",
        f"$link.Description = {_ps_quote(description)}",
        "$link.WindowStyle = 1",
    ]
    if icon:
        lines.append(f"$link.IconLocation = {_ps_quote(icon + ',0')}")
    lines.append("$link.Save()")

    handle, temp_name = tempfile.mkstemp(suffix=".ps1", prefix="dayloom-shortcut-")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8-sig") as fh:
            fh.write("\n".join(lines))
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(temp),
            ],
            capture_output=True,
            timeout=45,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode == 0 and link.exists()
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        temp.unlink(missing_ok=True)


def _install_one(link: Path) -> bool:
    command = gui_command()
    target = command[0]
    arguments = subprocess.list2cmdline(command[1:]) if len(command) > 1 else ""
    link.parent.mkdir(parents=True, exist_ok=True)
    return create_shortcut(
        link,
        target=target,
        arguments=arguments,
        workdir=Path.home(),
        icon=icon_location(),
    )


def install(*, desktop: bool = False) -> tuple[bool, str]:
    made: list[str] = []
    if not _install_one(start_menu_path()):
        return False, "创建开始菜单快捷方式失败"
    made.append("开始菜单")
    if desktop and _install_one(desktop_path()):
        made.append("桌面")
    return True, "已创建快捷方式：" + "、".join(made)


def remove(*, desktop: bool = False) -> tuple[bool, str]:
    removed: list[str] = []
    for path, label in ((start_menu_path(), "开始菜单"), (desktop_path(), "桌面")):
        if not desktop and label == "桌面":
            continue
        if path.exists():
            try:
                path.unlink()
                removed.append(label)
            except OSError:
                pass
    if not removed:
        return True, "本来就没有快捷方式"
    return True, "已移除：" + "、".join(removed)


def read_shortcut(link: Path) -> tuple[str, str]:
    """``(target, arguments)`` of a .lnk, or ``("", "")`` if it cannot be read.

    Needed because the *registered* command depends on the build that created
    it, not on the build asking the question.
    """
    if not link.exists():
        return "", ""
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$shell = New-Object -ComObject WScript.Shell",
        f"$link = $shell.CreateShortcut({_ps_quote(str(link))})",
        'Write-Output ("{0}`n{1}" -f $link.TargetPath, $link.Arguments)',
    ]
    handle, temp_name = tempfile.mkstemp(suffix=".ps1", prefix="dayloom-read-")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8-sig") as fh:
            fh.write("\n".join(lines))
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(temp),
            ],
            capture_output=True,
            text=True,
            # Windows PowerShell writes to a pipe in the OEM code page.
            encoding="oem",
            errors="replace",
            timeout=45,
            creationflags=CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            return "", ""
        parts = (proc.stdout or "").splitlines()
        target = parts[0].strip() if parts else ""
        arguments = parts[1].strip() if len(parts) > 1 else ""
        return target, arguments
    except (OSError, subprocess.SubprocessError):
        return "", ""
    finally:
        temp.unlink(missing_ok=True)


def status() -> ShortcutStatus:
    start = start_menu_path()
    desktop = desktop_path()
    return ShortcutStatus(
        start_menu=start.exists(),
        desktop=desktop.exists(),
        target=str(start if start.exists() else desktop),
    )


def describe() -> str:
    state = status()
    if state.installed:
        where = "、".join(
            name
            for name, flag in (("开始菜单", state.start_menu), ("桌面", state.desktop))
            if flag
        )
        return f"快捷方式：已安装（{where}）\n  文件：{state.target}"
    return (
        "快捷方式：未安装\n"
        "  安装：dayloom shortcut install\n"
        f"  位置：{start_menu_path()}"
    )
