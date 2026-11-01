"""阶段 6 的离屏 GUI 夹具检查：模式切换、归档与目标路径框、真实解压、警告、取消与退出。

不弹真实对话框，不写用户偏好，数据只放 /tmp。用法：python3 -B tests/gui/gui_stage6_check.py
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

WT = Path(__file__).resolve().parents[2]  # 仓库根目录（tests/gui 的上两级）
sys.path.insert(0, str(WT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QMimeData, QPointF, QSettings, Qt, QUrl  # noqa: E402
from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtGui import QDropEvent, QFocusEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import core  # noqa: E402
import main  # noqa: E402
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


def type_commit(edit, text: str) -> None:
    """模拟键入后回车提交。"""
    edit.setText(text)
    edit.textEdited.emit(text)
    QTest.keyClick(edit, Qt.Key_Return)
    app.processEvents()


def drop(w, path: Path) -> None:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    event = QDropEvent(QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    w.dropEvent(event)
    app.processEvents()


def make_tar(path: Path, members: list) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for m in members:
            if isinstance(m, tuple):
                info = tarfile.TarInfo(m[0])
                info.size = len(m[1])
                tf.addfile(info, io.BytesIO(m[1]))
            else:
                tf.addfile(m)
    return path


app = QApplication([])
tmp = Path(tempfile.mkdtemp(prefix="cleanpack-gui6-", dir="/tmp"))
dialogs: list[str] = []
titles: list[str] = []
main.MainWindow.show_message = lambda self, icon, text: dialogs.append(text)
main.MainWindow._show_done = lambda self, path, msg, title: (dialogs.append(msg),
                                                             titles.append(title))
main.confirm = lambda parent, title, text, key: True
main.brew_notice = lambda parent, tools: None
set_lang("en")

link = tarfile.TarInfo("proj/out")
link.type = tarfile.SYMTYPE
link.linkname = "/etc/hosts"
archive = make_tar(tmp / "proj.tar.gz", [("proj/a.txt", b"a"), ("proj/.DS_Store", b"j"),
                                         ("proj/sub/._x", b"j"), link])
folder = tmp / "folder"
folder.mkdir()
(tmp / "notes.txt").write_text("x")

ini = str(tmp / "s.ini")
settings = QSettings(ini, QSettings.IniFormat)
w = main.MainWindow(settings)
w.show()
app.processEvents()
panel = w.extract_panel
ai, di = panel.arc_input, panel.dest_input

# 模式切换
check("starts in compress mode", not w.is_extract() and w.src_box.isVisible()
      and not panel.isVisible() and w.btn_go.text() == tr("compress"))
check("tabs labelled", [w.mode_tabs.tabText(i) for i in range(2)]
      == [tr("mode_compress"), tr("mode_extract")])
w.mode_tabs.setCurrentIndex(1)
check("extract mode shows only extract groups", w.is_extract() and panel.isVisible()
      and not w.src_box.isVisible() and not w.opt_box.isVisible()
      and w.btn_go.text() == tr("extract"))
check("mode saved", settings.value("mode") == "extract")
check("no archive: Go and destination disabled", not w.btn_go.isEnabled()
      and not di.isEnabled() and not panel.btn_dest.isEnabled() and panel.fmt_lbl.isHidden())
check("placeholders and notes", ai.placeholderText() == tr("archive_hint")
      and panel.layout_note.text() == tr("layout_note") and "._*" in panel.skipped_lbl.text())

# 归档路径校验
type_commit(ai, str(folder))
check("folder rejected", ai.hint.text() == tr("archive_not_file", path=folder), ai.hint.text())
type_commit(ai, str(tmp / "notes.txt"))
check("unsupported extension rejected",
      ai.hint.text() == tr("archive_unsupported", name="notes.txt"), ai.hint.text())
type_commit(ai, f"'{archive}'")
check("valid archive committed", panel.archive == archive and ai.hint.isHidden()
      and panel.fmt_lbl.text() == tr("detected_format", fmt="tar.gz")
      and di.text() == str(tmp) and w.btn_go.isEnabled(), f"{panel.fmt_lbl.text()} {di.text()}")

# 拖放：解压模式下进入归档框；压缩模式下作为源，不自动切换
w.mode_tabs.setCurrentIndex(0)
drop(w, archive)
check("drop in compress mode sets source", w.src == archive and not w.is_extract())
w.mode_tabs.setCurrentIndex(1)
ai.submit("")
drop(w, archive)
check("drop in extract mode sets archive", panel.archive == archive)

# 目标文件夹
type_commit(di, "relative/dir")
check("relative destination rejected", di.hint.text() == tr("path_not_absolute")
      and not w.btn_go.isEnabled())
later = tmp / "later"
type_commit(di, str(later))
check("missing destination noted", di.hint.text() == tr("out_dir_missing", path=later)
      and not w.btn_go.isEnabled(), di.hint.text())
later.mkdir()
w.on_app_state_changed(Qt.ApplicationActive)
check("note clears after folder created", di.hint.isHidden() and w.btn_go.isEnabled())

# 真实解压：垃圾清理、链接警告、完成提示
dialogs.clear()
w.log_view.clear()
w.start()
done = wait_until(lambda: w.job is None)
result = later / "proj"
log = w.log_view.toPlainText()
check("extraction runs", done and result.is_dir() and (result / "a.txt").read_bytes() == b"a",
      log[-300:])
check("junk not extracted", not (result / ".DS_Store").exists()
      and not (result / "sub" / "._x").exists())
check("link warning logged", tr("link_outside", name="proj/out", target="/etc/hosts") in log)
check("done dialog uses extract wording", bool(dialogs) and dialogs[-1].startswith("Extracted to:")
      and titles[-1] == tr("extract_done_title"), repr(dialogs[-1:]))
check("no staging left", not list(later.glob(".cleanpack-*")))

# 换一个归档后，用户选过的目标文件夹保留；草稿状态下目标的问题也会停用按钮
other_arc = make_tar(tmp / "other.tar.gz", [("o/a.txt", b"a")])
type_commit(ai, str(other_arc))
check("destination kept across archives", panel.dest_path() == later and di.text() == str(later),
      di.text())
type_commit(ai, str(archive))
ai.setText(str(other_arc))
ai.textEdited.emit(str(other_arc))
ai.check()
later.rename(tmp / "later-moved")
w.update_go_state()
check("destination problem blocks Go during archive draft", not w.btn_go.isEnabled()
      and di.hint.text() == tr("out_dir_missing", path=later), di.hint.text())
(tmp / "later-moved").rename(later)
ai.submit(str(archive))
w.update_go_state()

# 选的正是当前默认显示的文件夹：也算显式选择，换归档后保留
import extract_panel  # noqa: E402
ai.submit(str(archive))
di.submit("")
default_folder = panel.dest_path()
extract_panel.QFileDialog.getExistingDirectory = lambda *a, **k: str(default_folder)
panel.choose_dest()
sub_arc_dir = tmp / "elsewhere"
sub_arc_dir.mkdir()
moved_arc = make_tar(sub_arc_dir / "m.tar.gz", [("m/a.txt", b"a")])
type_commit(ai, str(moved_arc))
check("choosing the shown default folder persists", panel.dest_path() == default_folder
      and di.text() == str(default_folder), di.text())

# 清空归档后，选过的目标仍显示并可修改；没选过目标时框为空
ai.submit("")
w.update_go_state()
check("chosen destination stays visible without archive", di.text() == str(default_folder)
      and di.isEnabled() and not w.btn_go.isEnabled(), di.text())

# 没选过目标时，目标框跟随通过校验的归档草稿
di.submit("")
ai.submit(str(archive))
ai.setText(str(moved_arc))
ai.textEdited.emit(str(moved_arc))
ai.check()
check("destination follows a valid archive draft", di.text() == str(sub_arc_dir)
      and panel.dest_path() == sub_arc_dir, di.text())
ai.submit(str(archive))
type_commit(di, str(later))

# 目标框有未提交的草稿时，在对话框里选的文件夹优先，草稿作废
di.setText(str(tmp / "typed-draft"))
di.textEdited.emit(str(tmp / "typed-draft"))
extract_panel.QFileDialog.getExistingDirectory = lambda *a, **k: str(later)
panel.choose_dest()
check("dialog choice replaces pending destination draft", not di.pending
      and di.text() == str(later) and panel.dest_path() == later, di.text())

# 提交了被拒绝的目标：文字和提示留在框里，刷新时不被之前的目标覆盖
type_commit(di, "relative/dir")
w.on_app_state_changed(Qt.ApplicationActive)
w.update_go_state()
check("rejected destination stays for correction", di.text() == "relative/dir"
      and di.hint.text() == tr("path_not_absolute") and not w.btn_go.isEnabled(), di.text())
# 没选过目标、目标被拒绝，然后清空归档：目标框随之停用，被拒绝的文字不能留在无法修改的框里
ai.submit(str(archive))
di.submit("")
type_commit(di, "relative/dir")
check("precondition: destination rejected", di.rejected and panel.dest_dir is None)
ai.submit("")
w.update_go_state()
check("rejected destination cleared when field is disabled", di.text() == ""
      and di.hint.isHidden() and not di.isEnabled(), f"{di.text()!r} {di.hint.text()!r}")
ai.submit(str(archive))
type_commit(di, str(later))

# 再解压一次：自动编号，不替换
w.start()
wait_until(lambda: w.job is None)
check("second extraction numbered", (later / "proj 2" / "a.txt").exists()
      and (later / "proj" / "a.txt").exists())

# 加密 zip：明确的失败原因
d = tmp / "zsrc"
d.mkdir()
(d / "s.txt").write_text("secret")
enc = tmp / "enc.zip"
subprocess.run(["zip", "-q", "-P", "pw", "-r", str(enc), "s.txt"], cwd=d, check=True,
               env=core.ENV, stdin=subprocess.DEVNULL)
type_commit(ai, str(enc))
dialogs.clear()
# 阶段 6b 起加密归档改为询问密码；这里取消询问，应按取消处理，不弹失败框。
asked: list[tuple[str, str | None]] = []
main.ask_password = lambda parent, text, error=None: asked.append((text, error))
w.start()
wait_until(lambda: w.job is None)
check("encrypted zip asks for password, cancel is quiet",
      dialogs == [] and asked == [(tr("password_prompt", name="enc.zip"), None)]
      and tr("extract_cancelled") in w.log_view.toPlainText(), repr((dialogs, asked)))

# 缺 zstd 时横幅与按钮
zst = tmp / "x.tar.zst"
core.ArchiveJob(d, zst, "tar.zst", log=lambda _: None).run()
type_commit(ai, str(zst))
real = dict(w.tools.paths)
w.tools.paths = {**real, "zstd": None}
w.tools.changed.emit()
check("banner for missing zstd in extract mode",
      w.warn_lbl.text() == tr("missing_tools", fmt="tar.zst", tools="zstd")
      and not w.btn_go.isEnabled(), w.warn_lbl.text())
w.mode_tabs.setCurrentIndex(0)
w.fmt_combo.setCurrentIndex(list(core.FORMATS).index("zip"))
check("banner follows mode", w.warn_lbl.isHidden())
w.mode_tabs.setCurrentIndex(1)

# 缺 zstd 时，键入但未提交的 .tar.zst 草稿：点击解压不能启动注定失败的任务
type_commit(ai, str(archive))
ai.setText(str(zst))
ai.textEdited.emit(str(zst))
dialogs.clear()
w.start()
check("missing tool for typed draft does not start a job", w.job is None and dialogs == []
      and panel.archive == zst and w.warn_lbl.text() == tr("missing_tools", fmt="tar.zst",
                                                            tools="zstd"), w.warn_lbl.text())
w.tools.paths = real
w.tools.changed.emit()

# 切换模式隐藏输入框时，未完成的草稿不提交，已选好的归档和目标保持不变
type_commit(ai, str(archive))
type_commit(di, str(later))
ai.setText(str(tmp / "half"))
ai.textEdited.emit(str(tmp / "half"))
w.mode_tabs.setCurrentIndex(0)
QApplication.sendEvent(ai, QFocusEvent(QEvent.FocusOut, Qt.OtherFocusReason))
app.processEvents()
w.mode_tabs.setCurrentIndex(1)
# 离开模式时未提交的草稿作废，输入框恢复已提交的归档，之后选的目标不会被它重置。
check("hidden draft discarded on mode switch", panel.archive == archive
      and panel.dest_path() == later and not ai.pending and ai.text() == str(archive)
      and ai.hint.isHidden(), f"{panel.archive} {ai.text()} {ai.hint.text()}")

# 压缩模式同理：源的草稿在切换后作废，之后键入的输出保持有效
w.mode_tabs.setCurrentIndex(0)
w.set_source(str(folder))
w.src_input.setText(str(tmp / "half-src"))
w.src_input.textEdited.emit(str(tmp / "half-src"))
w.mode_tabs.setCurrentIndex(1)
w.mode_tabs.setCurrentIndex(0)
type_commit(w.out_input, "custom-name")
check("compress source draft discarded, output kept", w.src == folder
      and w.output_path() == tmp / "custom-name.zip" and not w.src_input.pending,
      f"{w.src} {w.output_path()}")
w.mode_tabs.setCurrentIndex(1)

# 归档草稿通过校验时，格式和横幅跟随草稿而不是旧归档
w.tools.paths = {**real, "zstd": None}
type_commit(ai, str(zst))
ai.setText(str(archive))
ai.textEdited.emit(str(archive))
ai.check()
check("banner and format follow a valid draft", w.warn_lbl.isHidden() and w.btn_go.isEnabled()
      and panel.fmt_lbl.text() == tr("detected_format", fmt="tar.gz"), panel.fmt_lbl.text())
w.tools.paths = real
w.tools.changed.emit()
ai.submit(str(archive))
ai.submit(str(archive))

# 在另一个模式期间归档被删除：切回解压模式时立即提示
gone = tmp / "gone.tar.gz"
gone.write_bytes(archive.read_bytes())
type_commit(ai, str(gone))
w.mode_tabs.setCurrentIndex(0)
gone.unlink()
w.mode_tabs.setCurrentIndex(1)
check("switching back rechecks archive", ai.hint.text() == tr("src_missing", path=gone)
      and not w.btn_go.isEnabled(), ai.hint.text())

# 运行中停用输入，取消后恢复
sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
real_spawn = core.Job._spawn
core.Job._spawn = lambda self, cmd, **kw: real_spawn(self, sleeper, **kw)
type_commit(ai, str(archive))
w.log_view.clear()
w.start()
wait_until(lambda: w.job is not None and w.job._procs)
check("inputs disabled while extracting", not w.mode_tabs.isEnabled() and not ai.isEnabled()
      and not di.isEnabled() and w.btn_cancel.isEnabled())
w.cancel_job()
wait_until(lambda: w.job is None)
check("cancel logged with extract wording",
      w.log_view.toPlainText().rstrip().endswith(tr("extract_cancelled")))
check("inputs enabled after cancel", w.mode_tabs.isEnabled() and ai.isEnabled())

# 退出：解压进行中关闭窗口，确认后取消并关闭，不留暂存目录
w.start()
wait_until(lambda: w.job is not None and w.job._procs)
w.close()
closed = wait_until(lambda: not w.isVisible(), 10)
core.Job._spawn = real_spawn
check("quit during extraction closes cleanly", closed and w.job is None
      and not list(tmp.glob(".cleanpack-*")))

# 语言切换与模式恢复
settings.sync()
w2 = main.MainWindow(QSettings(ini, QSettings.IniFormat))
check("mode restored on next launch", w2.is_extract() and w2.btn_go.text() == tr("extract"))
w2.lang_combo.setCurrentIndex(3)  # Français
check("retranslated to French", w2.mode_tabs.tabText(1) == "Extraire"
      and w2.extract_panel.btn_archive.text() == "Choisir une archive…"
      and w2.btn_go.text() == "Extraire")
w2._quit_ready = True
w2.close()
set_lang("en")

for s in (settings, w2.settings):
    s.sync()
subprocess.run(["chmod", "-R", "u+rwx", str(tmp)], check=False)
subprocess.run(["rm", "-rf", str(tmp)], check=False)
width = max(len(n) for n, _, _ in results)
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print(f"{sum(ok for _, ok, _ in results)}/{len(results)}",
      "ALL PASS" if all(ok for _, ok, _ in results) else "SOME FAILED")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)  # CI 按退出码判断
