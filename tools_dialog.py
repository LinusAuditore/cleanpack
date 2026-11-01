"""工具链窗口：检测压缩工具与 Homebrew、用 Homebrew 安装缺失项，以及启动时的提示。"""
from __future__ import annotations

import codecs

from PySide6.QtCore import QObject, QProcess, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

import core
from gui_common import NOTE_STYLE, WARN_STYLE, process_environment
from i18n import tr


class ToolchainController(QObject):
    """持有工具检测结果和 Homebrew 安装进程，是安装与压缩互斥的唯一状态来源。

    主窗口和工具链窗口都只读这里的状态，避免两处各自判断忙碌导致不一致。
    """
    changed = Signal()      # 检测结果、安装状态或任务运行状态变化
    output = Signal(str)    # brew 的一行输出，只显示在工具链窗口
    status = Signal(str)    # 安装开始和结果的摘要，同时写入主窗口日志

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.brew: str | None = None
        self.paths: dict[str, str | None] = {}
        self.proc: QProcess | None = None
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._cancel_requested = False
        self._job_running = False

    # ---------- 状态 ----------
    @property
    def installing(self) -> bool:
        return self.proc is not None

    @property
    def busy(self) -> bool:
        return self.installing or self._job_running

    def set_job_running(self, running: bool) -> None:
        """由主窗口在压缩任务开始和结束时调用，让安装按钮随之停用或恢复。"""
        self._job_running = running
        self.changed.emit()

    def refresh(self) -> None:
        """重新检测 Homebrew 和各工具路径。"""
        self.brew = core.find_brew()
        self.paths = {k: core.locate(k) for k in core.TOOLS}
        self.changed.emit()

    def missing_for(self, fmt_key: str, encrypted: bool = False) -> list[str]:
        """返回压缩 fmt_key 缺少的工具；encrypted 表示要加密（加密 zip 改用系统 tar）。"""
        return core.missing_tools(fmt_key, self.paths, encrypted)

    def missing_formulas(self) -> list[str]:
        return core.missing_formulas(self.paths)

    def unfixable(self) -> list[str]:
        """缺失且没有 Homebrew formula 的工具（例如 tar），无法在应用内安装。"""
        return [k for k, p in self.paths.items() if not p and not core.TOOLS[k].formula]

    def can_install(self) -> bool:
        return bool(self.missing_formulas() and self.brew) and not self.busy

    # ---------- 安装 ----------
    def install(self) -> bool:
        """用异步 QProcess 安装缺失 formula；合并输出逐行发出。返回是否已开始。"""
        formulas = self.missing_formulas()
        if not self.can_install():
            return False
        self.proc = QProcess(self)
        env = process_environment()
        env.insert("NONINTERACTIVE", "1")
        env.insert("HOMEBREW_NO_ENV_HINTS", "1")
        self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read_proc)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        self._buffer = ""
        self._decoder.reset()
        self._cancel_requested = False
        self.status.emit(tr("installing", cmd="brew install " + " ".join(formulas)))
        self.proc.start(self.brew, ["install", *formulas])
        self.changed.emit()
        return True

    def cancel_install(self) -> None:
        """先请求 brew 正常退出以便它自行清理，5 秒后仍在运行再强制终止。"""
        proc = self.proc
        if proc is None:
            return
        self._cancel_requested = True
        proc.terminate()
        QTimer.singleShot(5000, lambda: self._kill_if_running(proc))

    def kill_install(self) -> None:
        """应用退出前的最后防线：立即结束仍在运行的安装进程。"""
        if self.proc is not None:
            self.proc.kill()
            self.proc.waitForFinished(2000)

    def _kill_if_running(self, proc: QProcess) -> None:
        # 安装结束后 self.proc 会被清空，只有仍是当前进程时才强制终止，避免操作已释放的对象。
        if self.proc is proc:
            proc.kill()

    def _read_proc(self) -> None:
        """增量解码并缓存未换行输出，避免分批到达的 UTF-8 字符和日志被拆坏。"""
        if self.proc is None:
            return
        self._buffer += self._decoder.decode(bytes(self.proc.readAllStandardOutput()))
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()
        for line in lines:
            if line.strip():
                self.output.emit(line.rstrip())

    def _on_error(self, error: QProcess.ProcessError) -> None:
        """处理不会触发 finished 信号的启动失败，恢复空闲状态。"""
        if error == QProcess.FailedToStart and self.proc is not None:
            message = tr("install_start_fail", err=self.proc.errorString())
            self._finish(message)

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        """根据退出情况记录结果；用户取消和意外中止分别说明，不当作普通失败。"""
        if self._cancel_requested:
            message = tr("install_cancelled")
        elif status == QProcess.CrashExit:
            message = tr("install_crashed")
        elif code == 0:
            message = tr("install_ok")
        else:
            message = tr("install_fail", code=code)
        self._finish(message)

    def _finish(self, message: str) -> None:
        """排空剩余输出、释放进程并重新检测工具，最后发出结果摘要。"""
        if self.proc is None:
            return
        self._read_proc()
        self._buffer += self._decoder.decode(b"", final=True)
        if self._buffer.strip():
            self.output.emit(self._buffer.rstrip())
        self._buffer = ""
        self.proc.deleteLater()
        self.proc = None
        self.status.emit(message)
        self.refresh()


