"""访问被拒补救的离屏 GUI 夹具检查：路径框旁的补救按钮、失败对话框的按钮和它们的动作。

隐私保护（TCC）的拦截无法在测试里真实触发，用带 denial 的 JobError 模拟；POSIX 权限和锁定
用 /tmp 下的真实文件夹触发。不弹真实对话框（QMessageBox 和文件面板都被替换），不写用户偏好，
数据只放 /tmp。用法：python3 -B tests/gui/gui_permission_check.py
"""
from __future__ import annotations

import atexit
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

WT = Path(__file__).resolve().parents[2]  # 仓库根目录（tests/gui 的上两级）
sys.path.insert(0, str(WT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import core  # noqa: E402
import extract_panel  # noqa: E402
import gui_common  # noqa: E402
import main  # noqa: E402
from i18n import set_lang, tr  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录并立即打印结果：进程中途崩溃时，已经得出的结果不会丢失。"""
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(56)}  {detail}", flush=True)


def type_commit(edit, text: str) -> None:
    edit.setText(text)
    edit.textEdited.emit(text)
    QTest.keyClick(edit, Qt.Key_Return)
    app.processEvents()


def visible(widget) -> bool:
    return widget.isVisibleTo(w)


app = QApplication([])
import theme  # noqa: E402
theme.apply(app)  # 与 main.main() 一样套用 M3 主题，夹具测的才是实际界面
tmp = Path(tempfile.mkdtemp(prefix="cleanpack-guiperm-", dir="/tmp"))


def cleanup() -> None:
    # 先恢复 0 权限（chflags -R 进不去读不了的文件夹），再解除锁定标志，临时目录才能删除。
    if tmp.exists():
        subprocess.run(["chmod", "-R", "u+rwx", str(tmp)], check=False)
        subprocess.run(["chflags", "-R", "nouchg", str(tmp)], check=False)
        subprocess.run(["rm", "-rf", str(tmp)], check=False)


atexit.register(cleanup)

dialogs: list[str] = []
revealed: list[str] = []
settings_opened: list[bool] = []
picked: list[tuple[str, str]] = []  # (面板种类, 起始位置)
pick_result: list[str] = [""]       # 文件面板返回的路径；空字符串表示取消
boxes: list[dict] = []              # 访问被拒对话框的内容
click_text: list[str | None] = [None]  # 对话框里要点按的按钮文字；None 表示直接关闭

main.MainWindow.show_message = lambda self, icon, text: dialogs.append(text)
main.MainWindow.show_in_finder = lambda self, path: revealed.append(str(path))
main.open_privacy_settings = lambda: settings_opened.append(True) or True
main.confirm = lambda parent, title, text, key: True
main.brew_notice = lambda parent, tools: None


def fake_dir(parent, title, start="", *a, **k) -> str:
    picked.append(("folder", str(start)))
    return pick_result[0]


def fake_open(parent, title, start="", *a, **k) -> tuple[str, str]:
    picked.append(("file", str(start)))
    return pick_result[0], ""


def fake_save(parent, title, start="", *a, **k) -> tuple[str, str]:
    picked.append(("save", str(start)))
    return "", ""


# 所有文件面板都替换掉：离屏环境里弹出真实的原生面板会一直等待，检查就卡住了。
for module in (main, extract_panel):
    module.QFileDialog.getExistingDirectory = staticmethod(fake_dir)
    module.QFileDialog.getOpenFileName = staticmethod(fake_open)
    module.QFileDialog.getSaveFileName = staticmethod(fake_save)


def fake_exec(box: QMessageBox) -> int:
    """记录对话框内容，按 click_text 点按对应按钮；clickedButton 随之设置，与真实交互一致。"""
    # buttons() 的顺序由平台布局决定（macOS 把「好」排在前面），比较时按集合比较。
    buttons = sorted(b.text() for b in box.buttons())
    escape = box.escapeButton()
    boxes.append({"text": box.text(), "info": box.informativeText(), "buttons": buttons,
                  "escape": escape.text() if escape is not None else None})
    for b in box.buttons():
        if b.text() == click_text[0]:
            b.click()
            break
    return 0


QMessageBox.exec = fake_exec


def privacy(path: Path | str, key: str = "privacy_denied") -> core.JobError:
    return core.JobError(key, denial="privacy", path=str(path))


set_lang("en")
src = tmp / "src"
src.mkdir()
(src / "a.txt").write_text("alpha")
other = tmp / "other"
other.mkdir()
(other / "b.txt").write_text("beta")

settings = QSettings(str(tmp / "s.ini"), QSettings.IniFormat)
settings.setValue("format", "tar")
w = main.MainWindow(settings)
w.show()
app.processEvents()
si, oi = w.src_input, w.out_input

# ---------- 路径框旁的补救按钮 ----------
check("no fix button initially", not visible(si.fix_btn) and not visible(si.hint_row))
w.set_source(str(src))
app.processEvents()
check("valid source: no fix button", w.src == src and not visible(si.fix_btn))

unreadable = tmp / "unreadable"
unreadable.mkdir()
unreadable.chmod(0)
type_commit(si, str(unreadable))
check("POSIX denial shows hint and Show in Finder",
      si.hint.text() == tr("src_denied", path=unreadable) and visible(si.hint)
      and visible(si.fix_btn) and si.fix_btn.text() == tr("show_in_finder"),
      f"{si.hint.text()!r} {si.fix_btn.text()!r}")
si.fix_btn.click()
check("Show in Finder reveals the denied item", revealed == [str(unreadable)], str(revealed))

# 隐私保护：校验函数（stat）被拒时的 EPERM 用模拟的错误代替。
protected = tmp / "protected"
protected.mkdir()
real_validate = core.validate_source


def validate_privacy(p):
    if Path(p) == protected:
        raise privacy(protected)
    return real_validate(p)


core.validate_source = validate_privacy
type_commit(si, str(protected))
check("privacy denial shows Grant Access",
      si.hint.text() == tr("privacy_denied", path=protected) and visible(si.fix_btn)
      and si.fix_btn.text() == tr("grant_access"), f"{si.hint.text()!r} {si.fix_btn.text()!r}")
pick_result[0] = str(other)
si.fix_btn.click()
app.processEvents()
check("Grant Access opens a folder panel at the denied item",
      picked[-1] == ("folder", str(protected)), str(picked))
check("choosing another location replaces the source",
      w.src == other and si.text() == str(other) and not visible(si.fix_btn),
      f"{w.src} {si.text()!r}")
core.validate_source = real_validate

# 重新选择时取消：路径框保持不变。
picked.clear()
pick_result[0] = ""
w.reselect("source", str(src / "a.txt"))
check("reselecting a file uses the file panel; cancel keeps the source",
      picked == [("file", str(src / "a.txt"))] and w.src == other, f"{picked} {w.src}")

# 锁定（uchg）的输出文件夹：预检提示在输出框，补救是在访达中显示。
w.set_source(str(src))
locked = tmp / "locked"
locked.mkdir()
os.chflags(locked, stat.UF_IMMUTABLE)
type_commit(oi, str(locked) + "/")
app.processEvents()
check("locked output folder noted with Show in Finder",
      oi.hint.text() == tr("locked_denied", path=locked) and visible(oi.fix_btn)
      and oi.fix_btn.text() == tr("show_in_finder") and not w.btn_go.isEnabled(),
      f"{oi.hint.text()!r} {oi.fix_btn.text()!r}")
revealed.clear()
oi.fix_btn.click()
check("Show in Finder reveals the locked folder", revealed == [str(locked)], str(revealed))
os.chflags(locked, 0)
type_commit(oi, "")
check("note clears after restoring the default output", not visible(oi.fix_btn)
      and w.btn_go.isEnabled(), oi.hint.text())

# 切回应用时重新校验源框；输出框只在键入后没通过或还没提交时重新校验，已提交的有效输出
# 由按钮刷新时的预检重新判断（重校验它会留下错误，之后换格式也不再更新路径）。
rechecked: list[str] = []
real_si_recheck, real_oi_recheck = si.recheck, oi.recheck
si.recheck = lambda: rechecked.append("source")
oi.recheck = lambda: rechecked.append("output")
w.on_app_state_changed(Qt.ApplicationActive)
check("reactivation rechecks the source only", rechecked == ["source"], str(rechecked))
oi.recheck = real_oi_recheck
type_commit(oi, "relative/out")
oi.recheck = lambda: rechecked.append("output")
rechecked.clear()
w.on_app_state_changed(Qt.ApplicationActive)
check("reactivation rechecks a rejected output", rechecked == ["source", "output"],
      str(rechecked))
si.recheck, oi.recheck = real_si_recheck, real_oi_recheck
type_commit(oi, "")

# 翻译与停用。
core.validate_source = validate_privacy
type_commit(si, str(protected))
set_lang("zh_Hans")
w.retranslate()
check("fix button retranslates", si.fix_btn.text() == tr("grant_access")
      and "授予" in si.fix_btn.text(), si.fix_btn.text())
set_lang("en")
w.retranslate()
si.setEnabled(False)
check("fix button disabled with its field", not si.fix_btn.isEnabled())
si.setEnabled(True)
check("fix button enabled again", si.fix_btn.isEnabled())
core.validate_source = real_validate

# 系统保护（SIP）的位置没有补救办法：路径框只显示原因，不显示按钮。
sip = tmp / "sip"
sip.mkdir()


def validate_system(p):
    if Path(p) == sip:
        raise core.JobError("system_read_denied", denial="system", path=str(sip))
    return real_validate(p)


core.validate_source = validate_system
type_commit(si, str(sip))
check("system denial in a path field: hint only, no fix button",
      visible(si.hint) and si.hint.text() == tr("system_read_denied", path=sip)
      and not visible(si.fix_btn), si.hint.text())
core.validate_source = real_validate
w.set_source(str(src))
app.processEvents()

# ---------- 失败对话框 ----------
boxes.clear()
click_text[0] = None
w.report_failure("failed", privacy(src))
box = boxes[-1] if boxes else {}
check("privacy failure: Grant Access, settings and OK buttons",
      box.get("buttons") == sorted([tr("grant_access"), tr("open_privacy_settings"), tr("ok")]),
      str(box.get("buttons")))
check("dialog closes with Esc on OK", box.get("escape") == tr("ok"), str(box.get("escape")))
check("privacy failure text and help",
      box.get("text") == tr("failed", err=tr("privacy_denied", path=src))
      and box.get("info", "").startswith(tr("privacy_grant_hint", button=tr("grant_access")))
      and tr("privacy_help_dev") in box.get("info", ""), repr(box.get("info")))

picked.clear()
pick_result[0] = ""
click_text[0] = tr("grant_access")
w.report_failure("failed", privacy(src))
check("dialog Grant Access reselects the source at the denied item",
      picked == [("folder", str(src))], str(picked))

# 被拦住的是源里面的子文件夹（开始前的检查逐层列出）：重新选择会把源换成那个子文件夹，
# 所以不提供「授予访问权限…」，说明可以换个文件夹或用完全磁盘访问权限。
inner = src / "inner"
inner.mkdir()
picked.clear()
w.report_failure("failed", privacy(inner))
check("denied subfolder of the source: no reselect, explains the options",
      boxes[-1]["buttons"] == sorted([tr("open_privacy_settings"), tr("ok")])
      and boxes[-1]["info"].startswith(tr("privacy_inside")) and picked == [] and w.src == src,
      repr(boxes[-1]))

# 为授权重新选择同一个源，但面板返回的写法不同（大小写）：不重新提交，输出设置保留。
variant = Path(str(src).upper())
if variant.exists() and str(variant) != str(src):
    w.out_dir, w.out_stem = other, "custom"
    w.update_output_path()
    pick_result[0] = str(variant)
    w.report_failure("failed", privacy(src))
    check("regranting the source in another spelling keeps the output settings",
          w.src == src and w.out_dir == other and w.out_stem == "custom",
          f"{w.src} {w.out_dir} {w.out_stem}")
    w.out_dir = w.out_stem = None
    w.update_output_path()
else:
    print("SKIP  卷区分大小写，跳过换写法重新选择的检查", flush=True)

# 上级文件夹不可进入时，被拒的项目本身 stat 不到，在访达中显示挡住访问的那个上级。
blocked = tmp / "blocked"
(blocked / "inner").mkdir(parents=True)
blocked.chmod(0)
revealed.clear()
click_text[0] = tr("show_in_finder")
w.report_failure("failed", core.JobError("src_denied", denial="posix",
                                         path=str(blocked / "inner")))
check("Show in Finder falls back to the blocking parent folder", revealed == [str(blocked)],
      str(revealed))
blocked.chmod(0o755)
click_text[0] = tr("grant_access")

settings_opened.clear()
click_text[0] = tr("open_privacy_settings")
w.report_failure("failed", core.JobError("privacy_partial", denial="privacy", tool="tar"))
box = boxes[-1]
check("partial denial: no Grant Access, settings opened",
      box["buttons"] == sorted([tr("open_privacy_settings"), tr("ok")])
      and settings_opened == [True]
      and tr("privacy_grant_hint", button=tr("grant_access")) not in box["info"],
      str(box["buttons"]))

# 对不上路径框的位置（例如源以外的某个文件）只提供系统设置。
w.report_failure("failed", privacy(tmp / "elsewhere"))
check("unrelated path: settings only",
      boxes[-1]["buttons"] == sorted([tr("open_privacy_settings"), tr("ok")]), str(boxes[-1]))

# 输出位置被拦：暂存和提交都在文件夹里进行，重新选择走文件夹面板，只换目录、保留名称。
picked.clear()
pick_result[0] = str(other)
click_text[0] = tr("grant_access")
stem = w.output_path().name
w.report_failure("failed", privacy(w.output_path().parent))
check("output denial reselects the folder, keeping the name",
      picked == [("folder", str(src.parent))] and w.output_path() == other / stem,
      f"{picked} {w.output_path()}")
w.out_dir = None
w.update_output_path()
picked.clear()
pick_result[0] = ""
w.report_failure("failed", privacy(w.output_path()))
check("output file path starts the panel in its folder", picked == [("folder", str(src.parent))],
      str(picked))

revealed.clear()
click_text[0] = tr("show_in_finder")
w.report_failure("failed", core.JobError("access_denied", denial="posix", path=str(src)))
check("POSIX failure: Show in Finder and OK",
      boxes[-1]["buttons"] == sorted([tr("show_in_finder"), tr("ok")])
      and revealed == [str(src)],
      f"{boxes[-1]['buttons']} {revealed}")

dialogs.clear()
n = len(boxes)
w.report_failure("failed", core.JobError("system_denied", denial="system", path="/System/x"))
w.report_failure("failed", core.JobError("tool_failed", tool="tar", code=2))
check("system denial and other errors use the plain message",
      len(boxes) == n and dialogs == [tr("failed", err=tr("system_denied", path="/System/x")),
                                      tr("failed", err=tr("tool_failed", tool="tar", code=2))],
      str(dialogs))

# 任务失败经 on_job_done 走同一个对话框，日志也记下原因。
click_text[0] = None
w.job = core.ArchiveJob(src, tmp / "out.tar", "tar", log=lambda line: None)
w.on_job_done(None, privacy(src))
check("job failure with privacy denial shows the dialog",
      tr("grant_access") in boxes[-1]["buttons"]
      and tr("failed", err=tr("privacy_denied", path=src)) in w.log_view.toPlainText())

# 开始前的预检被拒（例如源被锁在 0 权限的文件夹里）也走这个对话框。
boxes.clear()
w.set_source(str(src))
real_check = core.check_compress_output
core.check_compress_output = lambda s, o: (_ for _ in ()).throw(privacy(o.parent))
w.start()
core.check_compress_output = real_check
check("preflight denial shows the dialog", bool(boxes)
      and tr("grant_access") in boxes[-1]["buttons"] and w.job is None, str(boxes))

# ---------- 解压模式 ----------
archive = tmp / "a.tar"
subprocess.run(["/usr/bin/tar", "-c", "-f", str(archive), "-C", str(src), "a.txt"], check=True)
dest = tmp / "dest"
dest.mkdir()
w.mode_tabs.setCurrentIndex(1)
app.processEvents()
panel = w.extract_panel
panel.set_archive(str(archive))
pick_result[0] = str(dest)
panel.choose_dest()
app.processEvents()
check("extract mode ready", panel.archive == archive and panel.dest_path() == dest,
      f"{panel.archive} {panel.dest_path()}")
picked.clear()
pick_result[0] = ""
click_text[0] = tr("grant_access")
w.report_failure("extract_failed", privacy(archive))
check("archive denial reselects with the archive panel", picked == [("file", str(archive))],
      str(picked))
picked.clear()
w.report_failure("extract_failed", privacy(dest / ".cleanpack-x.part"))
check("destination denial reselects with the folder panel",
      picked == [("folder", str(dest / ".cleanpack-x.part"))], str(picked))

# 为授权重新选择默认的目标文件夹（归档所在的文件夹）时保持「跟随归档」。
panel.dest_dir = None
panel.dest_input.display(str(archive.parent))
picked.clear()
pick_result[0] = str(archive.parent)
w.report_failure("extract_failed", privacy(archive.parent))
check("regranting the default destination keeps it following the archive",
      picked == [("folder", str(archive.parent))] and panel.dest_dir is None
      and panel.dest_path() == archive.parent, f"{picked} {panel.dest_dir}")
arc_variant = Path(str(archive.parent).upper())
if arc_variant.exists() and str(arc_variant) != str(archive.parent):
    pick_result[0] = str(arc_variant)
    w.report_failure("extract_failed", privacy(archive.parent))
    check("regranting the default destination in another spelling keeps following",
          panel.dest_dir is None, str(panel.dest_dir))
pick_result[0] = str(dest)
w.report_failure("extract_failed", privacy(archive.parent))
check("choosing another destination while regranting sets it", panel.dest_dir == dest,
      str(panel.dest_dir))

# 解压面板的补救按钮经主窗口处理。
dest_locked = tmp / "dest-locked"
dest_locked.mkdir()
os.chflags(dest_locked, stat.UF_IMMUTABLE)
di = panel.dest_input
type_commit(di, str(dest_locked))
panel.refresh_note()
app.processEvents()
revealed.clear()
check("locked destination noted with Show in Finder",
      di.hint.text() == tr("locked_denied", path=dest_locked) and visible(di.fix_btn),
      di.hint.text())
di.fix_btn.click()
check("destination Show in Finder", revealed == [str(dest_locked)], str(revealed))
os.chflags(dest_locked, 0)
type_commit(di, str(dest))

# 归档框的补救按钮：放在归档分组里（不是游离的窗口），并按归档框处理。
ai = panel.arc_input
unreadable_arc = tmp / "unreadable.tar"
unreadable_arc.write_bytes(archive.read_bytes())
unreadable_arc.chmod(0)
type_commit(ai, str(unreadable_arc))
revealed.clear()
check("unreadable archive noted with Show in Finder inside the archive group",
      visible(ai.fix_btn) and ai.fix_btn.text() == tr("show_in_finder")
      and ai.hint_row.parentWidget() is panel.arc_box and not ai.hint_row.isWindow(),
      ai.hint.text())
ai.fix_btn.click()
check("archive Show in Finder", revealed == [str(unreadable_arc)], str(revealed))
unreadable_arc.chmod(0o644)
real_validate_archive = core.validate_archive


def validate_archive_privacy(p):
    if Path(p) == unreadable_arc:
        raise privacy(unreadable_arc)
    return real_validate_archive(p)


core.validate_archive = validate_archive_privacy
type_commit(ai, str(unreadable_arc))
picked.clear()
pick_result[0] = ""
ai.fix_btn.click()
check("archive Grant Access opens the archive panel at it",
      ai.fix_btn.text() == tr("grant_access") and picked == [("file", str(unreadable_arc))],
      str(picked))
core.validate_archive = real_validate_archive
type_commit(ai, str(archive))

# 解压开始前的预检被拒也走带补救按钮的对话框。
boxes.clear()
click_text[0] = None
real_check_dir = core.check_output_dir
core.check_output_dir = lambda p: (_ for _ in ()).throw(privacy(p))
w.start()
core.check_output_dir = real_check_dir
check("extract preflight denial shows the dialog", bool(boxes)
      and tr("grant_access") in boxes[-1]["buttons"] and w.job is None, str(boxes))

# ---------- 打开系统设置 ----------
opened: list[str] = []
answers = [False, True]
gui_common.QDesktopServices.openUrl = staticmethod(
    lambda url: (opened.append(url.toString()), answers.pop(0))[1])
ok = gui_common.open_privacy_settings()
check("settings URL falls back to the pane without anchor",
      ok and opened == list(gui_common.PRIVACY_SETTINGS_URLS), str(opened))
answers[:] = [False, False]
check("settings failure reported", not gui_common.open_privacy_settings())
main.open_privacy_settings = lambda: False
w.log_view.clear()
w.open_privacy_settings()
check("failure to open settings is logged",
      tr("privacy_settings_failed") in w.log_view.toPlainText())

sys.frozen = True
check("packaged app help names CleanPack", gui_common.privacy_help() == tr("privacy_help"))
del sys.frozen
check("source run help names the launching app",
      gui_common.privacy_help() == tr("privacy_help_dev"))

w._quit_ready = True
w.close()
settings.sync()
cleanup()
width = max(len(n) for n, _, _ in results)
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print(f"{sum(ok for _, ok, _ in results)}/{len(results)}",
      "ALL PASS" if all(ok for _, ok, _ in results) else "SOME FAILED")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
