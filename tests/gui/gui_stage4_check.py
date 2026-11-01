"""阶段 4 的离屏 GUI 夹具检查：工具链窗口、横幅、安装与压缩互斥、退出和 Qt 翻译。"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WT = Path(__file__).resolve().parents[2]  # 仓库根目录（tests/gui 的上两级）
sys.path.insert(0, str(WT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QCoreApplication, QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import core  # noqa: E402
import gui_common  # noqa: E402
import main  # noqa: E402
import tools_dialog  # noqa: E402
from i18n import set_lang, tr  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


def wait_until(cond, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


app = QApplication([])
tmp = Path(tempfile.mkdtemp(prefix="cleanpack-gui4-", dir="/tmp"))
dialogs: list[str] = []
main.MainWindow.show_message = lambda self, icon, text: dialogs.append(text)
main.MainWindow._show_done = lambda self, path, msg, title: dialogs.append(msg)
main.confirm = lambda parent, title, text, key: True
notices: list[str] = []
QMessageBox.exec = lambda self: notices.append(self.informativeText()) or 0


def new_window() -> main.MainWindow:
    settings = QSettings(str(tmp / f"s{time.monotonic_ns()}.ini"), QSettings.IniFormat)
    settings.setValue("format", "tar.zst")
    w = main.MainWindow(settings)
    w.show()
    app.processEvents()
    return w


def fake_brew(trap: bool = False) -> str:
    script = tmp / ("brew-trap" if trap else "brew")
    script.write_text("#!/bin/sh\n" + ("trap '' TERM\n" if trap else "")
                      + "echo installing $2\nsleep 60\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


set_lang("en")
src = tmp / "src"
src.mkdir()
(src / "a.txt").write_text("hello")

# 横幅：工具齐全时隐藏
w = new_window()
w.set_source(str(src))
check("banner hidden when nothing missing", w.warn_lbl.isHidden() and w.banner_btn.isHidden())
check("main window has no toolchain group", not hasattr(w, "tools_box"))

# 当前格式缺 zstd：横幅和“安装…”按钮；切到 zip 时隐藏
real = dict(w.tools.paths)
w.tools.paths = {**real, "zstd": None}
w.tools.changed.emit()
check("banner shows missing zstd with Install button",
      not w.warn_lbl.isHidden() and w.warn_lbl.text() == tr("missing_tools", fmt="tar.zst", tools="zstd")
      and w.banner_btn.text() == tr("tools_fix_btn") and not w.btn_go.isEnabled(),
      f"{w.warn_lbl.text()!r} / {w.banner_btn.text()!r}")
check("missing_tools text has no position word", "below" not in w.warn_lbl.text())
w.fmt_combo.setCurrentIndex(list(core.FORMATS).index("zip"))
check("banner hidden for a format with all tools", w.warn_lbl.isHidden())

# 只缺 tar：说明无法安装，按钮只打开工具链窗口
w.tools.paths = {**real, "tar": None}
w.fmt_combo.setCurrentIndex(list(core.FORMATS).index("tar"))
check("tar-only banner explains", w.warn_lbl.text() == tr("tools_not_brew", tools="tar")
      and w.banner_btn.text() == tr("tools_btn"), w.warn_lbl.text())
d = w.tools_dialog
d.update_state()
check("dialog note explains tar", not d.note_lbl.isHidden() and d.cmd_edit.isHidden(),
      d.note_lbl.text())

# 横幅按钮打开对话框
w.banner_btn.click()
check("banner button opens dialog", d.isVisible())
d.hide()

# 安装：主窗口忙碌、横幅显示安装中、关闭对话框后安装继续、在对话框中取消
w.tools.paths = {**real, "zstd": None}
w.fmt_combo.setCurrentIndex(list(core.FORMATS).index("tar.zst"))
w.tools.brew = fake_brew()
w.tools.changed.emit()
check("dialog install enabled when idle", d.btn_install.isEnabled())
d.btn_install.click()
started = wait_until(lambda: "installing zstd" in d.log_view.toPlainText(), 10)
check("install output goes to dialog log", started, d.log_view.toPlainText()[-80:])
check("main log gets install summary", tr("installing", cmd="brew install zstd") in w.log_view.toPlainText())
check("inputs disabled during install", not w.btn_folder.isEnabled() and not w.fmt_combo.isEnabled()
      and not w.btn_go.isEnabled())
check("banner shows installing", w.warn_lbl.text() == tr("installing_banner")
      and w.banner_btn.text() == tr("tools_btn"))
check("main Cancel is not for installs", not w.btn_cancel.isEnabled())
d.show()
d.btn_close.click()
app.processEvents()
check("closing dialog keeps install running", not d.isVisible() and w.tools.installing)
d.btn_cancel.click()
finished = wait_until(lambda: not w.tools.installing, 10)
check("cancel from dialog logs cancelled in both logs", finished
      and w.log_view.toPlainText().rstrip().endswith(tr("install_cancelled"))
      and tr("install_cancelled") in d.log_view.toPlainText())
check("inputs re-enabled after install", w.btn_folder.isEnabled() and w.fmt_combo.isEnabled())

# 压缩进行中：对话框的安装按钮停用
w.fmt_combo.setCurrentIndex(list(core.FORMATS).index("zip"))
# 上一次安装结束时会重新检测工具，这里重新制造“缺 zstd”的状态，并确认空闲时确实可以安装。
w.tools.paths = {**real, "zstd": None}
w.tools.changed.emit()
check("precondition: install possible before job", w.tools.can_install())
sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
real_spawn = core.Job._spawn
core.Job._spawn = lambda self, cmd, **kw: real_spawn(self, sleeper, **kw)
(tmp / "src.zip").unlink(missing_ok=True)
w.start()
wait_until(lambda: w.job is not None and w.job._procs)
check("install disabled while a job runs", not d.btn_install.isEnabled() and not w.tools.can_install())
w.cancel_job()
wait_until(lambda: w.job is None)
check("install re-enabled after job", d.btn_install.isEnabled())
core.Job._spawn = real_spawn

# 安装进行中退出：确认后取消安装再关闭（brew 忽略 SIGTERM 时 5 秒后强制结束）
w.tools.brew = fake_brew(trap=True)
w.tools.paths = {**real, "zstd": None}
d.log_view.clear()
check("precondition: install starts", w.tools.install())
wait_until(lambda: "installing zstd" in d.log_view.toPlainText(), 10)
t0 = time.monotonic()
w.close()
closed = wait_until(lambda: not w.isVisible(), 15)
took = time.monotonic() - t0
# 假 brew 忽略 SIGTERM，所以必须等 5 秒后的强制结束，耗时应接近 5 秒。
check("quit during install cancels then closes", closed and not w.tools.installing and took > 4,
      f"closed={closed} took={took:.1f}s")

# 启动提示：只有缺 Homebrew 且确有 formula 缺失时才出现
w2 = new_window()
notices.clear()
w2.tools.brew = None
w2.tools.paths = dict(real)
tools_dialog.brew_notice(w2, w2.tools)
check("no brew notice when nothing missing", notices == [])
w2.tools.paths = {**real, "xz": None, "zstd": None}
tools_dialog.brew_notice(w2, w2.tools)
check("brew notice lists needed tools", len(notices) == 1 and "xz, zstd" in notices[0], repr(notices))

# 没有 Homebrew 时横幅按钮不承诺“安装”
w2.fmt_combo.setCurrentIndex(list(core.FORMATS).index("tar.xz"))
w2.update_banner()
check("no-brew banner button opens toolchain", w2.banner_btn.text() == tr("tools_btn")
      and w2.warn_lbl.text() == tr("missing_tools", fmt="tar.xz", tools="xz"), w2.banner_btn.text())

# 启动提示中复制安装命令后，主窗口日志有确认
w2.log_view.clear()
QMessageBox.clickedButton = lambda self: [b for b in self.buttons() if b.text() == tr("copy_install")][0]
tools_dialog.brew_notice(w2, w2.tools)
del QMessageBox.clickedButton
check("notice copy logs confirmation", w2.log_view.toPlainText().strip() == tr("copied"),
      repr(w2.log_view.toPlainText()))

# 应用重新激活时重新检测
calls = []
real_locate = core.locate
core.locate = lambda key: calls.append(key) or real_locate(key)
w2.on_app_state_changed(Qt.ApplicationActive)
core.locate = real_locate
check("re-detect on app activation", len(calls) == len(core.TOOLS))

# Qt 自带文案随应用语言切换
set_lang("fr")
gui_common.install_qt_translator()
fr_copy = QCoreApplication.translate("QLineEdit", "&Copy")
set_lang("zh_Hant")
gui_common.install_qt_translator()
tw_copy = QCoreApplication.translate("QLineEdit", "&Copy")
set_lang("en")
gui_common.install_qt_translator()
en_copy = QCoreApplication.translate("QLineEdit", "&Copy")
check("Qt context menu follows app language", fr_copy != "&Copy" and tw_copy != "&Copy"
      and en_copy == "&Copy", f"fr={fr_copy!r} zh_Hant={tw_copy!r} en={en_copy!r}")

# 语言切换同步到工具链窗口和菜单
w2.lang_combo.setCurrentIndex(3)  # Français
check("dialog and menu retranslate", w2.tools_dialog.windowTitle() == "Chaîne d’outils"
      and w2.tools_action.text() == "Chaîne d’outils…" and w2.tools_menu.title() == "Outils",
      f"{w2.tools_dialog.windowTitle()} / {w2.tools_action.text()} / {w2.tools_menu.title()}")
w2._quit_ready = True
w2.close()
set_lang("en")

# 先把设置写完，否则 QSettings 会在进程退出时重新写 ini，把临时目录又建出来。
for win in (w, w2):
    win.settings.sync()
subprocess.run(["rm", "-rf", str(tmp)], check=False)
width = max(len(n) for n, _, _ in results)
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print("ALL PASS" if all(ok for _, ok, _ in results) else "SOME FAILED")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)  # CI 按退出码判断
