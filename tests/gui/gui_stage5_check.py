"""阶段 5 的离屏 GUI 夹具检查：可键入的源路径、实时校验、输出框解析与提交。

不弹真实对话框，不写用户偏好，数据只放 /tmp。用法：python3 -B tests/gui/gui_stage5_check.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path

WT = Path(__file__).resolve().parents[2]  # 仓库根目录（tests/gui 的上两级）
sys.path.insert(0, str(WT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QEvent, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QFocusEvent  # noqa: E402
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


def settle(ms: int = 300) -> None:
    """等过防抖间隔，让草稿校验完成。"""
    QTest.qWait(ms)


def type_into(edit, text: str, enter: bool = False) -> None:
    """模拟用户清空后逐字键入；enter 为 True 时再按回车提交。"""
    edit.selectAll()
    QTest.keyClick(edit, Qt.Key_Backspace)
    if text.isascii():
        QTest.keyClicks(edit, text)
    else:
        # QTest 只能模拟 ASCII 按键；中文路径直接填入，再发出用户编辑时的同一信号。
        edit.setText(text)
        edit.textEdited.emit(text)
    if enter:
        QTest.keyClick(edit, Qt.Key_Return)
    app.processEvents()


def focus_out(edit, reason) -> None:
    """向输入框发送失去焦点事件，模拟点击窗口内其他控件或切换到其他应用。"""
    QApplication.sendEvent(edit, QFocusEvent(QEvent.FocusOut, reason))
    app.processEvents()


app = QApplication([])
tmp = Path(tempfile.mkdtemp(prefix="cleanpack-gui5-", dir="/tmp"))
dialogs: list[str] = []
confirms: list[str] = []
main.MainWindow.show_message = lambda self, icon, text: dialogs.append(text)
main.MainWindow._show_done = lambda self, path, msg, title: dialogs.append(msg)
main.confirm = lambda parent, title, text, key: confirms.append(text) or True
# 启动时的 Homebrew 提示与本阶段无关，不弹出。
main.brew_notice = lambda parent, tools: None

set_lang("en")
src = tmp / "项目 src"
(src / "sub").mkdir(parents=True)
(src / "a.txt").write_text("hello")
other = tmp / "other folder"
other.mkdir()
(other / "b.txt").write_text("world")
junk = tmp / "._resource"
junk.write_bytes(b"x")
fifo = tmp / "pipe"
os.mkfifo(fifo)

settings = QSettings(str(tmp / "s.ini"), QSettings.IniFormat)
settings.setValue("format", "zip")
w = main.MainWindow(settings)
w.show()
app.processEvents()
si, oi = w.src_input, w.out_input

# 基本属性
check("source field is editable and refuses drops", not si.isReadOnly() and not si.acceptDrops()
      and not oi.acceptDrops())
check("placeholders", si.placeholderText() == tr("drop_hint")
      and oi.placeholderText() == tr("out_placeholder"))
check("hints hidden initially", si.hint.isHidden() and oi.hint.isHidden())

# 相对路径：防抖后提示，未提交，按钮不可用
type_into(si, "Documents/x")
check("hint waits for debounce", si.hint.isHidden())
settle()
check("relative path hint after debounce", not si.hint.isHidden()
      and si.hint.text() == tr("path_not_absolute"), si.hint.text())
check("relative path keeps no source", w.src is None and not w.btn_go.isEnabled())

# 合法草稿：未提交也能点压缩，回车后提交并生成输出
type_into(si, str(src))
settle()
check("valid draft clears hint, enables Go before commit", si.hint.isHidden() and w.src is None
      and w.btn_go.isEnabled(), f"src={w.src} go={w.btn_go.isEnabled()}")
QTest.keyClick(si, Qt.Key_Return)
check("Enter commits source", w.src == src and oi.text() == str(tmp / "项目 src.zip"), oi.text())

# 各种写法都会整理成同一路径
for label, raw in (("quoted", f"'{src}'"), ("file URL", "file://" + urllib.parse.quote(str(src))),
                   ("trailing space", f"  {src}  "),
                   ("terminal escape", str(src).replace(" ", "\\ "))):
    si.submit(str(other))
    type_into(si, raw, enter=True)
    check(f"{label} input normalized", w.src == src and si.text() == str(src), si.text())
type_into(si, "~", enter=True)
check("~ expands to home", w.src == Path.home() and si.text() == str(Path.home()), si.text())

# 不合格的源
cases = [("missing", str(tmp / "nope"), tr("src_missing", path=tmp / "nope")),
         ("junk", str(junk), tr("src_is_junk", name="._resource")),
         ("root", "/", tr("src_is_root")),
         ("fifo", str(fifo), tr("src_unsupported_type", path=fifo)),
         ("dotdot", f"{src}/../other folder", tr("path_invalid", path=f"{src}/../other folder"))]
for label, raw, expected in cases:
    si.submit(str(src))
    type_into(si, raw, enter=True)
    check(f"{label} rejected with hint", w.src is None and si.hint.text() == expected
          and not w.btn_go.isEnabled() and oi.text() == "" and not oi.isEnabled(),
          f"{si.hint.text()!r} src={w.src}")

# 拖放或对话框给出不合格路径时，同样显示原因
si.submit(str(src))
w.set_source(str(junk))
check("dropped junk shows hint", w.src is None and si.text() == str(junk)
      and si.hint.text() == tr("src_is_junk", name="._resource"))

# 切换语言后重新渲染提示
w.lang_combo.setCurrentIndex(1)  # 简体中文
check("hint retranslates", si.hint.text() == tr("src_is_junk", name="._resource")
      and "垃圾" in si.hint.text(), si.hint.text())
w.lang_combo.setCurrentIndex(0)

# 输出框：只写名称、去掉已知扩展名、切换格式保留名称
w.set_source(str(src))
type_into(oi, "backup.ZIP", enter=True)
check("name only goes to current folder", oi.text() == str(tmp / "backup.zip"), oi.text())
w.fmt_combo.setCurrentIndex(list(core.FORMATS).index("7z"))
check("format switch keeps typed name", oi.text() == str(tmp / "backup.7z"), oi.text())
w.fmt_combo.setCurrentIndex(list(core.FORMATS).index("zip"))

# 源的回车或失去焦点没有改动时，不重置输出命名
focus_out(si, Qt.OtherFocusReason)
QTest.keyClick(si, Qt.Key_Return)
check("re-committing same source keeps output name", oi.text() == str(tmp / "backup.zip"),
      oi.text())

# 以 / 结尾只换目录
type_into(oi, f"{other}/", enter=True)
check("trailing slash changes folder only", oi.text() == str(other / "backup.zip"), oi.text())
# 留空恢复默认
type_into(oi, "", enter=True)
check("empty output restores default", oi.text() == str(tmp / "项目 src.zip"), oi.text())

# 相对路径与无效名称：提示、按钮停用、保留原设置
type_into(oi, "sub/x.zip", enter=True)
check("relative output rejected", oi.hint.text() == tr("path_not_absolute")
      and not w.btn_go.isEnabled() and w.output_path() == tmp / "项目 src.zip", oi.hint.text())
confirms.clear()
dialogs.clear()
type_into(oi, ".", enter=False)
w.start()
check("#8 '.' output never reaches overwrite confirm", w.job is None and confirms == []
      and dialogs == [] and oi.hint.text() == tr("path_invalid", path="."),
      f"{confirms} {dialogs} {oi.hint.text()!r}")
type_into(oi, "", enter=True)

# 输出目录不存在：保存设置，提示并停用；建好目录、应用重新激活后恢复
later = tmp / "later"
type_into(oi, f"{later}/out.zip", enter=True)
check("missing output folder noted", oi.hint.text() == tr("out_dir_missing", path=later)
      and not w.btn_go.isEnabled() and w.output_path() == later / "out.zip", oi.hint.text())
later.mkdir()
w.on_app_state_changed(Qt.ApplicationActive)
check("note clears after folder appears", oi.hint.isHidden() and w.btn_go.isEnabled(),
      oi.hint.text())

# 输出在源目录里
type_into(oi, f"{src}/sub/inner.zip", enter=True)
check("output inside source noted", oi.hint.text() == tr("out_inside_src")
      and not w.btn_go.isEnabled(), oi.hint.text())
type_into(oi, "", enter=True)

# 源被删除后切回应用：提示并停用
gone = tmp / "gone"
gone.mkdir()
w.set_source(str(gone))
gone.rmdir()
w.on_app_state_changed(Qt.ApplicationActive)
check("deleted source detected on activation", si.hint.text() == tr("src_missing", path=gone)
      and not w.btn_go.isEnabled(), si.hint.text())

# 点击压缩时提交两个草稿：新源加上只写名称的输出
w.set_source(str(src))
type_into(si, str(other))
type_into(oi, "draft-name")
confirms.clear()
dialogs.clear()
w.start()
running = w.job is not None
done = wait_until(lambda: w.job is None)
out = tmp / "draft-name.zip"
check("start commits both drafts", running and done and w.src == other and out.exists()
      and confirms == [], f"{dialogs} exists={out.exists()}")
if out.exists():
    with zipfile.ZipFile(out) as z:
        check("archive has new source", z.namelist() == ["other folder/", "other folder/b.txt"],
              repr(z.namelist()))

# 已有输出：输出框没有提示，开始时询问是否替换
w.set_source(str(other))
type_into(oi, "draft-name", enter=True)
check("existing output is not an error", oi.hint.isHidden() and w.btn_go.isEnabled())
confirms.clear()
w.start()
wait_until(lambda: w.job is None)
check("existing output asks to replace", len(confirms) == 1, repr(confirms))

# 任务进行中路径框停用
sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
real_spawn = core.Job._spawn
core.Job._spawn = lambda self, cmd, **kw: real_spawn(self, sleeper, **kw)
type_into(oi, "busy", enter=True)
w.start()
wait_until(lambda: w.job is not None and w.job._procs)
check("inputs disabled while busy", not si.isEnabled() and not oi.isEnabled())
w.cancel_job()
wait_until(lambda: w.job is None)
core.Job._spawn = real_spawn
check("inputs enabled after job", si.isEnabled() and oi.isEnabled())

# 焦点移到窗口内其他控件时提交；切换到其他应用时保留草稿
w.set_source(str(src))
type_into(si, "/tmp/half-typ")
focus_out(si, Qt.ActiveWindowFocusReason)
check("switching apps keeps the draft uncommitted", si.pending and w.src == src, f"src={w.src}")
focus_out(si, Qt.TabFocusReason)
check("focus moving inside the window commits", not si.pending and w.src is None
      and si.hint.text() == tr("src_missing", path="/tmp/half-typ"), si.hint.text())

# 被拒绝的源在应用外创建后，切回应用即提交并可压缩
later_src = tmp / "later src"
type_into(si, str(later_src), enter=True)
check("precondition: missing source rejected", w.src is None and not w.btn_go.isEnabled())
later_src.mkdir()
(later_src / "c.txt").write_text("c")
w.on_app_state_changed(Qt.ApplicationActive)
check("rejected source committed after it appears", w.src == later_src and si.hint.isHidden()
      and w.btn_go.isEnabled() and oi.text() == str(tmp / "later src.zip"), oi.text())

# 已提交的有效源暂时消失：只提示，恢复后输出命名仍在
type_into(oi, "kept-name", enter=True)
later_src.rename(tmp / "moved away")
w.on_app_state_changed(Qt.ApplicationActive)
check("vanished source warns but keeps state", w.src == later_src and not si.hint.isHidden()
      and not w.btn_go.isEnabled(), si.hint.text())
(tmp / "moved away").rename(later_src)
w.on_app_state_changed(Qt.ApplicationActive)
check("restored source keeps output name", si.hint.isHidden() and w.btn_go.isEnabled()
      and oi.text() == str(tmp / "kept-name.zip"), oi.text())

# 切换格式前先提交输出草稿；不合格的草稿保留原文和提示
type_into(oi, "fmt-draft")
w.fmt_combo.setCurrentIndex(list(core.FORMATS).index("tar"))
check("format change commits output draft", oi.text() == str(tmp / "fmt-draft.tar"), oi.text())
type_into(oi, "rel/bad")
w.fmt_combo.setCurrentIndex(list(core.FORMATS).index("zip"))
check("format change keeps invalid draft and hint", oi.text() == "rel/bad"
      and oi.hint.text() == tr("path_not_absolute"), f"{oi.text()!r} {oi.hint.text()!r}")
type_into(oi, "", enter=True)

# 输出草稿有效时，旧的“文件夹不存在”提示不再阻止压缩
type_into(oi, f"{tmp}/nowhere/x.zip", enter=True)
check("precondition: missing folder blocks Go", not w.btn_go.isEnabled())
type_into(oi, "fine-name")
settle()
check("valid output draft re-enables Go", oi.hint.isHidden() and w.btn_go.isEnabled(),
      oi.hint.text())
type_into(oi, "", enter=True)

# 校验时的系统错误显示为提示，而不是让槽函数抛出异常
locked = tmp / "locked"
locked.mkdir()
locked.chmod(0)
type_into(oi, f"{locked}/sub/x.zip", enter=True)
check("OS error while parsing output becomes a hint",
      oi.hint.text() == tr("access_denied", path=f"{locked}/sub/x.zip"), oi.hint.text())
locked.chmod(0o700)
type_into(oi, "", enter=True)

# 键入指向文件夹的链接：显示并使用链接目标
link = tmp / "shortcut"
link.symlink_to(other)
type_into(si, str(link), enter=True)
real_other = Path(os.path.realpath(other))
check("symlink source resolves to target", w.src == real_other and si.text() == str(real_other),
      si.text())

w._quit_ready = True
w.close()
del w, settings  # 先让 QSettings 写完 ini，否则删除后它会在退出时重新建出临时目录
app.processEvents()
subprocess.run(["rm", "-rf", str(tmp)], check=False)
width = max(len(n) for n, _, _ in results)
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print(f"{sum(ok for _, ok, _ in results)}/{len(results)}",
      "ALL PASS" if all(ok for _, ok, _ in results) else "SOME FAILED")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)  # CI 按退出码判断
