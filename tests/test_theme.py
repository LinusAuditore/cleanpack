"""Material 3 令牌的静态检查：配色角色齐全、文字对比度、颜色运算，以及界面用到的图标都在。

不导入 Qt（theme_tokens 是纯数据）；主题在界面上的效果由 tests/gui/gui_theme_check.py 检查。
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import theme_tokens as tt  # noqa: E402

# 调用这些函数时，字符串参数是图标名（assets/icons 下的文件名）。
ICON_CALLS = {"set_icon", "set_label_icon", "icon", "_tinted_svg"}
GUI_FILES = ["main.py", "gui_common.py", "extract_panel.py", "encrypt_panel.py",
             "tools_dialog.py", "theme.py"]

# 文字与底色的组合：(文字角色, 底色角色)。界面上实际出现的搭配都要达到 WCAG AA 的 4.5:1。
TEXT_PAIRS = [
    ("on_primary", "primary"), ("on_primary_container", "primary_container"),
    ("on_secondary", "secondary"), ("on_secondary_container", "secondary_container"),
    ("on_tertiary", "tertiary"), ("on_tertiary_container", "tertiary_container"),
    ("on_error", "error"), ("on_error_container", "error_container"),
    ("inverse_on_surface", "inverse_surface"),
    # 正文、说明文字（note）、警告（warn）、文字按钮和描边按钮（primary）放在窗口底色、
    # 卡片（surface_container_low）、横幅（surface_container_highest）和对话框上。
    *[(fg, bg) for fg in ("on_surface", "on_surface_variant", "error", "primary")
      for bg in ("surface", "surface_container_lowest", "surface_container_low",
                 "surface_container", "surface_container_high", "surface_container_highest")],
]


class TokenTests(unittest.TestCase):
    def test_schemes_have_the_same_roles(self) -> None:
        light, dark = tt.SCHEMES["light"], tt.SCHEMES["dark"]
        self.assertEqual(set(light), set(dark))
        for name, scheme in tt.SCHEMES.items():
            for role, value in scheme.items():
                self.assertRegex(value, r"^#[0-9A-F]{6}$", f"{name}.{role}")

    def test_text_contrast(self) -> None:
        for name, scheme in tt.SCHEMES.items():
            for fg, bg in TEXT_PAIRS:
                with self.subTest(scheme=name, fg=fg, bg=bg):
                    self.assertGreaterEqual(tt.contrast_ratio(scheme[fg], scheme[bg]), 4.5)

    def test_outline_is_visible(self) -> None:
        # 文本框和描边按钮的边框是控件的边界，WCAG 对非文字元素要求 3:1。
        for name, scheme in tt.SCHEMES.items():
            for bg in ("surface", "surface_container_low"):
                with self.subTest(scheme=name, bg=bg):
                    self.assertGreaterEqual(tt.contrast_ratio(scheme["outline"], scheme[bg]), 3)

    def test_dark_is_dark(self) -> None:
        light, dark = tt.SCHEMES["light"], tt.SCHEMES["dark"]
        self.assertGreater(tt.relative_luminance(light["surface"]),
                           tt.relative_luminance(dark["surface"]))

    def test_color_math(self) -> None:
        self.assertEqual(tt.blend("#FFFFFF", "#000000", 0.5), "#808080")
        self.assertEqual(tt.blend("#123456", "#ABCDEF", 1.0), "#123456")
        self.assertEqual(tt.blend("#123456", "#ABCDEF", 0.0), "#ABCDEF")
        self.assertAlmostEqual(tt.contrast_ratio("#000000", "#FFFFFF"), 21.0, places=3)
        self.assertAlmostEqual(tt.contrast_ratio("#777777", "#777777"), 1.0)

    def test_type_scale_and_state(self) -> None:
        for name, (pt, weight) in tt.TYPE_SCALE.items():
            self.assertGreaterEqual(pt, 11, name)
            self.assertIn(weight, (400, 500), name)
        self.assertTrue(0 < tt.STATE["hover"] < tt.STATE["pressed"] < 0.2)


class IconTests(unittest.TestCase):
    def test_used_icons_exist(self) -> None:
        """界面代码里用到的每个图标名在 assets/icons 下都有 SVG（漏拷文件时界面会报错）。"""
        used: set[str] = set()
        for name in GUI_FILES:
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if fname not in ICON_CALLS:
                    continue
                for arg in node.args[1:2] if fname in ("set_icon", "set_label_icon") \
                        else node.args[:1]:
                    used |= {c.value for c in ast.walk(arg)
                             if isinstance(c, ast.Constant) and isinstance(c.value, str)}
        self.assertTrue(used, "没有找到任何图标调用")
        for icon in sorted(used):
            with self.subTest(icon=icon):
                svg = ROOT / "assets" / "icons" / f"{icon}.svg"
                self.assertTrue(svg.is_file(), svg)
                self.assertTrue(svg.read_text(encoding="utf-8").startswith("<svg "))

    def test_icons_are_bundled_and_attributed(self) -> None:
        """spec 的 Analysis(datas=...) 里确实有图标条目（注释里出现这个路径不算）。"""
        tree = ast.parse((ROOT / "CleanPack.spec").read_text(encoding="utf-8"))
        entries = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis":
                for kw in node.keywords:
                    if kw.arg == "datas":
                        entries |= {tuple(c.value for c in t.elts)
                                    for t in ast.walk(kw.value)
                                    if isinstance(t, ast.Tuple)
                                    and all(isinstance(c, ast.Constant) for c in t.elts)}
        self.assertIn(("assets/icons/*.svg", "assets/icons"), entries)
        notices = (ROOT / "licenses" / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertTrue(re.search(r"Material Symbols", notices))


if __name__ == "__main__":
    unittest.main()