class ToolsDialog(QDialog):
    """工具链窗口：非模态的单例，关闭时只是隐藏，安装会在后台继续。"""

    def __init__(self, tools: ToolchainController, parent: QWidget) -> None:
        super().__init__(parent)
        self.tools = tools
        # 拖放统一由主窗口处理，工具链窗口不接收文件。
        self.setAcceptDrops(False)
        root = QVBoxLayout(self)
        root.addWidget(self._build_brew_section())
        root.addLayout(self._build_tool_grid())
        root.addLayout(self._build_install_row())
        root.addWidget(self._build_log(), 1)
        close_row = QHBoxLayout()
        close_row.addStretch()
        self.btn_close = QPushButton()
        self.btn_close.clicked.connect(self.hide)
        close_row.addWidget(self.btn_close)
        root.addLayout(close_row)
        tools.changed.connect(self.update_state)
        tools.output.connect(self.append_output)
        tools.status.connect(self.append_output)
        self.resize(640, 560)
        self.retranslate()

    # ---------- 界面 ----------
    def _build_brew_section(self) -> QWidget:
        """Homebrew 状态；未安装时附上官网和安装命令，但不自动执行安装脚本。"""
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        self.brew_lbl = QLabel()
        self.brew_hint = QLabel()
        self.brew_hint.setWordWrap(True)
        self.brew_hint.setStyleSheet(NOTE_STYLE)
        self.brew_hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row = QHBoxLayout()
        self.btn_site = QPushButton()
        self.btn_copy_brew = QPushButton()
        self.btn_site.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://brew.sh")))
        self.btn_copy_brew.clicked.connect(lambda: self.copy_text(core.BREW_INSTALL_CMD))
        row.addWidget(self.btn_site)
        row.addWidget(self.btn_copy_brew)
        row.addStretch()
        self.brew_actions = QWidget()
        self.brew_actions.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.brew_lbl)
        v.addWidget(self.brew_hint)
        v.addWidget(self.brew_actions)
        return box

    def _build_tool_grid(self) -> QGridLayout:
        """每个工具一行：状态、用途、路径或安装命令。"""
        grid = QGridLayout()
        self.tool_rows: dict[str, tuple[QLabel, QLabel, QLabel]] = {}
        for i, key in enumerate(core.TOOLS):
            name, status, used, path = QLabel(f"<b>{key}</b>"), QLabel(), QLabel(), QLabel()
            path.setTextInteractionFlags(Qt.TextSelectableByMouse)
            path.setStyleSheet(NOTE_STYLE)
            for col, w in enumerate((name, status, used, path)):
                grid.addWidget(w, i, col)
            self.tool_rows[key] = (status, used, path)
        grid.setColumnStretch(3, 1)
        return grid

    def _build_install_row(self) -> QVBoxLayout:
        """安装命令、说明和安装操作按钮。"""
        v = QVBoxLayout()
        self.cmd_edit = QLineEdit()
        self.cmd_edit.setReadOnly(True)
        self.cmd_edit.setAcceptDrops(False)
        self.note_lbl = QLabel()
        self.note_lbl.setWordWrap(True)
        self.note_lbl.setStyleSheet(WARN_STYLE)
        h = QHBoxLayout()
        self.btn_install = QPushButton()
        self.btn_cancel = QPushButton()
        self.btn_copy = QPushButton()
        self.btn_refresh = QPushButton()
        self.btn_install.clicked.connect(self.tools.install)
        self.btn_cancel.clicked.connect(self.tools.cancel_install)
        self.btn_copy.clicked.connect(lambda: self.copy_text(self.cmd_edit.text()))
        self.btn_refresh.clicked.connect(self.tools.refresh)
        for b in (self.btn_install, self.btn_cancel, self.btn_copy):
            h.addWidget(b)
        h.addStretch()
        h.addWidget(self.btn_refresh)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.bg_note = QLabel()
        self.bg_note.setWordWrap(True)
        self.bg_note.setStyleSheet(NOTE_STYLE)
        v.addWidget(self.cmd_edit)
        v.addWidget(self.note_lbl)
        v.addLayout(h)
        v.addWidget(self.progress)
        v.addWidget(self.bg_note)
        return v

    def _build_log(self) -> QWidget:
        """Homebrew 输出日志；窗口隐藏后仍保留，再次打开可以继续查看。"""
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        self.log_lbl = QLabel()
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setAcceptDrops(False)
        self.log_view.setMaximumBlockCount(3000)
        v.addWidget(self.log_lbl)
        v.addWidget(self.log_view)
        return box

    def retranslate(self) -> None:
        self.setWindowTitle(tr("tools_group"))
        self.btn_site.setText(tr("open_site"))
        self.btn_copy_brew.setText(tr("copy_install"))
        self.btn_install.setText(tr("install_btn"))
        self.btn_cancel.setText(tr("cancel"))
        self.btn_copy.setText(tr("copy_cmd"))
        self.btn_refresh.setText(tr("refresh"))
        self.log_lbl.setText(tr("install_log"))
        self.bg_note.setText(tr("install_bg_note"))
        self.btn_close.setText(tr("close"))
        self.update_state()

    # ---------- 状态刷新 ----------
    def update_state(self) -> None:
        """按控制器的最新状态刷新所有标签和按钮。"""
        tools = self.tools
        self.brew_lbl.setText(tr("brew_ok", path=tools.brew) if tools.brew else tr("brew_missing"))
        self.brew_hint.setText(tr("brew_missing_hint") + "\n" + core.BREW_INSTALL_CMD)
        self.brew_hint.setVisible(tools.brew is None)
        self.brew_actions.setVisible(tools.brew is None)
        for key, (status, used, path_lbl) in self.tool_rows.items():
            tool, p = core.TOOLS[key], tools.paths.get(key)
            text = f"✅ {tr('tool_ok')}" if p else f"❌ {tr('tool_missing')}"
            # 按实际路径判断是否系统自带：PATH 中 Homebrew 目录在前时可能找到 Homebrew 的版本。
            if p and core.is_system_path(p):
                text += f" ({tr('system_tool')})"
            status.setText(text)
            used.setText(tr("used_by", fmts=", ".join(core.formats_using(key))))
            if p:
                path_lbl.setText(p)
            elif tool.formula:
                path_lbl.setText(f"brew install {tool.formula}")
            else:
                path_lbl.setText(tr("tools_not_brew", tools=key))
        formulas = tools.missing_formulas()
        unfixable = tools.unfixable()
        if formulas:
            self.cmd_edit.setText("brew install " + " ".join(formulas))
        elif not unfixable:
            self.cmd_edit.setText(tr("nothing_missing"))
        else:
            self.cmd_edit.setText("")
        self.cmd_edit.setVisible(bool(formulas) or not unfixable)
        # 没有 formula 的工具缺失时无法安装，单独说明，不能显示“所有工具均已安装”。
        self.note_lbl.setText(tr("tools_not_brew", tools=", ".join(unfixable)))
        self.note_lbl.setVisible(bool(unfixable))
        self.btn_install.setEnabled(tools.can_install())
        self.btn_copy.setEnabled(bool(formulas))
        self.btn_cancel.setVisible(tools.installing)
        self.btn_refresh.setEnabled(not tools.installing)
        self.progress.setVisible(tools.installing)
        self.bg_note.setVisible(tools.installing)

    def append_output(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def copy_text(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.append_output(tr("copied"))

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()


def brew_notice(parent: QWidget, tools: ToolchainController) -> None:
    """缺少 Homebrew 且确有工具需要它安装时，提供官网和安装命令；不自动执行安装脚本。"""
    needed = tools.missing_formulas()
    if tools.brew or not needed:
        return
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(tr("brew_dlg_title"))
    box.setText(tr("brew_dlg_title"))
    box.setInformativeText(tr("brew_dlg_text", tools=", ".join(needed)) + "\n\n"
                           + core.BREW_INSTALL_CMD)
    b_site = box.addButton(tr("open_site"), QMessageBox.ActionRole)
    b_copy = box.addButton(tr("copy_install"), QMessageBox.ActionRole)
    box.addButton(tr("continue"), QMessageBox.AcceptRole)
    box.exec()
    if box.clickedButton() is b_site:
        QDesktopServices.openUrl(QUrl("https://brew.sh"))
    elif box.clickedButton() is b_copy:
        QGuiApplication.clipboard().setText(core.BREW_INSTALL_CMD)
        # 通过 status 信号写入主窗口日志，给用户一个已复制的确认。
        tools.status.emit(tr("copied"))
