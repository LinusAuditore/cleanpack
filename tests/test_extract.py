"""解压模式测试：真实工具解压六种格式，验证垃圾清理、智能放置、改名、安全检查和失败清理。

数据只写入 /tmp 下的临时目录；缺少工具时明确跳过对应用例。
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import time
import unicodedata
import unittest
from unittest.mock import patch
import zipfile

import core

JUNK_MEMBERS = ["._resource", ".DS_Store", "sub/._x", "sub/.DS_Store", "Icon\r",
                "__MACOSX/._top", "sub/__MACOSX/deep/._y", ".LSOverride"]
DECOYS = {"DS_Store.txt": b"decoy", "a._x": b"inner dot underscore", "Icon": b"no CR",
          "sub/__MACOSX-not/f.txt": b"similar dir"}


def silent(_: str) -> None:
    """丢弃任务日志。"""


def nfc_tree(root: Path) -> dict[str, bytes | str]:
    """返回 {相对路径(NFC): 文件内容或 'dir'/'-> 目标'}，不跟随链接；名称按 NFC 比较，
    因为 pax 头和带 UTF-8 标志的 zip 在 APFS 上解出 NFD 名称。"""
    out: dict[str, bytes | str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
            if path.is_symlink():
                out[rel] = "-> " + os.readlink(path)
            elif path.is_dir():
                out[rel] = "dir"
            else:
                out[rel] = path.read_bytes()
    return out


class ExtractBase(unittest.TestCase):
    """每个用例独占 /tmp 下的临时目录：src 放素材，dest 是解压目标，arch 放归档。"""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="cleanpack-xtest-", dir="/tmp")
        self.addCleanup(self.force_cleanup, temp)
        self.root = Path(temp.name)
        self.dest = self.root / "dest"
        self.arch = self.root / "arch"
        self.dest.mkdir()
        self.arch.mkdir()

    @staticmethod
    def force_cleanup(temp: tempfile.TemporaryDirectory) -> None:
        core.make_tree_accessible(Path(temp.name))
        temp.cleanup()

    def require(self, *tools: str) -> None:
        missing = [t for t in tools if core.locate(t) is None]
        if missing:
            self.skipTest("缺少工具：" + ", ".join(missing))

    def extract(self, archive: Path, **kw: object) -> tuple[Path, core.ExtractJob, list[str]]:
        lines: list[str] = []
        job = core.ExtractJob(archive, self.dest, log=lines.append, **kw)
        return job.run(), job, lines

    def assert_fails(self, archive: Path, key: str) -> core.ExtractJob:
        job = core.ExtractJob(archive, self.dest, log=silent)
        with self.assertRaises(core.JobError) as caught:
            job.run()
        self.assertEqual(caught.exception.key, key, caught.exception.kw)
        self.assert_dest_untouched()
        return job

    def assert_dest_untouched(self, expected: set[str] | None = None) -> None:
        """失败后目标文件夹里不能留下任何新东西，包括暂存目录。"""
        self.assertEqual(set(os.listdir(self.dest)), expected or set())

    def tar_with(self, name: str, members: list[tarfile.TarInfo | tuple[str, bytes]],
                 mode: str = "w") -> Path:
        """用 tarfile 手工构造归档；元组表示普通文件 (名称, 内容)。"""
        path = self.arch / name
        with tarfile.open(path, mode, format=tarfile.GNU_FORMAT) as tf:
            for m in members:
                if isinstance(m, tuple):
                    info = tarfile.TarInfo(m[0])
                    info.size = len(m[1])
                    tf.addfile(info, io.BytesIO(m[1]))
                else:
                    tf.addfile(m)
        return path

    @staticmethod
    def info(name: str, kind: bytes, **attrs: object) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.type = kind
        for k, v in attrs.items():
            setattr(info, k, v)
        return info


class RoundTripTests(ExtractBase):
    """CleanPack 自己压缩、再自己解压，结果应与源文件夹（去掉垃圾）一致。"""

    def make_source(self) -> Path:
        src = self.root / "src" / "项目 café"
        for rel, data in {"keep.txt": b"keep\n", "sub/中文.txt": "多层\n".encode(),
                          "sub/deep/x.bin": os.urandom(5000), **DECOYS}.items():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_bytes(data)
        (src / "empty-dir").mkdir()
        (src / "link-in").symlink_to("keep.txt")
        (src / "link-out").symlink_to("/etc/hosts")
        for rel in ("sub/._x", ".DS_Store"):
            (src / rel).write_bytes(b"junk")
        return src

    def test_round_trip_all_formats(self) -> None:
        src = self.make_source()
        expected = {k: v for k, v in nfc_tree(src).items()
                    if not any(core.is_junk_name(p) for p in k.split("/"))}
        for fmt in core.FORMATS:
            with self.subTest(fmt=fmt):
                missing = core.missing_tools(fmt)
                if missing:
                    self.skipTest(f"{fmt} 缺少 {missing}")
                out = self.arch / ("rt" + core.FORMATS[fmt].ext)
                core.ArchiveJob(src, out, fmt, log=silent).run()
                result, job, _ = self.extract(out)
                # 只有一个顶层文件夹：直接放进目标，名称就是源文件夹名。
                self.assertEqual(unicodedata.normalize("NFC", result.name), src.name)
                self.assertEqual(result.parent, self.dest)
                self.assertEqual(nfc_tree(result), expected)
                # 指向 /etc/hosts 的链接原样保留，并给出警告。
                self.assertEqual([w.key for w in job.warnings], ["link_outside"])
                self.assert_dest_untouched({result.name})
                core.make_tree_accessible(result)
                subprocess.run(["rm", "-rf", str(result)], check=True)


class JunkAndLayoutTests(ExtractBase):
    def junk_tar(self, top: str | None = "top") -> Path:
        prefix = f"{top}/" if top else ""
        members = [(prefix + m, b"junk") for m in JUNK_MEMBERS]
        members += [(prefix + k, v) for k, v in DECOYS.items()]
        members.append((prefix + "real.txt", b"real"))
        return self.tar_with("j.tar.gz", members, "w:gz")

    def test_junk_removed_decoys_kept(self) -> None:
        self.require("tar")
        result, job, _ = self.extract(self.junk_tar())
        tree = nfc_tree(result)
        for rel in tree:
            self.assertFalse(any(core.is_junk_name(p) for p in rel.split("/")), rel)
        for rel, data in DECOYS.items():
            self.assertEqual(tree[rel], data)
        self.assertEqual(tree["real.txt"], b"real")

    def test_finder_zip_with_macosx_is_placed_directly(self) -> None:
        """Finder 生成的 zip 是 foo/ 加 __MACOSX/：清掉垃圾后只剩 foo，直接放进目标。"""
        self.require("tar")
        path = self.arch / "foo.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("foo/a.txt", "a")
            z.writestr("__MACOSX/foo/._a.txt", "meta")
            z.writestr("__MACOSX/._foo", "meta")
        result, _, _ = self.extract(path)
        self.assertEqual(result, self.dest / "foo")
        self.assertEqual(nfc_tree(result), {"a.txt": b"a"})

    def test_multiple_top_level_items_get_a_folder(self) -> None:
        self.require("tar")
        path = self.tar_with("bundle.TAR", [("a.txt", b"a"), ("b/c.txt", b"c")])
        result, _, _ = self.extract(path)
        self.assertEqual(result, self.dest / "bundle")
        self.assertEqual(set(nfc_tree(result)), {"a.txt", "b", "b/c.txt"})

    def test_single_file_and_name_collisions(self) -> None:
        """重名时像 Finder 一样编号，绝不覆盖，包括空文件夹和只差大小写的名称。"""
        self.require("tar")
        (self.dest / "report.pdf").write_bytes(b"mine")
        (self.dest / "REPORT 2.PDF").write_bytes(b"mine too")
        path = self.tar_with("r.tar", [("report.pdf", b"theirs")])
        result, _, _ = self.extract(path)
        self.assertEqual(result, self.dest / "report 3.pdf")
        self.assertEqual((self.dest / "report.pdf").read_bytes(), b"mine")
        (self.dest / "top").mkdir()  # 已有的空文件夹也不能被替换
        result, _, _ = self.extract(self.junk_tar())
        self.assertEqual(result, self.dest / "top 2")
        self.assertEqual(os.listdir(self.dest / "top"), [])

    def test_single_symlink_is_wrapped(self) -> None:
        """唯一的顶层项是链接时也包进文件夹，免得在目标里只放一个链接。"""
        self.require("tar")
        path = self.tar_with("l.tar", [self.info("only", tarfile.SYMTYPE, linkname="x")])
        result, _, _ = self.extract(path)
        self.assertEqual(result, self.dest / "l")
        self.assertTrue((result / "only").is_symlink())

    def test_only_junk_is_empty(self) -> None:
        self.require("tar")
        path = self.tar_with("junk.tar", [(m, b"j") for m in JUNK_MEMBERS])
        self.assert_fails(path, "extract_empty")
        self.assert_fails(self.tar_with("none.tar", []), "extract_empty")

    def test_wrong_extension_uses_content(self) -> None:
        """扩展名写错时按魔数路由：.tar.gz 实际是 zip 也能解开。"""
        self.require("tar")
        path = self.arch / "mislabeled.tar.gz"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("a.txt", "zip inside")
        result, _, _ = self.extract(path)
        self.assertEqual(result.read_bytes(), b"zip inside")


class SafetyTests(ExtractBase):
    def test_dotdot_member_fails_without_escaping(self) -> None:
        """解压根目录是 dest/.cleanpack-*.part/x，../ 若被执行会落在暂存目录里；
        assert_fails 已确认目标文件夹（含暂存目录）事后为空，这里再确认上一级也没有。"""
        self.require("tar")
        path = self.tar_with("evil.tar", [("ok.txt", b"ok"), ("../escape.txt", b"x"),
                                          ("../../escape2.txt", b"x")])
        self.assert_fails(path, "tool_failed")
        self.assertFalse((self.dest / "escape2.txt").exists())
        self.assertFalse((self.root / "escape2.txt").exists())

    def test_write_outside_root_is_unsafe_path(self) -> None:
        """模拟工具把内容写到解压根目录之外（暂存目录里多出别的项）：任务失败且不留下任何东西。"""
        self.require("tar")
        path = self.tar_with("a.tar", [("a.txt", b"a")])

        def stray(job: core.ExtractJob, family: str, root: Path) -> None:
            (root / "a.txt").write_bytes(b"a")
            (root.parent / "stray").write_bytes(b"outside x")

        with patch.object(core.ExtractJob, "_extract", stray):
            self.assert_fails(path, "unsafe_path")

    def test_symlink_chain_escape_is_warned(self) -> None:
        """s -> . 加上 up -> s/s/s/s/../../../..：字面规整看似留在原处，实际向上爬了四层。"""
        self.require("tar")
        path = self.tar_with("chain.tar", [
            ("d/readme.txt", b"r"),
            self.info("d/s", tarfile.SYMTYPE, linkname="."),
            self.info("d/up", tarfile.SYMTYPE, linkname="s/s/s/s/../../../..")])
        result, job, _ = self.extract(path)
        self.assertEqual({w.kw["name"] for w in job.warnings}, {"d/up"})
        self.assertNotIn(os.path.realpath(result), os.path.realpath(result / "up"))

    def test_links_judged_at_committed_location(self) -> None:
        """检查时链接还在暂存目录里，提交时会改名（例如「d 2」）或离开暂存目录。经由上级
        目录绕回来的链接在提交后会指到别处，甚至是用户已有的同名文件，必须警告。"""
        self.require("tar")
        (self.dest / "d").mkdir()
        (self.dest / "d" / "b").write_bytes(b"USERDATA")
        path = self.tar_with("one.tar", [
            ("d/b", b"theirs"),
            self.info("d/c", tarfile.SYMTYPE, linkname="../d/b"),
            self.info("d/a", tarfile.SYMTYPE, linkname="../../x/d/b"),
            self.info("d/sub", tarfile.SYMTYPE, linkname="./b")])
        result, job, _ = self.extract(path)
        self.assertEqual(result, self.dest / "d 2")
        self.assertEqual({w.kw["name"] for w in job.warnings}, {"d/c", "d/a"})
        path = self.tar_with("two.tar", [
            ("g", b"g"), ("h", b"h"),
            self.info("lnk", tarfile.SYMTYPE, linkname="../x/g"),
            self.info("lnk2", tarfile.SYMTYPE, linkname="../x")])
        result, job, _ = self.extract(path)
        self.assertEqual(result, self.dest / "two")
        self.assertEqual({w.kw["name"] for w in job.warnings}, {"lnk", "lnk2"})

    def test_link_bomb_is_fast(self) -> None:
        """Li -> L(i-1)/L(i-1) 的链接每层翻倍；按总预算解析，30 层也应瞬间完成且不误报。"""
        self.require("tar")
        members = [("top/f", b"f"), self.info("top/L0", tarfile.SYMTYPE, linkname=".")]
        members += [self.info(f"top/L{i}", tarfile.SYMTYPE, linkname=f"L{i - 1}/L{i - 1}")
                    for i in range(1, 31)]
        members.append(self.info("top/loop", tarfile.SYMTYPE, linkname="loop"))
        t0 = time.monotonic()
        result, job, _ = self.extract(self.tar_with("bomb.tar", members))
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(job.warnings, [])
        self.assertTrue((result / "L30").is_symlink())
        # 直接计时单个链接：缓存或共享预算都失效时，这里会在几秒内失败，而不是整个用例卡住。
        t0 = time.monotonic()
        self.assertFalse(core._link_escapes(str(result), str(result / "L20")))
        self.assertLess(time.monotonic() - t0, 1)

    def long_link_tar(self, count: int, chain: int = 30) -> Path:
        """同一目录里 count 个链接，目标都是 x/../ 重复两百次再接一条 chain 个链接的链路。
        chain 不超过 32 时系统能解析整条链路（macOS 的 MAXSYMLINKS）。"""
        hop = "x/../" * 200
        members = [self.info("top", tarfile.DIRTYPE, mode=0o755),
                   self.info("top/x", tarfile.DIRTYPE, mode=0o755)]
        members += [self.info(f"top/c{i}", tarfile.SYMTYPE, linkname=hop + f"c{i + 1}")
                    for i in range(1, chain + 1)]
        members += [self.info(f"top/L{i:05d}", tarfile.SYMTYPE, linkname=hop + "c1")
                    for i in range(count)]
        return self.tar_with("longlinks.tar", members)

    def count_islink(self) -> tuple[list[int], object]:
        """统计 core 解析链接时的 lstat 次数；用工作量而不是墙钟时间断言，机器繁忙时也稳定。"""
        calls = [0]
        real = os.path.islink

        def counting(path: str) -> bool:
            calls[0] += 1
            return real(path)
        return calls, patch.object(core.os.path, "islink", counting)

    def spy_checkers(self) -> tuple[list, object]:
        """记录 verify_tree 创建的 _LinkChecker，用来读出实际消耗的工作量（确定、不依赖机器快慢）。"""
        created: list = []
        real = core._LinkChecker

        class Spy(real):
            def __init__(self, boundary: str) -> None:
                super().__init__(boundary)
                self.initial = self.work
                created.append(self)
        return created, patch.object(core, "_LinkChecker", Spy)

    @staticmethod
    def work_used(checker: object) -> int:
        """实际消耗的工作量；与写死的数值比较，上限常量被改大时断言要能失败。"""
        return checker.initial - checker.work

    def test_deep_chained_positions_are_bounded(self) -> None:
        """每个链接指向前一个再往下走几百层（目录不存在）：位置越来越深，按深度计费后
        总开销仍有上限，不会随链接数平方增长。"""
        tree = self.root / "deep"
        tree.mkdir()
        down = "/x" * 505
        (tree / "o1").symlink_to(down[1:])
        for j in range(2, 31):
            (tree / f"o{j}").symlink_to(f"o{j - 1}" + down)
        joined = [0]
        real_join = os.path.join

        def counting_join(*parts: str) -> str:
            joined[0] += len(parts)
            return real_join(*parts)

        checker = core._LinkChecker(str(tree))
        with patch.object(core.os.path, "join", counting_join):
            for j in range(1, 31):
                checker.escapes(str(tree / f"o{j}"))
        # 不按深度计费时约 1.1 亿次分量拼接；计费后受总工作量上限约束。
        self.assertLess(joined[0], 4_000_000)

    def test_nested_budget_results_not_cached(self) -> None:
        """A 经过 33 个链接（超过 32）系统解析不了；但从 L32 出发只需 2 个链接就越界。
        A 的嵌套解析中途耗尽预算的结果不能被缓存，否则 L32 的越界会被漏报。"""
        tree = self.root / "chain"
        (tree / "c").mkdir(parents=True)
        (tree / "A").symlink_to("c/L1")
        for i in range(1, 33):
            (tree / "c" / f"L{i}").symlink_to(f"L{i + 1}")
        (tree / "c" / "L33").symlink_to("E")
        (tree / "c" / "E").symlink_to("../../..")
        checker = core._LinkChecker(str(tree))
        self.assertFalse(checker.escapes(str(tree / "A")))
        self.assertTrue(checker.escapes(str(tree / "c" / "L32")))
        self.assertTrue(checker.escapes(str(tree / "c" / "L20")))

    def test_many_long_links_resolve_once(self) -> None:
        """共享同一链路的大量链接只解析一次：工作量与链接数成线性，而不是每个都重走整条链路。"""
        self.require("tar")
        path = self.long_link_tar(2000)
        calls, patcher = self.count_islink()
        with patcher:
            _, job, _ = self.extract(path)
        # 有缓存时每个链接只走自己的目标（约 200 次），没有缓存时每个都重走整条 30 个链接的
        # 链路（约 6000 次），总处理量会触到上限并产生 links_unchecked 提示。
        self.assertLess(calls[0], 1_500_000)
        self.assertEqual(job.warnings, [])

    def test_overlong_chain_is_bounded(self) -> None:
        """超过 32 个链接的链路系统也解析不了。几千个经过它的链接可能各自重走一遍，
        但总处理量有硬上限：任务按时结束，最多合并成一条“无法检查”的提示。"""
        self.require("tar")
        path = self.long_link_tar(2000, chain=40)
        created, patcher = self.spy_checkers()
        with patcher:
            _, job, _ = self.extract(path)
        # 上限在每一步之前检查，最后一步最多超出一步的开销（几千）；上限被改成 300 万时这里会失败。
        self.assertLessEqual(self.work_used(created[0]), 2_000_000 + 20_000)
        self.assertTrue(all(w.key == "links_unchecked" for w in job.warnings), job.warnings)
        self.assertLessEqual(len(job.warnings), 1)

    def test_looping_link_bomb_is_bounded(self) -> None:
        """一个自我循环的长链接加上几千个指向它的链接：总处理量有上限，任务很快结束，
        并且只合并成一条“无法检查”的提示，而不是逐个刷屏或误报越界。"""
        self.require("tar")
        hop = "x/../" * 200
        members = [self.info("top", tarfile.DIRTYPE, mode=0o755),
                   self.info("top/x", tarfile.DIRTYPE, mode=0o755),
                   self.info("top/S", tarfile.SYMTYPE, linkname=hop + "S")]
        members += [self.info(f"top/m{i:05d}", tarfile.SYMTYPE, linkname="S")
                    for i in range(3000)]
        created, patcher = self.spy_checkers()
        with patcher:
            _, job, _ = self.extract(self.tar_with("loopbomb.tar", members))
        # 上限在每一步之前检查，最后一步最多超出一步的开销（几千）；上限被改成 300 万时这里会失败。
        self.assertLessEqual(self.work_used(created[0]), 2_000_000 + 20_000)
        self.assertTrue(all(w.key == "links_unchecked" for w in job.warnings), job.warnings)
        self.assertLessEqual(len(job.warnings), 1)

    def test_budget_exhaustion_is_not_cached(self) -> None:
        """单个链接处理量超限只影响它自己；链路上的其他链接仍按各自的真实结果判断。"""
        tree = self.root / "tree"
        (tree / "c").mkdir(parents=True)
        (tree / "c" / "f").write_bytes(b"f")
        pad = "./" * 510
        (tree / "c" / "Z").symlink_to("f")
        (tree / "c" / "Y").symlink_to("Z")
        (tree / "c" / "P30").symlink_to(pad + "Y")
        for i in range(29, 0, -1):
            (tree / "c" / f"P{i}").symlink_to(pad + f"P{i + 1}")
        (tree / "c" / "W").symlink_to("Z")
        (tree / "A").symlink_to("c/" + "./" * 509 + "P1")
        # 从 A 出发整条链约 15800 个分量；把单链接上限调到 8000，让 A 在链路中途超限，
        # 而从 P29、W 等较短的位置出发都在上限内。
        with patch.object(core, "_LINK_PARTS_MAX", 8000):
            checker = core._LinkChecker(str(tree))
            self.assertIsNone(checker.escapes(str(tree / "A")))
            for name in ("W", "Y", "P29"):
                self.assertFalse(checker.escapes(str(tree / "c" / name)), name)

    def test_cancel_during_link_checks(self) -> None:
        """检查链接时取消要立即生效：每检查一项就看一次取消标记。"""
        self.require("tar")
        path = self.long_link_tar(300)
        job = core.ExtractJob(path, self.dest, log=silent)
        calls = []

        def cancel_on_first(checker: core._LinkChecker, link: str) -> bool | None:
            calls.append(link)
            if len(calls) == 1:
                job.cancel()
            return real(checker, link)

        real = core._LinkChecker.escapes
        with patch.object(core._LinkChecker, "escapes", cancel_on_first):
            with self.assertRaises(core.Cancelled):
                job.run()
        self.assertLessEqual(len(calls), 2)
        self.assert_dest_untouched()

    def test_link_leaving_single_item_is_warned(self) -> None:
        """只有一个顶层文件夹时它被直接放进目标；指向它外面的相对链接也要警告。"""
        self.require("tar")
        path = self.tar_with("app.tar", [
            ("app/main.txt", b"m"),
            self.info("app/settings", tarfile.SYMTYPE, linkname="../secret.txt")])
        result, job, _ = self.extract(path)
        self.assertEqual(result, self.dest / "app")
        self.assertEqual([(w.key, w.kw["name"]) for w in job.warnings],
                         [("link_outside", "app/settings")])

    def test_write_through_symlink_fails(self) -> None:
        self.require("tar")
        outside = self.root / "outside"
        outside.mkdir()
        path = self.tar_with("through.tar", [
            self.info("link", tarfile.SYMTYPE, linkname=str(outside)),
            ("link/pwned.txt", b"x")])
        self.assert_fails(path, "tool_failed")
        self.assertEqual(os.listdir(outside), [])

    def test_absolute_member_stays_inside(self) -> None:
        self.require("tar")
        target = f"/tmp/cleanpack-xtest-abs-{os.getpid()}.txt"
        path = self.tar_with("abs.tar", [(target, b"abs")])
        result, _, _ = self.extract(path)
        self.assertFalse(os.path.lexists(target))
        self.assertEqual(nfc_tree(result)[target.lstrip("/").split("/", 1)[1]], b"abs")

    def test_outside_links_kept_with_warnings(self) -> None:
        self.require("tar")
        path = self.tar_with("links.tar", [
            ("top/a.txt", b"a"),
            self.info("top/in", tarfile.SYMTYPE, linkname="a.txt"),
            self.info("top/up", tarfile.SYMTYPE, linkname="../../etc"),
            self.info("top/abs", tarfile.SYMTYPE, linkname="/etc/hosts")])
        result, job, _ = self.extract(path)
        self.assertEqual(os.readlink(result / "up"), "../../etc")
        self.assertEqual(os.readlink(result / "abs"), "/etc/hosts")
        warned = {w.kw["name"] for w in job.warnings if w.key == "link_outside"}
        self.assertEqual(warned, {"top/up", "top/abs"})

    def test_fifo_fails(self) -> None:
        self.require("tar")
        path = self.tar_with("fifo.tar", [("a.txt", b"a"), self.info("p", tarfile.FIFOTYPE)])
        self.assert_fails(path, "unsafe_entry")

    @unittest.skipIf(os.geteuid() == 0, "root 能创建设备文件")
    def test_device_entry_is_not_a_privacy_problem(self) -> None:
        """普通用户不能创建设备文件，bsdtar 报 Operation not permitted 并失败。这与隐私保护
        无关，不能提示用户去系统设置里授权。"""
        self.require("tar")
        device = self.info("top/null", tarfile.CHRTYPE)
        device.devmajor, device.devminor = 3, 2
        path = self.tar_with("dev.tar", [("top/a.txt", b"a"), device])
        with self.assertRaises(core.JobError) as caught:
            core.ExtractJob(path, self.dest, log=silent).run()
        self.assertEqual((caught.exception.key, caught.exception.denial), ("tool_failed", None))
        self.assert_dest_untouched()

    def test_locked_dirs_are_committed(self) -> None:
        """目录条目在前、内容在后，bsdtar 会先建出 000/0500 的目录（实测文件仍能写入，
        目录权限在结束时才设置）；补上 u+rwx 后整理和提交都要成功。"""
        self.require("tar")
        path = self.tar_with("perm.tar", [
            self.info("top", tarfile.DIRTYPE, mode=0o755),
            self.info("top/locked", tarfile.DIRTYPE, mode=0o000),
            ("top/locked/f.txt", b"f"),
            self.info("top/ro", tarfile.DIRTYPE, mode=0o500),
            ("top/ro/g.txt", b"g")])
        result, _, _ = self.extract(path)
        self.assertEqual((result / "locked" / "f.txt").read_bytes(), b"f")
        self.assertEqual((result / "ro" / "g.txt").read_bytes(), b"g")

    def test_locked_dirs_cleaned_up_on_failure(self) -> None:
        """失败时暂存目录里有 000 的目录也要删干净。"""
        self.require("tar")
        path = self.tar_with("perm-fail.tar", [
            self.info("top", tarfile.DIRTYPE, mode=0o755),
            self.info("top/locked", tarfile.DIRTYPE, mode=0o000),
            ("top/locked/f.txt", b"f"),
            self.info("top/p", tarfile.FIFOTYPE)])
        self.assert_fails(path, "unsafe_entry")

    def test_verify_tree_clears_setuid(self) -> None:
        """bsdtar 的 --no-same-permissions 通常已去掉 setuid；这是不依赖工具的最后一道检查。"""
        tree = self.root / "tree"
        tree.mkdir()
        f = tree / "suid"
        f.write_bytes(b"x")
        f.chmod(0o4755)
        self.assertTrue(f.stat().st_mode & stat.S_ISUID)
        core.verify_tree(tree)
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o755)

    def test_in_tree_hardlink_allowed(self) -> None:
        self.require("tar")
        path = self.tar_with("hl.tar", [("top/a.txt", b"same"),
                                        self.info("top/b.txt", tarfile.LNKTYPE, linkname="top/a.txt")])
        result, _, _ = self.extract(path)
        self.assertTrue((result / "a.txt").samefile(result / "b.txt"))

    def test_verify_tree_rejects_outside_hardlink(self) -> None:
        """工具本身会拒绝越界硬链接；这里直接构造树，验证最后一道检查。"""
        tree = self.root / "tree"
        tree.mkdir()
        outside = self.root / "outside.txt"
        outside.write_bytes(b"o")
        os.link(outside, tree / "h")
        with self.assertRaises(core.JobError) as caught:
            core.verify_tree(tree)
        self.assertEqual(caught.exception.key, "unsafe_link")


class FailureTests(ExtractBase):
    def make_zip_dir(self) -> Path:
        d = self.root / "zsrc" / "dir"
        d.mkdir(parents=True)
        (d / "s.txt").write_bytes(b"secret contents")
        return d

    def test_encrypted_zip_fails_fast(self) -> None:
        self.require("tar", "zip")
        d = self.make_zip_dir()
        path = self.arch / "enc.zip"
        subprocess.run(["zip", "-q", "-P", "pw", "-r", str(path), d.name], cwd=d.parent,
                       check=True, env=core.ENV, stdin=subprocess.DEVNULL)
        t0 = time.monotonic()
        job = self.assert_fails(path, "archive_encrypted")
        self.assertLess(time.monotonic() - t0, 15)
        self.assertIsInstance(job, core.ExtractJob)

    def test_encrypted_zip_detected_before_extracting(self) -> None:
        """加密 zip 在解压前就按中央目录的标志位识别，不拿假密码去试：传统加密约有 1/256 的
        机会把假密码当成正确的，解出乱码后报 CRC 错误。这里连续生成多个加密 zip，每个都
        必须报“已加密”，并且根本不调用 tar。"""
        self.require("tar", "zip")
        d = self.make_zip_dir()
        for i in range(30):
            path = self.arch / f"enc{i}.zip"
            subprocess.run(["zip", "-q", "-P", f"pw{i}", "-r", str(path), d.name], cwd=d.parent,
                           check=True, env=core.ENV, stdin=subprocess.DEVNULL)
            lines: list[str] = []
            with self.assertRaises(core.JobError) as caught:
                core.ExtractJob(path, self.dest, log=lines.append).run()
            self.assertEqual(caught.exception.key, "archive_encrypted")
            self.assertEqual(lines, [])
        self.assert_dest_untouched()

    def test_prefixed_encrypted_zip_under_other_name(self) -> None:
        """开头有额外数据的加密 zip（例如自解压程序）存成 .tar.gz：没有魔数，按扩展名路由，
        仍要在解压前识别为加密，而不是交给 bsdtar 拿假密码去试。"""
        self.require("tar", "zip")
        d = self.make_zip_dir()
        plain = self.arch / "inner.zip"
        subprocess.run(["zip", "-q", "-P", "pw", "-r", str(plain), d.name], cwd=d.parent,
                       check=True, env=core.ENV, stdin=subprocess.DEVNULL)
        for name in ("sfx.tar.gz", "sfx.7z"):
            path = self.arch / name
            path.write_bytes(b"#!/bin/sh\n" + b"S" * 320 + plain.read_bytes())
            lines: list[str] = []
            with self.assertRaises(core.JobError) as caught:
                core.ExtractJob(path, self.dest, log=lines.append).run()
            self.assertEqual(caught.exception.key, "archive_encrypted", name)
            self.assertEqual(lines, [], name)

    def test_encrypted_flag_on_directories_only(self) -> None:
        """只有目录项带加密标志时没有需要密码的数据，照常解压。"""
        self.require("tar")
        path = self.arch / "dirflag.zip"
        with zipfile.ZipFile(path, "w") as z:
            info = zipfile.ZipInfo("d/")
            info.external_attr = 0o40755 << 16 | 0x10
            z.writestr(info, b"")
            z.writestr("d/a.txt", "a")
        # zipfile 写入时不保留加密标志，直接改中央目录和本地头里目录项的标志位。
        raw = bytearray(path.read_bytes())
        with path.open("rb") as stream:
            entry = next(e for e in core._read_central_directory(stream) if e.name == b"d/")
        for offset in (entry.central + 8, entry.local + 6):
            raw[offset] |= 0x1
        path.write_bytes(bytes(raw))
        with zipfile.ZipFile(path) as z:
            self.assertTrue(z.getinfo("d/").flag_bits & 0x1)  # 确认标志确实写进去了
        self.assertFalse(core.zip_is_encrypted(path))
        result, _, _ = self.extract(path)
        self.assertEqual((result / "a.txt").read_bytes(), b"a")

    def test_encrypted_zip_with_colon_member_name(self) -> None:
        """成员名含「 : 」时也要识别出加密，不能被成员名把提示挡掉。"""
        self.require("tar", "zip")
        d = self.root / "colon"
        d.mkdir()
        (d / "notes : 2026.txt").write_bytes(b"secret")
        path = self.arch / "colon.zip"
        subprocess.run(["zip", "-q", "-P", "pw", str(path), "notes : 2026.txt"], cwd=d,
                       check=True, env=core.ENV, stdin=subprocess.DEVNULL)
        self.assert_fails(path, "archive_encrypted")

    def test_deep_tree_within_fd_limit(self) -> None:
        """从 Finder 启动的应用只有 256 个文件描述符；300 层的目录树也要能整理和提交。"""
        self.require("tar")
        import resource
        name = "top/" + "/".join(["d"] * 300)
        path = self.tar_with("deep300.tar", [(name + "/f.txt", b"f")])
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        try:
            result, _, _ = self.extract(path)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        self.assertEqual((result / "/".join(["d"] * 300) / "f.txt").read_bytes(), b"f")

    def test_deep_tree_failure_cleans_up_within_fd_limit(self) -> None:
        """256 个描述符的上限下，300 层的树解压后因 FIFO 失败，暂存目录也要删干净。"""
        self.require("tar")
        import resource
        name = "top/" + "/".join(["d"] * 300)
        path = self.tar_with("deepfifo.tar", [(name + "/f.txt", b"f"),
                                              self.info("top/fifo", tarfile.FIFOTYPE)])
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        try:
            self.assert_fails(path, "unsafe_entry")
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

    def test_encrypted_7z_fails(self) -> None:
        self.require("tar", "7z")
        d = self.make_zip_dir()
        for name, extra in (("enc.7z", []), ("hdr.7z", ["-mhe=on"])):
            with self.subTest(name=name):
                path = self.arch / name
                subprocess.run([core.locate("7z"), "a", "-pS", *extra, "-bso0", "-bsp0",
                                str(path), str(d)], check=True, env=core.ENV,
                               stdin=subprocess.DEVNULL)
                self.assert_fails(path, "archive_encrypted")

    def test_truncated_archives_leave_nothing(self) -> None:
        self.require("tar", "gzip", "zstd")
        src = self.root / "big"
        src.mkdir()
        (src / "data.bin").write_bytes(os.urandom(300_000))
        for fmt in ("tar.gz", "tar.zst", "zip"):
            with self.subTest(fmt=fmt):
                out = self.arch / ("t" + core.FORMATS[fmt].ext)
                core.ArchiveJob(src, out, fmt, log=silent).run()
                data = out.read_bytes()
                out.write_bytes(data[: len(data) // 2])
                self.assert_fails(out, "tool_failed")

    def test_7z_fallback_for_unsupported_codec(self) -> None:
        """bsdtar 解不了 ARM64+PPMd 的 7z 时，改用 7-Zip 重新解压。"""
        self.require("tar", "7z")
        src = self.root / "bin"
        src.mkdir()
        subprocess.run(["lipo", "-thin", "arm64e", "/bin/ls", "-output", str(src / "ls")],
                       check=True)
        path = self.arch / "ppmd.7z"
        subprocess.run([core.locate("7z"), "a", "-m0=PPMd", "-bso0", "-bsp0", str(path),
                        str(src)], check=True, env=core.ENV, stdin=subprocess.DEVNULL)
        result, _, lines = self.extract(path)
        self.assertIn("→ 7z", lines)
        self.assertEqual((result / "ls").read_bytes(), (src / "ls").read_bytes())

    def deflate64_7z(self, name: str, src: Path) -> Path:
        """用 Deflate64 生成 7z：bsdtar 不支持这种编码，会触发 7-Zip 后备。"""
        path = self.arch / name
        subprocess.run([core.locate("7z"), "a", "-m0=Deflate64", "-snl", "-bso0", "-bsp0",
                        "-spd", str(path), str(src)], check=True, env=core.ENV,
                       stdin=subprocess.DEVNULL)
        return path

    def test_7z_fallback_ignores_wildcards_in_name(self) -> None:
        """名为 a*.7z 的归档不能把旁边匹配的 ab.7z 一起解开。"""
        self.require("tar", "7z")
        one, two = self.root / "one", self.root / "two"
        one.mkdir()
        two.mkdir()
        (one / "f").write_bytes(b"1")
        (two / "g").write_bytes(b"2")
        star = self.deflate64_7z("a*.7z", one)
        self.deflate64_7z("ab.7z", two)
        result, _, lines = self.extract(star)
        self.assertIn("→ 7z", lines)
        self.assertEqual(result, self.dest / "one")
        self.assertEqual(sorted(os.listdir(self.dest)), ["one"])

    def test_7z_fallback_keeps_absolute_links(self) -> None:
        """7-Zip 会把绝对链接改写到输出目录之下；提交前恢复成原目标并给出警告。"""
        self.require("tar", "7z")
        src = self.root / "proj"
        src.mkdir()
        (src / "a.txt").write_bytes(b"a")
        (src / "abs").symlink_to("/etc/hosts")
        (src / "rel").symlink_to("a.txt")
        result, job, lines = self.extract(self.deflate64_7z("links.7z", src))
        self.assertIn("→ 7z", lines)
        self.assertEqual(os.readlink(result / "abs"), "/etc/hosts")
        self.assertEqual(os.readlink(result / "rel"), "a.txt")
        self.assertEqual([w.kw for w in job.warnings if w.key == "link_outside"],
                         [{"name": "proj/abs", "target": "/etc/hosts"}])

    def test_7z_without_sevenzip_explains(self) -> None:
        """bsdtar 读不了且没装 7-Zip 时，明确提示安装 7-Zip。"""
        self.require("tar", "7z")
        src = self.root / "p"
        src.mkdir()
        (src / "a.txt").write_bytes(b"a")
        path = self.deflate64_7z("d64.7z", src)
        real_locate = core.locate
        with patch.object(core, "locate", lambda k: None if k == "7z" else real_locate(k)):
            self.assert_fails(path, "archive_needs_7z")

    def test_member_names_do_not_look_encrypted(self) -> None:
        """成员名含 Encrypted、passphrase 的未加密归档失败时，报告真正的原因而不是“已加密”。"""
        self.require("tar", "7z")
        path = self.tar_with("names.tar", [("ok/a.txt", b"a"),
                                           ("../Encrypted Volumes/b.txt", b"b"),
                                           ("../Incorrect passphrase", b"c")])
        self.assert_fails(path, "tool_failed")
        src = self.root / "d"
        src.mkdir()
        (src / "passphrase.txt").write_bytes(b"p")
        (src / "esc").symlink_to("../../outside")  # 7-Zip 拒绝越界相对链接并以 2 退出
        self.assert_fails(self.deflate64_7z("pp.7z", src), "tool_failed")

    def zst(self, name: str, raw: bytes) -> Path:
        src = self.root / (name + ".raw")
        src.write_bytes(raw)
        path = self.arch / name
        subprocess.run([core.locate("zstd"), "-q", "-f", "-o", str(path), str(src)], check=True,
                       env=core.ENV, stdin=subprocess.DEVNULL)
        return path

    @staticmethod
    def tar_bytes(name: str, data: bytes) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    def test_tar_zst_trailing_data_and_corruption(self) -> None:
        """结束标记之后的数据按标准 tar 的做法忽略（零填充、对不齐的尾巴都可以）；
        但 zstd 流本身损坏时，即使损坏处在 tar 结束标记之后，也必须失败。"""
        self.require("tar", "zstd")
        data = os.urandom(200_000)
        padded = self.zst("pad.tar.zst", self.tar_bytes("t/a.bin", data) + b"\0" * 8_192_000)
        result, _, _ = self.extract(padded)
        self.assertEqual((result / "a.bin").read_bytes(), data)
        odd = self.zst("odd.tar.zst", self.tar_bytes("o/a.txt", b"a") + b"\0" * 700 + b"tail")
        result, _, _ = self.extract(odd)
        self.assertEqual((result / "a.txt").read_bytes(), b"a")

        # 一处落在成员数据里，一处落在压缩流末尾（填充区和帧校验和）：tar 读到结束标记
        # 就会退出，后一种损坏只有完整校验 zstd 流才能发现。
        for label, offset in (("member", len(padded.read_bytes()) // 2),
                              ("tail", len(padded.read_bytes()) - 30)):
            with self.subTest(where=label):
                corrupt = bytearray(padded.read_bytes())
                corrupt[offset] ^= 0xFF
                bad = self.arch / f"corrupt-{label}.tar.zst"
                bad.write_bytes(bytes(corrupt))
                before = set(os.listdir(self.dest))
                with self.assertRaises(core.JobError) as caught:
                    core.ExtractJob(bad, self.dest, log=silent).run()
                self.assertEqual((caught.exception.key, caught.exception.kw.get("tool")),
                                 ("tool_failed", "zstd"))
                self.assertEqual(set(os.listdir(self.dest)), before)

    def test_zst_with_non_tar_content_blames_tar(self) -> None:
        """zstd 流完好但内容不是 tar：报告 tar 的失败，而不是被 SIGPIPE 杀死的 zstd。"""
        self.require("tar", "zstd")
        path = self.zst("notar.tar.zst", os.urandom(2_000_000))
        job = core.ExtractJob(path, self.dest, log=silent)
        with self.assertRaises(core.JobError) as caught:
            job.run()
        self.assertEqual(caught.exception.kw.get("tool"), "tar")

    def assert_fails_keep(self, archive: Path, key: str) -> None:
        """与 assert_fails 相同，但允许目标文件夹里已有之前用例放进去的结果。"""
        before = set(os.listdir(self.dest))
        with self.assertRaises(core.JobError) as caught:
            core.ExtractJob(archive, self.dest, log=silent).run()
        self.assertEqual(caught.exception.key, key)
        self.assertEqual(set(os.listdir(self.dest)), before)

    def test_very_deep_tree_is_cleaned_up(self) -> None:
        """成员路径超过 PATH_MAX 且含只读目录时，失败后暂存目录仍要删干净。"""
        self.require("tar")
        parts = [f"d{i:02d}_" + "0" * 60 for i in range(20)]  # 约 1300 字符
        members = [self.info("top", tarfile.DIRTYPE, mode=0o755)]
        for i in range(1, len(parts) + 1):
            # 每一层都只读：清理时必须逐层补权限，路径又超过 PATH_MAX，只能按目录描述符操作。
            members.append(self.info("top/" + "/".join(parts[:i]), tarfile.DIRTYPE, mode=0o555))
        members.append(("top/" + "/".join(parts) + "/f.txt", b"f"))
        path = self.arch / "deep.tar"
        with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tf:
            for m in members:
                if isinstance(m, tuple):
                    info = tarfile.TarInfo(m[0])
                    info.size = len(m[1])
                    tf.addfile(info, io.BytesIO(m[1]))
                else:
                    tf.addfile(m)
        job = core.ExtractJob(path, self.dest, log=silent)
        try:
            job.run()
        except core.JobError:
            pass
        leftovers = [n for n in os.listdir(self.dest) if n.startswith(".cleanpack-")]
        self.assertEqual(leftovers, [])

    def test_cancel_during_quarantine(self) -> None:
        """写隔离属性的 xattr 是登记过的进程，取消时会被结束，不必等它跑完。"""
        self.require("tar")
        sleeper = self.root / "slow-xattr"
        # exec：让被登记的进程就是 sleep 本身，和真实的 xattr 一样只有一个进程。否则取消只结束
        # sh，子进程 sleep 仍占着输出管道，读输出的一方要等它睡完（较慢的机器上必现）。
        sleeper.write_text("#!/bin/sh\nexec sleep 30\n")
        sleeper.chmod(0o755)
        path = self.tar_with("q.tar", [("top/a.txt", b"a")])
        job = core.ExtractJob(path, self.dest, log=silent)
        outcome: list[BaseException] = []

        def run() -> None:
            try:
                job.run()
            except BaseException as exc:  # noqa: BLE001
                outcome.append(exc)

        with patch.object(core, "read_quarantine", lambda p: "0081;1;Test;"), \
                patch.object(core, "_XATTR", str(sleeper)):
            import threading
            t = threading.Thread(target=run)
            t.start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not any(
                    p.args[0] == str(sleeper) for p in list(job._procs)):
                time.sleep(0.02)
            t0 = time.monotonic()
            job.cancel()
            t.join(10)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIsInstance(outcome[0], core.Cancelled)
        self.assert_dest_untouched()

    def test_xattr_start_failure_only_warns(self) -> None:
        """xattr 启动失败时只提示隔离属性没写上，解压结果照常提交。"""
        self.require("tar")
        path = self.tar_with("q.tar", [("top/a.txt", b"a")])
        with patch.object(core, "read_quarantine", lambda p: "0081;1;Test;"), \
                patch.object(core, "_XATTR", str(self.root / "no-such-xattr")):
            result, job, _ = self.extract(path)
        self.assertEqual((result / "a.txt").read_bytes(), b"a")
        self.assertEqual([w.key for w in job.warnings], ["quarantine_failed"])

    def test_cleanup_when_hard_fd_limit_is_low(self) -> None:
        """硬上限也只有 256 时无法提高软上限，深层树会失败；暂存目录仍要删干净。
        降低硬上限后本进程无法再提高，所以在子进程里运行。"""
        self.require("tar")
        name = "top/" + "/".join(["d"] * 400)
        path = self.tar_with("deep400.tar", [(name + "/f.txt", b"f")])
        script = (
            "import resource, sys\n"
            f"sys.path.insert(0, {str(Path(core.__file__).parent)!r})\n"
            "import core\n"
            "resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))\n"
            "try:\n"
            f"    core.ExtractJob({str(path)!r}, {str(self.dest)!r}, log=lambda _: None).run()\n"
            "    print('ok')\n"
            "except core.JobError as exc:\n"
            "    print('failed', exc.key)\n")
        r = subprocess.run([os.sys.executable, "-B", "-c", script], capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([n for n in os.listdir(self.dest) if n.startswith(".cleanpack-")],
                         r.stdout)

    def test_missing_and_invalid_archives(self) -> None:
        self.assert_fails(self.root / "nope.zip", "src_missing")
        (self.root / "folder.zip").mkdir()
        self.assert_fails(self.root / "folder.zip", "archive_not_file")
        (self.root / "notes.txt").write_text("x")
        self.assert_fails(self.root / "notes.txt", "archive_unsupported")

    def test_destination_must_exist(self) -> None:
        self.require("tar")
        path = self.tar_with("a.tar", [("a.txt", b"a")])
        job = core.ExtractJob(path, self.root / "missing-dest", log=silent)
        with self.assertRaises(core.JobError) as caught:
            job.run()
        self.assertEqual(caught.exception.key, "out_dir_missing")

    def test_cancel_during_extraction(self) -> None:
        self.require("tar")
        path = self.tar_with("a.tar", [("a.txt", b"a")])
        job = core.ExtractJob(path, self.dest, log=silent)
        sleeper = [os.sys.executable, "-c", "import time; time.sleep(30)"]
        real_spawn = core.Job._spawn

        def slow_spawn(this: core.Job, cmd: list[str], **kw: object):
            return real_spawn(this, sleeper, **kw)

        with patch.object(core.Job, "_spawn", slow_spawn):
            import threading
            outcome: list[BaseException] = []
            t = threading.Thread(target=lambda: self._run_catch(job, outcome))
            t.start()
            deadline = time.monotonic() + 10
            while not job._procs and time.monotonic() < deadline:
                time.sleep(0.02)
            job.cancel()
            t.join(15)
        self.assertIsInstance(outcome[0], core.Cancelled)
        self.assert_dest_untouched()

    @staticmethod
    def _run_catch(job: core.ExtractJob, outcome: list[BaseException]) -> None:
        try:
            job.run()
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)


class ExFatTests(ExtractBase):
    """在 exFAT 磁盘映像上解压：系统用 ._ 文件保存属性，不能误判为越界，也不能留下垃圾。"""

    def setUp(self) -> None:
        super().setUp()
        image = self.root / "ex.dmg"
        r = subprocess.run(["hdiutil", "create", "-size", "20m", "-fs", "ExFAT", "-volname",
                            "CPX", str(image)], capture_output=True, stdin=subprocess.DEVNULL)
        if r.returncode:
            self.skipTest("无法创建 exFAT 映像")
        self.mount = self.root / "mnt"
        self.mount.mkdir()
        r = subprocess.run(["hdiutil", "attach", "-nobrowse", "-mountpoint", str(self.mount),
                            str(image)], capture_output=True, stdin=subprocess.DEVNULL)
        if r.returncode:
            self.skipTest("无法挂载 exFAT 映像")
        self.addCleanup(subprocess.run, ["hdiutil", "detach", "-force", str(self.mount)],
                        capture_output=True, stdin=subprocess.DEVNULL)
        self.dest = self.mount / "dest"
        self.dest.mkdir()

    def test_extract_to_exfat(self) -> None:
        self.require("tar")
        path = self.tar_with("x.tar", [("top/a.txt", b"a"), ("top/sub/b.txt", b"b")])
        subprocess.run(["/usr/bin/xattr", "-w", "com.apple.quarantine", "0081;1;Safari;",
                        str(path)], check=True)
        result, job, _ = self.extract(path)
        self.assertEqual((result / "a.txt").read_bytes(), b"a")
        junk = [rel for rel in nfc_tree(result) if any(core.is_junk_name(p)
                                                       for p in rel.split("/"))]
        self.assertEqual(junk, [])
        self.assertFalse([n for n in os.listdir(self.dest) if n.startswith(".cleanpack-")])
        self.assertEqual([w.key for w in job.warnings], ["quarantine_skipped"])

    def test_extract_to_exfat_without_provenance(self) -> None:
        """进程不带 com.apple.provenance 时系统不会建 ._x（GitHub 运行器上就是这样）：
        仍要识别出这个卷只能用 ._ 文件保存属性，跳过隔离属性，不留下 ._ 文件。"""
        self.require("tar")
        path = self.tar_with("x.tar", [("top/a.txt", b"a"), ("top/sub/b.txt", b"b")])
        subprocess.run(["/usr/bin/xattr", "-w", "com.apple.quarantine", "0081;1;Safari;",
                        str(path)], check=True)
        original = core.ExtractJob._copy_quarantine

        def without_sidecar(job: core.ExtractJob, root: Path, value: str,
                            staging: Path) -> None:
            for name in os.listdir(staging):
                if name.startswith("._"):
                    os.unlink(staging / name)
            original(job, root, value, staging)

        with patch.object(core.ExtractJob, "_copy_quarantine", without_sidecar):
            result, job, _ = self.extract(path)
        junk = [rel for rel in nfc_tree(result) if any(core.is_junk_name(p)
                                                       for p in rel.split("/"))]
        self.assertEqual(junk, [])
        self.assertEqual([w.key for w in job.warnings], ["quarantine_skipped"])

    def test_sidecar_probe(self) -> None:
        """exFAT 上探测为是，探测文件和它的 ._ 文件都不留下。"""
        before = sorted(os.listdir(self.dest))
        self.assertTrue(core.uses_sidecar_xattrs(self.dest))
        self.assertEqual(sorted(os.listdir(self.dest)), before)

    def test_wrapper_folder_leaves_no_sidecar_on_exfat(self) -> None:
        """多个顶层项包进文件夹时，暂存目录的 ._x 不能随改名变成目标里的 ._名称。"""
        self.require("tar")
        path = self.tar_with("multi.tar", [("proj/a.txt", b"a"), ("top2.txt", b"t")])
        result, _, _ = self.extract(path)
        self.assertEqual(result, self.dest / "multi")
        self.assertEqual(sorted(os.listdir(self.dest)), ["multi"])


class QuarantineTests(ExtractBase):
    def test_sidecar_probe_on_apfs(self) -> None:
        """/tmp 所在的 APFS 卷原生支持扩展属性：探测为否，不留下探测文件。"""
        self.assertFalse(core.uses_sidecar_xattrs(self.dest))
        self.assertEqual(os.listdir(self.dest), [])

    def test_quarantine_copied_without_following_links(self) -> None:
        self.require("tar")
        outside = self.root / "outside.txt"
        outside.write_bytes(b"o")
        path = self.tar_with("q.tar", [
            ("top/a.txt", b"a"), self.info("top/ro.txt", tarfile.REGTYPE, mode=0o444),
            self.info("top/out", tarfile.SYMTYPE, linkname=str(outside))])
        value = "0081;65000000;Safari;"
        subprocess.run(["/usr/bin/xattr", "-w", "com.apple.quarantine", value, str(path)],
                       check=True)
        result, job, _ = self.extract(path)
        for rel in ("", "a.txt", "ro.txt"):
            self.assertEqual(core.read_quarantine(result / rel), value, rel)
        self.assertIsNone(core.read_quarantine(outside))
        self.assertEqual(stat.S_IMODE((result / "ro.txt").stat().st_mode) & 0o222, 0)
        self.assertNotIn("quarantine_failed", [w.key for w in job.warnings])

    def test_no_quarantine_when_archive_has_none(self) -> None:
        self.require("tar")
        result, _, _ = self.extract(self.tar_with("n.tar", [("a.txt", b"a")]))
        self.assertIsNone(core.read_quarantine(result))


class HelperTests(ExtractBase):
    def test_detect_and_sniff(self) -> None:
        self.assertEqual(core.detect_format("A.TAR.GZ"), "tar.gz")
        self.assertEqual(core.detect_format("x.7Z"), "7z")
        self.assertIsNone(core.detect_format(".zip"))
        self.assertIsNone(core.detect_format("x.tgz"))
        samples = {"gzip": b"\x1f\x8b\x08", "xz": b"\xfd7zXZ\x00", "zstd": b"\x28\xb5\x2f\xfd",
                   "7z": b"7z\xbc\xaf\x27\x1c", "zip": b"PK\x03\x04", }
        for family, head in samples.items():
            p = self.root / family
            p.write_bytes(head + b"\0" * 300)
            self.assertEqual(core.sniff_family(p), family)
        tar = self.tar_with("s.tar", [("a", b"a")])
        self.assertEqual(core.sniff_family(tar), "tar")
        (self.root / "plain").write_bytes(b"hello")
        self.assertIsNone(core.sniff_family(self.root / "plain"))

    def test_encryption_messages(self) -> None:
        """加密提示的识别：用工具的真实输出行；成员名既不能伪造提示，也不能挡住提示。
        （加密 zip 已在解压前识别，这里覆盖 7z 等仍依赖工具输出的情况。）"""
        cases = [
            ("tar", "notes : 2026.txt: Incorrect passphrase: Unknown error: -1", True),
            ("tar", "tar: The archive header is encrypted, but currently not supported", True),
            ("tar", "proj/ls: The file content is encrypted, but currently not supported: "
                    "Unknown error: -1", True),
            ("tar", "../Incorrect passphrase: Path contains '..': Unknown error: -1", False),
            ("tar", "../Encrypted Volumes/b.txt: Path contains '..': Unknown error: -1", False),
            ("7z", "ERROR: Data Error in encrypted file. Wrong password? : e/s.txt", True),
            ("7z", "Cannot open encrypted archive. Wrong password?", True),
            ("7z", "- d/passphrase.txt", False),
            ("7z", "ERROR: Dangerous link path was ignored : d/Wrong password : ../x", False),
        ]
        for tool, line, expected in cases:
            self.assertEqual(core._mentions_encryption(line, tool), expected, line)

    def test_unique_names(self) -> None:
        it = core.unique_names(self.dest, "report.pdf", is_dir=False)
        self.assertEqual([next(it).name for _ in range(3)],
                         ["report.pdf", "report 2.pdf", "report 3.pdf"])
        it = core.unique_names(self.dest, "v1.2", is_dir=True)
        self.assertEqual([next(it).name for _ in range(2)], ["v1.2", "v1.2 2"])
        it = core.unique_names(self.dest, ".hidden", is_dir=False)
        self.assertEqual([next(it).name for _ in range(2)], [".hidden", ".hidden 2"])

    def test_rename_noreplace(self) -> None:
        src = self.root / "staged"
        src.mkdir()
        (self.dest / "Out").mkdir()
        for name in ("Out", "out", unicodedata.normalize("NFD", "Out")):
            with self.assertRaises(FileExistsError):
                core.rename_noreplace(src, self.dest / name)
        self.assertTrue(src.is_dir())
        core.rename_noreplace(src, self.dest / "fresh")
        self.assertTrue((self.dest / "fresh").is_dir())

    def test_rename_noreplace_fallback(self) -> None:
        """不支持 RENAME_EXCL 的文件系统走占位方案，同样不覆盖。"""
        with patch.object(core, "_renamex_np", None):
            src = self.root / "staged"
            src.mkdir()
            (self.dest / "taken").mkdir()
            with self.assertRaises(FileExistsError):
                core.rename_noreplace(src, self.dest / "taken")
            f = self.root / "f.txt"
            f.write_bytes(b"new")
            (self.dest / "f.txt").write_bytes(b"old")
            with self.assertRaises(FileExistsError):
                core.rename_noreplace(f, self.dest / "f.txt")
            self.assertEqual((self.dest / "f.txt").read_bytes(), b"old")
            core.rename_noreplace(src, self.dest / "new-dir")
            core.rename_noreplace(f, self.dest / "g.txt")
            self.assertEqual((self.dest / "g.txt").read_bytes(), b"new")

    def test_sweep_junk_directly(self) -> None:
        """最终清理不依赖工具的排除规则：任意层级、目录与文件、不跟随链接。"""
        tree = self.root / "tree"
        outside = self.root / "outside"
        (outside / "precious").mkdir(parents=True)
        for rel in ("a/.DS_Store", "a/b/._x", "Icon\r", "keep/DS_Store.txt", "keep/a._x"):
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_bytes(b"x")
        (tree / "a" / "__MACOSX" / "deep").mkdir(parents=True)
        (tree / "a" / "__MACOSX" / "deep" / "f").write_bytes(b"x")
        locked = tree / "a" / "__MACOSX" / "deep"
        locked.chmod(0o000)
        (tree / "._linkdir").symlink_to(outside)  # 垃圾名的链接：删链接本身，不进入目标
        core.make_tree_accessible(tree)
        self.assertEqual(core.sweep_junk(tree), 5)
        self.assertEqual(sorted(nfc_tree(tree)), ["a", "a/b", "keep", "keep/DS_Store.txt",
                                                  "keep/a._x"])
        self.assertEqual(os.listdir(outside), ["precious"])

    def test_job_sweeps_what_tools_let_through(self) -> None:
        """模拟工具漏掉排除规则：任务流程里的最终清理仍要删掉垃圾。"""
        self.require("tar")
        path = self.tar_with("a.tar", [("a.txt", b"a")])

        def leaky(job: core.ExtractJob, family: str, root: Path) -> None:
            (root / "top" / "sub").mkdir(parents=True)
            (root / "top" / "real.txt").write_bytes(b"real")
            (root / "top" / "sub" / "._real.txt").write_bytes(b"junk")
            (root / "__MACOSX").mkdir()

        with patch.object(core.ExtractJob, "_extract", leaky):
            result, job, _ = self.extract(path)
        self.assertEqual(result, self.dest / "top")
        self.assertEqual(sorted(nfc_tree(result)), ["real.txt", "sub"])
        self.assertEqual(job.skipped, 2)

    def test_sweep_and_access_check_each_entry(self) -> None:
        """清理垃圾和补权限也要逐项响应取消：一个目录里可能有成千上万项。"""
        tree = self.root / "flat"
        tree.mkdir()
        for i in range(200):
            (tree / f"._{i}").write_bytes(b"j")
            (tree / f"d{i}").mkdir(mode=0o000)
        calls = [0]

        def cancel_second() -> None:
            calls[0] += 1
            if calls[0] >= 2:
                raise core.Cancelled()

        with self.assertRaises(core.Cancelled):
            core.make_tree_accessible(tree, cancel_second)
        opened = [d for d in tree.iterdir() if d.is_dir() and os.access(d, os.X_OK)]
        self.assertLessEqual(len(opened), 2)
        core.make_tree_accessible(tree)
        calls[0] = 0
        with self.assertRaises(core.Cancelled):
            core.sweep_junk(tree, cancel_second)
        left = [p for p in tree.iterdir() if p.name.startswith("._")]
        self.assertGreaterEqual(len(left), 198)

    def test_long_log_lines_are_capped(self) -> None:
        lines: list[str] = []
        job = core.Job(log=lines.append)
        job._pump(io.BytesIO(b"x" * 10_000 + b"\nshort\n")).join()
        self.assertEqual(len(lines[0]), core._LOG_LINE_MAX + 1)
        self.assertEqual(lines[1], "short")


if __name__ == "__main__":
    unittest.main()
