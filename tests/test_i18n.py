"""静态检查四种语言的翻译键与占位符，并确认代码中用到的 key 都有译文；不导入 Qt。"""
from __future__ import annotations

import ast
from pathlib import Path
import string
import unittest

import core
import i18n

ROOT = Path(core.__file__).resolve().parent
GUI_FILES = ("main.py", "gui_common.py", "tools_dialog.py", "extract_panel.py",
             "encrypt_panel.py", "theme.py")
# 以 f-string 拼出的 key（main.MODES 的每个模式、core.ZIP_METHODS 的每种加密方式），
# 静态扫描看不到，在这里列出。
DYNAMIC_KEYS = ({"mode_compress", "mode_extract"} | {f"zip_{m}" for m in core.ZIP_METHODS}
                # 外观菜单按 theme.APPEARANCES 拼出键名（这里不导入 Qt，所以照抄三个值）。
                | {f"appearance_{m}" for m in ("system", "light", "dark")})


def strings_node() -> ast.Dict:
    """返回 i18n.py 中 STRINGS 字典的 AST 节点，便于发现被字面量覆盖的重复键。"""
    tree = ast.parse((ROOT / "i18n.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "STRINGS":
            assert isinstance(node.value, ast.Dict)
            return node.value
    raise AssertionError("i18n.py 中没有 STRINGS")


def call_keys(path: Path, func_names: dict[str, int]) -> set[str]:
    """收集源码中 func(..., "字面量", ...) 调用里指定位置的字符串参数；func_names 映射函数名到参数下标。"""
    keys = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in func_names or len(node.args) <= func_names[name]:
            continue
        arg = node.args[func_names[name]]
        # tr("a" if 条件 else "b") 两个分支都要有译文。
        for value in (arg.body, arg.orelse) if isinstance(arg, ast.IfExp) else (arg,):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                keys.add(value.value)
    return keys


def fields(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def call_kwargs(path: Path, func_name: str) -> list[tuple[int, str, set[str]]]:
    """逐个列出 func("字面量", k=...) 调用：(行号, key, 传入的关键字参数名)。

    不按 key 合并，同一个 key 的某处调用漏传参数时也能发现。
    """
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        first = node.args[0]
        if name == func_name and isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.append((node.lineno, first.value, {k.arg for k in node.keywords if k.arg}))
    return found


class I18nTests(unittest.TestCase):
    def setUp(self) -> None:
        self.node = strings_node()
        self.strings: dict[str, dict[str, str]] = ast.literal_eval(self.node)

    def test_no_duplicate_keys(self) -> None:
        for lang_node, table in zip(self.node.keys, self.node.values):
            assert isinstance(table, ast.Dict)
            keys = [k.value for k in table.keys if isinstance(k, ast.Constant)]
            self.assertEqual(len(keys), len(set(keys)), getattr(lang_node, "value", lang_node))

    def test_key_parity(self) -> None:
        base = set(self.strings["en"])
        self.assertEqual(set(self.strings), {"en", "zh_Hans", "zh_Hant", "fr"})
        for lang, table in self.strings.items():
            self.assertEqual(set(table), base, lang)

    def test_placeholders_match(self) -> None:
        for key, text in self.strings["en"].items():
            for lang, table in self.strings.items():
                self.assertEqual(fields(table[key]), fields(text), f"{lang}.{key}")

    def test_used_keys_exist(self) -> None:
        en = self.strings["en"]
        used = call_keys(ROOT / "core.py", {"JobError": 0})
        for name in GUI_FILES:
            # confirm(parent, title, text, accept_key) 的按钮文案 key 是第 4 个参数。
            used |= call_keys(ROOT / name, {"tr": 0, "confirm": 3})
        self.assertIn("stop_quit", used)
        self.assertIn("extract_done_title", used)  # 只出现在条件表达式里
        self.assertIn("choose_archive", used)      # 只出现在 extract_panel.py 里
        self.assertIn("encrypt_names", used)       # 只出现在 encrypt_panel.py 里
        used |= DYNAMIC_KEYS
        used |= set(core._ERRNO_KEYS.values()) | {"io_error"}
        used |= set(core._DENIAL_KEYS.values())  # 访问被拒时按原因选用，见 core._denied
        self.assertEqual(sorted(used - set(en)), [])

    def test_core_errors_fill_placeholders(self) -> None:
        """核心层抛出的每个 key 都要传齐文案用到的占位符，否则界面格式化时会抛 KeyError。"""
        en = self.strings["en"]
        calls = call_kwargs(ROOT / "core.py", "JobError")
        self.assertGreater(len(calls), 20)
        for line, key, kwargs in calls:
            self.assertLessEqual(fields(en[key]), kwargs, f"core.py:{line} {key}")
        # job_error_from_os 以动态 key 抛出，统一传入 path 和 err。
        for key in set(core._ERRNO_KEYS.values()) | {"io_error"}:
            self.assertLessEqual(fields(en[key]), {"path", "err"}, key)
        # 按被拒原因选用的文案：_denied 只传 path（job_error_from_os 另外传 err）。
        for key in core._DENIAL_KEYS.values():
            self.assertLessEqual(fields(en[key]), {"path"}, key)

    def test_french_typographic_apostrophe(self) -> None:
        for key, text in self.strings["fr"].items():
            self.assertNotIn("'", text, f"fr.{key} 应使用 U+2019 撇号")

    def test_qt_locales_cover_langs(self) -> None:
        self.assertEqual(set(i18n.QT_LOCALES), {code for code, _ in i18n.LANGS})

    def test_detect_lang(self) -> None:
        cases = {
            ("zh-Hans-HK",): "zh_Hans", ("zh-Hans-TW",): "zh_Hans", ("zh_Hans_MO",): "zh_Hans",
            ("zh-Hant-CN",): "zh_Hant", ("zh-TW",): "zh_Hant", ("zh-HK",): "zh_Hant",
            ("zh-CN",): "zh_Hans", ("zh",): "zh_Hans", ("fr-CA",): "fr", ("fr",): "fr",
            ("frr-DE", "en-GB"): "en", ("de-DE", "fr-FR"): "fr", ("ja-JP",): "en", (): "en",
            # 扩展子标签与 POSIX 编码后缀不能影响文字判断。
            ("zh-Hant-TW-u-nu-hans",): "zh_Hant", ("zh-CN-u-nu-hant",): "zh_Hans",
            ("zh_TW.UTF-8",): "zh_Hant", ("", "und", "fr"): "fr",
        }
        for names, expected in cases.items():
            with self.subTest(names=names):
                self.assertEqual(i18n.detect_lang(list(names)), expected)

    def test_french_punctuation_spacing(self) -> None:
        for key, text in self.strings["fr"].items():
            # 占位符会被替换成任意文本，紧跟其后的标点视为前面没有空格。
            prev = "x"
            for literal, field, _, _ in string.Formatter().parse(text):
                for ch in literal:
                    if ch in ":?;!":
                        self.assertIn(prev, "   ", f"fr.{key}: {text!r}")
                    prev = ch
                if field is not None:
                    prev = "x"


if __name__ == "__main__":
    unittest.main()
