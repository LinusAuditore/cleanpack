"""阶段 6b 的离屏 GUI 夹具检查：加密选项、密码校验、真实加密压缩、询问密码重试、退出与四语。

不弹真实对话框（真实的密码对话框只用定时器自动操作），不写用户偏好，数据只放 /tmp。
用法：python3 -B tests/gui/gui_stage6b_check.py
"""
from __future__ import annotations

import atexit
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WT = Path(__file__).resolve().parents[2]  # 仓库根目录（tests/gui 的上两级）
sys.path.insert(0, str(WT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel,  # noqa: E402
                               QLineEdit)

import core  # noqa: E402
import gui_common  # noqa: E402
import main  # noqa: E402
from i18n import set_lang, tr  # noqa: E402

results: list[tuple[str, bool, str]] = []
PW = "Secret pw 1!"


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录并立即打印结果：进程中途崩溃时，已经得出的结果不会丢失。"""
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(56)}  {detail}", flush=True)


def wait_until(cond, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def type_commit(edit, text: str) -> None:
    edit.setText(text)
    edit.textEdited.emit(text)
    QTest.keyClick(edit, Qt.Key_Return)
    app.processEvents()


def select_format(key: str) -> None:
    w.fmt_combo.setCurrentIndex(list(core.FORMATS).index(key))
    app.processEvents()


def visible(widget) -> bool:
    return widget.isVisibleTo(w)


app = QApplication([])
import theme  # noqa: E402
theme.apply(app)  # 与 main.main() 一样套用 M3 主题，夹具测的才是实际界面
tmp = Path(tempfile.mkdtemp(prefix="cleanpack-gui6b-", dir="/tmp"))
# 中途失败也删掉测试数据（进程被信号杀死时除外）。
atexit.register(lambda: (subprocess.run(["chmod", "-R", "u+rwx", str(tmp)], check=False),
                         subprocess.run(["rm", "-rf", str(tmp)], check=False)))
# 保留每个窗口的引用：Python 包装对象被回收时，Qt 可能还在处理那个窗口的事件，进程会崩溃。
windows: list[main.MainWindow] = []
dialogs: list[str] = []
titles: list[str] = []
main.MainWindow.show_message = lambda self, icon, text: dialogs.append(text)
main.MainWindow._show_done = lambda self, path, msg, title: (dialogs.append(msg),
                                                             titles.append(title))
main.confirm = lambda parent, title, text, key: True
main.brew_notice = lambda parent, tools: None
set_lang("en")

src = tmp / "机密 src"
(src / "sub").mkdir(parents=True)
(src / "a.txt").write_text("alpha")
(src / "sub" / "中文.txt").write_text("内容")
(src / ".DS_Store").write_text("junk")

ini = str(tmp / "s.ini")
settings = QSettings(ini, QSettings.IniFormat)
settings.setValue("format", "zip")
w = main.MainWindow(settings)
windows.append(w)
w.show()
app.processEvents()
enc = w.encrypt_panel

# ---------------- 加密选项的显示 ----------------
check("encrypt off by default, fields hidden", enc.encrypt_chk.isEnabled()
      and not enc.encrypt_chk.isChecked() and not visible(enc.pw_input)
      and not visible(enc.method_combo) and enc.password() is None)
type_commit(w.src_input, str(src))
check("go enabled without encryption", w.btn_go.isEnabled())
enc.encrypt_chk.setChecked(True)
app.processEvents()
check("zip: password, confirm and method shown", visible(enc.pw_input)
      and visible(enc.pw_confirm) and visible(enc.method_combo) and not visible(enc.names_chk)
      and enc.method_combo.currentData() == "aes256"
      and enc.method_combo.currentText() == tr("zip_aes256"))
check("password fields masked and refuse drops",
      enc.pw_input.echoMode() == QLineEdit.EchoMode.Password
      and not enc.pw_input.acceptDrops() and not enc.pw_confirm.acceptDrops())
check("empty password: go disabled, no hint yet", not w.btn_go.isEnabled()
      and not visible(enc.hint), enc.hint.text())
check("zip note says names visible", enc.note.text() == tr("zip_names_note"), enc.note.text())

enc.pw_input.setText(PW)
app.processEvents()
check("confirm missing hint", enc.hint.text() == tr("password_confirm")
      and not w.btn_go.isEnabled(), enc.hint.text())
enc.pw_confirm.setText(PW + "x")
app.processEvents()
check("mismatch hint", enc.hint.text() == tr("password_mismatch") and not w.btn_go.isEnabled())
enc.pw_confirm.setText(PW)
app.processEvents()
check("matching passwords enable go", not visible(enc.hint) and w.btn_go.isEnabled()
      and enc.password() == PW)

enc.show_chk.setChecked(True)
check("show password", enc.pw_input.echoMode() == QLineEdit.EchoMode.Normal
      and enc.pw_confirm.echoMode() == QLineEdit.EchoMode.Normal)
enc.show_chk.setChecked(False)

enc.method_combo.setCurrentIndex(1)
app.processEvents()
check("zipcrypto adds weak note", tr("zipcrypto_note") in enc.note.text()
      and enc.zip_method() == "zipcrypt", enc.note.text())
enc.method_combo.setCurrentIndex(0)

for field in (enc.pw_input, enc.pw_confirm):
    field.setText("café")
app.processEvents()
check("zip rejects non-ASCII", enc.hint.text() == tr("password_not_ascii")
      and not w.btn_go.isEnabled(), enc.hint.text())
select_format("7z")
check("7z accepts non-ASCII, names option shown", not visible(enc.hint)
      and visible(enc.names_chk) and enc.names_chk.isChecked() and not visible(enc.method_combo)
      and w.btn_go.isEnabled() == (not w.missing_tools()[1]))
enc.names_chk.setChecked(False)
app.processEvents()
check("7z names-visible note", enc.note.text() == tr("names_visible_note"), enc.note.text())
enc.names_chk.setChecked(True)

long_pw = "k" * (core.PASSWORD_MAX + 1)
for field in (enc.pw_input, enc.pw_confirm):
    field.setText(long_pw)
app.processEvents()
check("over-long password not truncated, rejected",
      enc.pw_input.text() == long_pw
      and enc.hint.text() == tr("password_too_long", max=core.PASSWORD_MAX))

select_format("tar.gz")
check("tar: encrypt disabled with note, password ignored",
      not enc.encrypt_chk.isEnabled() and enc.password() is None
      and enc.note.text() == tr("encrypt_tar_note") and not visible(enc.pw_input)
      and w.btn_go.isEnabled() == (not w.missing_tools()[1]))
select_format("zip")
check("back to zip keeps typed password", enc.pw_input.text() == long_pw
      and enc.encrypt_chk.isChecked() and visible(enc.pw_input))

# ---------------- 横幅：加密 zip 不需要 zip 工具 ----------------
for field in (enc.pw_input, enc.pw_confirm):
    field.setText(PW)
saved_zip = w.tools.paths["zip"]
w.tools.paths["zip"] = None
w.update_go_state()
check("encrypted zip needs no zip tool", w.warn_lbl.isHidden() and w.btn_go.isEnabled(),
      w.warn_lbl.text())
enc.encrypt_chk.setChecked(False)
app.processEvents()
check("plain zip still needs zip", not w.warn_lbl.isHidden()
      and w.warn_lbl.text() == tr("missing_tools", fmt="zip", tools="zip"), w.warn_lbl.text())
w.tools.paths["zip"] = saved_zip
enc.encrypt_chk.setChecked(True)
app.processEvents()

# ---------------- 真实加密压缩 ----------------
w.log_view.clear()
dialogs.clear()
w.start()
wait_until(lambda: w.job is None)
out = w.output_path()
check("encrypted zip created", out.exists() and core.zip_is_encrypted(out)
      and titles[-1:] == [tr("done_title")], repr(dialogs))
check("password fields cleared after success, option kept",
      enc.pw_input.text() == "" and enc.pw_confirm.text() == "" and enc.encrypt_chk.isChecked()
      and not w.btn_go.isEnabled())
enc.show_chk.setChecked(True)
for field in (enc.pw_input, enc.pw_confirm):
    field.undo()
check("cleared password cannot be restored with undo",
      enc.pw_input.text() == "" and enc.pw_confirm.text() == ""
      and not enc.pw_input.isUndoAvailable(), repr(enc.pw_input.text()))
enc.show_chk.setChecked(False)
check("password not in log", PW not in w.log_view.toPlainText())
check("zip made by bsdtar", "--format zip" in w.log_view.toPlainText())
dest = tmp / "check"
dest.mkdir()
core.ExtractJob(out, dest, log=lambda _: None, password=PW).run()
check("archive opens with the password", (dest / "机密 src" / "a.txt").read_text() == "alpha"
      and not (dest / "机密 src" / ".DS_Store").exists())

# 7z 加密压缩
select_format("7z")
for field in (enc.pw_input, enc.pw_confirm):
    field.setText(PW)
app.processEvents()
seven_ok = core.locate("7z") is not None
if seven_ok:
    w.start()
    wait_until(lambda: w.job is None)
    out7 = w.output_path()
    listing = subprocess.run([core.locate("7z"), "l", "-p", str(out7)], capture_output=True,
                             text=True, stdin=subprocess.DEVNULL)
    check("encrypted 7z created with hidden names", out7.exists() and listing.returncode != 0
          and "a.txt" not in listing.stdout)
else:
    check("7zz available for 7z checks", False, "7zz missing")

# 任务进行中停用加密选项
sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
real_spawn = core.Job._spawn
core.Job._spawn = lambda self, cmd, **kw: real_spawn(self, sleeper, **kw)
for field in (enc.pw_input, enc.pw_confirm):
    field.setText(PW)
app.processEvents()
w.start()
wait_until(lambda: w.job is not None and w.job._procs)
check("encrypt options disabled while busy", not enc.encrypt_chk.isEnabled()
      and not enc.pw_input.isEnabled() and not enc.names_chk.isEnabled())
w.cancel_job()
wait_until(lambda: w.job is None)
core.Job._spawn = real_spawn
check("re-enabled after cancel, password kept after failure",
      enc.encrypt_chk.isEnabled() and enc.pw_input.isEnabled() and enc.pw_input.text() == PW)

# ---------------- 解压：询问密码并重试 ----------------
w.mode_tabs.setCurrentIndex(1)
app.processEvents()
panel = w.extract_panel
archive = out7 if seven_ok else out
answers = ["wrong", PW]
asked: list[tuple[str, str | None]] = []


def scripted(parent, text, error=None):
    asked.append((text, error))
    return answers.pop(0) if answers else None


main.ask_password = scripted
type_commit(panel.arc_input, str(archive))
dest2 = tmp / "dest2"
dest2.mkdir()
type_commit(panel.dest_input, str(dest2))
dialogs.clear()
titles.clear()
w.log_view.clear()
w.start()
wait_until(lambda: w.job is None and not answers, 60)
wait_until(lambda: w.job is None)
check("asked twice, second time with wrong-password reason",
      asked == [(tr("password_prompt", name=archive.name), None),
                (tr("password_prompt", name=archive.name), tr("archive_wrong_password"))],
      repr(asked))
check("extracted after retry", (dest2 / "机密 src" / "a.txt").exists()
      and titles == [tr("extract_done_title")], repr((dialogs, titles)))
check("no failure dialog during retries", not any(tr("extract_failed", err="")[:8] in d
                                                  for d in dialogs), repr(dialogs))
check("passwords never logged", PW not in w.log_view.toPlainText()
      and "wrong" not in w.log_view.toPlainText().replace("Wrong password", ""))

# 未加密的归档不询问
plain = tmp / "plain.zip"
core.ArchiveJob(src, plain, "zip", log=lambda _: None).run()
asked.clear()
type_commit(panel.arc_input, str(plain))
w.start()
wait_until(lambda: w.job is None)
check("plain archive does not ask", asked == [])

# 记录启动过多少个任务，用来确认取消或关闭之后没有再启动解压。
launched: list[core.Job] = []
real_launch = main.MainWindow._launch


def counting_launch(self, job):
    launched.append(job)
    real_launch(self, job)


main.MainWindow._launch = counting_launch

# 取消询问：只问一次，不再启动任务，按取消记录
asked.clear()
main.ask_password = lambda parent, text, error=None: asked.append(text)
type_commit(panel.arc_input, str(archive))
w.log_view.clear()
launched.clear()
w.start()
wait_until(lambda: w.job is None)
time.sleep(0.3)
app.processEvents()
check("cancel at prompt: asked once, no relaunch", len(asked) == 1 and len(launched) == 1
      and w.job is None and w.log_view.toPlainText().rstrip().endswith(tr("extract_cancelled")),
      repr((asked, len(launched))))

# 退出确认框显示期间任务需要密码：先不询问；用户选择不退出后再询问
real_execute = core.ExtractJob._execute


def slow_execute(self):
    time.sleep(0.5)  # 保证点关闭时任务还在运行
    return real_execute(self)


core.ExtractJob._execute = slow_execute
order: list[str] = []
main.ask_password = lambda parent, text, error=None: order.append("ask")


def confirm_after_job(decision):
    def fake(parent, title, text, key):
        order.append("confirm")
        wait_until(lambda: w.job is None, 10)  # 任务在确认框打开期间结束
        order.append("job done while confirming")
        return decision
    return fake


main.confirm = confirm_after_job(False)
launched.clear()
w.start()
wait_until(lambda: w.job is not None)
w.close()
app.processEvents()
check("prompt deferred until quit is declined",
      order == ["confirm", "job done while confirming", "ask"] and w.isVisible()
      and len(launched) == 1, repr(order))

# 拒绝退出之后，下一次需要密码时照常询问（确认框的标记已复位）
order.clear()
w.start()
wait_until(lambda: w.job is None and order, 10)
check("after declining quit, later prompts are not deferred", order == ["ask"], repr(order))

# 同样的情形，用户确认退出：不再询问密码，窗口关闭
order.clear()
main.confirm = confirm_after_job(True)
launched.clear()
w.log_view.clear()  # 之前的场景也记过“已取消”，清空后才能确认这一条来自这里
w.start()
wait_until(lambda: w.job is not None)
w.close()
wait_until(lambda: not w.isVisible(), 10)
check("quit confirmed: no prompt, no new job, window closed",
      order == ["confirm", "job done while confirming"] and len(launched) == 1
      and not w.isVisible(), repr(order))
check("dropped retry logged as cancelled",
      tr("extract_cancelled") in w.log_view.toPlainText().splitlines(),
      w.log_view.toPlainText()[-200:])
# 用户已确认退出、任务随后以需要密码结束：不询问，窗口关闭
w = main.MainWindow(QSettings(ini, QSettings.IniFormat))
windows.append(w)
w.show()
w.mode_tabs.setCurrentIndex(1)
type_commit(w.extract_panel.arc_input, str(archive))


def slow_encrypted(self):
    time.sleep(0.5)
    raise core.JobError("archive_encrypted")  # 不理会取消，模拟取消落在报错之后


core.ExtractJob._execute = slow_encrypted
main.confirm = lambda parent, title, text, key: True
order.clear()
w.start()
wait_until(lambda: w.job is not None)
w.close()
closed = wait_until(lambda: not w.isVisible(), 10)
check("password error after quit confirmed: no prompt, window closes",
      closed and order == [] and w.job is None, repr(order))
core.ExtractJob._execute = real_execute

# “密码可能不对或归档损坏”也重新询问
w = main.MainWindow(QSettings(ini, QSettings.IniFormat))
windows.append(w)
w.show()
w.mode_tabs.setCurrentIndex(1)
type_commit(w.extract_panel.arc_input, str(archive))
dest3 = tmp / "dest3"
dest3.mkdir()
type_commit(w.extract_panel.dest_input, str(dest3))
attempts: list[str | None] = []


def damaged_then_real(self):
    attempts.append(self.password)
    if len(attempts) == 1:
        raise core.JobError("archive_encrypted")             # 第一次没有密码
    if len(attempts) == 2:
        raise core.JobError("archive_password_or_damaged")   # 带密码的一次失败
    return real_execute(self)


core.ExtractJob._execute = damaged_then_real
asked.clear()
main.ask_password = lambda parent, text, error=None: (asked.append(error), PW)[1]
w.start()
wait_until(lambda: w.job is None and len(attempts) >= 3, 20)
wait_until(lambda: w.job is None)
check("password-or-damaged asks again with its reason",
      asked == [None, tr("archive_password_or_damaged")] and attempts == [None, PW, PW]
      and (dest3 / "机密 src").exists(), repr((asked, attempts)))
core.ExtractJob._execute = real_execute

# 询问期间用户确认了退出（例如还有安装在进行，窗口没有立即关闭）：回答密码后不再启动任务
w = main.MainWindow(QSettings(ini, QSettings.IniFormat))
windows.append(w)
w.show()
w.mode_tabs.setCurrentIndex(1)
type_commit(w.extract_panel.arc_input, str(archive))


def quit_then_answer(parent, text, error=None):
    w._quit_requested = True  # 等同于在密码框打开期间确认了退出
    return PW


main.ask_password = quit_then_answer
launched.clear()
w.start()
wait_until(lambda: w.job is None and launched, 10)
time.sleep(0.3)
app.processEvents()
check("quit confirmed during prompt starts no job", len(launched) == 1 and w.job is None,
      repr(len(launched)))

# 询问期间窗口关闭：不再启动新任务
w = main.MainWindow(QSettings(ini, QSettings.IniFormat))
windows.append(w)
w.show()
panel = w.extract_panel
w.mode_tabs.setCurrentIndex(1)


def close_then_answer(parent, text, error=None):
    w.close()
    app.processEvents()
    return PW


main.ask_password = close_then_answer
type_commit(panel.arc_input, str(archive))
launched.clear()
w.start()
wait_until(lambda: w.job is None)
time.sleep(0.3)
app.processEvents()
check("closing during prompt starts no job", w.job is None and not w.isVisible()
      and len(launched) == 1, repr(len(launched)))
main.MainWindow._launch = real_launch

# ---------------- 真实密码对话框：按钮与校验 ----------------
w3 = main.MainWindow(QSettings(ini, QSettings.IniFormat))
windows.append(w3)
state: dict[str, object] = {}


def drive_dialog() -> None:
    dlg = next(d for d in QApplication.topLevelWidgets() if isinstance(d, QDialog)
               and d.isVisible())
    box = dlg.findChild(QDialogButtonBox)
    ok = [b for b in box.buttons() if box.buttonRole(b) == QDialogButtonBox.ButtonRole.AcceptRole][0]
    field = dlg.findChild(QLineEdit)
    labels = [lbl.text() for lbl in dlg.findChildren(QLabel)]
    visible_hints = [lbl.text() for lbl in dlg.findChildren(QLabel)
                     if lbl.isVisibleTo(dlg) and lbl.text() not in ("prompt text", "error text")]
    state.update(title=dlg.windowTitle(), ok_text=ok.text(), empty_ok=ok.isEnabled(),
                 masked=field.echoMode() == QLineEdit.EchoMode.Password,
                 labels=labels, buttons=sorted(b.text() for b in box.buttons()),
                 drops=field.acceptDrops(), empty_hints=visible_hints, field=field)
    field.setText(PW)
    state["typed_ok"] = ok.isEnabled()
    QTest.keyClick(field, Qt.Key_Return)  # 回车即确定


QTimer.singleShot(200, drive_dialog)
got = gui_common.ask_password(w3, "prompt text", "error text")
check("real dialog: title, buttons, masked", state.get("title") == tr("password_title")
      and state.get("buttons") == sorted([tr("extract"), tr("cancel")])
      and state.get("masked") is True, repr(state))
check("real dialog: empty disables OK, Return accepts", state.get("empty_ok") is False
      and state.get("typed_ok") is True and got == PW, repr(state))
check("real dialog shows prompt and error", "prompt text" in state.get("labels", [])
      and "error text" in state.get("labels", []), repr(state.get("labels")))
check("real dialog refuses drops and shows no hint while empty",
      state.get("drops") is False and state.get("empty_hints") == [],
      repr((state.get("drops"), state.get("empty_hints"))))
kept = state.pop("field")  # 对话框已 deleteLater，但还没处理删除事件
# 密码模式下 Qt 拒绝撤销，要先切成明文再试，才能看出撤销历史里是否还留着密码。
kept.setEchoMode(QLineEdit.EchoMode.Normal)
kept.undo()
check("dialog field cleared without undo history before deletion", kept.text() == "",
      repr(kept.text()))


def cancel_dialog() -> None:
    dlg = next(d for d in QApplication.topLevelWidgets() if isinstance(d, QDialog)
               and d.isVisible())
    dlg.findChild(QLineEdit).setText("typed")
    QTest.keyClick(dlg, Qt.Key_Escape)


QTimer.singleShot(200, cancel_dialog)
check("real dialog: Esc cancels", gui_common.ask_password(w3, "p") is None)


def non_ascii_dialog() -> None:
    dlg = next(d for d in QApplication.topLevelWidgets() if isinstance(d, QDialog)
               and d.isVisible())
    box = dlg.findChild(QDialogButtonBox)
    ok = [b for b in box.buttons() if box.buttonRole(b) == QDialogButtonBox.ButtonRole.AcceptRole][0]
    dlg.findChild(QLineEdit).setText("密码 ü")
    state["non_ascii_ok"] = ok.isEnabled()
    if ok.isEnabled():
        QTest.keyClick(dlg.findChild(QLineEdit), Qt.Key_Return)
    else:
        dlg.reject()  # 确定按钮不可用时回车不起作用，主动关掉，免得夹具卡住


def hint_dialog() -> None:
    dlg = next(d for d in QApplication.topLevelWidgets() if isinstance(d, QDialog)
               and d.isVisible())
    dlg.findChild(QLineEdit).setText("bad\tpassword")  # 控制字符：出现提示
    for _ in range(5):
        app.processEvents()
        time.sleep(0.02)
    labels = [lbl for lbl in dlg.findChildren(QLabel) if lbl.isVisible() and lbl.text()]
    state["hint_fit"] = [(lbl.height(), lbl.heightForWidth(lbl.width())) for lbl in labels]
    dlg.reject()


long_text = "“" + "很长的归档名称" * 12 + ".zip” 已加密。请输入密码以解压。"
QTimer.singleShot(200, hint_dialog)
gui_common.ask_password(w3, long_text, tr("archive_password_or_damaged"))
fits = state.get("hint_fit", [])
check("dialog grows when the hint appears", len(fits) == 3 and all(h >= need for h, need in fits),
      repr(fits))

QTimer.singleShot(200, non_ascii_dialog)
check("real dialog accepts non-ASCII extraction passwords",
      gui_common.ask_password(w3, "p") == "密码 ü" and state.get("non_ascii_ok") is True)
QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
app.processEvents()
check("dialogs destroyed, no password left in widgets",
      not [d for d in w3.findChildren(QDialog) if d.windowTitle() == tr("password_title")]
      and not any(e.text() in (PW, "typed")
                                               for e in w3.findChildren(QLineEdit)),
      repr([e.text() for e in w3.findChildren(QLineEdit)]))

# ---------------- 四种语言 ----------------
w3.mode_tabs.setCurrentIndex(0)
w3.fmt_combo.setCurrentIndex(list(core.FORMATS).index("zip"))
w3.encrypt_panel.encrypt_chk.setChecked(True)
expect = {"zh_Hans": "使用密码加密", "zh_Hant": "使用密碼加密", "fr": "Chiffrer avec un mot de passe"}
w3.show()
w3.resize(780, 640)
for i, code in enumerate(("en", "zh_Hans", "zh_Hant", "fr")):
    w3.lang_combo.setCurrentIndex(i)
    app.processEvents()
    e3 = w3.encrypt_panel
    # 勾选加密前后，勾选框和格式、输出框的横向位置不变；说明文字不被压成一行。
    e3.encrypt_chk.setChecked(False)
    app.processEvents()
    before = (e3.encrypt_chk.x(), w3.fmt_combo.x(), w3.out_input.x())
    e3.encrypt_chk.setChecked(True)
    e3.method_combo.setCurrentIndex(1)
    for _ in range(5):
        app.processEvents()
        time.sleep(0.02)
    after = (e3.encrypt_chk.x(), w3.fmt_combo.x(), w3.out_input.x())
    check(f"{code}: no sideways jump when enabling encryption", before == after,
          repr((before, after)))
    check(f"{code}: notes not squeezed", e3.note.height() >= e3.note.heightForWidth(e3.note.width())
          and w3.excl_lbl.height() >= w3.excl_lbl.heightForWidth(w3.excl_lbl.width()),
          f"{e3.note.height()} / {e3.note.heightForWidth(e3.note.width())}")
    e3.method_combo.setCurrentIndex(0)
    app.processEvents()
    texts = [e3.encrypt_chk.text(), e3.pw_lbl.text(), e3.show_chk.text(), e3.method_lbl.text(),
             e3.method_combo.itemText(0), e3.method_combo.itemText(1), e3.names_chk.text(),
             e3.pw_confirm.placeholderText(), e3.note.text()]
    check(f"{code}: all encrypt texts set", all(texts) and not any(
        t in core.ZIP_METHODS or t.startswith("zip_") for t in texts)
        and (code == "en" or e3.encrypt_chk.text() == expect[code]), repr(texts))
def settle() -> None:
    for _ in range(5):
        app.processEvents()
        time.sleep(0.02)


def notes_fit(win) -> bool:
    note = win.encrypt_panel.note
    return (note.height() >= note.heightForWidth(note.width())
            and win.excl_lbl.height() >= win.excl_lbl.heightForWidth(win.excl_lbl.width()))


# 加密已打开时换格式、换语言：窗口也要加高，说明不能被截断
w3.lang_combo.setCurrentIndex(0)
w3.fmt_combo.setCurrentIndex(list(core.FORMATS).index("7z"))
w3.encrypt_panel.encrypt_chk.setChecked(True)
settle()
w3.resize(780, w3.minimumSizeHint().height())
settle()
w3.fmt_combo.setCurrentIndex(list(core.FORMATS).index("zip"))  # 只换格式，不碰加密选项
settle()
check("format switch with encryption on: notes fit", notes_fit(w3),
      f"{w3.encrypt_panel.note.height()} / "
      f"{w3.encrypt_panel.note.heightForWidth(w3.encrypt_panel.note.width())}")
w3.resize(780, w3.minimumSizeHint().height())
settle()
w3.lang_combo.setCurrentIndex(3)  # Français
settle()
check("language switch with encryption on: notes fit", notes_fit(w3),
      f"{w3.encrypt_panel.note.height()} / "
      f"{w3.encrypt_panel.note.heightForWidth(w3.encrypt_panel.note.width())}")

w3._quit_ready = True
w3.close()
set_lang("en")

for s in (settings, w3.settings):
    s.sync()
subprocess.run(["chmod", "-R", "u+rwx", str(tmp)], check=False)
subprocess.run(["rm", "-rf", str(tmp)], check=False)
print(f"{sum(ok for _, ok, _ in results)}/{len(results)}",
      "ALL PASS" if all(ok for _, ok, _ in results) else "SOME FAILED")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
