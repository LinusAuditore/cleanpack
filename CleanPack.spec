# -*- mode: python ; coding: utf-8 -*-
"""构建带应用图标和项目许可证的 macOS 应用。"""


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # 让许可证随二进制产物分发，保留版权声明、许可条件及免责声明。
    datas=[('LICENSE', '.')],
    hiddenimports=[],
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
    name='CleanPack',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CleanPack',
)
app = BUNDLE(
    coll,
    name='CleanPack.app',
    icon='icon.icns',
    bundle_identifier='com.yourname.cleanpack',
)
