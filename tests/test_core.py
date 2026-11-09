"""使用真实压缩工具验证六种格式的排除、内容和失败清理；数据仅写入 /tmp。"""
from __future__ import annotations

import errno
import fnmatch
import io
import os
from pathlib import Path
import shlex
import signal
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from typing import Callable
import unittest
import urllib.parse
from unittest.mock import patch
import zipfile
import zlib

import core

# 测试独立维护的垃圾清单：不从 core.JUNK 派生，core 少一项时测试才会失败。
EXPECTED_JUNK = [
    ".DS_Store", "._*", "__MACOSX", ".Spotlight-V100", ".Trashes",
    ".fseventsd", ".TemporaryItems", ".DocumentRevisions-V100",
    ".AppleDouble", ".AppleDB", ".AppleDesktop", ".LSOverride", "Icon\r",
]
# 每个模式在夹具中的实例；以 / 结尾的是目录，目录里放一个必须被排除的文件。
JUNK_FIXTURES = {
    ".DS_Store": [".DS_Store"],
    "._*": ["._resource", "._dir/"],
    "__MACOSX": ["__MACOSX/"],
    ".Spotlight-V100": [".Spotlight-V100/"],
    ".Trashes": [".Trashes/"],
    ".fseventsd": [".fseventsd/"],
    ".TemporaryItems": [".TemporaryItems/"],
    ".DocumentRevisions-V100": [".DocumentRevisions-V100/"],
    ".AppleDouble": [".AppleDouble/"],
    ".AppleDB": [".AppleDB/"],
    ".AppleDesktop": [".AppleDesktop/"],
    ".LSOverride": [".LSOverride"],
    "Icon\r": ["Icon\r"],
}
# 与垃圾名相似、但必须保留的正常文件。
DECOYS = {
    ".hidden": b"ordinary hidden file",
    "DS_Store.txt": b"similar name must survive",
    "a.DS_Store": b"suffix match must survive",
    "x._y": b"inner ._ must survive",
    "Icon": b"Icon without carriage return",
    "nested/__MACOSX-not-junk/file.txt": b"similar directory must survive",
    "nested/deep/x._y": b"nested inner ._ must survive",
}


def silent(_: str) -> None:
    """丢弃任务日志。"""


