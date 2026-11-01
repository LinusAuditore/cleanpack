"""CleanPack GUI（PySide6）：压缩与解压两种模式。运行：python3 main.py"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QLocale, QObject, QProcess, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QGuiApplication
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QMenuBar, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QSlider, QTabBar, QVBoxLayout,
                               QWidget)

import core
from gui_common import (NOTE_STYLE, WARN_STYLE, PathInput, ask_password, confirm, format_secs,
                        format_size, install_qt_translator, process_environment)
from encrypt_panel import EncryptPanel
from extract_panel import ExtractPanel
from i18n import LANGS, current_lang, detect_lang, set_lang, tr
from tools_dialog import ToolchainController, ToolsDialog, brew_notice

# 首次启动时的默认格式：只用 macOS 自带工具即可完成，不会一打开就提示缺少工具。
DEFAULT_FORMAT = "zip"
# 模式切换标签的顺序，也是偏好 mode 的取值。
MODES = ("compress", "extract")
# 解压任务因这些原因失败时询问密码，带密码重试，而不是报告失败。
PASSWORD_ERRORS = ("archive_encrypted", "archive_wrong_password", "archive_password_or_damaged")


class Bridge(QObject):
    """后台线程 -> 主线程 的信号桥。"""
    log = Signal(str)
    done = Signal(object, object)  # (Path | None, Exception | None)


class MainWindow(QWidget):
    """管理输入和任务状态；工具链由 ToolchainController 管理，后台结果仅通过 Bridge 更新控件。"""
    def __init__(self, settings: QSettings) -> None:
        """以 settings 保存语言和格式偏好，并初始化空闲任务状态。"""
        super().__init__()
        self.settings = settings
        self.src: Path | None = None
        self.out_dir: Path | None = None
        self.out_stem: str | None = None
        self.job: core.Job | None = None
        self._worker: threading.Thread | None = None
        self._quit_requested = False
        self._quit_ready = False
        self._closed = False  # 窗口已关闭；询问密码期间关闭窗口后不再重试
        self._confirming_quit = False  # 退出确认框正在显示
        # 退出确认框显示期间需要密码的解压任务：等用户决定不退出后再询问密码。
        self._deferred_retry: tuple[core.ExtractJob, core.JobError] | None = None
        self._last_fmt: str | None = None
        self.t0 = 0.0

        self.bridge = Bridge()
        # 跨线程信号由 Qt 排队到主线程，工作线程不能直接修改控件。
        self.bridge.log.connect(self.append_log)
        self.bridge.done.connect(self.on_job_done)

        self.tools = ToolchainController(self)
        self.tools_dialog = ToolsDialog(self.tools, self)
        # 工具链窗口是独立窗口，不随主窗口一起显示，也不阻止应用在关闭主窗口时退出。
        self.tools_dialog.setWindowFlag(Qt.Window, True)
        self.tools.changed.connect(self.refresh_busy_ui)
        self.tools.changed.connect(self._maybe_finish_quit)
        self.tools.status.connect(self.append_log)

        self.setAcceptDrops(True)
        self._build()
        self._build_menu()
        self.tools.refresh()
        self.on_format_changed()
        self.retranslate()
        mode = self.settings.value("mode")
        self.mode_tabs.setCurrentIndex(MODES.index(mode) if mode in MODES else 0)
        self.on_mode_changed()
        # 在终端里装好工具后切回应用，横幅应自动更新，不必打开工具链窗口。
        QGuiApplication.instance().applicationStateChanged.connect(self.on_app_state_changed)
        QTimer.singleShot(300, lambda: brew_notice(self, self.tools))

    # ---------------- 界面 ----------------
    def _build(self) -> None:
        """按视觉顺序组装界面；各分组负责创建自己的控件和信号连接。"""
        root = QVBoxLayout(self)
        root.addLayout(self._build_header())
        root.addLayout(self._build_mode_bar())
        root.addWidget(self._build_source_group())
        root.addWidget(self._build_options_group())
        self.extract_panel = ExtractPanel()
        self.extract_panel.changed.connect(self.update_go_state)
        root.addWidget(self.extract_panel)
        root.addWidget(self._build_log_group(), 1)
        root.addLayout(self._build_banner())
        root.addLayout(self._build_actions())

    def _build_header(self) -> QHBoxLayout:
        """构建标题、语言选择器和工具链入口，返回顶部布局。"""
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
        self.btn_tools = QPushButton()
        self.btn_tools.clicked.connect(self.open_tools)
        top.addWidget(self.title_lbl)
        top.addStretch()
        top.addWidget(self.lang_lbl)
        top.addWidget(self.lang_combo)
        top.addWidget(self.btn_tools)
        return top

    def _build_mode_bar(self) -> QHBoxLayout:
        """构建居中的「压缩 | 解压」模式切换，返回布局；切换时只显示当前模式的分组。"""
        bar = QHBoxLayout()
        self.mode_tabs = QTabBar()
        for _ in MODES:
            self.mode_tabs.addTab("")
        self.mode_tabs.setDrawBase(False)
        self.mode_tabs.currentChanged.connect(self.on_mode_changed)
        bar.addStretch()
        bar.addWidget(self.mode_tabs)
        bar.addStretch()
        return bar

    def _build_menu(self) -> None:
        """在 macOS 菜单栏加入「工具 ▸ 工具链…」；无父对象的 QMenuBar 会成为全局菜单栏。"""
        self.menu_bar = QMenuBar(None)
        self.tools_menu = self.menu_bar.addMenu("")
        self.tools_action = QAction(self)
        self.tools_action.triggered.connect(self.open_tools)
        self.tools_menu.addAction(self.tools_action)

    def _build_source_group(self) -> QGroupBox:
        """构建可键入的源路径框和选择按钮，校验提示显示在路径框之后，返回源文件分组。"""
        self.src_box = QGroupBox()
        v = QVBoxLayout(self.src_box)
        h = QHBoxLayout()
        self.src_input = PathInput(self._parse_source)
        self.src_input.committed.connect(self.on_source_committed)
        self.src_input.checked.connect(self.update_go_state)
        self.btn_folder = QPushButton()
        self.btn_file = QPushButton()
        self.btn_folder.clicked.connect(self.choose_folder)
        self.btn_file.clicked.connect(self.choose_file)
        h.addWidget(self.src_input, 1)
        h.addWidget(self.btn_folder)
        h.addWidget(self.btn_file)
        v.addLayout(h)
        v.addWidget(self.src_input.hint)
        return self.src_box

    def _build_options_group(self) -> QGroupBox:
        """构建格式、级别、加密和输出设置，返回选项分组。"""
        self.opt_box = QGroupBox()
        g = QGridLayout(self.opt_box)
        self.fmt_lbl = QLabel()
        self.fmt_combo = QComboBox()
        for key in core.FORMATS:
            self.fmt_combo.addItem(key, key)
        last = self.settings.value("format", DEFAULT_FORMAT)
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
        self.level_note.setStyleSheet(NOTE_STYLE)
        g.addWidget(self.level_note, 2, 1, 1, 4)
        self.extreme_chk = QCheckBox()
        g.addWidget(self.extreme_chk, 3, 1, 1, 4)

        self.encrypt_panel = EncryptPanel(self)
        self.encrypt_panel.changed.connect(self.update_go_state)
        # 打开加密选项后内容变多，等布局更新完再把窗口加高到能完整显示说明文字。
        self.encrypt_panel.changed.connect(lambda: QTimer.singleShot(0, self.grow_to_fit))
        row = self.encrypt_panel.place(g, 4, 4)

        self.out_lbl = QLabel()
        self.out_input = PathInput(self._parse_output)
        self.out_input.committed.connect(self.on_output_committed)
        self.out_input.checked.connect(self.update_go_state)
        self.btn_save = QPushButton()
        self.btn_save.clicked.connect(self.choose_output)
        g.addWidget(self.out_lbl, row, 0)
        g.addWidget(self.out_input, row, 1, 1, 3)
        g.addWidget(self.btn_save, row, 4)
        g.addWidget(self.out_input.hint, row + 1, 1, 1, 4)

        self.excl_lbl = QLabel()
        self.excl_lbl.setWordWrap(True)
        self.excl_lbl.setStyleSheet(NOTE_STYLE)
        g.addWidget(self.excl_lbl, row + 2, 0, 1, 5)
        g.setColumnStretch(2, 1)
        return self.opt_box

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

    def _build_banner(self) -> QHBoxLayout:
        """构建工具链横幅：只在当前格式缺工具或正在安装时显示，按钮打开工具链窗口。"""
        banner = QHBoxLayout()
        self.warn_lbl = QLabel()
        self.warn_lbl.setStyleSheet(WARN_STYLE)
        self.warn_lbl.setWordWrap(True)
        self.banner_btn = QPushButton()
        self.banner_btn.clicked.connect(self.open_tools)
        banner.addWidget(self.warn_lbl, 1)
        banner.addWidget(self.banner_btn)
        return banner

    def _build_actions(self) -> QHBoxLayout:
        """构建进度条与任务操作按钮，返回底部布局。"""
        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.btn_cancel = QPushButton()
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_job)
        self.btn_go = QPushButton()
        self.btn_go.setDefault(True)
        self.btn_go.clicked.connect(self.start)
        bottom.addWidget(self.progress, 1)
        bottom.addWidget(self.btn_cancel)
        bottom.addWidget(self.btn_go)
        return bottom

    def retranslate(self) -> None:
        """按当前语言刷新界面，保留路径、日志和正在运行的任务。"""
        self.setWindowTitle(core.APP_NAME)
        self.title_lbl.setText(tr("title"))
        self.lang_lbl.setText(tr("language"))
        self.btn_tools.setText(tr("tools_btn"))
        self.tools_menu.setTitle(tr("tools_menu"))
        self.tools_action.setText(tr("tools_btn"))
        self.src_box.setTitle(tr("source_group"))
        self.src_input.setPlaceholderText(tr("drop_hint"))
        self.src_input.retranslate()
        self.out_input.setPlaceholderText(tr("out_placeholder"))
        self.out_input.retranslate()
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
        self.log_box.setTitle(tr("log"))
        self.btn_cancel.setText(tr("cancel"))
        for i, mode in enumerate(MODES):
            self.mode_tabs.setTabText(i, tr(f"mode_{mode}"))
        self.btn_go.setText(tr("extract") if self.is_extract() else tr("compress"))
        self.encrypt_panel.retranslate()
        self.extract_panel.retranslate()
        self.tools_dialog.retranslate()
        self.on_level_changed()
        self.refresh_busy_ui()
        # 换语言后说明文字的行数会变，等布局更新后再按需加高窗口。
        QTimer.singleShot(0, self.grow_to_fit)

    def grow_to_fit(self) -> None:
        """窗口矮于内容所需的最小高度时加高，不缩小用户调整过的窗口。

        窗口的最小高度不考虑自动换行的标签需要几行，只按它加高时，加密说明会被压成一行、
        后半句看不到；这里按当前宽度计算换行后的最小高度。
        """
        layout = self.layout()
        layout.activate()
        need = layout.totalMinimumHeightForWidth(self.width())
        if (screen := self.screen()) is not None:
            need = min(need, screen.availableGeometry().height())
        if self.height() < need:
            self.resize(self.width(), need)

    # ---------------- 模式 / 语言 / 格式 / 级别 ----------------
    def is_extract(self) -> bool:
        return self.mode_tabs.currentIndex() == MODES.index("extract")

    def on_mode_changed(self) -> None:
        """切换压缩与解压：只显示当前模式的分组，主按钮随之改名；记住所选模式。"""
        extract = self.is_extract()
        self.settings.setValue("mode", MODES[self.mode_tabs.currentIndex()])
        # 离开的模式里未提交的草稿作废：否则它会一直挂在隐藏的输入框里，之后在开始时被提交，
        # 把用户在那之后选好的输出或目标重置掉。
        panel = self.extract_panel
        for field in ((self.src_input, self.out_input) if extract
                      else (panel.arc_input, panel.dest_input)):
            field.revert()
        self.src_box.setVisible(not extract)
        self.opt_box.setVisible(not extract)
        self.extract_panel.setVisible(extract)
        self.btn_go.setText(tr("extract") if extract else tr("compress"))
        if not self.is_busy():
            # 在另一个模式期间，这个模式的路径可能已在 Finder 里被删除或创建。
            (self.extract_panel if extract else self.src_input).recheck()
        self.refresh_busy_ui()

    def on_lang_changed(self) -> None:
        """保存用户选择的语言，并刷新可见标签和 Qt 自带文案的翻译。"""
        code = self.lang_combo.currentData()
        set_lang(code)
        self.settings.setValue("lang", code)
        install_qt_translator()
        self.retranslate()

    def current_format(self) -> core.Format:
        return core.FORMATS[self.fmt_combo.currentData()]

    def on_format_changed(self) -> None:
        """应用当前格式的级别范围及默认值，并同步输出扩展名。"""
        fmt = self.current_format()
        self.settings.setValue("format", fmt.key)
        if fmt.key != self._last_fmt:
            # 切换格式时恢复该格式的默认设置，极限模式也一并关闭。
            self.extreme_chk.setChecked(False)
            self._last_fmt = fmt.key
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
        self.encrypt_panel.set_format(fmt.key)
        self.on_level_changed()
        # macOS 上点选格式菜单不会让输出框失去焦点，先提交其中的草稿，免得被自动路径覆盖。
        if self.out_input.pending and self.out_input.isEnabled():
            self.out_input.commit()
        # 草稿不合格时保留原文和提示，让用户修改；否则按新格式更新扩展名。
        if self.out_input.error is None:
            self.update_output_path()
        self.update_go_state()
        # 换格式会显示或隐藏加密说明等行，等布局更新后再按需加高窗口。
        QTimer.singleShot(0, self.grow_to_fit)

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
        """以拖放或文件对话框给出的路径提交源；空路径（对话框取消）不改变当前输入。"""
        if path:
            self.src_input.submit(path)

    @staticmethod
    def _parse_source(text: str) -> Path | None:
        """校验源路径框的文字；空白返回 None，不合格时抛出 JobError。"""
        s = core.clean_typed_path(text)
        return core.validate_source(s) if s else None

    def on_source_committed(self, path: Path | None) -> None:
        """源提交后重置输出命名偏好；不合格或清空时没有可用的源，提示留在路径框里。"""
        self.src = path
        self.out_dir = None
        self.out_stem = None
        if path is not None:
            self.src_input.display(str(path))  # 显示整理后的路径，例如展开 ~ 后的结果
        self.update_output_path()
        self.update_go_state()

    def choose_folder(self) -> None:
        self.set_source(QFileDialog.getExistingDirectory(self, tr("choose_folder")))

    def choose_file(self) -> None:
        self.set_source(QFileDialog.getOpenFileName(self, tr("choose_file"))[0])

    def output_path(self) -> Path:
        """由源路径、命名偏好和当前格式组合输出路径；调用前必须已有源。"""
        assert self.src is not None
        d = self.out_dir or self.src.parent
        stem = self.out_stem or self.src.name
        return d / (stem + self.current_format().ext)

    def update_output_path(self) -> None:
        """在输出框显示组合出的输出路径；没有源时清空。"""
        self.out_input.display(str(self.output_path()) if self.src else "")

    def _parse_output(self, text: str) -> tuple[Path, str | None] | None:
        """解析输出框文字：只写名称时相对当前输出目录；不合格时抛出 JobError。"""
        current = self.out_dir or (self.src.parent if self.src else Path.home())
        return core.parse_output(text, current)

    def on_output_committed(self, result: tuple[Path, str | None] | None) -> None:
        """把键入的输出位置写回 out_dir/out_stem，切换格式时不会丢失；留空恢复默认。"""
        if self.out_input.error is not None:
            return  # 保留之前的设置，错误提示留在输出框
        if result is None:
            self.out_dir = self.out_stem = None
        else:
            self.out_dir, stem = result
            if stem is not None:
                self.out_stem = stem
        self.update_output_path()
        self.update_go_state()

    def choose_output(self) -> None:
        """通过原生保存对话框更新输出目录和不含归档扩展名的名称。"""
        if not self.src:
            return
        ext = self.current_format().ext
        p, _ = QFileDialog.getSaveFileName(self, tr("save_as"), str(self.output_path()), f"*{ext}")
        if p:
            p = Path(p)
            self.out_dir, self.out_stem = p.parent, core.strip_ext(p.name)
            self.update_output_path()
            self.update_go_state()

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        """仅接受空闲时拖入的一个本地路径，避免静默遗漏多选项目。"""
        urls = e.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile() and not self.is_busy():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        """把有效拖放交给当前模式：压缩模式作为源，解压模式作为归档；不会自动切换模式。"""
        urls = e.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile() and not self.is_busy():
            path = urls[0].toLocalFile()
            if self.is_extract():
                self.extract_panel.set_archive(path)
            else:
                self.set_source(path)
            e.acceptProposedAction()

    # ---------------- 工具链 ----------------
    def open_tools(self) -> None:
        self.tools_dialog.show_and_raise()

    def on_app_state_changed(self, state: Qt.ApplicationState) -> None:
        """应用重新激活时重新检测工具和路径；安装进行中由安装结束时统一刷新工具。

        用户可能在 Finder 或终端里新建了输出文件夹、删除了源，切回应用后提示应随之更新。
        """
        if state != Qt.ApplicationActive:
            return
        if not self.tools.installing:
            self.tools.refresh()
        if not self.is_busy():
            # 发出 checked，随后刷新按钮和输出提示。
            (self.extract_panel if self.is_extract() else self.src_input).recheck()

    def missing_tools(self) -> tuple[str | None, list[str]]:
        """返回当前模式涉及的格式和它缺少的工具；解压模式按已提交归档的实际格式判断。"""
        if self.is_extract():
            return (self.extract_panel.format_key(),
                    self.extract_panel.missing_tools(self.tools.paths))
        fmt = self.current_format().key
        return fmt, self.tools.missing_for(fmt, self.encrypt_panel.encrypting())

    def update_banner(self) -> None:
        """只在当前格式缺工具或正在安装时显示横幅；文案不写位置，按钮直接打开工具链窗口。"""
        fmt, missing = self.missing_tools()
        if self.tools.installing:
            text, button = tr("installing_banner"), tr("tools_btn")
        elif missing and not any(core.TOOLS[t].formula for t in missing):
            # 缺的都是 macOS 自带、无法用 Homebrew 安装的工具，不能提示去安装。
            text, button = tr("tools_not_brew", tools=", ".join(missing)), tr("tools_btn")
        elif missing:
            # 没有 Homebrew 时无法在应用内安装，按钮只打开工具链窗口，不承诺“安装”。
            button = tr("tools_fix_btn") if self.tools.brew else tr("tools_btn")
            text = tr("missing_tools", fmt=fmt, tools=", ".join(missing))
        else:
            text, button = "", ""
        self.warn_lbl.setText(text)
        self.banner_btn.setText(button)
        self.warn_lbl.setVisible(bool(text))
        self.banner_btn.setVisible(bool(text))

    # ---------------- 任务 ----------------
    def is_busy(self) -> bool:
        return self.job is not None or self.tools.installing

    def update_go_state(self) -> None:
        """根据当前模式的输入、预检、工具缺失和忙碌状态决定主按钮、相关控件及横幅内容。"""
        busy = self.is_busy()
        _, missing = self.missing_tools()
        self.update_banner()
        self.extract_panel.set_busy(busy)
        if self.is_extract():
            if not busy:
                self.extract_panel.refresh_note()
            ready = self.extract_panel.ready()
        else:
            ready = self._compress_ready(busy)
        self.btn_go.setEnabled(ready and not missing and not busy)

    def _compress_ready(self, busy: bool) -> bool:
        """刷新压缩模式的输出控件和提示，返回源与输出是否可以开始压缩。"""
        has_src = bool(self.src)
        if not busy:
            self.out_input.set_note(self._output_problem())
        if self.src_input.pending:
            # 源草稿还没提交：校验通过即可点击，start 会先提交它。提交新源会重新生成
            # 输出路径，旧的输出提示不再适用，由 start 完整预检。
            src_ok = self.src_input.error is None and bool(self.src_input.text().strip())
            out_ok = True
        else:
            src_ok = has_src and self.src_input.error is None
            out_ok = self.out_input.error is None and self.out_input.note is None
        # 密码的问题显示在加密选项里；不合格时不能开始。
        out_ok = out_ok and self.encrypt_panel.problem() is None
        # 选定源之前还没有可组合的输出路径，另存为和输出框暂不可用，避免选择被静默丢弃。
        self.btn_save.setEnabled(has_src and not busy)
        self.out_input.setEnabled(has_src and not busy)
        return src_ok and out_ok

    def _output_problem(self) -> core.JobError | None:
        """对已提交的源和输出做压缩前预检，返回应在输出框下提示的问题。

        任一路径框有草稿时不检查：已提交的位置即将被草稿替换，start 会先提交再完整预检。
        """
        if not self.src or self.src_input.pending or self.out_input.pending:
            return None
        try:
            core.check_compress_output(self.src, self.output_path())
        except core.JobError as exc:
            return exc
        except OSError as exc:
            return core.job_error_from_os(exc)
        return None

    def refresh_busy_ui(self) -> None:
        """按任务和安装状态切换进度、输入控件和按钮；进度条与取消按钮只对应压缩或解压任务。"""
        busy = self.is_busy()
        running = self.job is not None
        self.progress.setVisible(running)
        self.btn_cancel.setEnabled(running)
        for w in (self.mode_tabs, self.src_input, self.btn_folder, self.btn_file,
                  self.fmt_combo, self.extreme_chk):
            w.setEnabled(not busy)
        self.level_slider.setEnabled(not busy and self.current_format().level is not None)
        self.encrypt_panel.set_busy(busy)
        self.update_go_state()

    def start(self) -> None:
        """按当前模式开始压缩或解压。"""
        if self.is_extract():
            self.start_extract()
        else:
            self.start_compress()

    def start_compress(self) -> None:
        """提交键入中的路径，预检源和输出、确认覆盖意图，捕获参数后启动后台压缩线程。"""
        # 提交新的源会重置输出命名，所以先记下输出框的草稿，源提交之后再提交它。
        out_draft = self.out_input.text() if self.out_input.pending else None
        self.src_input.commit()
        if self.src_input.error is not None:
            return  # 原因已显示在源路径框之后
        if not self.src:
            self.show_message(QMessageBox.Warning, tr("no_source"))
            return
        if out_draft is not None:
            self.out_input.setText(out_draft)
        self.out_input.commit()
        if self.out_input.error is not None:
            return
        out = self.output_path()
        # 源可能在选择之后被删除；输出位置也要先检查，避免用户确认替换后才发现目录不可写。
        try:
            core.validate_source(self.src)
            core.check_compress_output(self.src, out)
        except (core.JobError, OSError) as exc:
            err = exc if isinstance(exc, core.JobError) else core.job_error_from_os(exc)
            self.show_message(QMessageBox.Warning, tr("failed", err=tr(err.key, **err.kw)))
            return
        if self.encrypt_panel.problem() is not None:
            return  # 原因已显示在加密选项里
        if out.exists() and not confirm(self, tr("overwrite_title"),
                                        tr("overwrite_q", path=out), "replace"):
            return
        fmt = self.current_format()
        enc = self.encrypt_panel
        self._launch(core.ArchiveJob(
            self.src, out, fmt.key,
            level=self.level_slider.value() if fmt.level else None,
            extreme=self.extreme_chk.isChecked() and fmt.key == "tar.xz",
            log=self.bridge.log.emit,
            password=enc.password(), zip_method=enc.zip_method(),
            encrypt_names=enc.encrypt_names(),
        ))

    def start_extract(self) -> None:
        """提交键入中的路径，预检归档和目标文件夹，然后启动后台解压线程。

        解压绝不覆盖已有内容（重名时自动编号），所以不需要替换确认。
        """
        panel = self.extract_panel
        if not panel.commit_drafts():
            return  # 原因已显示在路径框之后
        if panel.archive is None:
            self.show_message(QMessageBox.Warning, tr("no_archive"))
            return
        if self.missing_tools()[1]:
            return  # 草稿提交后才知道格式；缺少的工具已显示在横幅里
        dest = panel.dest_path()
        assert dest is not None  # 已提交归档时总有目标：选过的文件夹或归档所在的文件夹
        try:
            archive = core.validate_archive(panel.archive)
            core.check_output_dir(dest)
        except (core.JobError, OSError) as exc:
            err = exc if isinstance(exc, core.JobError) else core.job_error_from_os(exc)
            self.show_message(QMessageBox.Warning,
                              tr("extract_failed", err=tr(err.key, **err.kw)))
            return
        self._launch(core.ExtractJob(archive, dest, log=self.bridge.log.emit))

    def _launch(self, job: core.Job) -> None:
        """在后台线程运行 job，并通知工具链控制器有任务在运行。"""
        self.job = job
        self.t0 = time.monotonic()
        # 通知控制器任务开始，工具链窗口随之停用安装按钮，保持两者互斥。
        self.tools.set_job_running(True)
        # 非守护线程：退出流程会先取消任务并等待它结束，保证子进程和暂存目录被清理。
        self._worker = threading.Thread(target=self._run_job, args=(self.job,))
        self._worker.start()

    def _run_job(self, job: core.Job) -> None:
        """在工作线程运行 job；通过信号传递路径或异常，不接触控件。"""
        try:
            self.bridge.done.emit(job.run(), None)
        except Exception as e:  # noqa: BLE001
            self.bridge.done.emit(None, e)

    def cancel_job(self) -> None:
        """请求核心任务取消，由完成回调恢复界面；取消安装在工具链窗口中进行。"""
        if self.job:
            self.job.cancel()

    def on_job_done(self, path: Path | None, err: Exception | None) -> None:
        """在主线程处理结果；path 为成功产物，err 为失败或取消原因。退出途中只记日志不弹窗。"""
        extract = isinstance(self.job, core.ExtractJob)
        job = self.job
        self.job = None
        self._worker = None
        self.tools.set_job_running(False)
        if (isinstance(job, core.ExtractJob) and isinstance(err, core.JobError)
                and err.key in PASSWORD_ERRORS and not self._quit_requested):
            self.append_log(tr(err.key, **err.kw))
            if self._confirming_quit:
                # 不在退出确认框上面再弹密码框，更不能在用户回答之前启动新的解压。
                self._deferred_retry = (job, err)
            else:
                self._retry_with_password(job, err)
            return
        if err is None:
            assert path is not None
            if isinstance(job, core.ExtractJob):
                self._log_extract_notes(job)
            elif isinstance(job, core.ArchiveJob) and job.password is not None:
                self.encrypt_panel.clear_passwords()
            msg = self._done_message(path, extract)
            self.append_log(msg)
            if not self._quit_requested:
                self._show_done(path, msg, tr("extract_done_title" if extract else "done_title"))
        elif isinstance(err, core.Cancelled):
            self.append_log(tr("extract_cancelled" if extract else "cancelled"))
        else:
            text = tr("extract_failed" if extract else "failed", err=self._error_text(err))
            self.append_log(text)
            if not self._quit_requested:
                self.show_message(QMessageBox.Warning, text)
        self._maybe_finish_quit()

    def _retry_with_password(self, job: core.ExtractJob, err: core.JobError) -> None:
        """归档需要密码或密码不正确时询问密码，带密码重新解压；用户取消时按取消处理。

        第一次询问（还没给过密码）不显示错误，之后的询问附上上次失败的原因。
        """
        error = tr(err.key, **err.kw) if job.password is not None else None
        password = ask_password(self, tr("password_prompt", name=job.archive.name), error)
        if self._closed or self._quit_requested:
            return  # 询问期间窗口已关闭或开始退出
        if password is None:
            self.append_log(tr("extract_cancelled"))
            self._maybe_finish_quit()
            return
        self._launch(core.ExtractJob(job.archive, job.dest, log=self.bridge.log.emit,
                                     password=password))

    def _log_extract_notes(self, job: core.ExtractJob) -> None:
        """把解压的警告（越界链接、隔离属性）写进日志。

        不报告跳过的垃圾数量：大部分垃圾在工具层就被排除，事后只能数到漏网的那部分。
        """
        for warning in job.warnings:
            self.append_log(tr(warning.key, **warning.kw))

    def _done_message(self, path: Path, extract: bool = False) -> str:
        """组合完成提示；压缩产物在提交后被移走时省略大小，不让结果处理中途抛异常。"""
        secs = format_secs(time.monotonic() - self.t0)
        if extract:
            return tr("extract_done", path=path, secs=secs)
        try:
            size = format_size(path.stat().st_size)
        except OSError:
            size = "–"
        return tr("done", path=path, size=size, secs=secs)

    def _error_text(self, err: Exception) -> str:
        """把任务异常转成当前语言的说明；非 JobError 属于程序缺陷，附上异常类型便于排查。"""
        if isinstance(err, core.JobError):
            return tr(err.key, **err.kw)
        return tr("internal_error", err=f"{type(err).__name__}: {err}")

    def _show_done(self, path: Path, msg: str, title: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(msg)
        b_show = box.addButton(tr("show_in_finder"), QMessageBox.ActionRole)
        box.addButton(tr("ok"), QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is b_show:
            self.show_in_finder(path)

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

    # ---------------- 退出 ----------------
    def closeEvent(self, e: QCloseEvent) -> None:
        """任务或安装进行中时先确认，再取消并等它结束后关闭，不留下孤立进程和暂存文件。"""
        if self._quit_ready or not self.is_busy():
            self.tools_dialog.hide()
            self._closed = True
            e.accept()
            return
        e.ignore()
        if self._quit_requested:
            return
        question = tr("quit_job_q") if self.job else tr("quit_install_q")
        self._confirming_quit = True
        try:
            stop = confirm(self, tr("quit_title", app=core.APP_NAME), question, "stop_quit")
        finally:
            self._confirming_quit = False
        retry, self._deferred_retry = self._deferred_retry, None
        if not stop:
            if retry is not None:
                self._retry_with_password(*retry)
            return
        if retry is not None:
            self.append_log(tr("extract_cancelled"))
        self._quit_requested = True
        self.append_log(tr("quitting"))
        self.cancel_job()
        self.tools.cancel_install()
        # 确认框打开期间任务可能已经结束，此时没有完成回调会再来触发关闭，需要在这里检查一次。
        self._maybe_finish_quit()

    def _maybe_finish_quit(self) -> None:
        """退出请求已确认且任务和安装都已结束时，真正关闭窗口。"""
        if self._quit_requested and not self._quit_ready and not self.is_busy():
            self._quit_ready = True
            QTimer.singleShot(0, self.close)

    def shutdown(self) -> None:
        """应用退出前的最后防线：取消任务并等待工作线程，结束仍在运行的安装进程。"""
        if self.job is not None:
            self.job.cancel()
        if self._worker is not None:
            self._worker.join(10)
        self.tools.kill_install()


def settings_scope(bundle_id: str) -> tuple[str, str]:
    """把 bundle id 拆成 QSettings 的 (组织域名, 应用名)。

    macOS 上 Qt 会把组织域名倒序后接上应用名作为偏好域，得到的正是 bundle id，
    这样应用偏好与系统为该 bundle 保存的面板状态位于同一个 plist。
    """
    *org, app = bundle_id.split(".")
    return ".".join(reversed(org)), app


def migrate_legacy_settings(settings: QSettings, legacy: QSettings | None = None) -> None:
    """首次使用新偏好域时，从旧域复制语言和格式；旧文件保留不删。

    legacy 默认是旧版本使用的 com.cleanpack.CleanPack，测试时可传入临时设置。
    """
    if settings.value("prefs_version") is not None:
        return
    if legacy is None:
        # 旧偏好域是历史事实，写死名称；以后即使修改 APP_NAME 也仍能找到老用户的设置。
        legacy = QSettings("CleanPack", "CleanPack")
    for key in ("lang", "format"):
        value = legacy.value(key)
        if value is not None and settings.value(key) is None:
            settings.setValue(key, value)
    settings.setValue("prefs_version", 1)
    settings.sync()


def main() -> None:
    """创建 Qt 应用，加载语言偏好并启动主事件循环。"""
    # 从 Finder 启动时可打开文件数只有 256，解压很深的目录树需要更多（见 core.raise_fd_limit）。
    core.raise_fd_limit()
    app = QApplication(sys.argv)
    app.setApplicationName(core.APP_NAME)
    app.setApplicationVersion(core.APP_VERSION)
    settings = QSettings(*settings_scope(core.BUNDLE_ID))
    migrate_legacy_settings(settings)
    set_lang(settings.value("lang") or detect_lang(QLocale.system().uiLanguages()))
    install_qt_translator()
    w = MainWindow(settings)
    app.aboutToQuit.connect(w.shutdown)
    w.resize(780, 640)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
