# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['..\\camera_transfer.py'], pathex=[], binaries=[], datas=[('..\\web', 'web')],
    hiddenimports=['webview.platforms.edgechromium', 'webview.platforms.winforms'],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['tkinter'], noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [], name='Camera Transfer',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    console=False, icon=['..\\vendor\\airmtp\\appresource\\airnef.ico'],
)
