# -*- mode: python ; coding: utf-8 -*-
"""Two executables in one folder.

* ``dayloom.exe``     console build  — the CLI and the daemon
* ``dayloom-gui.exe`` windowed build — the desktop client

They must be separate: a windowed process has no stdout, so the CLI would go
blind inside it, and a console process would flash a black window when the user
double-clicks the app.
"""

import os

from PyInstaller.utils.hooks import collect_submodules

SRC = os.path.abspath(os.path.join(SPECPATH, "..", "src"))
ENTRY_CLI = os.path.join(SPECPATH, "entry_cli.py")
ENTRY_GUI = os.path.join(SPECPATH, "entry_gui.py")
ICON = os.path.abspath(os.path.join(SPECPATH, "..", "src", "dayloom", "assets", "dayloom.ico"))

hidden = collect_submodules("dayloom")
common = dict(
    pathex=[SRC],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "scipy", "pandas"],
    datas=[(ICON, ".")],
    noarchive=False,
)

a_cli = Analysis([ENTRY_CLI], **common)
a_gui = Analysis([ENTRY_GUI], **common)

pyz_cli = PYZ(a_cli.pure, a_cli.zipped_data)
pyz_gui = PYZ(a_gui.pure, a_gui.zipped_data)

exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="dayloom",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon=ICON,
)

exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="dayloom-gui",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
)

coll = COLLECT(
    exe_cli,
    a_cli.binaries,
    a_cli.datas,
    exe_gui,
    a_gui.binaries,
    a_gui.datas,
    strip=False,
    upx=False,
    name="dayloom",
)
