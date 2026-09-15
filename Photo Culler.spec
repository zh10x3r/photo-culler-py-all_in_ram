# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the Qt/VisPy GPU Photo Culler main window."""

from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files


project = Path(SPECPATH)
tk_runtime = Path(
    os.environ.get("PHOTO_CULLER_TK_RUNTIME", str(project.parent / "tk_runtime"))
)
if not tk_runtime.exists():
    raise FileNotFoundError(
        "Tcl/Tk runtime not found. Set PHOTO_CULLER_TK_RUNTIME to a folder "
        "containing tcl\\ and bin\\ before building."
    )

datas = [(str(tk_runtime / "tcl"), "tcl")]
binaries = [
    (str(tk_runtime / "bin" / "tcl86t.dll"), "bin"),
    (str(tk_runtime / "bin" / "tk86t.dll"), "bin"),
]
hiddenimports = [
    "tkinter",
    "_tkinter",
    "vispy.app.backends._pyside6",
    "PySide6.QtOpenGLWidgets",
]

rawpy_parts = collect_all("rawpy")
datas += rawpy_parts[0]
binaries += rawpy_parts[1]
hiddenimports += rawpy_parts[2]

# VisPy's shader/data files are not all discovered by the normal module hook.
datas += collect_data_files("vispy")


a = Analysis(
    [str(project / "app.py")],
    pathex=[str(project)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Qt 6.11 uses the Windows system ICU DLL. The Codex shell can expose an
# unrelated Poppler ICU build on PATH; collecting that same-named DLL makes
# QtCore fail with WinError 127. Keep the system-owned implementation.
incompatible_icu_names = {"icuuc.dll", "icudt78.dll"}
a.binaries = [
    entry
    for entry in a.binaries
    if entry[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    not in incompatible_icu_names
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Photo Culler",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
