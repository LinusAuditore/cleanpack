"""手动 GUI 检查用的启动器：设置写入临时 ini，预选 /tmp 下的测试源，不触碰用户偏好。

用法：python3 launch_fixture.py <源路径> <ini 路径> [格式] [语言] [模式]
不主动置前窗口，避免抢走用户正在使用的键盘焦点。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根目录

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import core  # noqa: E402
import main  # noqa: E402
import theme  # noqa: E402
from i18n import set_lang  # noqa: E402

src, ini = sys.argv[1], sys.argv[2]
fmt = sys.argv[3] if len(sys.argv) > 3 else "tar.xz"
lang = sys.argv[4] if len(sys.argv) > 4 else "en"
mode = sys.argv[5] if len(sys.argv) > 5 else "compress"
app = QApplication(sys.argv[:1])
app.setApplicationName(core.APP_NAME)
settings = QSettings(ini, QSettings.IniFormat)
# 与 main.main() 一样按保存的「显示 ▸ 外观」设置配色（没有保存时跟随系统）。
theme.set_appearance(app, main.appearance_pref(settings))
settings.setValue("format", fmt)
settings.setValue("mode", mode)
set_lang(lang)
w = main.MainWindow(settings)
app.aboutToQuit.connect(w.shutdown)
# 解压模式下第一个参数是要解压的归档：后台的辅助功能写值不触发路径框的提交，只能预先填好。
if mode == "extract":
    w.extract_panel.set_archive(src)
else:
    w.set_source(src)
if fmt == "tar.xz":
    w.level_slider.setValue(9)
    w.extreme_chk.setChecked(True)
w.resize(780, 760)
w.show()
sys.exit(app.exec())
