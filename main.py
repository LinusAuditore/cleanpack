"""CleanPack GUI（PySide6）。运行：python3 main.py"""
from __future__ import annotations

import codecs
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import (QLocale, QObject, QProcess, QProcessEnvironment,
                            QSettings, Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent, QGuiApplication
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QSlider, QVBoxLayout,
                               QWidget)

import core
from i18n import LANGS, current_lang, detect_lang, set_lang, tr


def human_size(n: int) -> str:
    """将字节数 n 转成适合完成提示的大小字符串。"""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def process_environment() -> QProcessEnvironment:
    """把核心层的统一子进程环境转换为 Qt 环境，保持 Finder 启动行为一致。"""
    env = QProcessEnvironment()
    for key, value in core.ENV.items():
        env.insert(key, value)
    return env


class Bridge(QObject):
    """后台线程 -> 主线程 的信号桥。"""
    log = Signal(str)
    done = Signal(object, object)  # (Path | None, Exception | None)


class MainWindow(QWidget):
    """管理输入、工具链和任务状态；后台结果仅通过 Bridge 更新控件。"""
    def __init__(self, settings: QSettings) -> None:
        """以 settings 保存语言和格式偏好，并初始化空闲任务状态。"""
        super().__init__()
        self.settings = settings
        self.src: Path | None = None
        self.out_dir: Path | None = None
        self.out_stem: str | None = None
        self.job: core.ArchiveJob | None = None
        self.proc: QProcess | None = None
        self._proc_buffer = ""
        self._proc_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.t0 = 0.0
        self.brew: str | None = None
        self.tool_paths: dict[str, str | None] = {}

        self.bridge = Bridge()
        # 跨线程信号由 Qt 排队到主线程，工作线程不能直接修改控件。
        self.bridge.log.connect(self.append_log)
        self.bridge.done.connect(self.on_job_done)

        self.setAcceptDrops(True)
        self._build()
        self.refresh_tools()
        self.on_format_changed()
        self.retranslate()
        QTimer.singleShot(300, self.check_brew_on_start)

    # ---------------- 界面 ----------------
    def _build(self) -> None:
        """按视觉顺序组装界面；各分组负责创建自己的控件和信号连接。"""
        root = QVBoxLayout(self)
        root.addLayout(self._build_header())
        root.addWidget(self._build_source_group())
        root.addWidget(self._build_options_group())
        root.addWidget(self._build_tools_group())
        root.addWidget(self._build_log_group(), 1)
        root.addLayout(self._build_actions())

    def _build_header(self) -> QHBoxLayout:
        """构建标题与语言选择器，返回顶部布局。"""
        top = QHBoxLayout()
        self.title_lbl = QLabel()
        f = self.title_lbl.font()
        f.setPointSize(f.pointSize() + 4)
        f.setBold(True)
        self.title_lbl.setFont(f)
        self.lang_lbl = QLabel()
        self.lang_combo = QComboBox()
        for code, name in LANGS:
            self.lang_combo.addItem(name, code)
        self.lang_combo.setCurrentIndex([c for c, _ in LANGS].index(current_lang()))
        self.lang_combo.currentIndexChanged.connect(self.on_lang_changed)
        top.addWidget(self.title_lbl)
        top.addStretch()
        top.addWidget(self.lang_lbl)
        top.addWidget(self.lang_combo)
        return top

    def _build_source_group(self) -> QGroupBox:
        """构建文件输入区，返回源文件分组。"""
        self.src_box = QGroupBox()
        h = QHBoxLayout(self.src_box)
        self.src_edit = QLineEdit()
        self.src_edit.setReadOnly(True)
        # 让拖放交给窗口统一处理，避免文本控件截获文件 URL。
        self.src_edit.setAcceptDrops(False)
        self.btn_folder = QPushButton()
        self.btn_file = QPushButton()
        self.btn_folder.clicked.connect(self.choose_folder)
        self.btn_file.clicked.connect(self.choose_file)
        h.addWidget(self.src_edit, 1)
        h.addWidget(self.btn_folder)
        h.addWidget(self.btn_file)
        return self.src_box

    def _build_options_group(self) -> QGroupBox:
        """构建格式、级别和输出设置，返回选项分组。"""
        self.opt_box = QGroupBox()
        g = QGridLayout(self.opt_box)
        self.fmt_lbl = QLabel()
        self.fmt_combo = QComboBox()
        for key in core.FORMATS:
            self.fmt_combo.addItem(key, key)
        last = self.settings.value("format", "tar.zst")
        if last in core.FORMATS:
            self.fmt_combo.setCurrentIndex(list(core.FORMATS).index(last))
        self.fmt_combo.currentIndexChanged.connect(self.on_format_changed)
        g.addWidget(self.fmt_lbl, 0, 0)
        g.addWidget(self.fmt_combo, 0, 1, 1, 4)

        self.level_lbl = QLabel()
        self.fast_lbl = QLabel()
        self.small_lbl = QLabel()
        self.level_slider = QSlider(Qt.Horizontal)
        self.level_slider.setTickPosition(QSlider.TicksBelow)
        self.level_slider.valueChanged.connect(self.on_level_changed)
        self.level_val = QLabel()
        self.level_val.setMinimumWidth(28)
        g.addWidget(self.level_lbl, 1, 0)
        g.addWidget(self.fast_lbl, 1, 1)
        g.addWidget(self.level_slider, 1, 2)
        g.addWidget(self.small_lbl, 1, 3)
        g.addWidget(self.level_val, 1, 4)

        self.level_note = QLabel()
        self.level_note.setWordWrap(True)
        # 使用系统调色板，保证浅色和深色主题下的说明都有足够对比度。
        self.level_note.setStyleSheet("color: palette(text);")
        g.addWidget(self.level_note, 2, 1, 1, 4)
        self.extreme_chk = QCheckBox()
        g.addWidget(self.extreme_chk, 3, 1, 1, 4)

        self.out_lbl = QLabel()
        self.out_edit = QLineEdit()
        self.out_edit.setAcceptDrops(False)
        self.btn_save = QPushButton()
        self.btn_save.clicked.connect(self.choose_output)
        g.addWidget(self.out_lbl, 4, 0)
        g.addWidget(self.out_edit, 4, 1, 1, 3)
        g.addWidget(self.btn_save, 4, 4)

        self.excl_lbl = QLabel()
        self.excl_lbl.setWordWrap(True)
        self.excl_lbl.setStyleSheet("color: palette(text);")
        g.addWidget(self.excl_lbl, 5, 0, 1, 5)
        g.setColumnStretch(2, 1)
        return self.opt_box

    def _build_tools_group(self) -> QGroupBox:
        """构建工具状态与安装操作，返回工具链分组。"""
        self.tools_box = QGroupBox()
        tv = QVBoxLayout(self.tools_box)
        self.brew_lbl = QLabel()
        tv.addWidget(self.brew_lbl)
        grid = QGridLayout()
        self.tool_rows: dict[str, tuple[QLabel, QLabel, QLabel]] = {}
        for i, key in enumerate(core.TOOLS):
            name, status, used, path = QLabel(f"<b>{key}</b>"), QLabel(), QLabel(), QLabel()
            path.setTextInteractionFlags(Qt.TextSelectableByMouse)
            path.setStyleSheet("color: palette(text);")
            for col, w in enumerate((name, status, used, path)):
                grid.addWidget(w, i, col)
            self.tool_rows[key] = (status, used, path)
        grid.setColumnStretch(3, 1)
        tv.addLayout(grid)
        self.cmd_edit = QLineEdit()
        self.cmd_edit.setReadOnly(True)
        tv.addWidget(self.cmd_edit)
        hb = QHBoxLayout()
        self.btn_install = QPushButton()
        self.btn_copy = QPushButton()
        self.btn_refresh = QPushButton()
        self.btn_install.clicked.connect(self.install_missing)
        self.btn_copy.clicked.connect(lambda: self.copy_text(self.cmd_edit.text()))
        self.btn_refresh.clicked.connect(self.refresh_tools)
        hb.addWidget(self.btn_install)
        hb.addWidget(self.btn_copy)
        hb.addStretch()
        hb.addWidget(self.btn_refresh)
        tv.addLayout(hb)
        return self.tools_box

    def _build_log_group(self) -> QGroupBox:
        """构建只读日志区，返回日志分组。"""
        self.log_box = QGroupBox()
        lv = QVBoxLayout(self.log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setAcceptDrops(False)
        self.log_view.setMaximumBlockCount(3000)
        lv.addWidget(self.log_view)
        return self.log_box

    def _build_actions(self) -> QVBoxLayout:
        """构建警告与任务操作区，返回底部布局。"""
        actions = QVBoxLayout()
        self.warn_lbl = QLabel()
        self.warn_lbl.setStyleSheet("color: palette(text); font-weight: bold;")
        self.warn_lbl.setWordWrap(True)
        actions.addWidget(self.warn_lbl)
        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.btn_cancel = QPushButton()
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_go = QPushButton()
        self.btn_go.setDefault(True)
        self.btn_go.clicked.connect(self.start)
        bottom.addWidget(self.progress, 1)
        bottom.addWidget(self.btn_cancel)
        bottom.addWidget(self.btn_go)
        actions.addLayout(bottom)
        return actions

    def retranslate(self) -> None:
        """按当前语言刷新界面，保留路径、日志和正在运行的任务。"""
        self.setWindowTitle(core.APP_NAME)
        self.title_lbl.setText(tr("title"))
        self.lang_lbl.setText(tr("language"))
        self.src_box.setTitle(tr("source_group"))
        self.src_edit.setPlaceholderText(tr("drop_hint"))
        self.btn_folder.setText(tr("choose_folder"))
        self.btn_file.setText(tr("choose_file"))
        self.opt_box.setTitle(tr("options_group"))
        self.fmt_lbl.setText(tr("format"))
        self.level_lbl.setText(tr("level"))
        self.fast_lbl.setText(tr("fast"))
        self.small_lbl.setText(tr("small"))
        self.extreme_chk.setText(tr("extreme"))
        self.out_lbl.setText(tr("output"))
        self.btn_save.setText(tr("save_as"))
        junk = ", ".join(p.replace("\r", "\\r") for p in core.JUNK)
        self.excl_lbl.setText(tr("excluded", list=junk))
        self.tools_box.setTitle(tr("tools_group"))
        self.btn_install.setText(tr("install_btn"))
        self.btn_copy.setText(tr("copy_cmd"))
        self.btn_refresh.setText(tr("refresh"))
        self.log_box.setTitle(tr("log"))
        self.btn_cancel.setText(tr("cancel"))
        self.btn_go.setText(tr("compress"))
        self.update_tool_labels()
        self.on_level_changed()
        self.update_go_state()

    # ---------------- 语言 / 格式 / 级别 ----------------
    def on_lang_changed(self) -> None:
        """保存用户选择的语言，并刷新可见标签。"""
        code = self.lang_combo.currentData()
        set_lang(code)
        self.settings.setValue("lang", code)
        self.retranslate()

    def current_format(self) -> core.Format:
        return core.FORMATS[self.fmt_combo.currentData()]

    def on_format_changed(self) -> None:
        """应用当前格式的级别范围及默认值，并同步输出扩展名。"""
        fmt = self.current_format()
        self.settings.setValue("format", fmt.key)
        has_level = fmt.level is not None
        for w in (self.level_slider, self.fast_lbl, self.small_lbl, self.level_val):
            w.setEnabled(has_level)
        if has_level:
            lo, hi, default = fmt.level
            self.level_slider.blockSignals(True)
            self.level_slider.setRange(lo, hi)
            self.level_slider.setValue(default)
            self.level_slider.blockSignals(False)
        self.extreme_chk.setVisible(fmt.key == "tar.xz")
        self.on_level_changed()
        self.update_output_path()
        self.update_go_state()

    def on_level_changed(self, *_: object) -> None:
        """根据滑块值刷新仅存储或高内存提示；忽略 Qt 信号的附加参数。"""
        fmt = self.current_format()
        if fmt.level is None:
            self.level_val.setText("–")
            self.level_note.setText(tr("level_none"))
            return
        v = self.level_slider.value()
        self.level_val.setText(str(v))
        if fmt.key == "tar.zst" and v > 19:
            self.level_note.setText(tr("ultra_note"))
        elif v == 0 and fmt.key in ("zip", "7z"):
            self.level_note.setText(tr("level_store"))
        else:
            self.level_note.setText("")

    # ---------------- 源 / 输出 ----------------
    def set_source(self, path: str) -> None:
        """接受本地文件或目录路径，并重置上一输入的输出命名偏好。"""
        if not path:
            return
        self.src = Path(path)
        self.out_dir = None
        self.out_stem = None
        self.src_edit.setText(str(self.src))
        self.update_output_path()
        self.update_go_state()

    def choose_folder(self) -> None:
        self.set_source(QFileDialog.getExistingDirectory(self, tr("choose_folder")))

    def choose_file(self) -> None:
        self.set_source(QFileDialog.getOpenFileName(self, tr("choose_file"))[0])

    def update_output_path(self) -> None:
        """由当前源路径、命名偏好和格式组合输出路径。"""
        if not self.src:
            return
        d = self.out_dir or self.src.parent
        stem = self.out_stem or self.src.name
        self.out_edit.setText(str(d / (stem + self.current_format().ext)))

    def choose_output(self) -> None:
        """通过原生保存对话框更新输出目录和不含归档扩展名的名称。"""
        ext = self.current_format().ext
        p, _ = QFileDialog.getSaveFileName(self, tr("save_as"), self.out_edit.text(), f"*{ext}")
        if p:
            p = Path(p)
            self.out_dir, self.out_stem = p.parent, core.strip_ext(p.name)
            self.update_output_path()

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        """仅接受空闲时拖入的一个本地路径，避免静默遗漏多选项目。"""
        urls = e.mimeData().urls()
        if (len(urls) == 1 and urls[0].isLocalFile()
                and self.job is None and self.proc is None):
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        """把有效拖放转为源路径选择，安装或压缩期间保持输入不变。"""
        urls = e.mimeData().urls()
        if (len(urls) == 1 and urls[0].isLocalFile()
                and self.job is None and self.proc is None):
            self.set_source(urls[0].toLocalFile())
            e.acceptProposedAction()

    # ---------------- 工具链 ----------------
    def refresh_tools(self) -> None:
        """重新检测 Homebrew 和工具路径，并刷新任务可用状态。"""
        self.brew = core.find_brew()
        self.tool_paths = {k: core.locate(k) for k in core.TOOLS}
        self.update_tool_labels()
        self.update_go_state()

    def update_tool_labels(self) -> None:
        """根据缓存的工具路径更新安装提示；运行任务时禁用安装按钮。"""
        self.brew_lbl.setText(tr("brew_ok", path=self.brew) if self.brew else tr("brew_missing"))
        for key, (status, used, path_lbl) in self.tool_rows.items():
            tool, p = core.TOOLS[key], self.tool_paths.get(key)
            text = f"✅ {tr('tool_ok')}" if p else f"❌ {tr('tool_missing')}"
            if p and tool.builtin:
                text += f" ({tr('system_tool')})"
            status.setText(text)
            used.setText(tr("used_by", fmts=", ".join(core.formats_using(key))))
            path_lbl.setText(p or (f"brew install {tool.formula}" if tool.formula else ""))
        formulas = core.missing_formulas(self.tool_paths)
        self.cmd_edit.setText("brew install " + " ".join(formulas) if formulas
                              else tr("nothing_missing"))
        busy = self.job is not None or self.proc is not None
        self.btn_install.setEnabled(bool(formulas and self.brew) and not busy)
        self.btn_copy.setEnabled(bool(formulas))

    def install_missing(self) -> None:
        """用异步 QProcess 安装缺失 formula，并把合并输出交给日志槽。"""
        formulas = core.missing_formulas(self.tool_paths)
        if not (formulas and self.brew):
            return
        self.proc = QProcess(self)
        env = process_environment()
        env.insert("NONINTERACTIVE", "1")
        env.insert("HOMEBREW_NO_ENV_HINTS", "1")
        self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read_proc)
        self.proc.finished.connect(self._install_finished)
        self.proc.errorOccurred.connect(self._install_error)
        self._proc_buffer = ""
        self._proc_decoder.reset()
        self.append_log(tr("installing", cmd="brew install " + " ".join(formulas)))
        self.set_busy(True)
        self.proc.start(self.brew, ["install", *formulas])

    def _read_proc(self) -> None:
        """增量解码并缓存未换行输出，避免分批到达的 UTF-8 字符和日志被拆坏。"""
        if self.proc is None:
            return
        self._proc_buffer += self._proc_decoder.decode(bytes(self.proc.readAllStandardOutput()))
        lines = self._proc_buffer.split("\n")
        self._proc_buffer = lines.pop()
        for line in lines:
            if line.strip():
                self.append_log(line.rstrip())

    def _install_error(self, error: QProcess.ProcessError) -> None:
        """处理不会触发 finished 信号的启动失败，恢复空闲界面。"""
        if error == QProcess.FailedToStart and self.proc is not None:
            self.append_log(tr("install_start_fail", err=self.proc.errorString()))
            self._finish_install()

    def _install_finished(self, code: int, _status: QProcess.ExitStatus) -> None:
        """根据退出码记录安装结果并统一释放进程。"""
        self._finish_install()
        self.append_log(tr("install_ok") if code == 0 else tr("install_fail", code=code))

    def _finish_install(self) -> None:
        """排空剩余日志、释放 QProcess，然后恢复按钮并重新检测工具。"""
        if self.proc is None:
            return
        self._read_proc()
        self._proc_buffer += self._proc_decoder.decode(b"", final=True)
        if self._proc_buffer.strip():
            self.append_log(self._proc_buffer.rstrip())
        self._proc_buffer = ""
        self.proc.deleteLater()
        self.proc = None
        self.set_busy(False)
        self.refresh_tools()

    def check_brew_on_start(self) -> None:
        """缺少 Homebrew 时提供官网和安装命令，不自动执行安装脚本。"""
        if self.brew:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(tr("brew_dlg_title"))
        box.setText(tr("brew_dlg_title"))
        box.setInformativeText(tr("brew_dlg_text") + "\n\n" + core.BREW_INSTALL_CMD)
        b_site = box.addButton(tr("open_site"), QMessageBox.ActionRole)
        b_copy = box.addButton(tr("copy_install"), QMessageBox.ActionRole)
        box.addButton(tr("continue"), QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is b_site:
            QDesktopServices.openUrl(QUrl("https://brew.sh"))
        elif box.clickedButton() is b_copy:
            self.copy_text(core.BREW_INSTALL_CMD)

    def copy_text(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.append_log(tr("copied"))

    # ---------------- 压缩 ----------------
    def update_go_state(self) -> None:
        """根据输入、工具缺失和忙碌状态决定压缩按钮及警告内容。"""
        busy = self.job is not None or self.proc is not None
        missing = core.missing_tools(self.current_format().key, self.tool_paths)
        if missing:
            self.warn_lbl.setText(tr("missing_tools", fmt=self.current_format().key,
                                     tools=", ".join(missing)))
        else:
            self.warn_lbl.setText("")
        self.btn_go.setEnabled(bool(self.src) and not missing and not busy)

    def set_busy(self, busy: bool) -> None:
        """根据 busy 切换进度和输入控件，结束后恢复当前格式的限制。"""
        self.progress.setVisible(busy)
        self.btn_cancel.setEnabled(busy)
        for w in (self.btn_folder, self.btn_file, self.btn_save, self.fmt_combo,
                  self.level_slider, self.extreme_chk, self.out_edit):
            w.setEnabled(not busy)
        if not busy:
            self.on_format_changed_enable_only()
        self.update_tool_labels()
        self.update_go_state()

    def on_format_changed_enable_only(self) -> None:
        self.level_slider.setEnabled(self.current_format().level is not None)

    def start(self) -> None:
        """确认已有输出的覆盖意图，捕获参数后启动后台压缩线程。"""
        if not self.src:
            self.show_message(QMessageBox.Information, tr("no_source"))
            return
        out = Path(self.out_edit.text()).expanduser()
        if out.exists():
            box = QMessageBox(QMessageBox.Question, tr("overwrite_title"),
                              tr("overwrite_q", path=out), parent=self)
            yes = box.addButton(tr("replace"), QMessageBox.YesRole)
            no = box.addButton(tr("cancel"), QMessageBox.NoRole)
            box.setDefaultButton(no)
            box.setEscapeButton(no)
            box.exec()
            if box.clickedButton() is not yes:
                return
        fmt = self.current_format()
        self.job = core.ArchiveJob(
            self.src, out, fmt.key,
            level=self.level_slider.value() if fmt.level else None,
            extreme=self.extreme_chk.isChecked() and fmt.key == "tar.xz",
            log=self.bridge.log.emit,
        )
        self.t0 = time.monotonic()
        self.set_busy(True)
        threading.Thread(target=self._run_job, args=(self.job,), daemon=True).start()

    def _run_job(self, job: core.ArchiveJob) -> None:
        """在工作线程运行 job；通过信号传递路径或异常，不接触控件。"""
        try:
            self.bridge.done.emit(job.run(), None)
        except Exception as e:  # noqa: BLE001
            self.bridge.done.emit(None, e)

    def cancel(self) -> None:
        """请求核心任务取消或终止安装进程，由完成回调恢复界面。"""
        if self.job:
            self.job.cancel()
        if self.proc:
            self.proc.kill()

    def on_job_done(self, path: Path | None, err: Exception | None) -> None:
        """在主线程处理结果；path 为成功产物，err 为失败或取消原因。"""
        self.job = None
        self.set_busy(False)
        if err is None:
            assert path is not None
            msg = tr("done", path=path, size=human_size(path.stat().st_size),
                     secs=time.monotonic() - self.t0)
            self.append_log(msg)
            box = QMessageBox(self)
            box.setWindowTitle(tr("done_title"))
            box.setText(msg)
            b_show = box.addButton(tr("show_in_finder"), QMessageBox.ActionRole)
            box.addButton(tr("ok"), QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is b_show:
                self.show_in_finder(path)
        elif isinstance(err, core.Cancelled):
            self.append_log(tr("cancelled"))
        else:
            text = tr(err.key, **err.kw) if isinstance(err, core.JobError) else str(err)
            self.append_log(tr("failed", err=text))
            self.show_message(QMessageBox.Warning, tr("failed", err=text))

    def show_in_finder(self, path: Path) -> None:
        """异步用 open -R 在 Finder 中选中 path，避免 Finder 响应慢时阻塞窗口。"""
        reveal = QProcess(self)
        reveal.setProcessEnvironment(process_environment())
        reveal.setProgram("/usr/bin/open")
        reveal.setArguments(["-R", str(path)])
        started, _pid = reveal.startDetached()
        reveal.deleteLater()
        if not started:
            self.append_log(tr("reveal_failed"))

    def show_message(self, icon: QMessageBox.Icon, text: str) -> None:
        """显示应用消息，并确保确认按钮跟随应用语言而非系统语言。"""
        box = QMessageBox(icon, core.APP_NAME, text, parent=self)
        box.addButton(tr("ok"), QMessageBox.AcceptRole)
        box.exec()

    def append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)


def main() -> None:
    """创建 Qt 应用，加载语言偏好并启动主事件循环。"""
    app = QApplication(sys.argv)
    app.setApplicationName(core.APP_NAME)
    settings = QSettings(core.APP_NAME, core.APP_NAME)
    set_lang(settings.value("lang") or detect_lang(QLocale.system().uiLanguages()))
    w = MainWindow(settings)
    w.resize(780, 760)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
