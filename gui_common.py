"""GUI 公用的 Qt 辅助函数：子进程环境、按界面语言格式化的数字和确认对话框。"""
from __future__ import annotations

from PySide6.QtCore import QLocale, QProcessEnvironment
from PySide6.QtWidgets import QMessageBox, QWidget

import core
from i18n import QT_LOCALES, current_lang, tr


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
