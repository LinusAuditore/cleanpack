"""Material 3 主题层：在 Qt Widgets 上用 Fusion 样式、调色板和样式表画出 M3 外观。

- apply(app)：设置样式、调色板、字体和样式表；不传 scheme 时跟随系统的浅色/深色外观，
  系统切换时自动重新应用。主程序和所有 GUI 夹具都要在建好 QApplication 后调用它，
  否则夹具测到的是没有主题的界面。
- set_role / set_button：给控件标上样式表认识的角色（说明文字、警告、按钮级别等），
  界面代码不直接写颜色。
- set_icon：按角色着色的 Material Symbols 图标，外观切换时自动重新着色。
- Switch：M3 开关，是 QCheckBox 的子类，接口与复选框相同。
- set_appearance：「显示 ▸ 外观」菜单的入口（跟随系统、浅色、深色），连同原生标题栏、
  菜单和文件面板一起切换。

令牌（颜色、字号、形状）在 theme_tokens.py。
"""
from __future__ import annotations

import string
import sys
from pathlib import Path

from PySide6.QtCore import (QByteArray, QEasingCurve, QEvent, QPointF, QRectF, QSize, Qt,
                            QTemporaryDir, QVariantAnimation)
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath,
                           QPalette, QPaintEvent, QPen, QPixmap)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QAbstractButton, QApplication, QCheckBox, QComboBox, QGroupBox,
                               QLabel, QLineEdit, QProxyStyle, QPushButton, QSizePolicy, QStyle,
                               QStyledItemDelegate, QStyleOption, QTabBar, QWidget)

from theme_tokens import (CONTROL_HEIGHT, DISABLED_CONTAINER, DISABLED_CONTENT, SCHEMES, SHAPE,
                          STATE, SWITCH_THUMB, SWITCH_TRACK, TYPE_SCALE, blend)

# 打包后数据文件在 sys._MEIPASS（Contents/Frameworks，指向 Resources 的链接）下。
_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ICON_DIR = _BASE / "assets" / "icons"

# 当前状态：scheme 为 light 或 dark；forced 表示调用方指定了外观，不再跟随系统。
_state: dict[str, object] = {"scheme": "light", "forced": False, "connected": False}


# ---------------- 颜色 ----------------
def scheme_name() -> str:
    """当前使用的配色（light 或 dark）。"""
    return str(_state["scheme"])


def color(role: str) -> QColor:
    """当前配色里某个角色的颜色。"""
    return QColor(SCHEMES[scheme_name()][role])


def _system_scheme(app: QGuiApplication) -> str:
    """系统外观：深色返回 dark；浅色或无法判断（例如离屏平台）返回 light。"""
    return "dark" if app.styleHints().colorScheme() == Qt.ColorScheme.Dark else "light"


def _rgba(hex_color: str, alpha: float) -> str:
    """样式表用的半透明颜色。"""
    c = QColor(hex_color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {round(alpha * 255)})"