class ArchiveTests(unittest.TestCase):
    """每个用例独占临时目录；缺少工具时明确跳过对应格式。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cleanpack-test-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / "项目 with spaces"
        self.src.mkdir()
        self.contents = {
            "keep.txt": b"retained contents\n",
            "nested/deep/中文.txt": "多层文件\n".encode(),
            **DECOYS,
        }
        for name, data in self.contents.items():
            path = self.src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # 同时覆盖顶层和深层，以及名称匹配的文件和目录。
        for depth in ("", "nested", "nested/deep"):
            for names in JUNK_FIXTURES.values():
                for name in names:
                    target = self.src / depth / name.rstrip("/")
                    if name.endswith("/"):
                        target.mkdir()
                        (target / "must-not-survive.txt").write_bytes(b"junk")
                    else:
                        target.write_bytes(b"junk")
        if sys.platform == "darwin":
            # 必须真实写入扩展属性，才能发现 macOS tar 自动生成 AppleDouble 的回归。
            subprocess.run(["/usr/bin/xattr", "-w", "com.test.x", "1",
                            str(self.src / "keep.txt")], check=True, env=core.ENV)

    def require_tools(self, fmt: str) -> None:
        missing = core.missing_tools(fmt)
        if missing:
            self.skipTest(f"缺少 {fmt} 所需工具：{', '.join(missing)}")

    def staging_left(self) -> list[Path]:
        return list(self.root.glob(".cleanpack-*.part")) + list(self.root.glob("*.part"))

    def read_archive(self, path: Path, fmt: str) -> tuple[list[str], dict[str, bytes]]:
        """返回成员名和普通文件内容，用独立读取路径验证真实归档。"""
        if fmt == "zip":
            with zipfile.ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
                return archive.namelist(), {i.filename: archive.read(i)
                                           for i in archive.infolist() if not i.is_dir()}
        if fmt == "7z":
            tool = core.locate("7z")
            listing = subprocess.run([tool, "l", "-slt", "-ba", str(path)],
                                     check=True, capture_output=True, text=True, env=core.ENV)
            names = [line.removeprefix("Path = ") for line in listing.stdout.splitlines()
                     if line.startswith("Path = ")]
            # 每次解到新目录，避免同一用例里前一次解压的结果混进来。
            dest = Path(tempfile.mkdtemp(prefix=path.name + "-", dir=self.root))
            subprocess.run([tool, "x", "-y", f"-o{dest}", str(path)], check=True,
                           capture_output=True, env=core.ENV)
            return names, {p.relative_to(dest).as_posix(): p.read_bytes()
                           for p in dest.rglob("*") if p.is_file() and not p.is_symlink()}
        with self.open_tar(path, fmt) as archive:
            contents = {}
            for member in archive.getmembers():
                self.assertFalse(any("xattr" in k.lower() for k in member.pax_headers),
                                 member.pax_headers)
                if member.isfile():
                    with archive.extractfile(member) as stream:
                        contents[member.name] = stream.read()
            return archive.getnames(), contents

    def open_tar(self, path: Path, fmt: str) -> tarfile.TarFile:
        if fmt == "tar.zst":
            raw = subprocess.run([core.locate("zstd"), "-d", "-c", str(path)],
                                 check=True, capture_output=True, env=core.ENV).stdout
            return tarfile.open(fileobj=io.BytesIO(raw))
        return tarfile.open(path)

    def expected_contents(self) -> dict[str, bytes]:
        return {f"{self.src.name}/{name}": data for name, data in self.contents.items()}

    def check_format(self, fmt: str, **kw: object) -> list[str]:
        """断言垃圾成员全部消失，普通文件及其内容完整，暂存目录已清理；返回任务日志。"""
        self.require_tools(fmt)
        out = self.root / ("output" + core.FORMATS[fmt].ext)
        out.unlink(missing_ok=True)
        lines: list[str] = []
        result = core.ArchiveJob(self.src, out, fmt, log=lines.append, **kw).run()
        self.assertEqual(result, out)
        names, contents = self.read_archive(out, fmt)
        self.assertTrue(names)
        for name in names:
            for component in Path(name).parts:
                self.assertFalse(any(fnmatch.fnmatchcase(component, p) for p in EXPECTED_JUNK),
                                 name)
        self.assertEqual(contents, self.expected_contents())
        self.assertEqual(self.staging_left(), [])
        return lines

    def test_junk_list_matches(self) -> None:
        self.assertEqual(sorted(core.JUNK), sorted(EXPECTED_JUNK))
        self.assertEqual(set(JUNK_FIXTURES), set(EXPECTED_JUNK))

    def test_tar_exclusions(self) -> None:
        self.check_format("tar")

    def test_gzip_exclusions(self) -> None:
        self.check_format("tar.gz")

    def test_xz_exclusions(self) -> None:
        self.check_format("tar.xz")

    def test_zstd_exclusions(self) -> None:
        self.check_format("tar.zst")

    def test_zip_exclusions(self) -> None:
        self.check_format("zip")

    def test_7z_exclusions(self) -> None:
        self.check_format("7z")

    def test_levels_reach_command_line(self) -> None:
        """非默认级别、xz 极限模式和 zstd --ultra 必须出现在实际命令里，且归档内容完整。"""
        cases = [
            ("tar.gz", 1, False, ["-1"], []), ("tar.gz", 9, False, ["-9"], []),
            ("tar.xz", 0, False, ["-0"], []), ("tar.xz", 9, True, ["-9e"], []),
            ("tar.zst", 19, False, ["-19"], ["--ultra"]),
            ("tar.zst", 20, False, ["--ultra", "-20"], []),
            ("zip", 0, False, ["-0"], []), ("zip", 9, False, ["-9"], []),
            ("7z", 0, False, ["-mx=0"], []), ("7z", 9, False, ["-mx=9"], []),
        ]
        for fmt, level, extreme, present, absent in cases:
            with self.subTest(fmt=fmt, level=level, extreme=extreme):
                lines = self.check_format(fmt, level=level, extreme=extreme)
                args = [a for line in lines if line.startswith("$ ")
                        for a in shlex.split(line[2:])]
                for flag in present:
                    self.assertIn(flag, args)
                for flag in absent:
                    self.assertNotIn(flag, args)

    def test_zip_level0_stores(self) -> None:
        self.check_format("zip", level=0)
        with zipfile.ZipFile(self.root / "output.zip") as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    self.assertEqual(info.compress_type, zipfile.ZIP_STORED, info.filename)

    def test_7z_level0_copy(self) -> None:
        self.check_format("7z", level=0)
        listing = subprocess.run([core.locate("7z"), "l", "-slt", "-ba",
                                  str(self.root / "output.7z")],
                                 check=True, capture_output=True, text=True, env=core.ENV).stdout
        methods = {line.removeprefix("Method = ") for line in listing.splitlines()
                   if line.startswith("Method = ") and line != "Method = "}
        self.assertEqual(methods, {"Copy"})

    def test_single_file_all_formats(self) -> None:
        """文件名含空格及 - 或 @ 前缀时，六种格式仍应保留正确内容。"""
        for name in ("-single file.txt", "@single file.txt"):
            src = self.root / name
            src.write_bytes(b"single file")
            for fmt in core.FORMATS:
                with self.subTest(name=name, fmt=fmt):
                    self.require_tools(fmt)
                    out = self.root / ("single" + core.FORMATS[fmt].ext)
                    out.unlink(missing_ok=True)
                    core.ArchiveJob(src, out, fmt, log=silent).run()
                    _, contents = self.read_archive(out, fmt)
                    self.assertEqual({k.removeprefix("./"): v for k, v in contents.items()},
                                     {src.name: b"single file"})

    def test_symlinks_stored_as_links(self) -> None:
        """源目录内的符号链接按链接保存，不沿链接打包外部文件的内容。"""
        outside = self.root / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_bytes(b"outside secret must not be archived")
        src = self.root / "linksrc"
        (src / "sub").mkdir(parents=True)
        (src / "real.txt").write_bytes(b"real")
        (src / "sub" / "inner.txt").write_bytes(b"inner")
        links = {"rel_link": "real.txt", "dir_link": "sub", "abs_link": str(secret)}
        for name, target in links.items():
            (src / name).symlink_to(target)
        for fmt in core.FORMATS:
            with self.subTest(fmt=fmt):
                self.require_tools(fmt)
                out = self.root / ("links" + core.FORMATS[fmt].ext)
                core.ArchiveJob(src, out, fmt, log=silent).run()
                found, contents = self.read_links(out, fmt)
                self.assertEqual(found, {f"linksrc/{n}": t for n, t in links.items()})
                self.assertNotIn(secret.read_bytes(), contents.values())

    def read_links(self, path: Path, fmt: str) -> tuple[dict[str, str], dict[str, bytes]]:
        """返回归档中的符号链接（名称到目标）和普通文件内容。"""
        if fmt == "zip":
            with zipfile.ZipFile(path) as archive:
                links, files = {}, {}
                for info in archive.infolist():
                    data = archive.read(info) if not info.is_dir() else b""
                    if stat.S_ISLNK(info.external_attr >> 16):
                        links[info.filename] = data.decode()
                    elif not info.is_dir():
                        files[info.filename] = data
                return links, files
        if fmt == "7z":
            # 7zz 解压时会改写绝对链接，所以按清单的属性识别链接，再用 e -so 读出保存的目标。
            tool = core.locate("7z")
            listing = subprocess.run([tool, "l", "-slt", "-ba", str(path)], check=True,
                                     capture_output=True, text=True, env=core.ENV).stdout
            links, files = {}, {}
            for block in listing.split("\n\n"):
                info = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
                mode = info.get("Attributes", "").split(" ")[-1]
                if not mode or mode.startswith("d"):
                    continue
                data = subprocess.run([tool, "e", "-so", str(path), info["Path"]], check=True,
                                      capture_output=True, env=core.ENV).stdout
                if mode.startswith("l"):
                    links[info["Path"]] = data.decode()
                else:
                    files[info["Path"]] = data
            return links, files
        with self.open_tar(path, fmt) as archive:
            links = {m.name: m.linkname for m in archive.getmembers() if m.issym()}
            files = {}
            for m in archive.getmembers():
                if m.isfile():
                    with archive.extractfile(m) as stream:
                        files[m.name] = stream.read()
            return links, files

    def test_source_cannot_be_overwritten(self) -> None:
        self.require_tools("tar")
        src = self.src / "keep.txt"
        with self.assertRaises(core.JobError) as caught:
            core.ArchiveJob(src, src, "tar", log=silent).run()
        self.assertEqual(caught.exception.key, "out_same_src")
        self.assertEqual(src.read_bytes(), self.contents["keep.txt"])

    def test_source_alias_cannot_be_overwritten(self) -> None:
        self.require_tools("tar")
        src = self.src / "keep.txt"
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                out = self.root / kind
                if kind == "symlink":
                    out.symlink_to(src)
                else:
                    os.link(src, out)
                with self.assertRaises(core.JobError) as caught:
                    core.ArchiveJob(src, out, "tar", log=silent).run()
                self.assertEqual(caught.exception.key, "out_same_src")
                self.assertEqual(src.read_bytes(), self.contents["keep.txt"])

    def test_output_inside_source_is_rejected(self) -> None:
        self.require_tools("tar")
        alias = self.root / "source-alias"
        alias.symlink_to(self.src, target_is_directory=True)
        for parent in (self.src, alias):
            with self.subTest(parent=parent):
                out = parent / "out.tar"
                with self.assertRaises(core.JobError) as caught:
                    core.ArchiveJob(self.src, out, "tar", log=silent).run()
                self.assertEqual(caught.exception.key, "out_inside_src")
                self.assertFalse(out.exists())

    def test_out_inside_src_case_variant(self) -> None:
        """大小写不敏感的卷上，大小写不同的拼写也必须被识别为源目录内部。"""
        self.require_tools("tar")
        variant = Path(str(self.src).swapcase())
        if not variant.exists():
            self.skipTest("当前卷区分大小写")
        with self.assertRaises(core.JobError) as caught:
            core.ArchiveJob(self.src, variant / "out.tar", "tar", log=silent).run()
        self.assertEqual(caught.exception.key, "out_inside_src")
        self.assertEqual(list(self.src.glob(".cleanpack-*")), [])

    def test_out_inside_src_firmlink_alias(self) -> None:
        """经 /System/Volumes/Data 的 firmlink 别名指向源目录内部时也必须拒绝。"""
        self.require_tools("tar")
        alias = Path("/System/Volumes/Data" + os.path.realpath(self.src))
        if not alias.exists():
            self.skipTest("没有 /System/Volumes/Data 别名")
        with self.assertRaises(core.JobError) as caught:
            core.ArchiveJob(self.src, alias / "out.tar", "tar", log=silent).run()
        self.assertEqual(caught.exception.key, "out_inside_src")

    def test_existing_part_is_preserved(self) -> None:
        self.require_tools("tar")
        out = self.root / "output.tar"
        part = self.root / "output.tar.part"
        part.write_bytes(b"unrelated data")
        core.ArchiveJob(self.src, out, "tar", log=silent).run()
        self.assertEqual(part.read_bytes(), b"unrelated data")
        self.assertEqual(list(self.root.glob(".cleanpack-*.part")), [])

    def test_failure_preserves_existing_output(self) -> None:
        self.require_tools("tar")
        out = self.root / "output.tar"
        out.write_bytes(b"previous archive")
        job = core.ArchiveJob(self.root / "nonexistent", out, "tar", log=silent)
        # 跳过开始时的源校验和读取检查，让 tar 自己因为找不到源而失败。
        with patch.object(core, "validate_source", lambda p: Path(p)), \
                patch.object(core, "check_source_readable",
                             lambda p, check=None, **kw: core.SourceStats()), \
                self.assertRaises(core.JobError) as caught:
            job.run()
        self.assertEqual(caught.exception.key, "tool_failed")
        self.assertEqual(caught.exception.kw["tool"], "tar")
        self.assertEqual(out.read_bytes(), b"previous archive")
        self.assertEqual(self.staging_left(), [])
        self.assertTrue(all(p.poll() is not None for p in job._procs))

    def test_cancel_before_start(self) -> None:
        out = self.root / "output.tar"
        job = core.ArchiveJob(self.src, out, "tar", log=silent)
        job.cancel()
        with self.assertRaises(core.Cancelled):
            job.run()
        self.assertFalse(out.exists())
        self.assertEqual(self.staging_left(), [])

    def test_cancel_between_pipeline_processes(self) -> None:
        """在 tar 已启动、压缩器未启动时取消，避免依赖机器速度的定时测试。"""
        self.require_tools("tar.gz")
        out = self.root / "output.tar.gz"
        out.write_bytes(b"previous archive")
        job = core.ArchiveJob(self.src, out, "tar.gz", log=silent)
        spawn = job._spawn

        def cancel_after_spawn(cmd: list[str], **kwargs: object) -> subprocess.Popen:
            proc = spawn(cmd, **kwargs)
            job.cancel()
            return proc

        with patch.object(job, "_spawn", side_effect=cancel_after_spawn):
            with self.assertRaises(core.Cancelled):
                job.run()
        self.assertEqual(out.read_bytes(), b"previous archive")
        self.assertTrue(all(p.poll() is not None for p in job._procs))
        self.assertEqual(self.staging_left(), [])

    def test_cancel_during_running_tool(self) -> None:
        """工具运行途中取消：进程被回收、旧输出保留、没有暂存残留。"""
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        # 管道格式要等两个进程都启动后再取消，确保覆盖“两端都在运行”的情况。
        for fmt, running in (("tar", 1), ("tar.gz", 2), ("zip", 1)):
            with self.subTest(fmt=fmt):
                self.require_tools(fmt)
                out = self.root / ("busy" + core.FORMATS[fmt].ext)
                out.write_bytes(b"previous archive")
                job = core.ArchiveJob(self.src, out, fmt, log=silent)
                spawn = job._spawn

                def slow(cmd: list[str], **kwargs: object) -> subprocess.Popen:
                    return spawn(sleeper, **kwargs)

                errors: list[BaseException] = []

                def worker() -> None:
                    try:
                        job.run()
                    except BaseException as exc:  # noqa: BLE001
                        errors.append(exc)

                with patch.object(job, "_spawn", side_effect=slow):
                    thread = threading.Thread(target=worker)
                    thread.start()
                    deadline = time.monotonic() + 10
                    while len(job._procs) < running and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(len(job._procs), running)
                    job.cancel()
                    thread.join(15)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], core.Cancelled)
                self.assertTrue(all(p.poll() is not None for p in job._procs))
                self.assertEqual(out.read_bytes(), b"previous archive")
                self.assertEqual(self.staging_left(), [])

    def test_compressor_start_failure_reaps_tar(self) -> None:
        """第二个子进程启动失败时，也必须回收已启动的 tar，并报告可翻译的错误。"""
        self.require_tools("tar.gz")
        core._is_bsdtar(core.locate("tar"))  # 预先缓存检测结果，避免它也走被替换的 Popen
        gzip = core.locate("gzip")
        real_popen = subprocess.Popen

        def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> subprocess.Popen:
            if cmd[0] == gzip:
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", gzip)
            return real_popen(cmd, *args, **kwargs)

        job = core.ArchiveJob(self.src, self.root / "output.tar.gz", "tar.gz", log=silent)
        with patch.object(core.subprocess, "Popen", side_effect=fake_popen):
            with self.assertRaises(core.JobError) as caught:
                job.run()
        self.assertEqual(caught.exception.key, "tool_start_failed")
        self.assertEqual(caught.exception.kw["tool"], "gzip")
        self.assertTrue(job._procs)
        self.assertTrue(all(p.poll() is not None for p in job._procs))
        self.assertFalse(job.out.exists())
        self.assertEqual(self.staging_left(), [])

    def test_pipeline_blames_failing_consumer(self) -> None:
        """压缩器先失败时报告压缩器，而不是被 SIGPIPE 连带杀死的 tar。"""
        self.require_tools("tar.gz")
        (self.src / "big.bin").write_bytes(os.urandom(2 * 1024 * 1024))
        job = core.ArchiveJob(self.src, self.root / "output.tar.gz", "tar.gz", log=silent)
        spawn = job._spawn
        failing = [sys.executable, "-c", "import sys; sys.exit(3)"]

        def broken_gzip(cmd: list[str], **kwargs: object) -> subprocess.Popen:
            return spawn(failing if kwargs.get("tool") == "gzip" else cmd, **kwargs)

        with patch.object(job, "_spawn", side_effect=broken_gzip):
            with self.assertRaises(core.JobError) as caught:
                job.run()
        self.assertEqual(caught.exception.key, "tool_failed")
        self.assertEqual(caught.exception.kw, {"tool": "gzip", "code": 3})
        self.assertEqual(self.staging_left(), [])

    def test_blame_ignores_broken_pipe_upstream_when_extracting(self) -> None:
        """解压方向上游写管道被拒（SIGPIPE 或 zstd 的写入错误码 70）是连带结果；
        别的退出码照常报告。压缩方向的归咎不受影响。"""
        sigpipe = -signal.SIGPIPE
        cases = [
            ([("zstd", 70), ("tar", 1)], ("tar", 1)),
            ([("zstd", sigpipe), ("tar", 1)], ("tar", 1)),
            ([("zstd", 70), ("tar", 0)], None),
            ([("zstd", sigpipe), ("tar", 0)], None),
            ([("zstd", 1), ("tar", 0)], ("zstd", 1)),
            ([("zstd", 1), ("tar", 1)], ("zstd", 1)),
        ]
        for results, expected in cases:
            with self.subTest(results=results):
                self.assertEqual(core._blame(results, downstream=False), expected)
        # 70 只对 zstd 有这层含义；压缩方向两端都失败时仍报下游。
        self.assertEqual(core._blame([("gzip", 70), ("tar", 0)], downstream=False), ("gzip", 70))
        self.assertEqual(core._blame([("tar", 1), ("zstd", 70)], downstream=True), ("zstd", 70))

    def test_warning_exit_does_not_commit_archive(self) -> None:
        """7z 的退出码 1 也可能遗漏文件，必须保留旧输出并报告失败。"""
        self.require_tools("7z")
        out = self.root / "warning.7z"
        out.write_bytes(b"previous archive")
        job = core.ArchiveJob(self.src, out, "7z", log=silent)
        spawn = job._spawn

        def warning_process(cmd: list[str], **kwargs: object) -> subprocess.Popen:
            return spawn([sys.executable, "-c", "raise SystemExit(1)"], **kwargs)

        with patch.object(job, "_spawn", side_effect=warning_process):
            with self.assertRaises(core.JobError) as caught:
                job.run()
        self.assertEqual(caught.exception.kw, {"tool": "7z", "code": 1})
        self.assertEqual(out.read_bytes(), b"previous archive")
        self.assertEqual(self.staging_left(), [])

    def test_missing_tools_uses_localization_key(self) -> None:
        with patch.object(core, "locate", return_value=None):
            with self.assertRaises(core.JobError) as caught:
                core.ArchiveJob(self.src, self.root / "out.tar.zst", "tar.zst").run()
        self.assertEqual(caught.exception.key, "missing_tools")
        self.assertEqual(caught.exception.kw["tools"], "tar, zstd")

    def test_output_dir_missing(self) -> None:
        self.require_tools("tar")
        out = self.root / "missing" / "out.tar"
        with self.assertRaises(core.JobError) as caught:
            core.ArchiveJob(self.src, out, "tar", log=silent).run()
        self.assertEqual(caught.exception.key, "out_dir_missing")
        self.assertEqual(caught.exception.kw["path"], str(out.parent))

    def test_output_is_existing_dir(self) -> None:
        self.require_tools("tar")
        out = self.root / "existing.tar"
        out.mkdir()
        with self.assertRaises(core.JobError) as caught:
            core.ArchiveJob(self.src, out, "tar", log=silent).run()
        self.assertEqual(caught.exception.key, "out_is_dir")
        self.assertTrue(out.is_dir())

    def test_output_dir_not_writable(self) -> None:
        self.require_tools("tar")
        if os.geteuid() == 0:
            self.skipTest("root 不受目录权限限制")
        locked = self.root / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        self.addCleanup(locked.chmod, 0o755)
        with self.assertRaises(core.JobError) as caught:
            core.ArchiveJob(self.src, locked / "out.tar", "tar", log=silent).run()
        self.assertEqual(caught.exception.key, "access_denied")

    def test_no_space_is_localized(self) -> None:
        self.require_tools("tar")
        out = self.root / "out.tar"
        disk_full = OSError(errno.ENOSPC, "No space left on device")
        with patch.object(core.os, "replace", side_effect=disk_full):
            with self.assertRaises(core.JobError) as caught:
                core.ArchiveJob(self.src, out, "tar", log=silent).run()
        self.assertEqual(caught.exception.key, "no_space")
        self.assertFalse(out.exists())
        self.assertEqual(self.staging_left(), [])

    def test_output_parent_is_a_file(self) -> None:
        self.require_tools("tar")
        parent = self.root / "plain-file"
        parent.write_bytes(b"not a folder")
        with self.assertRaises(core.JobError) as caught:
            core.ArchiveJob(self.src, parent / "out.tar", "tar", log=silent).run()
        self.assertEqual(caught.exception.key, "out_dir_not_dir")

    def test_replace_error_names_the_output(self) -> None:
        """提交失败时报告用户选择的输出路径，而不是已删除的隐藏暂存文件。"""
        self.require_tools("tar")
        out = self.root / "out.tar"

        def denied(src: str, dst: str) -> None:
            # 与真实的 os.replace 一样设置 filename（源）和 filename2（目标）；第 4 个参数是 winerror。
            raise PermissionError(errno.EPERM, "Operation not permitted", src, None, dst)

        with patch.object(core.os, "replace", side_effect=denied):
            with self.assertRaises(core.JobError) as caught:
                core.ArchiveJob(self.src, out, "tar", log=silent).run()
        # 没有锁定标志、不在系统保护位置的 EPERM 按隐私保护报告。
        self.assertEqual((caught.exception.key, caught.exception.denial),
                         ("privacy_denied", "privacy"))
        self.assertEqual(caught.exception.kw["path"], str(out))

    def test_os_error_after_cancel_is_cancelled(self) -> None:
        """取消引发的文件系统错误仍按取消报告，而不是失败。"""
        self.require_tools("tar")
        job = core.ArchiveJob(self.src, self.root / "out.tar", "tar", log=silent)

        def cancel_then_fail(src: str, dst: str) -> None:
            job.cancel()
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", src)

        with patch.object(core.os, "replace", side_effect=cancel_then_fail):
            with self.assertRaises(core.Cancelled):
                job.run()
        self.assertEqual(self.staging_left(), [])

    def test_missing_source_folder_is_not_a_tool_error(self) -> None:
        """源所在目录消失时（例如卷已推出）报告缺失的路径，而不是“无法启动工具”。"""
        self.require_tools("tar")
        gone = self.root / "ejected"
        # 模拟源在开始时的校验之后才消失，确认工具启动阶段的报错仍然正确。
        with patch.object(core, "validate_source", lambda p: Path(p)), \
                patch.object(core, "check_source_readable",
                             lambda p, check=None, **kw: core.SourceStats()), \
                self.assertRaises(core.JobError) as caught:
            core.ArchiveJob(gone / "project", self.root / "out.tar", "tar", log=silent).run()
        self.assertEqual(caught.exception.key, "not_found")
        self.assertEqual(caught.exception.kw["path"], str(gone))
        self.assertEqual(self.staging_left(), [])

    def test_missing_source_is_reported_at_start(self) -> None:
        """任务开始时再校验一次源：选择之后被删除的源报告 src_missing，不启动任何工具。"""
        self.require_tools("tar")
        with self.assertRaises(core.JobError) as caught:
            core.ArchiveJob(self.root / "deleted", self.root / "out.tar", "tar", log=silent).run()
        self.assertEqual(caught.exception.key, "src_missing")
        self.assertFalse((self.root / "out.tar").exists())

    def test_symlinked_source_archives_target_contents(self) -> None:
        """源是指向文件夹的链接时，归档的是目标的内容，而不是只有一个链接。"""
        self.require_tools("tar")
        link = self.root / "shortcut"
        link.symlink_to(self.src)
        out = self.root / "via-link.tar"
        core.ArchiveJob(link, out, "tar", log=silent).run()
        with tarfile.open(out) as archive:
            names = archive.getnames()
        self.assertIn(f"{self.src.name}/keep.txt", names)
        self.assertNotIn("shortcut", names)

    def test_junk_source_is_rejected(self) -> None:
        """源本身是垃圾名称时，工具会把它整个排除并“成功”生成空归档，必须提前拒绝。"""
        self.require_tools("tar")
        junk = self.root / "._resource"
        junk.write_bytes(b"x")
        with self.assertRaises(core.JobError) as caught:
            core.ArchiveJob(junk, self.root / "out.tar", "tar", log=silent).run()
        self.assertEqual(caught.exception.key, "src_is_junk")

    def test_zip_utf8_flags_in_both_headers(self) -> None:
        """非 ASCII 文件名在中央目录和本地头都要带 UTF-8 标志，ASCII 名称不加。"""
        self.check_format("zip")
        path = self.root / "output.zip"
        with zipfile.ZipFile(path) as archive, path.open("rb") as raw:
            infos = archive.infolist()
            self.assertTrue(any(not i.filename.isascii() for i in infos))
            for info in infos:
                expected = 0 if info.filename.isascii() else 0x800
                self.assertEqual(info.flag_bits & 0x800, expected, info.filename)
                raw.seek(info.header_offset)
                header = raw.read(30)
                self.assertEqual(header[:4], b"PK\x03\x04")
                local_flags = struct.unpack_from("<H", header, 6)[0]
                self.assertEqual(local_flags & 0x800, expected, info.filename)


def _local_header(name: bytes, data: bytes, flags: int = 0) -> bytes:
    """构造一个仅存储（不压缩）的本地文件头加数据。"""
    crc = zlib.crc32(data)
    return struct.pack("<4sHHHHHIIIHH", b"PK\x03\x04", 20, flags, 0, 0, 0, crc,
                       len(data), len(data), len(name), 0) + name + data


def _central_header(name: bytes, data: bytes, offset: int, extra: bytes = b"",
                    flags: int = 0) -> bytes:
    crc = zlib.crc32(data)
    return struct.pack("<4sHHHHHHIIIHHHHHII", b"PK\x01\x02", 45, 45, flags, 0, 0, 0, crc,
                       len(data), len(data), len(name), len(extra), 0, 0, 0, 0,
                       offset) + name + extra


def _zip64_blob(name: bytes, data: bytes, *, saturated: bool = True, big_size: bool = False,
                local_offset: int = 0) -> bytes:
    """手工构造带 ZIP64 结束记录、本地头偏移放在扩展字段里的归档。

    saturated 为 False 时 EOCD 字段保持真实值（Info-ZIP 遇到大于 4 GiB 的条目时就这样写）；
    big_size 为 True 时中央头的原始大小也溢出，扩展字段先放原始大小再放偏移；
    local_offset 写入扩展字段的本地头偏移。
    """
    local = _local_header(name, data)
    if big_size:
        extra = struct.pack("<HHQQ", 0x0001, 16, 5 * 2**30, local_offset)
        central = _central_header(name, data, 0xFFFFFFFF, extra)
        # 把中央头里的原始大小字段（偏移 24）改成溢出标记。
        central = central[:24] + struct.pack("<I", 0xFFFFFFFF) + central[28:]
    else:
        extra = struct.pack("<HHQ", 0x0001, 8, local_offset)
        central = _central_header(name, data, 0xFFFFFFFF, extra)
    cd_offset, cd_size = len(local), len(central)
    z64_at = cd_offset + cd_size
    z64 = struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, 1, 1, cd_size, cd_offset)
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, z64_at, 1)
    if saturated:
        eocd = struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF,
                           0xFFFFFFFF, 0xFFFFFFFF, 0)
    else:
        eocd = struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 1, 1, cd_size, cd_offset, 0)
    return local + central + z64 + locator + eocd


class ZipParserTests(unittest.TestCase):
    """自带 ZIP 中央目录解析器的单元测试，不需要外部工具。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cleanpack-test-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job = core.ArchiveJob(self.root / "src", self.root / "out.zip", "zip", log=silent)

    def make_zip(self, names: list[str]) -> Path:
        path = self.root / "sample.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name in names:
                archive.writestr(name, f"data of {name}")
        return path

    def clear_utf8_flags(self, path: Path) -> None:
        """模拟 Apple zip：保留 UTF-8 文件名字节，但清除两处头部的 bit 11。"""
        with path.open("r+b") as raw:
            for entry in core._read_central_directory(raw):
                for at in (entry.central + 8, entry.local + 6):
                    raw.seek(at)
                    flags = struct.unpack("<H", raw.read(2))[0]
                    raw.seek(at)
                    raw.write(struct.pack("<H", flags & ~0x800))

    def test_matches_zipfile(self) -> None:
        path = self.make_zip(["a.txt", "目录/中文.txt"])
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
        with path.open("rb") as raw:
            entries = core._read_central_directory(raw)
        self.assertEqual([e.name.decode() for e in entries], [i.filename for i in infos])
        self.assertEqual([e.local for e in entries], [i.header_offset for i in infos])

    def test_prefix_data_shifts_offsets(self) -> None:
        path = self.make_zip(["a.txt", "b.txt"])
        path.write_bytes(b"#!/bin/sh\nexit 0\n" * 4 + path.read_bytes())
        with path.open("rb") as raw:
            entries = core._read_central_directory(raw)
            self.assertEqual(len(entries), 2)
            for entry in entries:
                raw.seek(entry.local)
                self.assertEqual(raw.read(4), b"PK\x03\x04")

    def test_zip64_records(self) -> None:
        name = "é.txt".encode()
        path = self.root / "z64.zip"
        path.write_bytes(_zip64_blob(name, b"hi"))
        with path.open("rb") as raw:
            entries = core._read_central_directory(raw)
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].local, entries[0].name), (0, name))
        self.job._mark_zip_utf8(path)
        with path.open("rb") as raw:
            entry = core._read_central_directory(raw)[0]
            for at in (entry.central + 8, entry.local + 6):
                raw.seek(at)
                self.assertTrue(struct.unpack("<H", raw.read(2))[0] & 0x800)

    def test_zip64_records_with_plain_eocd(self) -> None:
        """有 ZIP64 记录但 EOCD 字段未溢出时（大于 4 GiB 的条目），也要按 ZIP64 记录解析。"""
        path = self.root / "z64plain.zip"
        path.write_bytes(_zip64_blob(b"big.img", b"hi", saturated=False))
        with path.open("rb") as raw:
            entries = core._read_central_directory(raw)
        self.assertEqual([(e.local, e.name) for e in entries], [(0, b"big.img")])

    def test_zip64_extra_with_large_size(self) -> None:
        """原始大小也溢出时，扩展字段里的偏移排在原始大小之后。"""
        path = self.root / "z64big.zip"
        path.write_bytes(_zip64_blob(b"big.img", b"hi", big_size=True))
        with path.open("rb") as raw:
            self.assertEqual(core._read_central_directory(raw)[0].local, 0)

    def test_local_offset_out_of_range(self) -> None:
        path = self.root / "z64bad.zip"
        path.write_bytes(_zip64_blob(b"a.txt", b"hi", local_offset=2**63))
        with path.open("rb") as raw, self.assertRaises(core.JobError) as caught:
            core._read_central_directory(raw)
        self.assertEqual(caught.exception.key, "zip_invalid")

    def test_exactly_65535_entries_without_zip64(self) -> None:
        """条目数正好 65535 时 EOCD 字段等于上限但没有 ZIP64 记录，仍是合法归档。"""
        path = self.root / "many.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for i in range(0xFFFF):
                archive.writestr(f"f{i}", b"")
        with path.open("rb") as raw:
            entries = core._read_central_directory(raw)
        self.assertEqual(len(entries), 0xFFFF)

    def test_marks_unflagged_utf8_names(self) -> None:
        path = self.make_zip(["plain.txt", "目录/中文.txt"])
        self.clear_utf8_flags(path)
        self.job._mark_zip_utf8(path)
        with zipfile.ZipFile(path) as archive:
            flags = {i.filename: i.flag_bits & 0x800 for i in archive.infolist()}
        self.assertEqual(flags, {"plain.txt": 0, "目录/中文.txt": 0x800})

    def test_bad_local_signature(self) -> None:
        path = self.make_zip(["目录/中文.txt"])
        data = bytearray(path.read_bytes())
        data[0:4] = b"XX\x03\x04"
        path.write_bytes(bytes(data))
        with self.assertRaises(core.JobError) as caught:
            self.job._mark_zip_utf8(path)
        self.assertEqual(caught.exception.key, "zip_invalid")

    def test_local_name_mismatch(self) -> None:
        path = self.make_zip(["abc.txt"])
        data = bytearray(path.read_bytes())
        data[30] = ord("x")  # 本地头文件名的第一个字节
        path.write_bytes(bytes(data))
        with self.assertRaises(core.JobError) as caught:
            self.job._mark_zip_utf8(path)
        self.assertEqual(caught.exception.key, "zip_invalid")

    def test_not_a_zip(self) -> None:
        path = self.root / "junk.zip"
        path.write_bytes(b"not a zip archive")
        with path.open("rb") as raw, self.assertRaises(core.JobError) as caught:
            core._read_central_directory(raw)
        self.assertEqual(caught.exception.key, "zip_invalid")


