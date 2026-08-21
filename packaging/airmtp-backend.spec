# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['..\\vendor\\airmtp\\airmtp.py'], pathex=[], binaries=[], datas=[],
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=[], noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [], name='airmtp-backend',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    console=True,
)

