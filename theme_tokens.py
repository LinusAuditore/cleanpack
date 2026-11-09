"""Material 3 设计令牌：颜色角色、字号层级、形状和状态层透明度。

只有数据和简单的颜色运算，不导入 Qt，单元测试可以直接检查（tests/test_theme.py）。
界面怎样使用这些令牌见 theme.py。

颜色由 Google 官方的 @material/material-color-utilities 0.4.0（npm，Apache-2.0）生成，
种子色 #3388BB，2021 版规范（不是 M3 Expressive 所用的 2025 版），对比度级别 0：

    const s = new SchemeTonalSpot(Hct.fromInt(argbFromHex('#3388BB')), isDark, 0.0, '2021');
    for (const c of new MaterialDynamicColors().allColors) roles[c.name] = hexFromArgb(c.getArgb(s));

这里只保留界面用到的角色。0.4.0 的 npm 包里有 10 处相对导入没写 .js 扩展名，node 直接
加载会失败，需要在解压出的副本里补上。用同样方法算出的基线种子色 #6750A4 与 Material
Theme Builder 当前的基线配色一致（primary #65558F、primaryContainer #E9DDFF）。
"""
from __future__ import annotations

SEED = "#3388BB"

# 颜色角色名沿用官方库的写法（snake_case）。浅色和深色的角色集合必须相同。
SCHEMES: dict[str, dict[str, str]] = {
    "light": {
        "primary": "#266489",
        "on_primary": "#FFFFFF",
        "primary_container": "#C9E6FF",
        "on_primary_container": "#004B6F",
        "secondary": "#50606E",
        "on_secondary": "#FFFFFF",
        "secondary_container": "#D3E5F5",
        "on_secondary_container": "#384956",
        "tertiary": "#64597B",
        "on_tertiary": "#FFFFFF",
        "tertiary_container": "#EADDFF",
        "on_tertiary_container": "#4C4162",
        "error": "#BA1A1A",
        "on_error": "#FFFFFF",
        "error_container": "#FFDAD6",
        "on_error_container": "#93000A",
        "surface": "#F7F9FF",
        "on_surface": "#181C20",
        "on_surface_variant": "#41474D",
        "surface_dim": "#D7DADF",
        "surface_bright": "#F7F9FF",
        "surface_container_lowest": "#FFFFFF",
        "surface_container_low": "#F1F4F9",
        "surface_container": "#EBEEF3",
        "surface_container_high": "#E5E8ED",
        "surface_container_highest": "#E0E3E8",
        "outline": "#72787E",
        "outline_variant": "#C1C7CE",
        "inverse_surface": "#2D3135",
        "inverse_on_surface": "#EEF1F6",
        "inverse_primary": "#95CDF7",
    },
    "dark": {
        "primary": "#95CDF7",
        "on_primary": "#00344E",
        "primary_container": "#004B6F",
        "on_primary_container": "#C9E6FF",
        "secondary": "#B7C9D9",
        "on_secondary": "#22323F",
        "secondary_container": "#384956",
        "on_secondary_container": "#D3E5F5",
        "tertiary": "#CFC0E8",
        "on_tertiary": "#352B4B",
        "tertiary_container": "#4C4162",
        "on_tertiary_container": "#EADDFF",
        "error": "#FFB4AB",
        "on_error": "#690005",
        "error_container": "#93000A",
        "on_error_container": "#FFDAD6",
        "surface": "#101417",
        "on_surface": "#E0E3E8",
        "on_surface_variant": "#C1C7CE",
        "surface_dim": "#101417",
        "surface_bright": "#363A3E",
        "surface_container_lowest": "#0B0F12",
        "surface_container_low": "#181C20",
        "surface_container": "#1C2024",
        "surface_container_high": "#262A2E",
        "surface_container_highest": "#313539",
        "outline": "#8B9198",
        "outline_variant": "#41474D",
        "inverse_surface": "#E0E3E8",
        "inverse_on_surface": "#2D3135",
        "inverse_primary": "#266489",
    },
}

# 字号层级：(磅值, 字重)。M3 以 14px 的 Body Medium 为正文，macOS 的正文是 13pt，
# 所以按 13/14 的比例换算后取整；Body Small 和 Label Medium 按比例是 11pt，
# 中文在 11pt 下偏小，取 12pt。字体用系统字体：Roboto 不含中文，本机也没有安装。
TYPE_SCALE: dict[str, tuple[int, int]] = {
    "title_large": (20, 400),
    "title_medium": (15, 500),
    "title_small": (13, 500),
    "body_large": (15, 400),
    "body_medium": (13, 400),
    "body_small": (12, 400),
    "label_large": (13, 500),
    "label_medium": (12, 500),
}

# 形状（圆角半径，像素）。full 是「完全圆角」，实际半径取控件高度的一半。
SHAPE = {"none": 0, "extra_small": 4, "small": 8, "medium": 12, "large": 16,
         "extra_large": 28}

# 状态层不透明度，以及停用时内容和容器的不透明度。
# 取 material-web v1.0.0 的 v0_192 令牌（2023 版基线）；最新版（含 Expressive）改成了 0.10。
STATE = {"hover": 0.08, "focus": 0.12, "pressed": 0.12}
DISABLED_CONTENT = 0.38
DISABLED_CONTAINER = 0.12

# 控件尺寸（像素）。M3 的按钮和分段按钮默认高 40，每降一级密度减 4；这里用密度 -1（36），
# 文本框也取同样高度（规范默认 56，桌面上过高）。用默认的 40 时，打开加密后窗口要 890–950
# 像素高，13 英寸 MacBook 的屏幕放不下。开关按规范 52×32（开关没有密度级别）。
CONTROL_HEIGHT = 36
SWITCH_TRACK = (52, 32)
SWITCH_THUMB = {"off": 16, "on": 24, "pressed": 28}


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def blend(fg: str, bg: str, alpha: float) -> str:
    """把不透明度为 alpha 的 fg 叠在 bg 上，返回不透明的结果色。

    状态层（悬停、按下）和停用色都预先混合成不透明色：样式表里的半透明颜色叠在什么
    底色上取决于控件层级，预先混合结果才确定。
    """
    f, b = _rgb(fg), _rgb(bg)
    mixed = (round(fc * alpha + bc * (1 - alpha)) for fc, bc in zip(f, b))
    return "#" + "".join(f"{c:02X}" for c in mixed)


def relative_luminance(color: str) -> float:
    """WCAG 2 的相对亮度。"""
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    """WCAG 2 对比度，范围 1–21。"""
    la, lb = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)
