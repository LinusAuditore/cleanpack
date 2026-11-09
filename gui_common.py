"""GUI 公用的 Qt 辅助：子进程环境、样式、Qt 翻译、本地化数字、确认与密码对话框、
可键入的路径框，以及访问被拒时的补救（打开系统设置的隐私页面）。"""
from __future__ import annotations

import sys
from typing import Any, Callable

from PySide6.QtCore import (QCoreApplication, QEvent, QLibraryInfo, QLocale,
                            QProcessEnvironment, Qt, QTimer, QTranslator, QUrl, Signal)
from PySide6.QtGui import QDesktopServices, QFocusEvent
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget)

import core
import theme
from i18n import QT_LOCALES, current_lang, tr

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


def format_eta(seconds: float) -> str:
    """剩余时间，取整方式见 core.eta_parts；数字随界面语言格式化。"""
    h, m, s = core.eta_parts(seconds)
    n = app_locale().toString
    if h:
        return tr("eta_hours", h=n(h), m=n(m))
    if m:
        return tr("eta_minutes", m=n(m), s=n(s)) if s else tr("eta_minutes_only", m=n(m))
    return tr("eta_seconds", s=n(s))


# 进度标签里各部分之间的分隔符。
PROGRESS_SEP = " · "


def format_progress(phase: str, done: int, total: int | None, unit: str,
                    rate: float | None, eta: float | None) -> str:
    """进度条旁的文字：统计、准备、整理阶段说明在做什么；工具运行中显示百分比，按字节计时
    再加速度，最后是剩余时间（还估不出时说正在估算）。总量未知时不显示。"""
    if phase in ("scan", "prepare", "finish"):
        return tr({"scan": "progress_scanning", "prepare": "progress_preparing",
                   "finish": "progress_finishing"}[phase])
    if not total:
        return ""
    parts = [tr("progress_percent", n=app_locale().toString(min(100, done * 100 // total)))]
    if unit == "bytes" and rate:
        parts.append(tr("progress_speed", size=format_size(int(rate))))
    parts.append(format_eta(eta) if eta is not None else tr("progress_estimating"))
    return PROGRESS_SEP.join(parts)


# 「系统设置 ▸ 隐私与安全 ▸ 文件与文件夹」。macOS 13 起的写法；锚点 Privacy_FilesAndFolders
# 没有文档，只在系统设置的二进制里出现，打不开时退回只打开「隐私与安全」。不引导用户去
# 「完全磁盘访问权限」：Apple 建议用打开面板或文件与文件夹授权，那个权限范围过大。
PRIVACY_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension"
    "?Privacy_FilesAndFolders",
    "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension",
)


def open_privacy_settings() -> bool:
    """打开系统设置的隐私页面，返回是否成功交给系统打开。"""
    return any(QDesktopServices.openUrl(QUrl(url)) for url in PRIVACY_SETTINGS_URLS)


def privacy_help() -> str:
    """说明怎样解除隐私保护的拦截。从源码运行时，macOS 检查的是启动 CleanPack 的应用
    （例如终端），要在系统设置里允许的是它，打包后的应用才算 CleanPack 自己。"""
    return tr("privacy_help" if getattr(sys, "frozen", False) else "privacy_help_dev")


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
    theme.set_role(dlg, "dialog")
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
        theme.set_role(err_lbl, "warn")
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
    theme.set_role(hint, "warn")
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
    fix_requested = Signal(object)  # 点按了提示旁的补救按钮，参数是带 denial 的 JobError

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
        # 窗口缩窄时路径框至少能显示一段路径，不被挤成几个字符。顶栏标题可以省略之后，这也是
        # 主窗口最小宽度的主要来源，同一行的级别滑块因此也留得出能拖动的轨道。
        self.setMinimumWidth(160)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        theme.set_role(self.hint, "warn")
        self.hint.hide()
        # 访问被拒时提示旁的补救按钮：隐私保护给「授予访问权限…」，POSIX 权限和锁定给
        # 「在访达中显示」，方便用「显示简介」修改；系统保护的位置没有补救办法，不显示。
        self.fix_btn = QPushButton()
        theme.set_button(self.fix_btn, "text")
        self.fix_btn.hide()
        self.fix_btn.clicked.connect(self._request_fix)
        # 调用方把 hint_row（而不是 hint）放进布局，提示和按钮在同一行。
        self.hint_row = QWidget()
        row = QHBoxLayout(self.hint_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.hint, 1)
        row.addWidget(self.fix_btn, 0, Qt.AlignmentFlag.AlignTop)
        self.hint_row.hide()
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

    def changeEvent(self, e: QEvent) -> None:
        """输入框停用（任务进行中或还没有可用的值）时补救按钮一起停用，与拖放规则一致。"""
        super().changeEvent(e)
        if e.type() == QEvent.Type.EnabledChange:
            self.fix_btn.setEnabled(self.isEnabled())

    def problem(self) -> core.JobError | None:
        """当前显示的问题：文字本身的错误优先，其次是调用方给出的附加提示。"""
        return self.error or self.note

    def _render(self) -> None:
        err = self.problem()
        text = tr(err.key, **err.kw) if err else ""
        self.hint.setText(text)
        self.hint.setVisible(bool(text))
        fix = _FIX_LABELS.get(err.denial) if err else None
        if fix:
            self.fix_btn.setText(tr(fix))
        self.fix_btn.setVisible(bool(fix))
        self.hint_row.setVisible(bool(text))
        # M3 文本框的错误状态：有提示时描边变成 error 色。
        theme.set_error(self, bool(text))

    def _request_fix(self) -> None:
        if (err := self.problem()) is not None and err.denial in _FIX_LABELS:
            self.fix_requested.emit(err)


# 访问被拒的原因对应的补救按钮文案；system 没有补救办法。
_FIX_LABELS = {"privacy": "grant_access", "posix": "show_in_finder", "locked": "show_in_finder"}
