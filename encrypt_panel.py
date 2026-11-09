"""压缩模式的加密选项：是否加密、密码与确认、zip 的加密方式、7z 是否加密文件名。

密码只保存在输入框里，不写入偏好，压缩成功后清空。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QWidget)

import core
import theme
from i18n import tr


class EncryptPanel(QObject):
    """加密选项。格式不支持加密（tar 系列）时停用并说明原因。

    控件由 place 放进选项分组的网格，与格式、输出共用标签列；标签列预留了隐藏标签的宽度，
    勾选加密时控件不会左右跳动。
    状态变化时发出 changed，由主窗口刷新主按钮。password() 返回要使用的密码，
    不加密时为 None；problem() 返回阻止开始压缩的问题。
    """
    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._fmt = core.ENCRYPTABLE[0]
        self._busy = False
        self._grid: QGridLayout | None = None  # place 放入的网格
        # 打开、关闭一项设置用 M3 开关；「显示密码」只是临时查看，保留复选框（用户决定）。
        self.encrypt_chk = theme.Switch()
        self.encrypt_chk.toggled.connect(self._on_changed)

        self.pw_lbl = QLabel()
        self.pw_input = QLineEdit()
        self.pw_confirm = QLineEdit()
        for w in (self.pw_input, self.pw_confirm):
            w.setEchoMode(QLineEdit.EchoMode.Password)
            # 拖放由窗口统一处理；密码框也不应接收拖进来的文件路径。
            w.setAcceptDrops(False)
            # 不用 setMaxLength：粘贴过长的密码会被悄悄截断，两个框截得一样也能通过确认，
            # 用户之后拿完整密码却打不开归档。超长时由 check_password 报错。
            w.textChanged.connect(self._on_changed)
        self.show_chk = QCheckBox()
        self.show_chk.toggled.connect(self._on_show_toggled)
        self.method_lbl = QLabel()
        self.method_combo = QComboBox()
        for method in core.ZIP_METHODS:
            self.method_combo.addItem("", method)
        self.method_combo.currentIndexChanged.connect(self._on_changed)
        self.names_chk = theme.Switch()
        self.names_chk.setChecked(True)
        self.names_chk.toggled.connect(self._on_changed)
        self.hint = QLabel()  # 密码的问题，例如两次输入不一致
        theme.set_role(self.hint, "warn")
        self.note = QLabel()  # 当前格式下加密的说明
        theme.set_role(self.note, "note")
        for lbl in (self.hint, self.note):
            lbl.setWordWrap(True)

    def place(self, grid: QGridLayout, row: int, span: int) -> int:
        """把控件放进 grid：标签在第 0 列，其余从第 1 列起占 span 列；返回下一个空行。"""
        self._grid = grid
        # 「显示密码」放在两个密码框同一行的末尾：M3 的控件比原生的高，单独占一行会让打开
        # 加密后的窗口在小屏幕上放不下。
        fields = QHBoxLayout()
        fields.addWidget(self.pw_input, 1)
        fields.addWidget(self.pw_confirm, 1)
        fields.addWidget(self.show_chk)
        rows: list[tuple[QLabel | None, QWidget | QHBoxLayout]] = [
            (None, self.encrypt_chk), (self.pw_lbl, fields),
            (self.method_lbl, self.method_combo), (None, self.names_chk),
            (None, self.hint), (None, self.note)]
        for label, item in rows:
            if label is not None:
                grid.addWidget(label, row, 0)
            if isinstance(item, QHBoxLayout):
                grid.addLayout(item, row, 1, 1, span)
            else:
                grid.addWidget(item, row, 1, 1, span)
            row += 1
        # 控件有了父窗口再按状态显示或隐藏；之前调用 setVisible(True) 会让它们成为独立窗口。
        self._refresh()
        self._reserve_label_width()
        return row

    def _reserve_label_width(self) -> None:
        """让标签列始终容得下「密码」「加密方式」：这两个标签隐藏时列宽会收窄，
        勾选加密后整列变宽，格式、输出等控件和勾选框都会向右跳。"""
        if self._grid is not None:
            width = max(lbl.sizeHint().width() for lbl in (self.pw_lbl, self.method_lbl))
            self._grid.setColumnMinimumWidth(0, width)

    # ---------------- 公共接口 ----------------
    def retranslate(self) -> None:
        self.encrypt_chk.setText(tr("encrypt"))
        self.pw_lbl.setText(tr("password"))
        self.pw_input.setPlaceholderText(tr("password"))
        self.pw_confirm.setPlaceholderText(tr("password_again"))
        self.show_chk.setText(tr("show_password"))
        self.method_lbl.setText(tr("zip_method"))
        for i, method in enumerate(core.ZIP_METHODS):
            self.method_combo.setItemText(i, tr(f"zip_{method}"))
        self.names_chk.setText(tr("encrypt_names"))
        self._reserve_label_width()  # 标签文字随语言变化
        self._refresh()

    def set_format(self, fmt_key: str) -> None:
        """切换格式时调用；密码保留，切回可加密的格式后不必重新输入。"""
        self._fmt = fmt_key
        self._refresh()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh()

    def encrypting(self) -> bool:
        """当前格式支持加密且用户勾选了加密。"""
        return self._fmt in core.ENCRYPTABLE and self.encrypt_chk.isChecked()

    def password(self) -> str | None:
        return self.pw_input.text() if self.encrypting() else None

    def zip_method(self) -> str:
        return self.method_combo.currentData()

    def encrypt_names(self) -> bool:
        return self.names_chk.isChecked()

    def problem(self) -> core.JobError | None:
        """返回阻止开始压缩的密码问题；不加密时为 None。"""
        if not self.encrypting():
            return None
        try:
            core.check_password(self.pw_input.text(), self._fmt)
        except core.JobError as exc:
            return exc
        if self.pw_confirm.text() != self.pw_input.text():
            return core.JobError("password_mismatch" if self.pw_confirm.text()
                                 else "password_confirm")
        return None

    def clear_passwords(self) -> None:
        """压缩成功后清空密码；加密选项保留，下次压缩需要重新输入密码。

        用 setText("") 而不是 clear()：clear() 会进撤销历史，打开「显示密码」后按 Cmd+Z
        就能把密码恢复出来；setText 会清空撤销历史。
        """
        self.pw_input.setText("")
        self.pw_confirm.setText("")

    # ---------------- 内部实现 ----------------
    def _on_show_toggled(self, shown: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        for w in (self.pw_input, self.pw_confirm):
            w.setEchoMode(mode)

    def _on_changed(self, *_: object) -> None:
        self._refresh()
        self.changed.emit()

    def _refresh(self) -> None:
        """按格式、勾选和忙碌状态更新控件的可用性与可见性，以及提示文字。"""
        supported = self._fmt in core.ENCRYPTABLE
        on = self.encrypting()
        self.encrypt_chk.setEnabled(supported and not self._busy)
        for w in (self.pw_lbl, self.pw_input, self.pw_confirm, self.show_chk):
            w.setVisible(on)
            w.setEnabled(not self._busy)
        is_zip, is_7z = self._fmt == "zip", self._fmt == "7z"
        self.method_lbl.setVisible(on and is_zip)
        self.method_combo.setVisible(on and is_zip)
        self.method_combo.setEnabled(not self._busy)
        self.names_chk.setVisible(on and is_7z)
        self.names_chk.setEnabled(not self._busy)

        # 两次输入都还空着时不报错，只是主按钮不可用；用户开始输入后再提示具体问题。
        problem = self.problem()
        typed = bool(self.pw_input.text() or self.pw_confirm.text())
        hint = tr(problem.key, **problem.kw) if problem and typed else ""
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))
        self.note.setText(self._note_text(supported, on, is_zip, is_7z))
        self.note.setVisible(bool(self.note.text()))

    def _note_text(self, supported: bool, on: bool, is_zip: bool, is_7z: bool) -> str:
        """当前格式下加密的说明：tar 不能加密、zip 不隐藏文件名、ZipCrypto 是弱加密等。"""
        if not supported:
            return tr("encrypt_tar_note")
        if not on:
            return ""
        if is_zip:
            notes = [tr("zip_names_note")]
            if self.zip_method() == "zipcrypt":
                notes.append(tr("zipcrypto_note"))
            return " ".join(notes)
        if is_7z and not self.names_chk.isChecked():
            return tr("names_visible_note")
        return ""