def _palette(c: dict[str, str]) -> QPalette:
    """把颜色角色映射到 QPalette：Fusion 画的框线、选区、占位文字等都从这里取色。"""
    pal = QPalette()
    groups = (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive,
              QPalette.ColorGroup.Disabled)
    R = QPalette.ColorRole
    mapping = {
        R.Window: "surface", R.WindowText: "on_surface",
        R.Base: "surface_container_lowest", R.AlternateBase: "surface_container",
        R.Text: "on_surface", R.Button: "surface_container_low", R.ButtonText: "on_surface",
        R.BrightText: "on_error", R.Highlight: "primary", R.HighlightedText: "on_primary",
        R.Link: "primary", R.LinkVisited: "tertiary", R.PlaceholderText: "on_surface_variant",
        R.ToolTipBase: "inverse_surface", R.ToolTipText: "inverse_on_surface",
        R.Light: "surface_bright", R.Midlight: "surface_container_high",
        R.Mid: "outline_variant", R.Dark: "outline", R.Shadow: "on_surface",
        R.Accent: "primary",
    }
    for group in groups:
        for role, name in mapping.items():
            pal.setColor(group, role, QColor(c[name]))
    disabled = QColor(blend(c["on_surface"], c["surface"], DISABLED_CONTENT))
    for role in (R.WindowText, R.Text, R.ButtonText, R.PlaceholderText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    return pal


# ---------------- 样式表 ----------------
def _font_rules() -> dict[str, str]:
    """各字号层级的样式表片段（font-size 和 font-weight）。"""
    return {f"type_{name}": f"font-size: {pt}pt; font-weight: {weight};"
            for name, (pt, weight) in TYPE_SCALE.items()}


_QSS = string.Template("""
QToolTip { background: $inverse_surface; color: $inverse_on_surface; border: none;
           padding: 4px 8px; border-radius: 4px; $type_body_small }

/* 文字角色 */
QLabel[m3role="title"] { color: $on_surface; $type_title_large }
QLabel[m3role="note"] { color: $on_surface_variant; $type_body_small }
QLabel[m3role="warn"] { color: $error; font-weight: 600; }
QLabel[m3role="mono"] { color: $on_surface_variant; $type_body_small }

/* 卡片：分组框画成填充卡片，层次只靠色调区分，不用阴影 */
QGroupBox { background: $surface_container_low; border: none; border-radius: ${r_medium}px;
            margin-top: 0px; padding: 22px 4px 2px 4px; $type_title_small }
/* 卡片内布局还有 Fusion 默认的 9px 边距，加上这里的内边距，实际是上 31、左右 13、下 11。 */
QGroupBox::title { subcontrol-origin: border; subcontrol-position: top left;
                   left: 16px; top: 10px; color: $on_surface; }
QFrame[m3role="banner"] { background: $surface_container_highest; border-radius: ${r_medium}px; }

/* 按钮。没有指定级别的按钮是描边按钮；对话框里的是文字按钮。
   样式表的 min/max-height 是内容高度，内边距和边框加在外面：各级别的边框宽度不同，用内边距
   补齐，保证所有按钮总高都是 ${h}px，焦点加粗边框时也不变。
   焦点：描边、文字、色调按钮是 2px 的 secondary 边框；实心按钮在边缘留出 2px 的底色
   （surface）细环——在 primary 上换成 on_primary_container 只有约 1.4:1，看不出来。
   每个级别的 :focus 规则都写明自己的内边距：通用的 QPushButton:focus 带伪类，比
   「QMessageBox QPushButton」这类选择器更具体，不写就会套用描边按钮的 23px，对话框里
   有焦点的按钮文字会被挤没（2026-11-09 实机发现）。 */
QPushButton { min-height: ${btn_h}px; max-height: ${btn_h}px; padding: 1px 24px;
              border-radius: ${r_full}px; border: 1px solid $outline;
              background: transparent; color: $primary; $type_label_large }
QPushButton:hover { background: $primary_hover; }
QPushButton:pressed { background: $primary_pressed; }
QPushButton:focus { border: 2px solid $secondary; padding: 0px 23px; }
QPushButton:disabled { border-color: $disabled_container; color: $disabled_content; }
QPushButton[m3button="filled"] { background: $primary; color: $on_primary;
                                 border: 2px solid transparent; padding: 0px 22px; }
QPushButton[m3button="filled"]:hover { background: $filled_hover; }
QPushButton[m3button="filled"]:pressed { background: $filled_pressed; }
QPushButton[m3button="filled"]:focus { border-color: $surface; padding: 0px 22px; }
QPushButton[m3button="filled"]:disabled { background: $disabled_container;
                                          color: $disabled_content; }
QPushButton[m3button="tonal"] { background: $secondary_container; color: $on_secondary_container;
                                border: 2px solid transparent; padding: 0px 22px; }
QPushButton[m3button="tonal"]:hover { background: $tonal_hover; }
QPushButton[m3button="tonal"]:pressed { background: $tonal_pressed; }
QPushButton[m3button="tonal"]:focus { border-color: $secondary; padding: 0px 22px; }
QPushButton[m3button="tonal"]:disabled { background: $disabled_container;
                                         color: $disabled_content; }
QPushButton[m3button="text"], QMessageBox QPushButton, QDialogButtonBox QPushButton {
    border: 2px solid transparent; padding: 0px 10px; background: transparent; color: $primary; }
QPushButton[m3button="text"]:hover, QMessageBox QPushButton:hover,
QDialogButtonBox QPushButton:hover { background: $primary_hover; }
QPushButton[m3button="text"]:pressed, QMessageBox QPushButton:pressed,
QDialogButtonBox QPushButton:pressed { background: $primary_pressed; }
QPushButton[m3button="text"]:focus, QMessageBox QPushButton:focus,
QDialogButtonBox QPushButton:focus { border-color: $secondary; padding: 0px 10px; }
QPushButton[m3button="text"]:disabled, QMessageBox QPushButton:disabled,
QDialogButtonBox QPushButton:disabled { background: transparent; color: $disabled_content; }

/* 描边文本框与下拉框（M3 的 outlined text field / exposed dropdown menu）。焦点时边框加粗到
   2px，内边距相应减 1，总尺寸不变。 */
QLineEdit, QComboBox { min-height: ${field_h}px; max-height: ${field_h}px; padding: 1px 12px;
                       border: 1px solid $outline; border-radius: ${r_extra_small}px;
                       background: transparent; color: $on_surface;
                       selection-background-color: $primary; selection-color: $on_primary;
                       $type_body_medium }
QLineEdit:hover, QComboBox:hover { border-color: $on_surface; }
QLineEdit:focus, QComboBox:focus, QComboBox:on { border: 2px solid $primary; padding: 0px 11px; }
QLineEdit[m3error="true"] { border-color: $error; }
QLineEdit[m3error="true"]:focus { border: 2px solid $error; padding: 0px 11px; }
QLineEdit:disabled, QComboBox:disabled { border-color: $disabled_container;
                                         color: $disabled_content; }
QComboBox::drop-down { border: none; width: 28px; subcontrol-origin: padding;
                       subcontrol-position: center right; }
QComboBox::down-arrow { image: url("$arrow"); width: 24px; height: 24px; }
QComboBox::down-arrow:disabled { image: url("$arrow_disabled"); }
/* 下拉菜单：键盘或鼠标所在的条目用 secondary_container（M3 菜单的选中色），当前值另由
   _ComboDelegate 画对勾。 */
QComboBox QAbstractItemView { background: $surface_container; color: $on_surface;
                              border: none; outline: 0px; padding: 4px 0px;
                              selection-background-color: $secondary_container;
                              selection-color: $on_secondary_container; }
QComboBox QAbstractItemView::item { min-height: 32px; padding: 0px 12px; }

/* 日志 */
QPlainTextEdit { background: $surface_container_lowest; color: $on_surface;
                 border: 1px solid $outline_variant; border-radius: ${r_small}px; padding: 4px;
                 selection-background-color: $primary; selection-color: $on_primary;
                 $type_body_small }
QPlainTextEdit:focus { border: 2px solid $primary; padding: 3px; }

/* 分段按钮：模式切换的 QTabBar */
QTabBar::tab { min-height: ${seg_h}px; max-height: ${seg_h}px; padding: 0px 20px;
               border: 1px solid $outline; background: transparent; color: $on_surface;
               $type_label_large }
QTabBar::tab:first { border-top-left-radius: ${r_full}px; border-bottom-left-radius: ${r_full}px; }
QTabBar::tab:last { border-top-right-radius: ${r_full}px; border-bottom-right-radius: ${r_full}px;
                    border-left: none; }
QTabBar::tab:hover { background: $on_surface_hover; }
QTabBar::tab:selected { background: $secondary_container; color: $on_secondary_container; }
QTabBar::tab:disabled { color: $disabled_content; border-color: $disabled_container; }
QTabBar::tab:selected:disabled { background: $disabled_container; }

/* 滑块（2021 版：4px 轨道，20px 圆形手柄） */
QSlider { min-height: 24px; }
QSlider::groove:horizontal { height: 4px; border-radius: 2px;
                             background: $surface_container_highest; }
QSlider::sub-page:horizontal { height: 4px; border-radius: 2px; background: $primary; }
QSlider::add-page:horizontal { height: 4px; border-radius: 2px;
                               background: $surface_container_highest; }
QSlider::handle:horizontal { width: 20px; height: 20px; margin: -8px 0px; border-radius: 10px;
                             background: $primary; }
QSlider::handle:horizontal:hover { background: $filled_hover; }
/* 键盘焦点：与实心按钮一样在手柄边缘留出底色细环（尺寸不变） */
QSlider::handle:horizontal:focus { border: 2px solid $surface; width: 16px; height: 16px; }
QSlider::sub-page:horizontal:disabled, QSlider::handle:horizontal:disabled {
    background: $disabled_content; }
QSlider::add-page:horizontal:disabled, QSlider::groove:horizontal:disabled {
    background: $disabled_container; }

/* 线性进度条（不确定进度） */
QProgressBar { max-height: 4px; min-height: 4px; border: none; border-radius: 2px;
               background: $surface_container_highest; text-align: center;
               color: transparent; }
QProgressBar::chunk { background: $primary; border-radius: 2px; }

/* 复选框：指示器由 _M3Style 绘制，这里只定间距 */
QCheckBox { spacing: 10px; color: $on_surface; }
QCheckBox:disabled { color: $disabled_content; }

/* 滚动条 */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle { background: $outline_variant; border-radius: 3px; }
QScrollBar::handle:vertical { min-height: 24px; }
QScrollBar::handle:horizontal { min-width: 24px; }
QScrollBar::handle:hover { background: $outline; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

/* 对话框容器 */
QMessageBox, QDialog[m3role="dialog"] { background: $surface_container_high; }
""")


def _qss(c: dict[str, str]) -> str:
    """用当前配色填好样式表模板。状态层：透明底的按钮用半透明色叠在下层上，
    实心底的按钮把状态层预先混合进底色。"""
    values = dict(c)
    values.update(_font_rules())
    values.update(
        # 内容高度：总高减去边框和内边距（按钮共 4px，文本框和分段按钮共 2px＋2px 内边距）。
        h=CONTROL_HEIGHT, btn_h=CONTROL_HEIGHT - 4, seg_h=CONTROL_HEIGHT - 2,
        field_h=CONTROL_HEIGHT - 4,
        r_full=CONTROL_HEIGHT // 2,
        **{f"r_{k}": v for k, v in SHAPE.items()},
        primary_hover=_rgba(c["primary"], STATE["hover"]),
        primary_pressed=_rgba(c["primary"], STATE["pressed"]),
        on_surface_hover=_rgba(c["on_surface"], STATE["hover"]),
        filled_hover=blend(c["on_primary"], c["primary"], STATE["hover"]),
        filled_pressed=blend(c["on_primary"], c["primary"], STATE["pressed"]),
        tonal_hover=blend(c["on_secondary_container"], c["secondary_container"], STATE["hover"]),
        tonal_pressed=blend(c["on_secondary_container"], c["secondary_container"],
                            STATE["pressed"]),
        disabled_content=_rgba(c["on_surface"], DISABLED_CONTENT),
        disabled_container=_rgba(c["on_surface"], DISABLED_CONTAINER),
        arrow=_tinted_svg("arrow_drop_down", c["on_surface_variant"]),
        arrow_disabled=_tinted_svg("arrow_drop_down",
                                   blend(c["on_surface"], c["surface"], DISABLED_CONTENT)),
    )
    return _QSS.substitute(values)


def _tinted_svg(name: str, fill: str) -> str:
    """把着色后的 SVG 写进临时目录并返回路径。样式表的 image 只能引用文件，图标颜色又要
    随外观变化，所以每种颜色写一份；目录在应用退出时由 QTemporaryDir 删除。"""
    tmp = _state.get("tmpdir")
    # 长时间运行时系统可能清理临时目录；目录没了就重建，否则切换外观时写文件失败。
    if tmp is None or not tmp.isValid() or not Path(tmp.path()).is_dir():
        tmp = QTemporaryDir()
        _state["tmpdir"] = tmp
    path = Path(tmp.path()) / f"{name}-{fill.lstrip('#').lower()}.svg"
    if not path.exists():
        data = (ICON_DIR / f"{name}.svg").read_text(encoding="utf-8")
        path.write_text(data.replace("<svg ", f'<svg fill="{fill}" ', 1), encoding="utf-8")
    return path.as_posix()


# ---------------- 样式：样式表画不了的指示器 ----------------
class _M3Style(QProxyStyle):
    """以 Fusion 为底：自己画复选框的指示器和焦点环，按钮不因鼠标点按取得焦点，下拉框换成
    会标出当前值的条目委托；其余交给样式表和 Fusion。

    样式表一旦定制了复选框的指示器，就必须提供图片才会画勾；用代码画可以直接用当前配色，
    不需要为浅色、深色各生成一套图片文件。
    """
    def __init__(self) -> None:
        super().__init__("Fusion")

    def pixelMetric(self, metric: QStyle.PixelMetric, option: QStyleOption | None = None,
                    widget: QWidget | None = None) -> int:
        if metric in (QStyle.PixelMetric.PM_IndicatorWidth,
                      QStyle.PixelMetric.PM_IndicatorHeight):
            return 18
        return super().pixelMetric(metric, option, widget)

    def styleHint(self, hint: QStyle.StyleHint, option: QStyleOption | None = None,
                  widget: QWidget | None = None, data=None) -> int:  # type: ignore[override]
        # 与 macOS 原生样式一样，按钮（含复选框和开关）只能用 Tab 取得焦点。Fusion 给的是
        # StrongFocus：点按按钮会让它留着焦点描边，还会让路径框失去焦点、提交写了一半的草稿。
        if hint == QStyle.StyleHint.SH_Button_FocusPolicy:
            return int(Qt.FocusPolicy.TabFocus.value)
        return super().styleHint(hint, option, widget, data)

    def drawPrimitive(self, element: QStyle.PrimitiveElement, option: QStyleOption,
                      painter: QPainter, widget: QWidget | None = None) -> None:
        if element == QStyle.PrimitiveElement.PE_IndicatorCheckBox:
            _draw_checkbox(option, painter)
        elif element == QStyle.PrimitiveElement.PE_FrameFocusRect:
            # 按钮、文本框、下拉框的焦点由样式表的边框表示；复选框、分段按钮等没有对应的
            # 样式表规则，在这里画 M3 的焦点环（2px secondary）。卡片（分组框）不是可操作的
            # 控件，Qt 在别无选择时把焦点交给它（例如叠放控件换页），也不画环。
            if not isinstance(widget, (QPushButton, QLineEdit, QComboBox, QGroupBox)):
                _draw_focus_ring(option, painter)
        else:
            super().drawPrimitive(element, option, painter, widget)

    def polish(self, arg):  # type: ignore[override]
        # 下拉框默认的条目委托不认样式表的 ::item 规则；换成 _ComboDelegate 后条目才有
        # M3 菜单的高度和内边距，当前值前面也有对勾。
        if isinstance(arg, QComboBox) and not isinstance(arg.itemDelegate(), _ComboDelegate):
            arg.setItemDelegate(_ComboDelegate(arg))
        return super().polish(arg)


class _ComboDelegate(QStyledItemDelegate):
    """下拉菜单的条目：在当前值的末尾画一个对勾。键盘移动的高亮只表示“将要选的”，
    不画对勾就看不出现在选的是哪一个。"""
    def __init__(self, combo: QComboBox) -> None:
        super().__init__(combo)
        self._combo = combo

    def paint(self, painter: QPainter, option, index) -> None:  # type: ignore[override]
        super().paint(painter, option, index)
        if index.row() != self._combo.currentIndex():
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        size = 18
        r = option.rect
        target = QRectF(r.right() - size - 12, r.center().y() - size / 2 + 1, size, size)
        role = "on_secondary_container" if selected else "primary"
        painter.drawPixmap(target.toRect(), icon("check", role, size).pixmap(size, size))


def _draw_focus_ring(option: QStyleOption, painter: QPainter) -> None:
    """2px 的 secondary 圆角焦点环，画在控件给出的焦点区域边上。"""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(color("secondary"), 2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(option.rect).adjusted(1, 1, -1, -1), 4, 4)
    painter.restore()


def _draw_checkbox(option: QStyleOption, painter: QPainter) -> None:
    """M3 复选框：18px、2px 圆角；未选是 2px 描边，选中是 primary 填充加白勾。"""
    enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
    on = bool(option.state & QStyle.StateFlag.State_On)
    rect = QRectF(option.rect)
    rect.setSize(QSize(18, 18).toSizeF())  # 指示器区域就是 18px（见 pixelMetric）
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    disabled = _alpha(color("on_surface"), DISABLED_CONTENT)
    if on:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color("primary") if enabled else disabled)
        painter.drawRoundedRect(rect, 2, 2)
        pen = QPen(color("on_primary") if enabled else color("surface"), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        w, h, x, y = rect.width(), rect.height(), rect.x(), rect.y()
        path = QPainterPath(QPointF(x + w * 0.22, y + h * 0.52))
        path.lineTo(x + w * 0.42, y + h * 0.72)
        path.lineTo(x + w * 0.78, y + h * 0.30)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
    else:
        painter.setPen(QPen(color("on_surface_variant") if enabled else disabled, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 2, 2)
    painter.restore()


def _alpha(c: QColor, alpha: float) -> QColor:
    c = QColor(c)
    c.setAlphaF(alpha)
    return c


# ---------------- 角色与图标 ----------------
def _repolish(w: QWidget) -> None:
    """属性变化后重新套用样式表规则。"""
    style = w.style()
    style.unpolish(w)
    style.polish(w)
    w.update()


def set_role(w: QWidget, role: str | None) -> None:
    """标记控件的文字或容器角色：note（说明）、warn（警告）、title（标题）、banner（横幅容器）、
    dialog（对话框容器）。"""
    w.setProperty("m3role", role)
    _repolish(w)


def set_button(b: QAbstractButton, kind: str) -> None:
    """按钮级别：filled（每个界面最多一个主操作）、tonal、outlined（默认）、text。"""
    b.setProperty("m3button", kind)
    _repolish(b)
    if b.property("m3icon"):
        _refresh_icon(b)


def set_error(w: QWidget, error: bool) -> None:
    """文本框的错误状态（红色描边）；状态没变时不重新套用样式。"""
    if bool(w.property("m3error")) != error:
        w.setProperty("m3error", error)
        _repolish(w)


# 按钮级别对应的图标颜色角色。
_ICON_ROLES = {"filled": "on_primary", "tonal": "on_secondary_container"}


def icon(name: str, role: str, size: int = 18) -> QIcon:
    """按当前配色着色的 Material Symbols 图标；停用状态用 38% 的 on_surface。"""
    disabled = blend(SCHEMES[scheme_name()]["on_surface"],
                     SCHEMES[scheme_name()]["surface"], DISABLED_CONTENT)
    ic = QIcon()
    ic.addPixmap(_render_svg(name, SCHEMES[scheme_name()][role], size), QIcon.Mode.Normal)
    ic.addPixmap(_render_svg(name, disabled, size), QIcon.Mode.Disabled)
    return ic


def _render_svg(name: str, fill: str, size: int) -> QPixmap:
    """把 SVG 按 fill 着色后渲染成 2 倍分辨率的位图。"""
    data = (ICON_DIR / f"{name}.svg").read_text(encoding="utf-8")
    data = data.replace("<svg ", f'<svg fill="{fill}" ', 1)
    ratio = 2.0
    pm = QPixmap(QSize(round(size * ratio), round(size * ratio)))
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    QSvgRenderer(QByteArray(data.encode("utf-8"))).render(painter)
    painter.end()
    pm.setDevicePixelRatio(ratio)
    return pm


def set_icon(b: QAbstractButton, name: str | None) -> None:
    """给按钮加图标，颜色随按钮级别和外观变化；name 为 None 时去掉图标。"""
    b.setProperty("m3icon", name)
    _refresh_icon(b)


def set_label_icon(lbl: QLabel, name: str, role: str, size: int = 20) -> None:
    """在标签里显示一个按 role 着色的图标（例如横幅的警告图标），外观切换时自动重新着色。"""
    lbl.setProperty("m3icon", name)
    lbl.setProperty("m3iconrole", role)
    lbl.setFixedSize(size, size)
    _refresh_icon(lbl)


def _refresh_icon(w: QWidget) -> None:
    name = w.property("m3icon")
    if isinstance(w, QLabel):
        size = w.width()
        w.setPixmap(icon(name, w.property("m3iconrole"), size).pixmap(size, size))
        return
    if not name:
        w.setIcon(QIcon())
        return
    w.setIcon(icon(name, _ICON_ROLES.get(w.property("m3button"), "primary")))
    w.setIconSize(QSize(18, 18))


class SegmentedTabBar(QTabBar):
    """M3 分段按钮：每一段一样宽（取最宽的一段），与 M3 的按钮组一致。选中的一段只用
    secondary_container 底色区分，不加对勾（用户决定，2026-11-09）。仍是 QTabBar，
    currentIndex 等接口不变。"""
    def tabSizeHint(self, index: int) -> QSize:
        widest = max(QTabBar.tabSizeHint(self, i).width() for i in range(self.count()))
        return QSize(widest, QTabBar.tabSizeHint(self, index).height())

    def minimumTabSizeHint(self, index: int) -> QSize:
        return self.tabSizeHint(index)


# ---------------- 开关 ----------------
class Switch(QCheckBox):
    """M3 开关：52×32 的轨道，关闭时 16px 的描边小圆点，打开时 24px 的实心圆点并带对勾；
    按住时圆点变成 28px。文字在开关右边，点按开关或文字都能切换。

    是 QCheckBox 的子类，setChecked、isChecked、toggled 等接口不变，所以调用方和夹具
    不用区分开关和复选框。宽度固定为开关加文字：放进网格后不会被拉宽，旁边的空白处
    点按不会切换。
    """
    _GAP = 12

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._pos = 0.0  # 圆点位置：0 在左（关），1 在右（开）
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        easing = QEasingCurve(QEasingCurve.Type.BezierSpline)
        # M3 的标准缓动 cubic-bezier(0.2, 0, 0, 1)。
        easing.addCubicBezierSegment(QPointF(0.2, 0.0), QPointF(0.0, 1.0), QPointF(1.0, 1.0))
        self._anim.setEasingCurve(easing)
        self._anim.valueChanged.connect(self._on_anim)
        self.toggled.connect(self._animate)

    def _on_anim(self, value: float) -> None:
        self._pos = float(value)
        self.update()

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        if not self.isVisible():
            self._pos = 1.0 if on else 0.0
            return
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def showEvent(self, e: QEvent) -> None:  # type: ignore[override]
        super().showEvent(e)
        self._pos = 1.0 if self.isChecked() else 0.0

    def sizeHint(self) -> QSize:
        tw, th = SWITCH_TRACK
        text_w = self.fontMetrics().horizontalAdvance(self.text())
        return QSize(tw + (self._GAP + text_w if self.text() else 0) + 2,
                     max(th, self.fontMetrics().height()) + 2)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def hitButton(self, pos) -> bool:  # type: ignore[override]
        return self.rect().contains(pos)

    def paintEvent(self, _e: QPaintEvent) -> None:
        tw, th = SWITCH_TRACK
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()
        on = self.isChecked()
        track = QRectF(1, (self.height() - th) / 2, tw, th)
        radius = th / 2
        dim = lambda role, a=DISABLED_CONTENT: _alpha(color(role), a)  # noqa: E731
        # 轨道
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color("primary") if enabled else dim("on_surface", DISABLED_CONTAINER))
        else:
            p.setPen(QPen(color("outline") if enabled else dim("on_surface", DISABLED_CONTAINER),
                          2))
            p.setBrush(color("surface_container_highest") if enabled
                       else _alpha(color("surface_container_highest"), DISABLED_CONTAINER))
        p.drawRoundedRect(track.adjusted(1, 1, -1, -1) if not on else track, radius, radius)
        # 圆点：大小和位置随动画在关、开之间过渡；按住时放大。
        pressed = self.isDown()
        size_off, size_on = SWITCH_THUMB["off"], SWITCH_THUMB["on"]
        size = SWITCH_THUMB["pressed"] if pressed else size_off + (size_on - size_off) * self._pos
        cx = track.left() + radius + (tw - 2 * radius) * self._pos
        cy = track.center().y()
        # 悬停或有键盘焦点（按钮只能用 Tab 取得焦点，见 _M3Style.styleHint）时，圆点外加一圈
        # 状态层。M3 是 40px，这里取控件高度（34px），否则上下左右会被控件边缘切平。
        if enabled and (self.underMouse() or self.hasFocus()):
            layer = color("primary") if on else color("on_surface")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_alpha(layer, STATE["focus"] if self.hasFocus() else STATE["hover"]))
            halo = min(20.0, self.height() / 2, cx)
            p.drawEllipse(QPointF(cx, cy), halo, halo)
        if enabled and self.hasFocus():
            # 状态层只有 12% 的不透明度，键盘焦点另外用 2px 的 secondary 环围住轨道。
            p.setPen(QPen(color("secondary"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(track.adjusted(-0.5, 0, 0.5, 0), radius, radius)
        if on:
            thumb = color("on_primary") if enabled else color("surface")
        else:
            thumb = color("outline") if enabled else dim("on_surface")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(thumb)
        p.drawEllipse(QPointF(cx, cy), size / 2, size / 2)
        if on and self._pos > 0.5:
            # 打开时圆点里的对勾（16px），颜色取轨道色。
            pen = QPen(color("on_primary_container") if enabled else dim("on_surface"), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            path = QPainterPath(QPointF(cx - 5, cy))
            path.lineTo(cx - 1.5, cy + 3.5)
            path.lineTo(cx + 5, cy - 3.5)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        # 文字
        if self.text():
            p.setPen(self.palette().color(QPalette.ColorGroup.Normal if enabled
                                          else QPalette.ColorGroup.Disabled,
                                          QPalette.ColorRole.WindowText))
            x = int(track.right()) + self._GAP
            p.drawText(x, 0, self.width() - x, self.height(),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       self.text())
        p.end()


# ---------------- 入口 ----------------
def apply(app: QApplication, scheme: str | None = None) -> None:
    """给整个应用套用 M3 主题。

    scheme 为 None 时跟随系统外观，并在系统切换浅色/深色时自动重新应用；传 light 或 dark
    时固定使用该配色（夹具用它检查两种外观）。
    """
    # 设置了样式表后 app.style() 返回的是 Qt 的样式表包装，不是这里的样式，所以记下装过的。
    if _state.get("style") is None:
        _state["style"] = _M3Style()
        app.setStyle(_state["style"])
    name = scheme or _system_scheme(app)
    c = SCHEMES[name]
    # 先生成样式表（要写着色的箭头文件）：万一失败，整个界面仍停在原来的配色上，
    # 不会出现新调色板配旧样式表的混合状态。
    qss = _qss(c)
    _state["forced"] = scheme is not None
    _state["scheme"] = name
    app.setPalette(_palette(c))
    font = QFont(app.font())
    font.setPointSize(TYPE_SCALE["body_medium"][0])
    app.setFont(font)
    app.setStyleSheet(qss)
    for w in app.allWidgets():
        if w.property("m3icon"):
            _refresh_icon(w)
    if not _state["connected"]:
        _state["connected"] = True
        app.styleHints().colorSchemeChanged.connect(lambda _s: _follow_system(app))


def _follow_system(app: QApplication) -> None:
    if not _state["forced"]:
        apply(app)


# 外观菜单的三个选项（偏好键 appearance 的取值），第一项是默认值。
APPEARANCES = ("system", "light", "dark")


def set_appearance(app: QApplication, mode: str) -> None:
    """按「显示 ▸ 外观」的选择切换配色：system 跟随系统，light/dark 固定。

    同时通过 QStyleHints 请求应用级外观（macOS 上即 NSApp.appearance），原生标题栏、
    全局菜单和文件面板随之变成同一种外观；只换 Qt 的配色时它们仍跟随系统，与窗口内容不一致。
    """
    hints = app.styleHints()
    if mode in ("light", "dark"):
        # 先固定配色再请求外观：请求触发的 colorSchemeChanged 到达时已不再跟随系统。
        apply(app, mode)
        hints.setColorScheme(Qt.ColorScheme.Light if mode == "light" else Qt.ColorScheme.Dark)
        return
    # 先撤销请求再按系统外观重画。macOS 可能稍后才报告新的系统外观，那时的
    # colorSchemeChanged 会经 _follow_system 再应用一次。
    hints.unsetColorScheme()
    apply(app)
