"""Material 3 主题的离屏 GUI 夹具检查：浅色和深色、四种语言、压缩和解压两种模式。

检查主题确实套用（应用实际使用的样式、调色板、样式表）、控件带上了角色、换外观时已有
控件的图标重新着色、跟随系统切换、控件高度一致、开关和分段按钮的行为、键盘焦点看得见、
下拉菜单标出当前值、标题与无障碍名称、启动焦点、宽度和高度预算、切换模式时窗口大小
不变且两种模式的第二张卡片一样高、「显示 ▸ 外观」菜单。

字号按 72 dpi 计算（QT_FONT_DPI=72，与 macOS 的逻辑 dpi 一致；离屏平台默认 96 dpi，
同样的 13pt 会高出约三分之一）。离屏字体仍不是系统字体，几何结果只是近似，最终外观要在
真实窗口里看。设置环境变量 CLEANPACK_THEME_SHOTS=<文件夹> 时把截图存进去供人工查看。
不写用户偏好，数据只放 /tmp。用法：python3 -B tests/gui/gui_theme_check.py
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

WT = Path(__file__).resolve().parents[2]  # 仓库根目录（tests/gui 的上两级）
sys.path.insert(0, str(WT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_FONT_DPI"] = "72"

from PySide6.QtCore import QModelIndex, QPoint, QRect, QSettings, QSize, Qt  # noqa: E402
from PySide6.QtGui import QAccessible, QColor, QImage, QPainter, QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QLabel,  # noqa: E402
                               QLineEdit, QPushButton, QStyle, QStyleOptionFocusRect,
                               QStyleOptionFrame, QStyleOptionViewItem, QWidget)

import core  # noqa: E402
import main  # noqa: E402
import theme  # noqa: E402
from i18n import LANGS, set_lang, tr  # noqa: E402
from theme_tokens import CONTROL_HEIGHT, SCHEMES  # noqa: E402

# 13 英寸 MacBook 的可用高度约 900 像素，减去菜单栏、标题栏和程序坞后更少，窗口内容要留余量。
MAX_HEIGHT = 800
WIDTH = 780          # main.main() 的初始宽度
TOOLS_WIDTH = 760    # 工具链窗口的初始宽度

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录并立即打印结果：进程中途崩溃时，已经得出的结果不会丢失。"""
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(60)}  {detail}", flush=True)


def settle() -> None:
    for _ in range(3):
        app.processEvents()


def first_opaque(img: QImage, rect: QRect | None = None) -> QColor | None:
    """图像（或其中 rect 范围）里最不透明的像素的颜色（不计透明度）。细线图标缩小后边缘
    是半透明的，可能没有完全不透明的像素，所以取最不透明的那个。"""
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    r = rect or img.rect()
    best: QColor | None = None
    for y in range(r.top(), r.bottom() + 1):
        for x in range(r.left(), r.right() + 1):
            c = img.pixelColor(x, y)
            if c.alpha() > 0 and (best is None or c.alpha() > best.alpha()):
                best = c
    if best is None or best.alpha() < 128:
        return None
    best.setAlpha(255)
    return best


def icon_color(icon, size: int = 18) -> str:
    c = first_opaque(icon.pixmap(size, size).toImage())
    return c.name().upper() if c else "none"


def near(a: str, b: str, tol: int = 6) -> bool:
    """两个颜色是否只差抗锯齿的取整误差（半透明像素换算回不透明色时会差 1–2）。"""
    if "none" in (a, b):
        return a == b
    ca, cb = QColor(a), QColor(b)
    return (abs(ca.red() - cb.red()) + abs(ca.green() - cb.green())
            + abs(ca.blue() - cb.blue())) <= tol


def has_color(img: QImage, rect: QRect, hex_color: str, tol: int = 24) -> bool:
    """rect 范围内是否有接近 hex_color 的像素（用于找背景上画的标记）。"""
    want = QColor(hex_color)
    for y in range(rect.top(), rect.bottom() + 1):
        for x in range(rect.left(), rect.right() + 1):
            c = img.pixelColor(x, y)
            if (abs(c.red() - want.red()) + abs(c.green() - want.green())
                    + abs(c.blue() - want.blue())) <= tol:
                return True
    return False


def changed_pixels(a: QImage, b: QImage) -> int:
    return sum(a.pixel(x, y) != b.pixel(x, y)
               for y in range(min(a.height(), b.height()))
               for x in range(min(a.width(), b.width())))


