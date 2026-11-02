# -*- mode: python ; coding: utf-8 -*-

import shutil

ffmpeg_binary = shutil.which('ffmpeg')
binaries = [(ffmpeg_binary, '.')] if ffmpeg_binary else []


# Paths are relative to this file (deploy/): build from the repo root with
#   python -m PyInstaller deploy/main.spec
a = Analysis(
    ['../main.py'],
    pathex=['..'],
    binaries=binaries,
    datas=[('../config.py', '.'),
           # the whole web panel: pages, scripts, styles, dictionaries, PWA, printer images
           ('../app/web', 'app/web'),
           # Vendored pybambu data, found next to the package's __file__ (see app/vendor/pybambu/VENDORED.md).
           ('../app/vendor/pybambu/certs', 'app/vendor/pybambu/certs'),
           ('../app/vendor/pybambu/hms_error_text', 'app/vendor/pybambu/hms_error_text'),
           ('../app/vendor/pybambu/filaments.json', 'app/vendor/pybambu'),
           ('../app/vendor/pybambu/LICENSE', 'app/vendor/pybambu')],
    hiddenimports=['aiogram', 'websocket', 'app.vendor.pybambu'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='BambuDiagnosticData',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
