# -*- mode: python ; coding: utf-8 -*-
#
# The ONE description of how AIMScribe_Agent.exe is built. BUILD.bat runs
#     pyinstaller --noconfirm --clean AIMScribe_Agent.spec
# and nothing else: there used to be a second, slightly different list of
# flags inside BUILD.bat, so the spec and the shipped program could disagree.
#
# Choices that matter for antivirus and SmartScreen:
#   onedir, not onefile   onefile unpacks the whole program into %TEMP% at
#                         every start - slow, and "an exe that writes and runs
#                         DLLs from TEMP" is a classic malware pattern.
#   upx=False             compressed (packed) binaries are what malware uses to
#                         hide; antivirus treats them with suspicion and has to
#                         unpack every one at every start.
#   windowed              no console window - the agent shows itself in the
#                         system tray instead.
#   version resource      company, product and version, so Windows and IT can
#                         see what the file is.
# Every .exe/.dll/.pyd this produces is then signed by signing\sign.ps1.
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = [
    'pystray._win32',
    'uvicorn.logging',
    'uvicorn.loops.auto',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.protocols.websockets.wsproto_impl',
    'uvicorn.lifespan.on',
    'cryptography.hazmat.primitives.asymmetric.ed25519',
    'cryptography.hazmat.primitives.ciphers.aead',
    'jwt.algorithms',
    'ui.overlay',
]
tmp_ret = collect_all('pystray')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'PyQt5', 'PyQt6', 'IPython', 'pytest', 'tkinter.test'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AIMScribe_Agent',
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
    version='version_info.txt',
    icon=['assets\\aimscribe.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AIMScribe_Agent',
)