def needed_height(win) -> int:
    lay = win.layout()
    lay.activate()
    return lay.totalMinimumHeightForWidth(win.width())


app = QApplication([])
main.brew_notice = lambda parent, tools: None
tmp = Path(tempfile.mkdtemp(prefix="cleanpack-guitheme-", dir="/tmp"))


def cleanup() -> None:
    for p in tmp.rglob("*"):
        if p.is_dir():
            p.chmod(0o755)
    shutil.rmtree(tmp, ignore_errors=True)


atexit.register(cleanup)
shots = os.environ.get("CLEANPACK_THEME_SHOTS")
src = tmp / "src"
src.mkdir()
(src / "a.txt").write_text("x")
ro = tmp / "ro"            # 只读文件夹：其中的源默认输出到这里，输出框会提示没有权限
(ro / "src").mkdir(parents=True)
(ro / "src" / "a.txt").write_text("x")
ro.chmod(0o555)
archive = tmp / "a.zip"
archive.write_bytes(b"PK\x05\x06" + b"\0" * 18)  # 空 zip（只有中央目录结尾）


def new_window(name: str) -> main.MainWindow:
    settings = QSettings(str(tmp / f"{name}.ini"), QSettings.Format.IniFormat)
    settings.setValue("format", "zip")
    w = main.MainWindow(settings)
    w.show()
    w.resize(WIDTH, 640)
    settle()
    return w


def close(w: main.MainWindow) -> None:
    w._quit_ready = True
    w.close()
    settle()


# ---------------- 样式与跟随系统 ----------------
theme.apply(app)
check("offscreen without a system scheme falls back to light", theme.scheme_name() == "light",
      theme.scheme_name())
style = app.style()  # 有样式表时是 Qt 的包装，查询会转给实际装上的样式
check("application style is the M3 proxy (18px checkbox indicator)",
      style.pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth) == 18,
      str(style.pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth)))
check("buttons take focus only by Tab, like the native macOS style",
      style.styleHint(QStyle.StyleHint.SH_Button_FocusPolicy)
      == int(Qt.FocusPolicy.TabFocus.value)
      and QPushButton("x").focusPolicy() == Qt.FocusPolicy.TabFocus)
first_style = theme._state.get("style")
theme.apply(app)
check("applying again keeps the same style", theme._state.get("style") is first_style)

w = new_window("follow")
original = theme._system_scheme
theme._system_scheme = lambda _app: "dark"
app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
settle()
check("follows the system switching to dark", theme.scheme_name() == "dark"
      and app.palette().color(QPalette.ColorRole.Window).name().upper()
      == SCHEMES["dark"]["surface"], theme.scheme_name())
recolored = [icon_color(w.btn_go.icon()), icon_color(w.btn_tools.icon())]
expected = [SCHEMES["dark"]["on_primary"], SCHEMES["dark"]["on_secondary_container"]]
check("existing window's icons are recolored on the switch",
      all(near(a, b) for a, b in zip(recolored, expected)), repr(recolored))
tool_px = first_opaque(w.tools_dialog.tool_icons["tar"].pixmap().toImage())
check("toolchain status icons are recolored on the switch",
      tool_px is not None and near(tool_px.name().upper(), SCHEMES["dark"]["primary"]),
      tool_px.name() if tool_px else "none")
banner_px = first_opaque(w.banner_icon.pixmap().toImage())
check("banner icon is recolored on the switch",
      banner_px is not None and banner_px.name().upper() == SCHEMES["dark"]["error"],
      banner_px.name() if banner_px else "none")
theme.apply(app, "light")
app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
settle()
check("a forced scheme ignores the system switch", theme.scheme_name() == "light")
theme._system_scheme = original
close(w)

