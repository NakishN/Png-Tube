# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec для PNGTuber.

Один spec на все платформы:
  * Windows — WebView2 (winforms/edgechromium), без Qt;
  * macOS   — WKWebView (cocoa) + NSMicrophoneUsageDescription, без Qt;
  * Linux   — Qt WebEngine (pywebview[qt]), без WebView2.

Картинки обрабатываются Pillow, поэтому PyQt6 нужен только Linux-сборке.
"""
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

APP_NAME = 'PNGTuber'
APP_VERSION = '1.2.0'

datas = [('web', 'web')]
binaries = []
hiddenimports = []

# sounddevice загружает PortAudio через ctypes/dlopen; PyInstaller не видит
# эту зависимость автоматически. collect_data_files забирает бинарники
# portaudio из wheel на Windows/macOS.
datas += collect_data_files('sounddevice', includes=['_sounddevice_data/**/*'])
binaries += collect_dynamic_libs('sounddevice')

if sys.platform == 'win32':
    hiddenimports += [
        'webview.platforms.winforms',
        'webview.platforms.edgechromium',
        'webview.platforms.mshtml',
    ]
elif sys.platform == 'darwin':
    hiddenimports += ['webview.platforms.cocoa']
else:
    hiddenimports += [
        'webview.platforms.qt',
        'PyQt6.QtWebEngineCore',
        'PyQt6.QtWebEngineWidgets',
    ]

info_plist = {
    'CFBundleDisplayName': APP_NAME,
    'CFBundleName': APP_NAME,
    'CFBundleIdentifier': 'com.nakishn.pngtuber',
    'CFBundleShortVersionString': APP_VERSION,
    'CFBundleVersion': APP_VERSION,
    'NSHighResolutionCapable': True,
    'LSMinimumSystemVersion': '11.0',
    # Без этого ключа macOS 10.14+ не даёт доступ к микрофону и может
    # завершить приложение при первом обращении PortAudio к устройству.
    'NSMicrophoneUsageDescription':
        'PNGTuber использует микрофон, чтобы аватар реагировал на речь.',
}

a = Analysis(
    ['main.py'],
    pathex=[],
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

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name=f'{APP_NAME}.app',
        icon=None,
        bundle_identifier='com.nakishn.pngtuber',
        info_plist=info_plist,
    )