class HelperTests(unittest.TestCase):
    """不调用外部工具的辅助函数测试。"""

    def test_job_error_from_os(self) -> None:
        err = core.job_error_from_os(PermissionError(errno.EACCES, "Permission denied", "/x"))
        self.assertEqual((err.key, err.kw["path"]), ("access_denied", "/x"))
        # 没有文件名时，需要路径的文案退回通用错误，避免残缺句子。
        err = core.job_error_from_os(OSError(errno.EACCES, "Permission denied"))
        self.assertEqual(err.key, "io_error")
        err = core.job_error_from_os(OSError(errno.ENAMETOOLONG, "File name too long", "/x"))
        self.assertEqual(err.key, "name_too_long")
        err = core.job_error_from_os(OSError(errno.ENOSPC, "No space left on device"))
        self.assertEqual(err.key, "no_space")
        err = core.job_error_from_os(OSError(errno.EIO, "Input/output error"))
        self.assertEqual((err.key, err.kw["err"]), ("io_error", "Input/output error"))
        # 暂存目录内的路径换成它所在的输出目录。
        hidden = "/Users/me/Desktop/.cleanpack-k3j2x9.part/out.zip.part"
        err = core.job_error_from_os(PermissionError(errno.EPERM, "Operation not permitted",
                                                     hidden))
        self.assertEqual(err.kw["path"], "/Users/me/Desktop")

    def test_blame(self) -> None:
        self.assertIsNone(core._blame([("tar", 0), ("gzip", 0)], downstream=True))
        # 压缩方向：tar 因管道断开失败（bsdtar 退出码 1）是压缩器失败的连带结果。
        self.assertEqual(core._blame([("tar", 1), ("gzip", 3)], downstream=True), ("gzip", 3))
        self.assertEqual(core._blame([("tar", 2), ("gzip", 0)], downstream=True), ("tar", 2))
        # 解压方向：解码器遇到损坏数据时，下游 tar 的失败是连带结果。
        self.assertEqual(core._blame([("zstd", 1), ("tar", 1)], downstream=False), ("zstd", 1))
        self.assertEqual(core._blame([("zstd", 0), ("tar", 1)], downstream=False), ("tar", 1))

    def test_is_within(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cleanpack-test-", dir="/tmp") as d:
            root = Path(d)
            (root / "a" / "b").mkdir(parents=True)
            self.assertTrue(core.is_within(root / "a" / "b", root / "a"))
            self.assertTrue(core.is_within(root / "a" / "b" / "not-yet", root / "a"))
            self.assertTrue(core.is_within(root / "a", root / "a"))
            self.assertFalse(core.is_within(root, root / "a"))
            self.assertFalse(core.is_within(root / "a", root / "missing"))

    def test_is_system_path(self) -> None:
        self.assertTrue(core.is_system_path("/usr/bin/tar"))
        self.assertTrue(core.is_system_path("/bin/sh"))
        self.assertFalse(core.is_system_path("/opt/homebrew/bin/gzip"))
        self.assertFalse(core.is_system_path("/usr/binary/gzip"))
        # 数据卷经 firmlink 挂在 /System/Volumes/Data 下，其中的 Homebrew 不是系统工具。
        self.assertFalse(core.is_system_path("/System/Volumes/Data/opt/homebrew/bin/zstd"))
        self.assertTrue(core.is_system_path("/System/Library/CoreServices"))
        with tempfile.TemporaryDirectory(prefix="cleanpack-test-", dir="/tmp") as d:
            # 指向系统工具的链接按目标判断，系统目录外的同名文件不算。
            link = Path(d) / "tar"
            link.symlink_to("/usr/bin/tar")
            self.assertTrue(core.is_system_path(str(link)))
            fake = Path(d) / "gzip"
            fake.write_text("")
            self.assertFalse(core.is_system_path(str(fake)))

    def test_version_and_bundle_id(self) -> None:
        self.assertRegex(core.APP_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertRegex(core.BUNDLE_ID, r"^[a-z0-9]+(\.[a-z0-9]+)+$")



class DenialTests(unittest.TestCase):
    """访问被拒时区分原因：隐私保护、锁定、系统保护和 POSIX 权限，界面据此提供补救按钮。"""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="cleanpack-test-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def locked(self, path: Path) -> Path:
        """给 path 加上访达「已锁定」标志（uchg），测试结束时解除，临时目录才能删除。"""
        os.chflags(path, stat.UF_IMMUTABLE)
        self.addCleanup(os.chflags, path, 0)
        return path

    def unreadable(self, path: Path) -> Path:
        """把 path（不存在时先建成文件夹）设成 0 权限，测试结束时恢复，临时目录才能删除。"""
        if not path.exists():
            path.mkdir()
        path.chmod(0)
        self.addCleanup(path.chmod, 0o700)
        return path

    def raises(self, func: Callable[..., object], *args: object) -> core.JobError:
        with self.assertRaises(core.JobError) as caught:
            func(*args)
        return caught.exception

    def test_classify(self) -> None:
        plain = self.root / "plain.txt"
        plain.write_bytes(b"")
        classify = core.classify_denial
        self.assertEqual(classify(plain, errno.EACCES), "posix")
        self.assertEqual(classify(plain, None), "posix")
        # 没有锁定标志、不在系统保护位置的 EPERM：按隐私保护处理。
        self.assertEqual(classify(plain, errno.EPERM), "privacy")
        self.assertEqual(classify(self.root / "new.txt", errno.EPERM), "privacy")
        # /tmp 链接本身带 restricted 标志：直接在 /tmp 下新建时上级就是这个链接，判断前要先
        # 解析成真实路径（/private/tmp 是普通文件夹）。
        self.assertEqual(classify("/tmp/cleanpack-no-such-item", errno.EPERM), "privacy")
        locked_file = self.root / "locked.txt"
        locked_file.write_bytes(b"")
        self.locked(locked_file)
        self.assertEqual(classify(locked_file, errno.EPERM), "locked")
        self.assertEqual(classify(locked_file, None), "locked")
        # 锁定只阻止修改：读取被拒（EPERM）仍是隐私保护，EACCES 和 os.access 的读失败仍是权限。
        self.assertEqual(classify(locked_file, errno.EPERM, write=False), "privacy")
        self.assertEqual(classify(locked_file, None, write=False), "posix")
        self.assertEqual(classify(locked_file, errno.EACCES), "posix")
        # 锁定的文件夹：在其中新建看文件夹的标志；里面已有的项目只看它自己的标志。
        locked_dir = self.root / "locked-dir"
        locked_dir.mkdir()
        (locked_dir / "inside.txt").write_bytes(b"")
        self.locked(locked_dir)
        self.assertEqual(classify(locked_dir / "new", errno.EPERM), "locked")
        self.assertEqual(classify(locked_dir / "inside.txt", errno.EPERM), "privacy")
        self.assertEqual(classify(locked_dir / "inside.txt", errno.EACCES), "posix")
        if os.lstat("/System").st_flags & stat.SF_RESTRICTED:
            self.assertEqual(classify("/System/new-item", errno.EPERM), "system")

    def test_real_errors_from_locked_items(self) -> None:
        """真实的系统调用：锁定的文件写入、锁定的文件夹作为输出，都识别为锁定。"""
        locked_file = self.root / "locked.txt"
        locked_file.write_bytes(b"")
        self.locked(locked_file)
        with self.assertRaises(PermissionError) as caught:
            open(locked_file, "wb")
        err = core.job_error_from_os(caught.exception)
        self.assertEqual((err.key, err.denial, err.kw["path"]),
                         ("locked_denied", "locked", str(locked_file)))
        locked_dir = self.root / "locked-dir"
        locked_dir.mkdir()
        self.locked(locked_dir)
        err = self.raises(core.check_output_dir, locked_dir)
        self.assertEqual((err.key, err.denial), ("locked_denied", "locked"))
        # 锁定的文件和文件夹都能读，作为压缩源没有问题。
        self.assertEqual(core.validate_source(locked_file), locked_file)
        core.check_source_readable(locked_dir)

    def test_job_error_keys(self) -> None:
        err = core.job_error_from_os(PermissionError(errno.EACCES, "Permission denied",
                                                     str(self.root)))
        self.assertEqual((err.key, err.denial), ("access_denied", "posix"))
        err = core.job_error_from_os(PermissionError(errno.EPERM, "Operation not permitted",
                                                     str(self.root)))
        self.assertEqual((err.key, err.denial), ("privacy_denied", "privacy"))
        # 不是权限问题的错误不带 denial，界面不提供补救按钮。
        err = core.job_error_from_os(OSError(errno.ENOENT, "No such file", str(self.root)))
        self.assertIsNone(err.denial)
        self.assertIsNone(core.JobError("tool_failed", tool="tar", code=1).denial)

    @unittest.skipIf(os.geteuid() == 0, "root 不受文件权限限制")
    def test_validators_keep_the_reason(self) -> None:
        err = self.raises(core.validate_source, self.unreadable(self.root / "unreadable"))
        self.assertEqual((err.key, err.denial), ("src_denied", "posix"))
        archive = self.root / "a.zip"
        archive.write_bytes(b"")
        err = self.raises(core.validate_archive, self.unreadable(archive))
        self.assertEqual((err.key, err.denial), ("src_denied", "posix"))
        # 锁定文件夹里读不了的项目是权限问题，不是“已锁定”。
        locked_dir = self.root / "locked-dir"
        locked_dir.mkdir()
        inner = locked_dir / "inner.txt"
        inner.write_bytes(b"")
        self.unreadable(inner)
        self.locked(locked_dir)
        err = self.raises(core.validate_source, inner)
        self.assertEqual((err.key, err.denial), ("src_denied", "posix"))
        # stat 被隐私保护拒绝（EPERM）时报告隐私保护，而不是笼统的“没有权限”。
        src = self.root / "src"
        src.mkdir()
        denied = PermissionError(errno.EPERM, "Operation not permitted", str(src))
        with patch.object(core.os, "stat", side_effect=denied):
            err = self.raises(core.validate_source, src)
        self.assertEqual((err.key, err.denial, err.kw["path"]),
                         ("privacy_denied", "privacy", str(src)))

    def test_check_source_readable(self) -> None:
        folder = self.root / "folder"
        (folder / "deep" / "deeper").mkdir(parents=True)
        (folder / "a.txt").write_bytes(b"a")
        core.check_source_readable(folder)
        core.check_source_readable(folder / "a.txt")
        empty = self.root / "empty"
        empty.mkdir()
        core.check_source_readable(empty)
        # 链接不跟随：指向读不了的文件夹的链接不影响检查（工具也只保存链接本身）。
        if os.geteuid() != 0:
            outside = self.unreadable(self.root / "outside")
            (folder / "link").symlink_to(outside)
            core.check_source_readable(folder)
        # 每个文件夹调用一次 check，取消能及时生效。
        calls: list[int] = []
        core.check_source_readable(folder, lambda: calls.append(1))
        self.assertEqual(len(calls), 3)

    @unittest.skipIf(os.geteuid() == 0, "root 不受文件权限限制")
    def test_check_source_readable_finds_denied_subfolders(self) -> None:
        folder = self.root / "folder"
        (folder / "ok").mkdir(parents=True)
        denied = self.unreadable(folder / "ok" / "denied")
        err = self.raises(core.check_source_readable, folder)
        self.assertEqual((err.key, err.denial, err.kw["path"]),
                         ("src_denied", "posix", str(denied)))
        # 名称匹配排除规则的文件夹本来就不进归档，读不了也不影响。
        denied.chmod(0o700)
        self.unreadable(folder / ".Trashes")
        core.check_source_readable(folder)

    def test_check_source_readable_reports_privacy(self) -> None:
        folder = self.root / "folder"
        folder.mkdir()
        denied = PermissionError(errno.EPERM, "Operation not permitted", str(folder))
        with patch.object(core.os, "scandir", side_effect=denied):
            err = self.raises(core.check_source_readable, folder)
        self.assertEqual((err.key, err.denial, err.kw["path"]),
                         ("privacy_denied", "privacy", str(folder)))
        single = self.root / "single.txt"
        single.write_bytes(b"x")
        denied = PermissionError(errno.EPERM, "Operation not permitted", str(single))
        with patch("builtins.open", side_effect=denied):
            err = self.raises(core.check_source_readable, single)
        self.assertEqual((err.key, err.denial, err.kw["path"]),
                         ("privacy_denied", "privacy", str(single)))

    def test_archive_job_reports_privacy_before_running_tools(self) -> None:
        """os.access 看不出隐私保护；任务开始时实际读一遍源，被拒时不启动任何工具。"""
        if core.locate("tar") is None:
            self.skipTest("缺少 tar")
        src = self.root / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"a")
        denied = PermissionError(errno.EPERM, "Operation not permitted", str(src))
        job = core.ArchiveJob(src, self.root / "out.tar", "tar", log=silent)
        with patch.object(core.os, "scandir", side_effect=denied), \
                self.assertRaises(core.JobError) as caught:
            job.run()
        self.assertEqual((caught.exception.key, caught.exception.denial,
                          caught.exception.kw["path"]), ("privacy_denied", "privacy", str(src)))
        self.assertEqual(job._procs, [])
        self.assertEqual(os.listdir(self.root), ["src"])

    @unittest.skipIf(os.geteuid() == 0, "root 不受文件权限限制")
    def test_zip_with_denied_subfolder_fails_instead_of_skipping(self) -> None:
        """Info-ZIP 遇到读不了的子文件夹会静默跳过并报告成功，归档里少了内容；bsdtar 会以 1
        退出，但只能报“工具失败”。开始前的检查要把它连同原因报告出来，而且不启动工具。"""
        for fmt in ("zip", "tar"):
            if core.locate(fmt) is None:
                self.skipTest(f"缺少 {fmt}")
            src = self.root / f"src-{fmt}"
            (src / "sub").mkdir(parents=True)
            (src / "a.txt").write_bytes(b"a")
            (src / "sub" / "b.txt").write_bytes(b"b")
            denied = self.unreadable(src / "sub")
            out = self.root / f"out.{fmt}"
            job = core.ArchiveJob(src, out, fmt, log=silent)
            with self.assertRaises(core.JobError) as caught:
                job.run()
            self.assertEqual((caught.exception.key, caught.exception.denial,
                              caught.exception.kw["path"]), ("src_denied", "posix", str(denied)))
            self.assertEqual(job._procs, [])
            self.assertFalse(out.exists())

    def test_extract_job_reports_privacy_for_the_archive(self) -> None:
        archive = self.root / "a.tar"
        with tarfile.open(archive, "w") as tf:
            tf.addfile(tarfile.TarInfo("a.txt"), io.BytesIO(b""))
        dest = self.root / "dest"
        dest.mkdir()
        denied = PermissionError(errno.EPERM, "Operation not permitted", str(archive))
        with patch.object(core, "sniff_family", side_effect=denied), \
                self.assertRaises(core.JobError) as caught:
            core.ExtractJob(archive, dest, log=silent).run()
        self.assertEqual((caught.exception.key, caught.exception.denial,
                          caught.exception.kw["path"]),
                         ("privacy_denied", "privacy", str(archive)))
        self.assertEqual(os.listdir(dest), [])
        # 锁定的归档照样能读：读取被拒不能说成“已锁定”。
        self.locked(archive)
        with patch.object(core, "sniff_family", side_effect=denied), \
                self.assertRaises(core.JobError) as caught:
            core.ExtractJob(archive, dest, log=silent).run()
        self.assertEqual((caught.exception.key, caught.exception.denial),
                         ("privacy_denied", "privacy"))

    @staticmethod
    def fake_tool(stderr: str, code: int) -> list[str]:
        """一个假工具：把 stderr 原样写到标准错误后以 code 退出（避免 shell 转义问题）。"""
        return [sys.executable, "-c",
                f"import sys; sys.stderr.write({stderr!r}); sys.exit({code})"]

    EPERM_LINE = "tar: Can't open `a.txt': Operation not permitted\n"

    def test_compress_tool_reporting_eperm_fails_as_privacy(self) -> None:
        """压缩时工具报告读取被拒并失败，多半是隐私保护拦住了某些文件。"""
        job = core.ArchiveJob(self.root, self.root / "o.tar", "tar", log=silent)
        with self.assertRaises(core.JobError) as caught:
            job._run_simple(self.fake_tool(self.EPERM_LINE, 1), tool="tar", cwd=self.root)
        self.assertEqual((caught.exception.key, caught.exception.denial,
                          caught.exception.kw["tool"]), ("privacy_partial", "privacy", "tar"))
        # 其他失败照旧报告工具失败；成员名里恰好有这句话的行不算工具的报错。
        for stderr in ("boom\n", "  adding: Operation not permitted\n"):
            job = core.ArchiveJob(self.root, self.root / "o.tar", "tar", log=silent)
            with self.assertRaises(core.JobError) as caught:
                job._run_simple(self.fake_tool(stderr, 2), tool="tar", cwd=self.root)
            self.assertEqual((caught.exception.key, caught.exception.denial),
                             ("tool_failed", None), stderr)
        # 报告了被拒但成功退出的不算失败。
        job = core.ArchiveJob(self.root, self.root / "o.tar", "tar", log=silent)
        job._run_simple(self.fake_tool(self.EPERM_LINE, 0), tool="tar", cwd=self.root)

    def test_eperm_from_another_tool_keeps_the_real_failure(self) -> None:
        """tar 报告过读取被拒，失败的却是压缩器（例如写满磁盘）：照实报告压缩器失败。"""
        for tar_code, gzip_code, expected in ((0, 3, ("tool_failed", "gzip")),
                                              (1, 0, ("privacy_partial", "tar"))):
            job = core.ArchiveJob(self.root, self.root / "o.tar.gz", "tar.gz", log=silent)
            with open(self.root / "out", "wb") as f, self.assertRaises(core.JobError) as caught:
                job._run_pipeline(self.fake_tool(self.EPERM_LINE, tar_code),
                                  [sys.executable, "-c",
                                   f"import sys; sys.stdin.read(); sys.exit({gzip_code})"],
                                  tools=("tar", "gzip"), cwd=self.root, stdout=f,
                                  blame_downstream=True)
            self.assertEqual((caught.exception.key, caught.exception.kw["tool"]), expected)

    def test_eperm_formats_are_checked_per_tool(self) -> None:
        """每个工具只按自己的格式识别读取被拒；Info-ZIP 列出成员的行以成员名结尾，名字恰好
        是 7-Zip 汇总格式时不能把 zip 的普通失败说成隐私保护。"""
        privacy = (("zip", "zip warning: Operation not permitted\n"),
                   ("7z", "WARNING: errno=1 : Operation not permitted\n"),
                   ("7z", "src/x : errno=1 : Operation not permitted\n"),
                   ("tar", self.EPERM_LINE))
        other = (("zip", "  adding: x : errno=1 : Operation not permitted\n"),
                 ("zip", self.EPERM_LINE),
                 ("7z", "src/x : errno=13 : Permission denied\n"),
                 ("tar", "zip warning: Operation not permitted\n"))
        for (tool, line), expected in ([(p, "privacy_partial") for p in privacy]
                                       + [(o, "tool_failed") for o in other]):
            job = core.ArchiveJob(self.root, self.root / "o.zip", "zip", log=silent)
            with self.assertRaises(core.JobError) as caught:
                job._run_simple(self.fake_tool(line, 1), tool=tool, cwd=self.root)
            self.assertEqual(caught.exception.key, expected, (tool, line))

    def test_check_source_readable_skips_overlong_paths(self) -> None:
        """路径超过 PATH_MAX 的深层文件夹不在开始前的检查里报错：这一步按完整路径遍历，
        bsdtar 按相对路径遍历，能归档这样的树（改动之前就能）。"""
        src = self.root / "deep"
        src.mkdir()
        fd = os.open(src, os.O_RDONLY)
        try:
            for i in range(60):  # 60 层 × 21 个字符，超过 1024 字节
                name = f"{i:02d}" + "d" * 19
                os.mkdir(name, dir_fd=fd)
                child = os.open(name, os.O_RDONLY, dir_fd=fd)
                os.close(fd)
                fd = child
            leaf = os.open("leaf.txt", os.O_CREAT | os.O_WRONLY, dir_fd=fd)
            os.write(leaf, b"leaf")
            os.close(leaf)
        finally:
            os.close(fd)
        core.check_source_readable(src)  # 不抛出
        if core.locate("tar") is None:
            self.skipTest("缺少 tar")
        out = self.root / "deep.tar"
        core.ArchiveJob(src, out, "tar", log=silent).run()
        with tarfile.open(out) as tf:
            self.assertTrue(any(n.endswith("/leaf.txt") for n in tf.getnames()))

    @unittest.skipIf(os.geteuid() == 0, "root 不受文件权限限制")
    def test_check_source_readable_rejects_unenterable_folder(self) -> None:
        """有 r 没有 x 的文件夹能列出、进不去：Info-ZIP 会静默漏掉其中的文件，要先报告。"""
        src = self.root / "src"
        sub = src / "nox"
        sub.mkdir(parents=True)
        (sub / "f.txt").write_bytes(b"f")
        sub.chmod(0o644)
        self.addCleanup(sub.chmod, 0o755)
        err = self.raises(core.check_source_readable, src)
        self.assertEqual((err.key, err.denial, err.kw["path"]), ("src_denied", "posix", str(sub)))

    def test_system_protection_wording_and_access_only_checks(self) -> None:
        """SIP：读取被拒用“不能读取”的文案，修改被拒用“不能修改”；只有 os.access 的结论
        （没有 errno）时，带 restricted 标志的位置也按系统保护处理。"""
        system = Path("/System")
        if not os.lstat(system).st_flags & stat.SF_RESTRICTED:
            self.skipTest("/System 没有 restricted 标志")
        self.assertEqual(core.classify_denial(system, None, write=False), "system")
        self.assertEqual(core.classify_denial(system, None, write=True), "system")
        read = core._denied("src_denied", system, errno.EPERM, write=False)
        write = core._denied("access_denied", system, errno.EPERM, write=True)
        self.assertEqual((read.key, read.denial), ("system_read_denied", "system"))
        self.assertEqual((write.key, write.denial), ("system_denied", "system"))
        plain = self.root / "plain.txt"
        plain.write_bytes(b"")
        self.assertEqual(core.classify_denial(plain, None, write=False), "posix")

    def test_same_location(self) -> None:
        """按 inode 判断同一位置：大小写不同或经过符号链接也算同一个；不存在时为 False。"""
        folder = self.root / "Folder"
        folder.mkdir()
        link = self.root / "link"
        link.symlink_to(folder)
        self.assertTrue(core.same_location(folder, link))
        if (self.root / "folder").exists():  # 不区分大小写的卷（APFS 默认）
            self.assertTrue(core.same_location(folder, self.root / "folder"))
        self.assertFalse(core.same_location(folder, self.root))
        self.assertFalse(core.same_location(folder, self.root / "missing"))
        self.assertFalse(core.same_location(folder, None))

    def test_extract_tool_eperm_is_not_privacy(self) -> None:
        """解压只写自己的暂存目录，工具报告的 EPERM（例如不能创建设备文件）与隐私保护无关。"""
        job = core.ExtractJob(self.root / "a.tar", self.root, log=silent)
        with self.assertRaises(core.JobError) as caught:
            job._run_simple(self.fake_tool(self.EPERM_LINE, 1), tool="tar", cwd=self.root)
        self.assertEqual((caught.exception.key, caught.exception.denial), ("tool_failed", None))


class PathInputTests(unittest.TestCase):
    """键入路径的整理、源校验和输出解析；只使用临时目录，不调用外部工具。"""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="cleanpack-test-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        # /tmp 是 /private/tmp 的符号链接，这里用原样路径，验证不会被解析掉。
        self.root = Path(temp.name)
        self.folder = self.root / "My Folder"
        self.folder.mkdir()
        self.file = self.folder / "报告 1.pdf"
        self.file.write_bytes(b"pdf")

    def assert_key(self, key: str, func, *args) -> core.JobError:
        with self.assertRaises(core.JobError) as caught:
            func(*args)
        self.assertEqual(caught.exception.key, key, args)
        return caught.exception

    def test_clean_typed_path(self) -> None:
        clean = core.clean_typed_path
        f = str(self.folder)
        self.assertEqual(clean(""), "")
        self.assertEqual(clean("   "), "")
        self.assertEqual(clean(f"  {f}\n"), f)
        self.assertEqual(clean(f"'{f}'"), f)
        self.assertEqual(clean(f'"{f}"'), f)
        self.assertEqual(clean("file://" + urllib.parse.quote(f)), f)
        self.assertEqual(clean("file://localhost" + urllib.parse.quote(str(self.file))),
                         str(self.file))
        self.assertEqual(clean("~/x"), str(Path.home() / "x"))
        self.assertEqual(clean("~"), str(Path.home()))
        # 终端拖入的转义只在原文不存在时去掉。
        self.assertEqual(clean(f.replace(" ", "\\ ")), f)
        self.assertEqual(clean("/no/such\\ path"), "/no/such\\ path")
        self.assert_key("path_invalid", clean, "file://server/share/x")
        self.assert_key("path_invalid", clean, "/tmp/a\0b")
        self.assert_key("path_invalid", clean, "file:///tmp/a%00b")
        # urlsplit 对无法解析的主机名抛 ValueError，孤立代理项无法编码成路径，都应是 JobError。
        for raw in ("file://[", "file://[abc]/x", "/tmp/\ud800x"):
            err = self.assert_key("path_invalid", clean, raw)
            err.kw["path"].encode("utf-8")  # 提示中的路径必须能正常显示

    def test_clean_keeps_unusual_existing_names(self) -> None:
        """文件名可以以空格结尾或带引号、反斜杠；存在的原文不能被整理坏。"""
        for name in ("trailing ", "'quoted'", "back\\ slash", " lead"):
            p = self.root / name
            p.write_bytes(b"")
            self.assertEqual(core.clean_typed_path(str(p)), str(p), name)
            self.assertEqual(core.validate_source(core.clean_typed_path(str(p))), p, name)

    def test_validate_source_accepts(self) -> None:
        self.assertEqual(core.validate_source(self.folder), self.folder)
        self.assertEqual(core.validate_source(str(self.file)), self.file)
        # 路径末尾的 / 和中间的 . 由 pathlib 规整，不算 ..。
        self.assertEqual(core.validate_source(f"{self.folder}/./"), self.folder)
        # 源本身是链接时返回目标：工具只会保存顶层链接本身，归档里将没有任何内容。
        link = self.root / "link"
        link.symlink_to(self.folder)
        real = Path(os.path.realpath(self.folder))
        self.assertEqual(core.validate_source(link), real)
        self.assertEqual(core.validate_source(core.clean_typed_path(f"{link}/")), real)
        self.assertEqual(core.validate_source("/tmp"), Path(os.path.realpath("/tmp")))
        # 链接所在位置的上级不解析，只解析源本身。
        inner = self.folder / "inner"
        inner.mkdir()
        self.assertEqual(core.validate_source(inner), inner)
        junk_link = self.root / "to-junk"
        (self.root / "._x").write_bytes(b"")
        junk_link.symlink_to(self.root / "._x")
        self.assert_key("src_is_junk", core.validate_source, junk_link)

    def test_validate_source_rejects(self) -> None:
        v = core.validate_source
        self.assert_key("path_not_absolute", v, "relative/path")
        self.assert_key("path_not_absolute", v, "~nosuchuser/x")
        self.assert_key("path_invalid", v, f"{self.folder}/../My Folder")
        self.assert_key("src_is_root", v, "/")
        err = self.assert_key("src_missing", v, self.root / "nope")
        self.assertEqual(err.kw["path"], str(self.root / "nope"))
        self.assert_key("src_missing", v, self.file / "child")  # 上级是文件
        dangling = self.root / "dangling"
        dangling.symlink_to(self.root / "gone")
        self.assert_key("src_missing", v, dangling)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        self.assert_key("src_unsupported_type", v, fifo)

    def test_validate_source_junk_names(self) -> None:
        for name in (".DS_Store", "._x", "__MACOSX", ".Trashes", "Icon\r", ".LSOverride"):
            p = self.root / name
            p.mkdir()
            err = self.assert_key("src_is_junk", core.validate_source, p)
            self.assertNotIn("\r", err.kw["name"])
        for name in ("DS_Store.txt", "a._x", "Icon", "__MACOSX-not"):
            p = self.root / name
            p.mkdir()
            self.assertEqual(core.validate_source(p), p)

    @unittest.skipIf(os.geteuid() == 0, "root 不受文件权限限制")
    def test_validate_source_permissions(self) -> None:
        locked = self.root / "locked"
        locked.mkdir()
        (locked / "inner").write_bytes(b"")
        unreadable = self.root / "unreadable.txt"
        unreadable.write_bytes(b"")
        unreadable.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        locked.chmod(0o300)  # 可写可进入但不可列出
        self.assert_key("src_denied", core.validate_source, locked)
        self.assert_key("src_denied", core.validate_source, unreadable)
        locked.chmod(0)
        self.assert_key("src_denied", core.validate_source, locked / "inner")

    def test_parse_output(self) -> None:
        parse, here = core.parse_output, self.folder
        self.assertIsNone(parse("", here))
        self.assertIsNone(parse("  ", here))
        self.assertEqual(parse("backup", here), (here, "backup"))
        self.assertEqual(parse("backup.ZIP", here), (here, "backup"))
        self.assertEqual(parse("backup.tar.gz", here), (here, "backup"))
        self.assertEqual(parse("v1.2", here), (here, "v1.2"))  # 未知扩展名保留
        self.assertEqual(parse(f"{self.root}/", here), (self.root, None))
        self.assertEqual(parse(str(self.root), here), (self.root, None))  # 已有文件夹
        self.assertEqual(parse(f"{self.root}/new.7z", here), (self.root, "new"))
        self.assertEqual(parse(f"'{self.root}/new name.zip'", here), (self.root, "new name"))
        self.assertEqual(parse("~/x.zip", here), (Path.home(), "x"))
        self.assertEqual(parse("~", here), (Path.home(), None))
        self.assert_key("path_not_absolute", parse, "sub/x.zip", here)
        self.assert_key("path_invalid", parse, f"{self.root}/../x.zip", here)
        for bad in (".zip", ".", "..", f"{self.root}/.tar.zst"):
            self.assert_key("path_invalid", parse, bad, here)
        # 判断是否为文件夹时的系统错误也要转成 JobError，不能直接抛到界面。
        self.assert_key("name_too_long", parse, f"{self.root}/{'a' * 300}/x.zip", here)
        # 终端转义的新输出名：文件还不存在，但去掉转义后的文件夹存在。
        escaped = str(self.folder).replace(" ", "\\ ") + "/new\\ name.zip"
        self.assertEqual(parse(escaped, here), (self.folder, "new name"))
        # 已有文件夹里的新文件名带反斜杠时按原文保留，反斜杠是合法的文件名字符。
        self.assertEqual(parse(f"{self.folder}/a\\ b.zip", here), (self.folder, "a\\ b"))

    @unittest.skipIf(os.geteuid() == 0, "root 不受文件权限限制")
    def test_parse_output_permission_error(self) -> None:
        locked = self.root / "locked"
        locked.mkdir()
        self.addCleanup(locked.chmod, 0o700)
        locked.chmod(0)
        self.assert_key("access_denied", core.parse_output, f"{locked}/sub/x.zip", self.folder)

    def test_strip_ext_ignores_case(self) -> None:
        self.assertEqual(core.strip_ext("a.TAR.GZ"), "a")
        self.assertEqual(core.strip_ext("a.Zip"), "a")
        self.assertEqual(core.strip_ext("a.gz"), "a.gz")

    def test_output_name_too_long(self) -> None:
        """暂存文件名在输出名后加 .part，加上之后超过 255 字节时提前报告。"""
        # APFS 按 UTF-16 码元计数：汉字算 1 个，emoji 算 2 个，加上 .part 正好 255 时可以。
        core.check_compress_output(self.file, self.root / ("长" * 246 + ".zip"))
        core.check_compress_output(self.file, self.root / ("😀" * 123 + ".zip"))
        for name in ("长" * 247 + ".zip", "😀" * 124 + ".zip"):
            self.assert_key("name_too_long", core.check_compress_output, self.file,
                            self.root / name)


class ProgressTests(unittest.TestCase):
    """压缩、解压的进度：每种格式的来源、节流与单调、解析工具输出、剩余时间的估算。"""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="cleanpack-test-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.src = self.root / "src"
        (self.src / "sub").mkdir(parents=True)
        (self.src / "big.bin").write_bytes(os.urandom(3 * 1024 * 1024 + 123))
        (self.src / "sub" / "a.txt").write_text("hello\n" * 500)
        (self.src / ".DS_Store").write_bytes(b"junk" * 1000)
        (self.src / "__MACOSX").mkdir()
        (self.src / "__MACOSX" / "x").write_bytes(b"junk" * 1000)

    @staticmethod
    def recorder() -> tuple[list[tuple[str, int, int | None, str]], Callable]:
        events: list[tuple[str, int, int | None, str]] = []
        return events, lambda *e: events.append(e)

    def setUpProgress(self) -> None:
        """关掉节流，测试才能看到中间的进度（数据只有几 MB，节流期内就做完了）。"""
        patcher = patch.object(core, "PROGRESS_INTERVAL", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_work(self, events, unit: str = "bytes") -> list[tuple[str, int, int | None, str]]:
        """工作阶段的进度单调不减、中间有推进（不是最后才一下补满）、最后补满总量，之后进入
        finish。"""
        work = [e for e in events if e[0] == "work"]
        self.assertTrue(work, events)
        done = [e[1] for e in work]
        self.assertEqual(done, sorted(done))
        total = work[-1][2]
        self.assertIsNotNone(total)
        self.assertEqual((done[-1], work[-1][3]), (total, unit))
        self.assertTrue(any(0 < d < total for d in done), done)
        self.assertEqual(events[-1][0], "finish")
        return work

    def test_source_stats(self) -> None:
        """统计跳过垃圾项，名称以工具使用的源名称开头，tar 大小按 512 字节块估算。"""
        (self.src / "link").symlink_to("big.bin")
        stats = core.check_source_readable(self.src, name="./src", sizes=True)
        big = 3 * 1024 * 1024 + 123
        self.assertEqual(stats.total, big + 3000)
        self.assertEqual(stats.sizes, {"./src": 0, "./src/big.bin": big, "./src/link": 0,
                                       "./src/sub": 0, "./src/sub/a.txt": 3000})
        blocks = sum(512 + -(-s // 512) * 512 for s in stats.sizes.values())
        self.assertEqual((stats.entries, stats.tar_size), (5, 1024 + blocks))
        single = core.check_source_readable(self.src / "big.bin")
        self.assertEqual((single.total, single.entries, single.sizes), (big, 1, None))

    def test_compress_progress_every_format(self) -> None:
        self.setUpProgress()
        cases = [(fmt, None) for fmt in core.FORMATS] + [("zip", "pw-123"), ("7z", "pw-123")]
        for fmt, password in cases:
            if core.missing_tools(fmt, encrypted=password is not None):
                continue
            with self.subTest(fmt=fmt, password=password):
                events, cb = self.recorder()
                out = self.root / f"out{core.FORMATS[fmt].ext}"
                core.ArchiveJob(self.src, out, fmt, log=silent, password=password,
                                progress=cb).run()
                self.assertEqual(events[0][0], "scan")
                work = self.assert_work(events)
                self.assertGreater(work[-1][2], 3 * 1024 * 1024)
                out.unlink()

    def test_extract_progress_every_format(self) -> None:
        self.setUpProgress()
        for fmt in core.FORMATS:
            if core.missing_tools(fmt):  # 解压需要的工具是压缩所需的子集
                continue
            with self.subTest(fmt=fmt):
                archive = self.root / f"a{core.FORMATS[fmt].ext}"
                core.ArchiveJob(self.src, archive, fmt, log=silent).run()
                dest = self.root / f"dest-{fmt}"
                dest.mkdir()
                events, cb = self.recorder()
                core.ExtractJob(archive, dest, log=silent, progress=cb).run()
                self.assertEqual(events[0][0], "prepare")
                unit = "items" if fmt == "7z" else "bytes"
                work = self.assert_work(events, unit)
                size = archive.stat().st_size
                expected = {"tar.zst": 2 * size, "zip": 3 * 1024 * 1024 + 123 + 3000,
                            "7z": 4}.get(fmt, size)
                self.assertEqual(work[-1][2], expected)
                if fmt == "tar.zst":  # 第二遍（真正解压）也在计数，不是校验完就停住
                    self.assertTrue(any(size < e[1] < 2 * size for e in work), work)

    def test_encrypted_zip_extraction_has_no_verbose_listing(self) -> None:
        """有密码时不加 -v（密码提示会粘在名称后面），进度不确定，但照常解出。"""
        if core.missing_tools("zip", encrypted=True):
            self.skipTest("缺少 tar")
        archive = self.root / "enc.zip"
        core.ArchiveJob(self.src, archive, "zip", log=silent, password="pw-123").run()
        dest = self.root / "dest"
        dest.mkdir()
        events, cb = self.recorder()
        lines: list[str] = []
        core.ExtractJob(archive, dest, log=lines.append, password="pw-123", progress=cb).run()
        self.assertFalse(any(" -v " in line for line in lines if line.startswith("$ ")))
        self.assertIn(("work", 0, None, "bytes"), events)

    def test_throttle_and_monotonic(self) -> None:
        events, cb = self.recorder()
        p = core._Progress(cb)
        p.start("work", 100)
        p.update(10)          # 刚开始阶段，节流期内不通知
        p.update(5)           # 倒退的值忽略
        p.update(500)         # 超过总量的按总量算
        self.assertEqual(p.done, 100)
        self.assertEqual(events, [("work", 0, 100, "bytes")])
        time.sleep(core.PROGRESS_INTERVAL + 0.02)
        p.start("finish")
        p.complete()
        self.assertEqual(events[-2:], [("finish", 0, None, "bytes")] * 2)
        core._Progress(None).update(1)  # 没有回调时什么也不做

    def test_estimator(self) -> None:
        est = core.ProgressEstimator(now=0.0)
        self.assertEqual(est.update(0, 1000, 0.0), (None, None))
        self.assertEqual(est.update(50, 1000, 0.01), (None, None))  # 数据太少，不给速度
        self.assertEqual(est.update(100, 1000, 1.0)[1], None)  # 还在预热
        rate, eta = est.update(300, 1000, 3.0)
        self.assertAlmostEqual(rate, 100.0)
        self.assertAlmostEqual(eta, 7.0)
        rate2, eta2 = est.update(300, 1000, 6.0)  # 停住三秒：速度下降，剩余时间变长
        self.assertLess(rate2, rate)
        self.assertGreater(eta2, eta)
        self.assertEqual(est.update(10, 1000, 7.0), (rate2, None))  # 新阶段重新开始
        self.assertIsNone(core.ProgressEstimator(0.0).update(5, None, 9.0)[1])

    def test_eta_parts(self) -> None:
        self.assertEqual(core.eta_parts(0.4), (0, 0, 5))
        self.assertEqual(core.eta_parts(41), (0, 0, 45))
        self.assertEqual(core.eta_parts(64), (0, 1, 0))
        self.assertEqual(core.eta_parts(125), (0, 2, 0))
        self.assertEqual(core.eta_parts(3725), (1, 2, 0))

    def test_tool_output_parsers(self) -> None:
        """7-Zip 的百分比要求后面紧跟退格（成员名里的「50%」不算）；退格覆盖的部分不进日志；
        -v 只认得已知的名称，粘在名称后的密码提示去掉，报错行照常记日志。"""
        out = (b"  0%\x08\x08\x08\x08    \x08\x08\x08\x08+ src/50% off.txt\n"
               b" 75% 1\x08\x08\x08\x08\x08\x08      \x08\x08\x08\x08\x08\x08+ src/b\n")
        self.assertEqual([int(m) for m in core._SEVENZIP_PERCENT.findall(out)], [0, 75])
        self.assertEqual(core._strip_backspaces("  0%\x08\x08\x08\x08    \x08\x08\x08\x08+ a"),
                         "+ a")
        self.assertEqual(core._strip_backspaces(" 75% 2\x08\x08\x08\x08\x08\x08  \x08\x08"), "")
        self.assertEqual(core._strip_backspaces("+ plain"), "+ plain")

        logged: list[str] = []
        events, cb = self.recorder()
        job = core.ArchiveJob(self.src, self.root / "o.zip", "zip", log=logged.append,
                              progress=cb)
        job._track_verbose("a ", {"src": 0, "src/a b": 10, "src/c": 5}, 15)
        job._note_pending(b"a src/a b")
        self.assertEqual(job._current, "src/a b")
        job._emit_line(b"a src/a bEnter passphrase:", "tar")
        job._emit_line(b"a src/a b: Can't open", "tar")   # 不是已知名称：照常记日志
        job._emit_line(b"a src/c", "tar")
        self.assertEqual((job._verbose_done, logged), (15, ["a src/a b: Can't open"]))
        self.assertEqual(job._progress.done, 15)
        job._verbose = None
        job._emit_line(b"  0%\x08\x08\x08\x08    \x08\x08\x08\x08+ src/b", "7z")
        job._emit_line(b" 75% 2\x08\x08\x08\x08\x08\x08      \x08\x08\x08\x08\x08\x08", "7z")
        self.assertEqual(logged[1:], ["+ src/b"])  # 只剩进度的行不记日志

        # 7-Zip 的百分比被切在两个数据块之间也能读到
        job._progress.start("work", 200)
        watch = job._percent_watcher(lambda pct: pct * 2)
        watch(b" 4")
        watch(b"2%\x08\x08\x08")
        self.assertEqual(job._progress.done, 84)

    def test_relay_keeps_blame_and_reports_write_errors(self) -> None:
        """经中转线程连接时，归因与直接相连相同；写输出文件失败（例如磁盘已满）照实报告。"""
        upstream = [sys.executable, "-c",
                    "import sys\nfor _ in range(64): sys.stdout.buffer.write(b'x' * 65536)"]
        failing = [sys.executable, "-c", "import sys; sys.stdin.buffer.read(10); sys.exit(3)"]
        seen: list[int] = []
        job = core.ArchiveJob(self.src, self.root / "o.tar.gz", "tar.gz", log=silent)
        with open(self.root / "out", "wb") as f, self.assertRaises(core.JobError) as caught:
            job._run_pipeline(upstream, failing, tools=("tar", "gzip"), cwd=self.root,
                              stdout=f, blame_downstream=True, meter=seen.append)
        self.assertEqual(caught.exception.kw["tool"], "gzip")
        bad_tar = [sys.executable, "-c", "import sys; sys.stdout.write('ab'); sys.exit(2)"]
        cat = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]
        seen.clear()
        job = core.ArchiveJob(self.src, self.root / "o.tar.gz", "tar.gz", log=silent)
        with open(self.root / "out", "wb") as f, self.assertRaises(core.JobError) as caught:
            job._run_pipeline(bad_tar, cat, tools=("tar", "gzip"), cwd=self.root,
                              stdout=f, blame_downstream=True, meter=seen.append)
        self.assertEqual((caught.exception.kw["tool"], seen), ("tar", [2]))
        self.assertEqual((self.root / "out").read_bytes(), b"ab")

        class Full(io.BytesIO):
            def write(self, data: bytes) -> int:
                raise OSError(errno.ENOSPC, "No space left on device")
        job = core.ArchiveJob(self.src, self.root / "o.tar", "tar", log=silent)
        with self.assertRaises(OSError) as raised:
            job._run_simple(upstream, tool="tar", cwd=self.root, stdout=Full(), meter=seen.append)
        self.assertEqual(raised.exception.errno, errno.ENOSPC)


if __name__ == "__main__":
    unittest.main()
