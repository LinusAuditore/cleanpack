"""GUI 公用的 Qt 辅助：子进程环境、样式、Qt 翻译、本地化数字、确认与密码对话框和可键入的路径框。"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import (QCoreApplication, QLibraryInfo, QLocale, QProcessEnvironment, Qt,
                            QTimer, QTranslator, Signal)
from PySide6.QtGui import QFocusEvent
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QLabel, QLineEdit,
                               QMessageBox, QVBoxLayout, QWidget)

import core
from i18n import QT_LOCALES, current_lang, tr

# 说明文字和警告使用系统文字色，保证浅色和深色主题下都有足够对比度。
NOTE_STYLE = "color: palette(text);"
WARN_STYLE = "color: palette(text); font-weight: bold;"

# 当前安装的 Qt 自带文案翻译；必须保持引用，否则对象被回收后翻译失效。
_qt_translator: QTranslator | None = None


def install_qt_translator() -> None:
    """按应用语言加载 Qt 自带文案（右键菜单、标准按钮等）的翻译，替换之前安装的翻译。"""
    global _qt_translator
    app = QCoreApplication.instance()
    if app is None:
        return
    if _qt_translator is not None:
        app.removeTranslator(_qt_translator)
        _qt_translator = None
    translator = QTranslator()
    folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    # 英文是 Qt 的源语言，没有翻译文件，加载失败即保持原文。
    if translator.load(app_locale(), "qtbase", "_", folder):
        app.installTranslator(translator)
        _qt_translator = translator


def process_environment() -> QProcessEnvironment:
    """把核心层的统一子进程环境转换为 Qt 环境，保持 Finder 启动行为一致。"""
    env = QProcessEnvironment()
    for key, value in core.ENV.items():
        env.insert(key, value)
    return env


def app_locale() -> QLocale:
    """返回与应用界面语言一致的 QLocale，而不是系统区域设置。"""
    return QLocale(QT_LOCALES.get(current_lang(), "en_US"))


def format_size(n: int) -> str:
    """与 Finder 一样按 1000 进制显示大小，小数点和单位随界面语言变化。"""
    locale = app_locale()
    # Qt 的 CLDR 数据在繁体中文下把字节写成英文 byte，小于 1 kB 时改用自己的译文。
    if n < 1000:
        return tr("size_bytes", n=locale.toString(n))
    # Qt 先按数量级选单位再四舍五入，999 950 字节会显示成 1,000.0 kB；离进位只差舍入误差时直接进位。
    for unit in (10**6, 10**9, 10**12):
        if unit * 0.99995 <= n < unit:
            n = unit
    return locale.formattedDataSize(n, 1, QLocale.DataSizeFormat.DataSizeSIFormat)


def format_secs(secs: float) -> str:
    """把秒数格式化为一位小数，小数点随界面语言变化。"""
    return app_locale().toString(secs, "f", 1)


def confirm(parent: QWidget, title: str, text: str, accept_key: str) -> bool:
    """显示确认对话框并返回是否确认；默认按钮和 Esc 都是取消，按钮文字跟随应用语言。"""
    box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=parent)
    accept = box.addButton(tr(accept_key), QMessageBox.ButtonRole.YesRole)
    cancel = box.addButton(tr("cancel"), QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is accept


def ask_password(parent: QWidget, text: str, error: str | None = None) -> str | None:
    """询问解压用的密码，返回输入的密码；用户取消时返回 None。

    text 说明要哪个归档的密码，error 是上一次尝试失败的原因。这不是破坏性操作的确认，
    所以回车即确定、Esc 取消；按钮文字跟随应用语言。密码不合格（空、控制字符、过长）时
    确定按钮不可用；输入了内容才显示原因，空着时只停用按钮。
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle(tr("password_title"))
    # 默认宽度只有两百像素左右，归档名稍长说明就会一行只剩几个字。
    dlg.setMinimumWidth(380)
    layout = QVBoxLayout(dlg)
    label = QLabel(text)
    label.setWordWrap(True)
    layout.addWidget(label)
    if error:
        err_lbl = QLabel(error)
        err_lbl.setWordWrap(True)
        err_lbl.setStyleSheet(WARN_STYLE)
        layout.addWidget(err_lbl)
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    field.setAcceptDrops(False)
    layout.addWidget(field)
    show = QCheckBox(tr("show_password"))
    show.toggled.connect(lambda on: field.setEchoMode(
        QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
    layout.addWidget(show)
    hint = QLabel()
    hint.setWordWrap(True)
    hint.setStyleSheet(WARN_STYLE)
    hint.hide()
    layout.addWidget(hint)
    buttons = QDialogButtonBox()
    ok = buttons.addButton(tr("extract"), QDialogButtonBox.ButtonRole.AcceptRole)
    buttons.addButton(tr("cancel"), QDialogButtonBox.ButtonRole.RejectRole)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    def validate() -> None:
        try:
            core.check_password(field.text(), None)
            problem = ""
        except core.JobError as exc:
            # 还没输入时不提示，只停用确定按钮。
            problem = tr(exc.key, **exc.kw) if field.text() else ""
            ok.setEnabled(False)
        else:
            ok.setEnabled(True)
        hint.setText(problem)
        hint.setVisible(bool(problem))
        # 提示出现后内容变高；等布局更新完再加高对话框，免得说明和提示被压成一行。
        QTimer.singleShot(0, dlg, grow)  # 以对话框为上下文：对话框销毁后不会再调用

    def grow() -> None:
        layout.activate()
        need = layout.totalMinimumHeightForWidth(dlg.width())
        if dlg.height() < need:
            dlg.resize(dlg.width(), need)

    field.textChanged.connect(validate)
    validate()
    ok.setDefault(True)
    field.setFocus()
    accepted = dlg.exec() == QDialog.DialogCode.Accepted
    password = field.text() if accepted else None
    # 对话框挂在主窗口下，不销毁就会把输入过的密码一直留在隐藏的输入框里。setText 同时清空
    # 撤销历史（clear 不会）。
    field.setText("")
    dlg.deleteLater()
    return password


class PathInput(QLineEdit):
    """可键入的路径框：键入后防抖实时校验草稿，回车或焦点移到窗口内其他控件时提交。

    validate 接收输入框原文并返回解析结果；空输入应返回 None，不合格时抛出 JobError。
    错误以 key 和参数保存，切换语言后由 retranslate 重新渲染。提示标签 hint 由调用方放进布局。
    """
    committed = Signal(object)  # 提交后的解析结果；不合格时为 None，原因见 error
    checked = Signal()          # 草稿的校验结果已更新

    DEBOUNCE_MS = 200

    def __init__(self, validate: Callable[[str], Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._validate = validate
        self.error: core.JobError | None = None  # 当前文字的校验错误
        self.note: core.JobError | None = None   # 调用方给出的附加提示，例如输出目录不可写
        self._committed_text = ""
        self._committed_ok = True
        # 拖放由窗口统一处理，避免输入框截获文件 URL。
        self.setAcceptDrops(False)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(WARN_STYLE)
        self.hint.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.DEBOUNCE_MS)
        self._timer.timeout.connect(self.check)
        # textEdited 只在用户编辑时触发，程序设置文字不会启动校验。
        self.textEdited.connect(self._timer.start)
        # 回车立即提交；失去焦点的提交见 focusOutEvent。
        self.returnPressed.connect(self.commit)

    @property
    def pending(self) -> bool:
        """输入框里是否有尚未提交的草稿。"""
        return self.text() != self._committed_text

    @property
    def rejected(self) -> bool:
        """当前文字已提交但未通过校验；它和提示应留在框里，等用户修改。"""
        return not self.pending and not self._committed_ok

    def focusOutEvent(self, e: QFocusEvent) -> None:
        """焦点移到窗口内其他控件时提交草稿；切换到其他应用或弹出菜单时不提交，
        用户可能只是去 Finder 里查看名称，回来后还要继续键入。输入框被隐藏（例如切换
        模式）时也不提交，未完成的草稿不应替换那个模式里已经选好的路径。"""
        super().focusOutEvent(e)
        if not self.isVisible():
            return
        if e.reason() not in (Qt.FocusReason.ActiveWindowFocusReason,
                              Qt.FocusReason.PopupFocusReason):
            self.commit()

    def check(self) -> Any:
        """立即校验当前文字，更新提示并返回解析结果；不合格时返回 None。"""
        self._timer.stop()
        try:
            result, self.error = self._validate(self.text()), None
        except core.JobError as exc:
            result, self.error = None, exc
        except OSError as exc:  # 校验函数应只抛 JobError，这里兜底，避免提示停留在旧状态
            result, self.error = None, core.job_error_from_os(exc)
        self._render()
        self.checked.emit()
        return result

    def commit(self) -> None:
        """校验并提交当前文字；文字和有效性都没有变化时不重复提交，避免重置调用方的状态。"""
        result = self.check()
        ok = self.error is None
        if not self.pending and ok == self._committed_ok:
            return
        self._committed_text, self._committed_ok = self.text(), ok
        self.committed.emit(result)

    def recheck(self) -> None:
        """重新校验，例如应用重新激活时：之前被拒绝且未改动的文字若已变得有效，就提交它；
        其余情况只更新提示，已提交的有效值即使暂时失效也保留，恢复后无需重新设置。"""
        if not self.pending and not self._committed_ok:
            self.commit()
        else:
            self.check()

    def revert(self) -> None:
        """放弃未提交的草稿，恢复已提交的文字并重新校验提示。"""
        if self.pending:
            self.setText(self._committed_text)
            self.check()

    def submit(self, text: str) -> None:
        """以程序方式填入并提交，供拖放和文件对话框使用。"""
        self.setText(text)
        self.commit()

    def display(self, text: str) -> None:
        """显示由调用方状态得出的有效值并清除错误，不触发提交。"""
        self._timer.stop()
        self.setText(text)
        self._committed_text, self._committed_ok = text, True
        self.error = None
        self._render()

    def set_note(self, note: core.JobError | None) -> None:
        """设置附加提示；只在当前文字本身没有错误时显示。"""
        self.note = note
        self._render()

    def retranslate(self) -> None:
        self._render()

    def _render(self) -> None:
        err = self.error or self.note
        text = tr(err.key, **err.kw) if err else ""
        self.hint.setText(text)
        self.hint.setVisible(bool(text))
