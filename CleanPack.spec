# -*- mode: python ; coding: utf-8 -*-
"""构建带应用图标、版本信息和许可证的 macOS 应用。"""
import sys
import sysconfig
from pathlib import Path

from macholib.MachO import MachO
from macholib.mach_o import LC_BUILD_VERSION, LC_VERSION_MIN_MACOSX

# 名称、版本和 bundle id 只在 core.py 中定义一次；core 不依赖 Qt，导入时只构建子进程环境。
sys.path.insert(0, SPECPATH)
from core import APP_NAME, APP_VERSION, BUNDLE_ID  # noqa: E402

# 随应用分发的许可证文件；任何一个缺失都让构建失败，而不是悄悄发布缺少许可证的产物。
LICENSE_FILES = ['LICENSE', 'licenses/THIRD_PARTY_NOTICES.md', 'licenses/LGPL-3.0.txt',
                 'licenses/GPL-3.0.txt', 'licenses/Apache-2.0.txt']
PYTHON_LICENSE = Path(sysconfig.get_path('stdlib'), 'LICENSE.txt')
for required in [*(Path(SPECPATH, f) for f in LICENSE_FILES), PYTHON_LICENSE]:
    if not required.is_file():
        raise SystemExit(f"找不到许可证文件：{required}")

# 程序只用 QtCore、QtGui 和 QtWidgets。PyInstaller 收集 QtGui 插件时会带入虚拟键盘输入法插件
# 和 PDF 图片格式插件，进而拖入只以 GPL-3.0 授权的 QtVirtualKeyboard 以及 Quick/QML/PDF 模块，
# 这里把它们排除，避免发布用不到的 GPL 组件。
UNUSED_QT = ('libqtvirtualkeyboardplugin', 'libqpdf', 'QtVirtualKeyboard', 'QtQuick',
             'QtQml', 'QtOpenGL', 'QtPdf')


def is_unused_qt(dest: str) -> bool:
    return any(name in dest for name in UNUSED_QT)


def min_macos(paths: list[str]) -> str:
    """读取 Mach-O 中声明的最低 macOS 版本，返回所有二进制中最高的那个。"""
    best = (0, 0)
    for path in paths:
        try:
            headers = MachO(path).headers
        except Exception:  # noqa: BLE001  不是 Mach-O 文件（脚本、资源等）
            continue
        for header in headers:
            for load_cmd, cmd, _ in header.commands:
                if load_cmd.cmd == LC_BUILD_VERSION:
                    packed = cmd.minos
                elif load_cmd.cmd == LC_VERSION_MIN_MACOSX:
                    packed = cmd.version
                else:
                    continue
                best = max(best, (packed >> 16, (packed >> 8) & 0xFF))
    return f"{best[0]}.{best[1]}"


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # 让项目许可证和第三方许可证随二进制产物分发，保留版权声明、许可条件及免责声明。
    datas=[(f, str(Path(f).parent)) for f in LICENSE_FILES]
          + [(str(PYTHON_LICENSE), 'licenses/Python')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
a.binaries = [entry for entry in a.binaries if not is_unused_qt(entry[0])]
a.datas = [entry for entry in a.datas if not is_unused_qt(entry[0])]
# 以实际打包的二进制为准，wheel 标签声明的系统版本可能低于其中库的真实要求。
MIN_MACOS = min_macos([entry[1] for entry in a.binaries])
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
    name=APP_NAME,
)
app = BUNDLE(
    coll,
    name=f'{APP_NAME}.app',
    icon='icon.icns',
    bundle_identifier=BUNDLE_ID,
    version=APP_VERSION,
    info_plist={
        # PyInstaller 只根据 version 设置 CFBundleShortVersionString，构建号需要单独写入。
        'CFBundleVersion': APP_VERSION,
        'CFBundleDevelopmentRegion': 'en',
        # 声明支持的语言，原生文件对话框才会在这些系统语言下显示本地化按钮。
        'CFBundleLocalizations': ['en', 'zh-Hans', 'zh-Hant', 'fr'],
        'NSHumanReadableCopyright': 'Copyright 2026 Xinghao Zuo',
        'LSMinimumSystemVersion': MIN_MACOS,
    },
)
