# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['qtpy', 'PySide6.QtWidgets', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineCore', 'PySide6.QtWebChannel'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Bundle exactly ONE Qt binding. With PyQt5 also installed, PyInstaller was collecting both
    # PySide6 (Qt 6) and PyQt5 (Qt 5) and their runtime hooks fought over the Qt plugin/DLL paths,
    # so the frozen exe could load the old Qt 5.15 / Chromium 87 engine (layout crammed top-left).
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PyQtWebEngine'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='app',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,   # UPX-compressing Qt/Chromium DLLs can corrupt them and cause crashes
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
