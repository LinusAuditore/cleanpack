"""解压模式的输入区：归档路径、识别出的格式、目标文件夹，以及放置规则和跳过项说明。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

import core
import theme
from gui_common import PathInput
from i18n import tr

# 文件对话框的过滤器：只列出支持的归档扩展名。
ARCHIVE_PATTERNS = " ".join(f"*{f.ext}" for f in core.FORMATS.values())


class ExtractPanel(QWidget):
    """管理要解压的归档和目标文件夹；状态或有效性变化时发出 changed，由主窗口刷新按钮和横幅。

    目标文件夹留空时使用归档所在的文件夹。拖放由主窗口转交给 set_archive。

    面板本身只装「归档」卡片；「解压」卡片 ext_box 由主窗口放进与压缩选项卡片共用的
    QStackedWidget，两种模式的第二张卡片因此一样高，切换模式时布局不动。
    """
    changed = Signal()
    # 路径框旁的补救按钮被点按：参数是带 denial 的 JobError 和框的名称（archive 或 dest），
    # 由主窗口统一处理（重新选择或在访达中显示）。
    fix_requested = Signal(object, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.archive: Path | None = None
        self.dest_dir: Path | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._build_archive_group())
        self._build_extract_group()  # 由主窗口放置，见类文档

    # ---------------- 界面 ----------------
    def _build_archive_group(self) -> QGroupBox:
        """构建可键入的归档路径框和「选择归档…」按钮，校验提示显示在路径框之后。"""
        self.arc_box = QGroupBox()
        v = QVBoxLayout(self.arc_box)
        h = QHBoxLayout()
        self.arc_input = PathInput(self._parse_archive)
        self.arc_input.committed.connect(self.on_archive_committed)
        self.arc_input.checked.connect(self._update_format_label)
        self.arc_input.checked.connect(self.changed)
        self.btn_archive = QPushButton()
        theme.set_icon(self.btn_archive, "folder_open")
        self.btn_archive.clicked.connect(self.choose_archive)
        self.arc_input.fix_requested.connect(lambda err: self.fix_requested.emit(err, "archive"))
        h.addWidget(self.arc_input, 1)
        h.addWidget(self.btn_archive)
        v.addLayout(h)
        v.addWidget(self.arc_input.hint_row)
        return self.arc_box

    def _build_extract_group(self) -> QGroupBox:
        """构建识别出的格式、目标文件夹，以及放置规则与始终跳过项的说明。"""
        self.ext_box = QGroupBox()
        g = QGridLayout(self.ext_box)
        self.fmt_lbl = QLabel()
        g.addWidget(self.fmt_lbl, 0, 0, 1, 3)
        self.dest_lbl = QLabel()
        self.dest_input = PathInput(self._parse_dest)
        self.dest_input.committed.connect(self.on_dest_committed)
        self.dest_input.checked.connect(self.changed)
        self.btn_dest = QPushButton()
        theme.set_icon(self.btn_dest, "folder_open")
        self.btn_dest.clicked.connect(self.choose_dest)
        self.dest_input.fix_requested.connect(lambda err: self.fix_requested.emit(err, "dest"))
        g.addWidget(self.dest_lbl, 1, 0)
        g.addWidget(self.dest_input, 1, 1)
        g.addWidget(self.btn_dest, 1, 2)
        g.addWidget(self.dest_input.hint_row, 2, 1, 1, 2)
        self.layout_note = QLabel()
        self.skipped_lbl = QLabel()
        for lbl in (self.layout_note, self.skipped_lbl):
            lbl.setWordWrap(True)
            theme.set_role(lbl, "note")
        g.addWidget(self.layout_note, 3, 0, 1, 3)
        g.addWidget(self.skipped_lbl, 4, 0, 1, 3)
        g.setColumnStretch(1, 1)
        # 卡片与压缩选项卡片一样高，多出的空间留在底部，内容保持靠上。
        g.setRowStretch(5, 1)
        return self.ext_box

    def retranslate(self) -> None:
        self.arc_box.setTitle(tr("archive_group"))
        self.arc_input.setPlaceholderText(tr("archive_hint"))
        self.btn_archive.setText(tr("choose_archive"))
        self.ext_box.setTitle(tr("extract_group"))
        self.dest_lbl.setText(tr("dest"))
        self.dest_input.setPlaceholderText(tr("dest_placeholder"))
        self.btn_dest.setText(tr("choose_folder"))
        self.layout_note.setText(tr("layout_note"))
        junk = ", ".join(p.replace("\r", "\\r") for p in core.JUNK)
        self.skipped_lbl.setText(tr("skipped", list=junk))
        self.arc_input.retranslate()
        self.dest_input.retranslate()
        self._update_format_label()

    # ---------------- 归档 ----------------
    def set_archive(self, path: str) -> None:
        """以拖放或文件对话框给出的路径提交归档；空路径（对话框取消）不改变当前输入。"""
        if path:
            self.arc_input.submit(path)

    def choose_archive(self) -> None:
        self.pick_archive("")

    def pick_archive(self, start: str) -> None:
        """在原生面板里选择归档，start 是面板一开始显示的位置。重新选中被隐私保护拦住的
        归档也走这里：在系统提供的面板里亲自选择，就是 macOS 认可的授权。"""
        filters = f"{tr('archive_group')} ({ARCHIVE_PATTERNS})"
        self.set_archive(QFileDialog.getOpenFileName(self, tr("choose_archive"), start,
                                                     filters)[0])

    @staticmethod
    def _parse_archive(text: str) -> Path | None:
        s = core.clean_typed_path(text)
        return core.validate_archive(s) if s else None

    def on_archive_committed(self, path: Path | None) -> None:
        """提交归档。用户选过的目标文件夹保留：它与归档名无关，显示的就是实际使用的位置；
        没选过时目标跟随归档所在的文件夹。"""
        self.archive = path
        if path is not None:
            self.arc_input.display(str(path))
        self._update_dest_display()
        self._update_format_label()
        self.changed.emit()

    def checked_archive(self) -> Path | None:
        """判断格式和缺少的工具时依据的归档：有通过校验的草稿时用草稿（开始时会先提交它），
        否则用已提交的归档。"""
        if self.arc_input.pending:
            if self.arc_input.error is not None:
                return None
            try:
                return self._parse_archive(self.arc_input.text())
            except (OSError, core.JobError):
                return None
        return self.archive

    def format_key(self) -> str | None:
        """归档按内容识别出的格式；读取失败时返回 None，开始时会报告具体错误。"""
        archive = self.checked_archive()
        if archive is None:
            return None
        try:
            return core.archive_format(archive)
        except (OSError, core.JobError):
            return None

    def missing_tools(self, paths: dict[str, str | None]) -> list[str]:
        """返回解压当前归档缺少的必需工具；没有归档时为空。"""
        archive = self.checked_archive()
        if archive is None:
            return []
        try:
            family = core.extract_family(archive)
        except (OSError, core.JobError):
            return []  # 归档读不了，开始时会报告具体原因
        return [t for t in core.extract_tools(family) if not paths.get(t)]

    def _update_format_label(self) -> None:
        fmt = self.format_key()
        self.fmt_lbl.setText(tr("detected_format", fmt=fmt) if fmt else "")
        self.fmt_lbl.setVisible(bool(fmt))

    # ---------------- 目标文件夹 ----------------
    def dest_path(self) -> Path | None:
        """实际使用的目标文件夹：选过的文件夹，否则是归档所在的文件夹；都没有时为 None。

        归档有通过校验的草稿时按草稿判断，因为开始解压时会先提交它。目标框里显示的
        始终是这个值，所见即所用。
        """
        if self.dest_dir is not None:
            return self.dest_dir
        archive = self.checked_archive()
        return archive.parent if archive is not None else None

    def choose_dest(self) -> None:
        self.pick_dest(None)

    def pick_dest(self, start: Path | None, *, regrant: bool = False) -> None:
        """在对话框里选择目标，start 为 None 时从当前目标开始。显式选择总是生效，即使选的
        正是当前默认显示的文件夹，这样换归档后它也会保留。

        regrant 表示只是为了授权而重新选择被拒的目标：选的仍是默认的（归档所在的）文件夹时
        保持「跟随归档」，不把它变成选过的目标。
        """
        start = start or self.dest_path()
        folder = QFileDialog.getExistingDirectory(self, tr("dest"), str(start or ""))
        if folder:
            chosen = core.parse_folder(folder)
            # 按 inode 比较：面板返回的写法可能与键入的不同（大小写、经过符号链接）。
            if regrant and self.dest_dir is None and core.same_location(chosen, self.dest_path()):
                self.refresh_note()
                self.changed.emit()
                return
            self.dest_dir = chosen
            # 对话框的选择优先于框里还没提交的草稿：直接显示所选文件夹，草稿作废。
            self.dest_input.display(str(self.dest_dir))
            self.changed.emit()

    @staticmethod
    def _parse_dest(text: str) -> Path | None:
        """目标必须是绝对路径；留空返回 None，表示使用归档所在的文件夹。"""
        return core.parse_folder(text)

    def on_dest_committed(self, result: Path | None) -> None:
        if self.dest_input.error is not None:
            return  # 保留之前的设置，错误提示留在目标框
        self.dest_dir = result
        self._update_dest_display()
        self.changed.emit()

    def _update_dest_display(self) -> None:
        """目标框显示实际使用的目标；框里有用户的草稿时不覆盖。文字没变时不重设，
        免得打断正在框里移动光标的用户。"""
        dest = self.dest_path()
        # 不覆盖用户正在键入的草稿；被拒绝、带着提示等待修改的文字也保留，但只在框可编辑时
        # （有归档或选过目标）——否则框会停用，用户无法修改，这时清空它。
        if self.dest_input.pending or (self.dest_input.rejected and dest is not None):
            return
        text = str(dest) if dest is not None else ""
        if text != self.dest_input.text() or self.dest_input.error is not None:
            self.dest_input.display(text)

    # ---------------- 状态 ----------------
    def refresh_note(self) -> None:
        """预检目标文件夹，把问题作为附加提示显示，并刷新识别出的格式。

        目标框有草稿时不检查，开始时会先提交再预检；归档有通过校验的草稿时按草稿判断。
        """
        self._update_format_label()
        self._update_dest_display()
        note = None
        dest = self.dest_path()
        if dest is not None and not self.dest_input.pending:
            try:
                core.check_output_dir(dest)
            except core.JobError as exc:
                note = exc
            except OSError as exc:
                note = core.job_error_from_os(exc)
        self.dest_input.set_note(note)

    def ready(self) -> bool:
        """归档和目标都可用时返回 True；归档有已校验通过的草稿也算，开始时会先提交。"""
        if self.arc_input.pending:
            archive_ok = self.arc_input.error is None and bool(self.arc_input.text().strip())
        else:
            archive_ok = self.archive is not None and self.arc_input.error is None
        return archive_ok and self.dest_input.error is None and self.dest_input.note is None

    def commit_drafts(self) -> bool:
        """提交两个路径框中的草稿，返回是否没有错误。提交归档会刷新目标框的显示，
        所以先记下目标草稿，归档提交之后再提交它。"""
        dest_draft = self.dest_input.text() if self.dest_input.pending else None
        self.arc_input.commit()
        if self.arc_input.error is not None or self.archive is None:
            return self.arc_input.error is None
        if dest_draft is not None:
            self.dest_input.setText(dest_draft)
        self.dest_input.commit()
        return self.dest_input.error is None

    def recheck(self) -> None:
        """应用重新激活或切回解压模式时重新校验两个路径框。"""
        self.arc_input.recheck()
        self.dest_input.recheck()

    def set_busy(self, busy: bool) -> None:
        for w in (self.arc_input, self.btn_archive):
            w.setEnabled(not busy)
        # 有归档或已选过目标时目标框可用；只选了目标、还没有归档时也能修改或清空它。
        has_dest = self.dest_path() is not None
        for w in (self.dest_input, self.btn_dest):
            w.setEnabled(has_dest and not busy)
