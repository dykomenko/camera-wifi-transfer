# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['..\\app.py'], pathex=[], binaries=[], datas=[], hiddenimports=[],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [], name='Camera WiFi Transfer',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    console=False, icon=['..\\vendor\\airmtp\\appresource\\airnef.ico'],
)