for scheme in ("light", "dark"):
    theme.apply(app, scheme)
    c = SCHEMES[scheme]
    qss = app.styleSheet()
    check(f"{scheme}: style sheet fully substituted", bool(qss) and "$" not in qss)
    arrows = [Path(p) for p in qss.split('"') if p.endswith(".svg")]
    check(f"{scheme}: tinted arrow files exist", bool(arrows) and all(p.is_file() for p in arrows),
          repr(arrows))

    w = new_window(scheme)
    w.set_source(str(src))
    settle()
    e = w.encrypt_panel

    # ---------- 角色 ----------
    roles = {"title": w.title_lbl, "note": w.level_note, "warn": w.src_input.hint,
             "banner": w.banner}
    check(f"{scheme}: text and container roles",
          all(wid.property("m3role") == role for role, wid in roles.items())
          and w.excl_lbl.property("m3role") == "note"
          and e.note.property("m3role") == "note" and e.hint.property("m3role") == "warn")
    check(f"{scheme}: button levels",
          w.btn_go.property("m3button") == "filled" and w.btn_tools.property("m3button") == "tonal"
          and w.banner_btn.property("m3button") == "text"
          and w.tools_dialog.btn_install.property("m3button") == "filled")
    check(f"{scheme}: switches and the remaining checkbox",
          all(isinstance(s, theme.Switch) for s in (e.encrypt_chk, w.extreme_chk, e.names_chk))
          and type(e.show_chk) is QCheckBox)
    check(f"{scheme}: filled button icon uses on_primary",
          near(icon_color(w.btn_go.icon()), c["on_primary"]), icon_color(w.btn_go.icon()))

    # ---------- 工具链窗口的状态：M3 图标加文字，不用 Emoji ----------
    d = w.tools_dialog
    saved = dict(w.tools.paths)
    w.tools.paths["zstd"] = None
    d.update_state()
    status = {}
    for key, (lbl, _used, _path) in d.tool_rows.items():
        px = first_opaque(d.tool_icons[key].pixmap().toImage())
        status[key] = (px.name().upper() if px else "none", lbl.text())
    expect_ok = {k: v for k, v in status.items() if w.tools.paths.get(k)}
    check(f"{scheme}: toolchain status uses colored icons and no emoji",
          all(not any(ord(ch) >= 0x2600 for ch in text) for _c, text in status.values())
          and near(status["zstd"][0], c["error"])
          and all(near(col, c["primary"]) for col, _t in expect_ok.values())
          and status["zstd"][1] == tr("tool_missing"), repr(status))
    w.tools.paths.clear()
    w.tools.paths.update(saved)
    d.update_state()

    # ---------- 控件高度一致（含焦点状态） ----------
    e.encrypt_chk.setChecked(True)
    settle()
    controls = [x for x in w.findChildren(QWidget)
                if isinstance(x, (QPushButton, QLineEdit, QComboBox)) and x.isVisibleTo(w)]
    heights = {f"{type(x).__name__}:{x.property('m3button')}": x.height() for x in controls}
    w.btn_folder.setFocus(Qt.FocusReason.TabFocusReason)
    settle()
    heights["focused outlined"] = w.btn_folder.height()
    w.src_input.setFocus(Qt.FocusReason.TabFocusReason)
    settle()
    heights["focused field"] = w.src_input.height()
    check(f"{scheme}: buttons and fields are all {CONTROL_HEIGHT}px",
          set(heights.values()) == {CONTROL_HEIGHT}, repr(heights))
    e.encrypt_chk.setChecked(False)
    settle()

    # ---------- 键盘焦点看得见 ----------
    def focus_diff(widget) -> int:
        w.btn_go.setFocus(Qt.FocusReason.TabFocusReason)
        settle()
        before = widget.grab().toImage()
        widget.setFocus(Qt.FocusReason.TabFocusReason)
        settle()
        return changed_pixels(before, widget.grab().toImage())

    e.encrypt_chk.setChecked(True)
    settle()
    diffs = {"show_chk": focus_diff(e.show_chk), "mode_tabs": focus_diff(w.mode_tabs),
             "slider": focus_diff(w.level_slider), "switch": focus_diff(e.encrypt_chk),
             "log": focus_diff(w.log_view)}
    check(f"{scheme}: keyboard focus is visible on checkbox, tabs, slider, switch, log",
          all(n > 20 for n in diffs.values()), repr(diffs))
    e.encrypt_chk.setChecked(False)
    settle()

    # ---------- 分段按钮：不加对勾，选中的一段只用底色区分（用户决定） ----------
    rects = []
    for index in (1, 0):
        w.mode_tabs.setCurrentIndex(index)
        settle()
        rects.append([w.mode_tabs.tabRect(i) for i in range(w.mode_tabs.count())])
        img = w.mode_tabs.grab().toImage()
        fills = []
        for i in range(w.mode_tabs.count()):
            r = w.mode_tabs.tabRect(i)
            # 段内靠边的一点：避开文字和圆角描边。
            fills.append(img.pixelColor(int((r.left() + 14) * img.devicePixelRatio()),
                                        int(r.center().y() * img.devicePixelRatio())).name().upper())
        check(f"{scheme}: segments have no icon; only the selected one is tinted ({index})",
              all(w.mode_tabs.tabIcon(i).isNull() for i in range(w.mode_tabs.count()))
              and near(fills[index], c["secondary_container"])
              and not near(fills[1 - index], c["secondary_container"]), repr(fills))
    check(f"{scheme}: segments are equal and do not move when switching",
          rects[0] == rects[1] and len({r.width() for r in rects[0]}) == 1, repr(rects))

    # ---------- 开关 ----------
    sw = e.encrypt_chk
    sw.setChecked(False)
    settle()
    label_x = theme.SWITCH_TRACK[0] + sw._GAP + 4
    QTest.mouseClick(sw, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(label_x, sw.height() // 2))
    check(f"{scheme}: clicking the switch label toggles it", sw.isChecked())
    sw.setChecked(False)
    settle()
    blank = sw.mapTo(w.opt_box, QPoint(sw.width() + 40, sw.height() // 2))
    QTest.mouseClick(w.opt_box, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, blank)
    check(f"{scheme}: clicking blank space beside the switch does not toggle it",
          not sw.isChecked() and sw.width() == sw.sizeHint().width(),
          f"{sw.width()} / {sw.sizeHint().width()}")
    sw.setFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(sw, Qt.Key.Key_Space)
    check(f"{scheme}: space toggles the switch", sw.isChecked())
    sw.setChecked(False)
    w.fmt_combo.setCurrentIndex(list(core.FORMATS).index("tar.gz"))
    settle()
    check(f"{scheme}: switch disabled for tar formats", not sw.isEnabled())
    w.fmt_combo.setCurrentIndex(list(core.FORMATS).index("zip"))
    settle()

    # ---------- 下拉菜单标出当前值 ----------
    combo = w.fmt_combo
    delegate = combo.itemDelegate()

    def row_image(row: int) -> QImage:
        img = QImage(QSize(300, 32), QImage.Format.Format_ARGB32)
        img.fill(QColor(c["surface_container"]))
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 300, 32)
        opt.state = QStyle.StateFlag.State_Enabled
        opt.palette = combo.palette()
        opt.font = combo.font()
        p = QPainter(img)
        delegate.paint(p, opt, combo.model().index(row, 0, QModelIndex()))
        p.end()
        return img

    cur = combo.currentIndex()
    tail = QRect(300 - 40, 0, 40, 32)
    marked = has_color(row_image(cur), tail, c["primary"])
    unmarked = has_color(row_image((cur + 1) % combo.count()), tail, c["primary"])
    check(f"{scheme}: menu marks the current value with a check mark",
          isinstance(delegate, theme._ComboDelegate) and marked and not unmarked,
          f"current {marked}, other {unmarked}")

    # ---------- 文本框错误状态 ----------
    w.src_input.setText("relative/path")
    w.src_input.commit()
    check(f"{scheme}: rejected path gets the error outline",
          w.src_input.property("m3error") is True and w.src_input.hint.isVisibleTo(w))
    w.src_input.submit(str(src))
    check(f"{scheme}: valid path clears the error outline", not w.src_input.property("m3error"))

    # ---------- 四种语言 × 两种模式 ----------
    for i, (code, _name) in enumerate(LANGS):
        w.lang_combo.setCurrentIndex(i)
        w.mode_tabs.setCurrentIndex(0)
        w.resize(WIDTH, 640)
        w.set_source(str(src))
        e.encrypt_chk.setChecked(True)
        e.pw_input.setText("abc")
        e.method_combo.setCurrentIndex(1)  # ZipCrypto：说明文字最长
        settle()
        check(f"{scheme}/{code}: compress minimum width fits {WIDTH}",
              w.minimumSizeHint().width() <= WIDTH, str(w.minimumSizeHint().width()))
        frame = QStyleOptionFrame()
        e.pw_confirm.initStyleOption(frame)
        room = e.pw_confirm.style().subElementRect(QStyle.SubElement.SE_LineEditContents,
                                                    frame, e.pw_confirm).width()
        need_ph = e.pw_confirm.fontMetrics().horizontalAdvance(e.pw_confirm.placeholderText())
        check(f"{scheme}/{code}: 'confirm password' placeholder fits",
              need_ph <= room, f"{need_ph} / {room}")
        # 最坏情况：zip 级别 0（仅存储的提示）、ZipCrypto、密码提示，以及输出文件夹没有权限
        # 时输出框的提示和「在访达中显示」按钮。缺工具的横幅另算，见 AGENTS.md 设计决策 28。
        w.set_source(str(ro / "src"))
        w.level_slider.setValue(0)
        settle()
        settle()
        need = needed_height(w)
        check(f"{scheme}/{code}: compress worst case fits {MAX_HEIGHT}px",
              need <= MAX_HEIGHT and w.out_input.hint_row.isVisibleTo(w)
              and w.level_note.isVisibleTo(w), str(need))
        check(f"{scheme}/{code}: window grew when the output hint appeared",
              w.height() >= need, f"{w.height()} / {need}")
        if shots:
            w.grab().save(str(Path(shots) / f"compress-{scheme}-{code}.png"))
        w.level_slider.setValue(6)
        w.set_source(str(src))
        w.mode_tabs.setCurrentIndex(1)
        w.extract_panel.set_archive(str(archive))
        settle()
        check(f"{scheme}/{code}: extract minimum width fits {WIDTH}",
              w.minimumSizeHint().width() <= WIDTH, str(w.minimumSizeHint().width()))
        need = needed_height(w)
        check(f"{scheme}/{code}: extract fits {MAX_HEIGHT}px", need <= MAX_HEIGHT, str(need))
        d = w.tools_dialog
        d.show_and_raise()
        settle()
        check(f"{scheme}/{code}: toolchain window fits {TOOLS_WIDTH}px",
              d.minimumSizeHint().width() <= TOOLS_WIDTH, str(d.minimumSizeHint().width()))
        if shots:
            w.grab().save(str(Path(shots) / f"extract-{scheme}-{code}.png"))
            d.grab().save(str(Path(shots) / f"tools-{scheme}-{code}.png"))
        d.hide()
        e.pw_input.setText("")
    close(w)
    set_lang("en")

# ---------------- 标题、无障碍、启动焦点、最小宽度 ----------------
theme.apply(app, "light")
set_lang("fr")
w = new_window("misc")
for i in range(len(LANGS)):
    w.lang_combo.setCurrentIndex(i)
    settle()
    check(f"title is just the app name ({LANGS[i][0]})", w.title_lbl.text() == core.APP_NAME,
          w.title_lbl.text())
w.lang_combo.setCurrentIndex([c for c, _ in LANGS].index("fr"))
settle()
rel =QAccessible.queryAccessibleInterface(w.lang_combo).relations()
labels = [iface.text(QAccessible.Text.Name) for iface, kind in rel
          if kind & QAccessible.RelationFlag.Label]
check("language combo is labelled for assistive technology", tr("language") in labels,
      repr(labels))
check("startup focus is on the source field", w.focusWidget() is w.src_input,
      type(w.focusWidget()).__name__)
w.tools_dialog.show_and_raise()
settle()
check("toolchain window opens with no control focused",
      w.tools_dialog.focusWidget() in (None, w.tools_dialog),
      type(w.tools_dialog.focusWidget()).__name__)
w.tools_dialog.hide()
set_lang("en")
w.retranslate()
w.set_source(str(src))
settle()
w.resize(WIDTH, needed_height(w))
settle()
w.tools.paths["zip"] = None  # 缺工具：横幅出现，窗口要随之加高
w.update_go_state()
settle()
settle()
check("window grew when the missing-tools banner appeared",
      w.banner.isVisibleTo(w) and w.height() >= needed_height(w),
      f"{w.height()} / {needed_height(w)}")
w.tools.refresh()
w.update_go_state()
probe = QCheckBox("x", w)
probe.setChecked(True)
probe.show()
settle()
img = probe.grab().toImage()
cols = [x for x in range(24) for y in range(img.height())  # 指示器在左端
        if near(img.pixelColor(x, y).name().upper(), SCHEMES["light"]["primary"], 12)]
check("checkbox indicator is drawn at 18px", bool(cols) and max(cols) - min(cols) + 1 == 18,
      f"{min(cols) if cols else '-'}..{max(cols) if cols else '-'}")
probe.deleteLater()
# 系统清理了临时目录：再次套用主题时重建，不会停在新调色板配旧样式表的状态。
shutil.rmtree(theme._state["tmpdir"].path(), ignore_errors=True)
theme.apply(app, "dark")
arrows = [Path(p) for p in app.styleSheet().split('"') if p.endswith(".svg")]
check("apply recovers when the temp folder was removed",
      theme.scheme_name() == "dark" and bool(arrows) and all(p.is_file() for p in arrows))
theme.apply(app, "light")
set_lang("fr")
w.retranslate()
w.encrypt_panel.encrypt_chk.setChecked(True)
w.resize(1, 640)
settle()
check("at minimum width the slider and path fields stay usable",
      w.level_slider.width() >= 120 and w.src_input.width() >= 160,
      f"slider {w.level_slider.width()}, source {w.src_input.width()}")
close(w)
set_lang("en")

# ---------------- 进度条与剩余时间 ----------------
import time  # noqa: E402

import gui_common  # noqa: E402

for code, _name in LANGS:
    set_lang(code)
    fp = gui_common.format_progress
    texts = {
        "scan": fp("scan", 0, None, "bytes", None, None),
        "finish": fp("finish", 0, None, "bytes", None, None),
        "estimating": fp("work", 10, 1000, "bytes", 5e6, None),
        "full": fp("work", 420, 1000, "bytes", 85.3e6, 80),
        "items": fp("work", 3, 4, "items", 2.0, 30),
        "unknown": fp("work", 5, None, "bytes", None, None),
    }
    check(f"progress text ({code})",
          texts["scan"] == tr("progress_scanning") and texts["finish"] == tr("progress_finishing")
          and texts["estimating"].endswith(tr("progress_estimating"))
          and texts["full"].count(gui_common.PROGRESS_SEP) == 2
          and tr("progress_percent", n="42") in texts["full"]
          and gui_common.format_eta(80) in texts["full"]
          and texts["items"].count(gui_common.PROGRESS_SEP) == 1  # 按项数计时不显示速度
          and texts["unknown"] == "", repr(texts))
set_lang("en")
check("ETA wording",
      gui_common.format_eta(41) == tr("eta_seconds", s="45")
      and gui_common.format_eta(125) == tr("eta_minutes_only", m="2")
      and gui_common.format_eta(135) == tr("eta_minutes", m="2", s="20")
      and gui_common.format_eta(3725) == tr("eta_hours", h="1", m="2"),
      gui_common.format_eta(135))

w = new_window("progress")
w.set_source(str(src))
settle()
w.job = core.ArchiveJob(src, tmp / "unused.zip", "zip")  # 只用于显示，不运行
w.refresh_busy_ui()
for i, (code, _name) in enumerate(LANGS):
    w.lang_combo.setCurrentIndex(i)
    w.on_progress("work", 420, 1000, "bytes")
    settle()
    long_text = gui_common.format_progress("work", 999, 1000, "bytes", 999.9e6, 3590)
    reserved = w.progress_lbl.minimumWidth()
    check(f"running layout fits {WIDTH}px and reserves room for long text ({code})",
          w.minimumSizeHint().width() <= WIDTH and w.progress_lbl.isVisibleTo(w)
          and reserved >= w.progress_lbl.fontMetrics().horizontalAdvance(long_text),
          f"{w.minimumSizeHint().width()} / reserved {reserved}")
check("determinate bar while working",
      (w.progress.minimum(), w.progress.maximum(), w.progress.value()) == (0, 1000, 420))
w.on_progress("scan", 0, None, "bytes")
check("indeterminate bar while counting files",
      w.progress.maximum() == 0 and w.progress_lbl.text() == tr("progress_scanning"))
w.on_progress("finish", 0, None, "bytes")
check("full bar while finishing", w.progress.value() == w.progress.maximum() == 1)
w.job = None
w.refresh_busy_ui()
w.lang_combo.setCurrentIndex(0)

# 真实运行一次压缩和解压：进度经 Bridge 到达界面，任务结束后进度区隐藏。
big = tmp / "bigsrc"
big.mkdir()
(big / "data.bin").write_bytes(os.urandom(8 * 1024 * 1024))
seen: list[tuple] = []
w.bridge.progress.connect(lambda *e: seen.append((*e, w.progress.maximum(),
                                                   w.progress_lbl.text())))
main.MainWindow._show_done = lambda self, path, msg, title: None
for mode, prepare in (("compress", lambda: w.set_source(str(big))),
                      ("extract", lambda: w.extract_panel.set_archive(str(tmp / "bigsrc.zip")))):
    seen.clear()
    w.mode_tabs.setCurrentIndex(0 if mode == "compress" else 1)
    prepare()
    settle()
    (w.src_input if mode == "compress" else w.extract_panel.arc_input).setFocus()
    w.start()
    settle()
    check(f"real {mode}: focus does not jump to the log when inputs are disabled",
          w.focusWidget() is not w.log_view, type(w.focusWidget()).__name__)
    deadline = time.monotonic() + 60
    while w.job is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    phases = [e[0] for e in seen]
    check(f"real {mode}: progress reached the window and the bar became determinate",
          "work" in phases and phases[-1] == "finish"
          and any(e[0] == "work" and e[4] == 1000 for e in seen), repr(seen[-3:]))
    check(f"real {mode}: progress hidden after the job", not w.progress.isVisibleTo(w)
          and not w.progress_lbl.isVisibleTo(w) and not w._progress_timer.isActive())
close(w)

# ---------------- 切换模式：窗口大小不变，两种模式的第二张卡片一样高（用户要求） ----------------
theme.apply(app, "light")
set_lang("en")
w = new_window("modes")
w.set_source(str(src))
w.extract_panel.set_archive(str(archive))
settle()


def switch_round(label: str) -> None:
    """来回切换两次，窗口大小、日志区高度都不变；当前可见的第二张卡片高度相同。"""
    before = (w.size(), w.log_box.height())
    cards = []
    for index in (1, 0, 1, 0):
        w.mode_tabs.setCurrentIndex(index)
        settle()
        card = w.extract_panel.ext_box if index else w.opt_box
        cards.append(card.height())
        check(f"modes/{label}: window and log unchanged after switching to {MODES[index]}",
              (w.size(), w.log_box.height()) == before,
              f"{before} -> {(w.size(), w.log_box.height())}")
    check(f"modes/{label}: second cards are equally tall", len(set(cards)) == 1, repr(cards))


MODES = main.MODES
# 焦点：从路径框切换模式后落在新模式的路径框；没有归档时解压卡片里没有可用的控件，
# 叠放控件换页会把焦点交给卡片本身（真实窗口里看到的焦点环），这里不能出现。
w.extract_panel.arc_input.submit("")  # 清掉归档：目标框和按钮随之停用
w.src_input.setFocus()
settle()
for index, field in ((1, w.extract_panel.arc_input), (0, w.src_input)):
    w.mode_tabs.setCurrentIndex(index)
    settle()
    check(f"modes: focus moves to the {MODES[index]} path field", w.focusWidget() is field,
          type(w.focusWidget()).__name__)
w.mode_tabs.setFocus(Qt.FocusReason.TabFocusReason)
w.mode_tabs.setCurrentIndex(1)
settle()
check("modes: switching from the keyboard keeps focus on the tabs", w.focusWidget() is w.mode_tabs,
      type(w.focusWidget()).__name__)
w.mode_tabs.setCurrentIndex(0)
settle()
focus_opt = QStyleOptionFocusRect()
focus_opt.initFrom(w.opt_box)
ring = QImage(QSize(80, 40), QImage.Format.Format_ARGB32)
ring.fill(QColor("#00000000"))
painter = QPainter(ring)
focus_opt.rect = QRect(4, 4, 60, 30)
app.style().drawPrimitive(QStyle.PrimitiveElement.PE_FrameFocusRect, focus_opt, painter, w.opt_box)
painter.end()
check("modes: cards draw no focus ring", first_opaque(ring) is None)
w.extract_panel.set_archive(str(archive))
settle()
switch_round("default size")
w.resize(WIDTH + 120, 720)  # 用户调整过的窗口
settle()
switch_round("user-resized")
w.encrypt_panel.encrypt_chk.setChecked(True)  # 压缩卡片变高，解压卡片跟着一样高
settle()
switch_round("encryption on")
w.encrypt_panel.encrypt_chk.setChecked(False)
settle()
for i, (code, _name) in enumerate(LANGS):
    # 在解压模式下换语言：隐藏的压缩卡片文字变了，叠放控件也要重新计算高度。
    w.mode_tabs.setCurrentIndex(1)
    w.lang_combo.setCurrentIndex(i)
    settle()
    dest_x = w.extract_panel.dest_input.mapTo(w, QPoint(0, 0)).x()
    switch_round(f"{code} after changing language in extract mode")
    fmt_x = w.fmt_combo.mapTo(w, QPoint(0, 0)).x()
    check(f"modes/{code}: label columns line up across modes", fmt_x == dest_x,
          f"{fmt_x} / {dest_x}")
# 隐藏的压缩卡片在另一种语言里变高（窄窗口、加密、ZipCrypto、仅存储的提示都换行）：
# 解压卡片当场跟着变高，之后切回压缩时窗口不再变。
w.mode_tabs.setCurrentIndex(0)
w.lang_combo.setCurrentIndex([c for c, _ in LANGS].index("zh_Hans"))
w.resize(620, 640)
e = w.encrypt_panel
e.encrypt_chk.setChecked(True)
e.pw_input.setText("abc")
e.method_combo.setCurrentIndex(1)
w.level_slider.setValue(0)
settle()
w.mode_tabs.setCurrentIndex(1)
settle()
short = w.extract_panel.ext_box.height()
w.lang_combo.setCurrentIndex([c for c, _ in LANGS].index("fr"))
settle()
grown = w.extract_panel.ext_box.height()
check("modes: hidden compress card growing in another language grows the extract card too",
      grown > short, f"{short} -> {grown}")
switch_round("fr, hidden card grew")
e.pw_input.setText("")
e.encrypt_chk.setChecked(False)
close(w)
set_lang("en")

# ---------------- 「显示 ▸ 外观」菜单 ----------------
w = new_window("appearance")
acts = w.appearance_actions
check("appearance menu: system, light, dark; system checked by default",
      list(acts) == list(theme.APPEARANCES) and acts["system"].isChecked()
      and all(a.isCheckable() and a.actionGroup() is not None
              and a.actionGroup().isExclusive() for a in acts.values()))
check("appearance menu is translated",
      w.view_menu.title() == tr("view_menu") and w.appearance_menu.title() == tr("appearance_menu")
      and [a.text() for a in acts.values()] == [tr(f"appearance_{m}") for m in theme.APPEARANCES])
for mode in ("dark", "light", "dark"):
    acts[mode].trigger()
    settle()
    check(f"appearance menu: choosing {mode} applies and saves it",
          theme.scheme_name() == mode and theme._state["forced"]
          and w.settings.value("appearance") == mode and acts[mode].isChecked()
          and app.palette().color(QPalette.ColorRole.Window).name().upper()
          == SCHEMES[mode]["surface"]
          and near(icon_color(w.btn_go.icon()), SCHEMES[mode]["on_primary"]),
          theme.scheme_name())
app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Light)
settle()
check("appearance menu: a fixed choice ignores the system switching",
      theme.scheme_name() == "dark")
acts["system"].trigger()
settle()
check("appearance menu: system follows the system again",
      not theme._state["forced"] and w.settings.value("appearance") == "system"
      and theme.scheme_name() == theme._system_scheme(app), theme.scheme_name())
w.settings.setValue("appearance", "dark")
check("saved appearance is read back", main.appearance_pref(w.settings) == "dark")
w.settings.setValue("appearance", "purple")
check("unknown saved appearance falls back to system", main.appearance_pref(w.settings) == "system")
w.settings.setValue("appearance", "light")
close(w)
w = new_window("appearance")  # 同一个 ini：菜单勾选的是保存的选择
check("appearance menu checks the saved choice", w.appearance_actions["light"].isChecked()
      and not w.appearance_actions["system"].isChecked())
close(w)
theme.set_appearance(app, "system")

# ---------------- 有焦点的按钮文字不被挤没（对话框里的默认按钮总有焦点） ----------------
from PySide6.QtWidgets import QMessageBox, QStyleOptionButton  # noqa: E402


def text_room(b: QPushButton) -> tuple[int, int]:
    """(按钮内容区宽度, 文字宽度)。"""
    opt = QStyleOptionButton()
    b.initStyleOption(opt)
    room = b.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, opt, b).width()
    return room, b.fontMetrics().horizontalAdvance(b.text())


for scheme in ("light", "dark"):
    theme.apply(app, scheme)
    for code, _name in LANGS:
        set_lang(code)
        box = QMessageBox(QMessageBox.Icon.NoIcon, "t", "text")
        box.addButton(tr("show_in_finder"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(tr("ok"), QMessageBox.ButtonRole.AcceptRole)
        box.show()
        settle()
        squeezed = []
        for b in box.buttons():
            b.setFocus(Qt.FocusReason.TabFocusReason)
            settle()
            room, need = text_room(b)
            if room < need:
                squeezed.append((b.text(), room, need))
        box.close()
        w = new_window(f"focus-{scheme}-{code}")
        for b in (w.btn_go, w.btn_tools, w.btn_folder, w.banner_btn):
            b.setVisible(True)
            b.setEnabled(True)
            b.setFocus(Qt.FocusReason.TabFocusReason)
            settle()
            room, need = text_room(b)
            if room < need:
                squeezed.append((b.text(), room, need))
        close(w)
        check(f"{scheme}/{code}: focused buttons keep room for their text", not squeezed,
              repr(squeezed))
set_lang("en")

width = max(len(n) for n, _, _ in results)
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print(f"{sum(ok for _, ok, _ in results)}/{len(results)}",
      "ALL PASS" if all(ok for _, ok, _ in results) else "SOME FAILED")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
