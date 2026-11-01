"""使用真实压缩工具验证六种格式的排除、内容和失败清理；数据仅写入 /tmp。"""
from __future__ import annotations

import errno
import fnmatch
import io
import os
from pathlib import Path
import shlex
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
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
        # 跳过开始时的源校验，让 tar 自己因为找不到源而失败。
        with patch.object(core, "validate_source", lambda p: Path(p)), \
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
        self.assertEqual(caught.exception.key, "access_denied")
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


if __name__ == "__main__":
    unittest.main()
