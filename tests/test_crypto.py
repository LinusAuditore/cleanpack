"""加密压缩与解压测试：真实工具往返、外部工具交叉验证、错误密码、密码泄露与终端挂起。

数据只写入 /tmp 下的临时目录；缺少工具时明确跳过对应用例。
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import textwrap
import time
import unicodedata
import unittest
from unittest.mock import patch
import zipfile
import zlib

import core
from test_extract import DECOYS, ExtractBase, nfc_tree, silent

PASSWORD = " Pa ss!w0rd ~ "  # 含首尾空格和符号：密码按整行原样传递，空格都不能丢
UNICODE_PASSWORD = "密码 ünï 🔑"


def zipcrypto_check_byte(password: bytes, header: bytes) -> int:
    """按 PKWARE 传统加密算法解密 12 字节的加密头，返回最后一个字节（密码校验字节）。"""
    def crc(c: int, b: int) -> int:
        return zlib.crc32(bytes([b]), c ^ 0xFFFFFFFF) ^ 0xFFFFFFFF

    k0, k1, k2 = 0x12345678, 0x23456789, 0x34567890

    def update(b: int) -> None:
        nonlocal k0, k1, k2
        k0 = crc(k0, b)
        k1 = ((k1 + (k0 & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
        k2 = crc(k2, k1 >> 24)

    for b in password:
        update(b)
    plain = 0
    for c in header:
        t = (k2 | 2) & 0xFFFF
        plain = c ^ (((t * (t ^ 1)) >> 8) & 0xFF)
        update(plain)
    return plain


class CryptoBase(ExtractBase):
    """加密用例的公共部分：带中文名、链接、垃圾和近似名称的源文件夹。"""

    def make_source(self) -> Path:
        src = self.root / "src" / "机密 café"
        for rel, data in {"keep.txt": b"keep\n", "sub/中文.txt": "多层\n".encode(),
                          "sub/deep/x.bin": os.urandom(5000), **DECOYS}.items():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_bytes(data)
        (src / "link-in").symlink_to("keep.txt")
        for rel in ("sub/._x", ".DS_Store", "__MACOSX/._y"):
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_bytes(b"junk")
        return src

    @staticmethod
    def expected(src: Path) -> dict[str, bytes | str]:
        return {k: v for k, v in nfc_tree(src).items()
                if not any(core.is_junk_name(p) for p in k.split("/"))}

    def compress(self, src: Path, fmt: str, name: str, password: str = PASSWORD,
                 **kw: object) -> Path:
        out = self.arch / (name + core.FORMATS[fmt].ext)
        core.ArchiveJob(src, out, fmt, log=silent, password=password, **kw).run()
        return out

    def assert_extract_fails(self, archive: Path, key: str, password: str | None) -> None:
        job = core.ExtractJob(archive, self.dest, log=silent, password=password)
        with self.assertRaises(core.JobError) as caught:
            job.run()
        self.assertEqual(caught.exception.key, key, caught.exception.kw)
        self.assert_dest_untouched()

    def clear_dest(self) -> None:
        core.make_tree_accessible(self.dest)
        shutil.rmtree(self.dest)
        self.dest.mkdir()


class RoundTripTests(CryptoBase):
    """CleanPack 加密压缩、再用密码解压，结果应与源文件夹（去掉垃圾）一致。"""

    def test_zip_methods_and_levels(self) -> None:
        self.require("tar")
        src = self.make_source()
        for method in core.ZIP_METHODS:
            for level in (0, 6, 9):
                with self.subTest(method=method, level=level):
                    out = self.compress(src, "zip", f"z-{method}-{level}", level=level,
                                        zip_method=method)
                    result, job, _ = self.extract(out, password=PASSWORD)
                    self.assertEqual(unicodedata.normalize("NFC", result.name), src.name)
                    self.assertEqual(nfc_tree(result), self.expected(src))
                    self.clear_dest()

    def test_zip_headers(self) -> None:
        """有内容的文件成员都加密；非 ASCII 名称带 UTF-8 标志；级别 0 不压缩；方式与选择一致。

        bsdtar 不加密符号链接成员（目标路径以明文存储）和空文件，界面的说明里写明了这一点；
        这里把它固定下来，工具行为一变测试就会提醒。
        """
        self.require("tar")
        src = self.make_source()
        (src / "empty.txt").write_bytes(b"")
        for method, level in (("zipcrypt", 0), ("zipcrypt", 9), ("aes256", 0), ("aes256", 9)):
            with self.subTest(method=method, level=level):
                out = self.compress(src, "zip", f"h-{method}-{level}", level=level,
                                    zip_method=method)
                with zipfile.ZipFile(out) as zf:
                    infos = zf.infolist()
                links = [i for i in infos if (i.external_attr >> 16) & 0o170000 == 0o120000]
                files = [i for i in infos if not i.is_dir() and i not in links and i.file_size]
                self.assertEqual([i.filename.split("/")[-1] for i in links], ["link-in"])
                self.assertFalse(links[0].flag_bits & 0x1)
                self.assertTrue(files)
                self.assertTrue(all(i.flag_bits & 0x1 for i in files))
                for i in infos:
                    if not i.filename.isascii():
                        self.assertTrue(i.flag_bits & 0x800, i.filename)
                for i in files:
                    if method == "aes256":
                        # WinZip AES：方法 99，真正的压缩方法在 0x9901 扩展字段里，强度 3 = 256 位。
                        self.assertEqual(i.compress_type, 99)
                        at = i.extra.find(b"\x01\x99")
                        self.assertGreaterEqual(at, 0)
                        strength, actual = struct.unpack("<BH", i.extra[at + 8:at + 11])
                        self.assertEqual(strength, 3)
                        self.assertEqual(actual, 0 if level == 0 else 8)
                    else:
                        self.assertEqual(i.compress_type, 0 if level == 0 else 8)
                names = {unicodedata.normalize("NFC", i.filename) for i in infos}
                self.assertFalse(any(core.is_junk_name(p) for n in names for p in n.split("/")))

    def test_7z_names(self) -> None:
        self.require("tar", "7z")
        src = self.make_source()
        for names in (True, False):
            with self.subTest(encrypt_names=names):
                out = self.compress(src, "7z", f"s-{names}", encrypt_names=names)
                result, _, _ = self.extract(out, password=PASSWORD)
                self.assertEqual(nfc_tree(result), self.expected(src))
                self.clear_dest()
                # 不给密码列目录：加密文件名时打不开，否则能看到成员名但内容仍加密。
                listing = subprocess.run([core.locate("7z"), "l", "-p", str(out)],
                                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
                if names:
                    self.assertNotEqual(listing.returncode, 0)
                    self.assertNotIn("keep.txt", listing.stdout)
                else:
                    self.assertIn("keep.txt", listing.stdout)
                    self.assertIn("7zAES", listing.stdout)

    def test_7z_unicode_password(self) -> None:
        self.require("tar", "7z")
        src = self.make_source()
        out = self.compress(src, "7z", "u", password=UNICODE_PASSWORD)
        result, _, _ = self.extract(out, password=UNICODE_PASSWORD)
        self.assertEqual(nfc_tree(result), self.expected(src))
        self.clear_dest()
        self.assert_extract_fails(out, "archive_wrong_password", "密码")

    def test_single_file_source(self) -> None:
        self.require("tar", "7z")
        f = self.root / "报告.txt"
        f.write_bytes(b"secret\n")
        for fmt in core.ENCRYPTABLE:
            with self.subTest(fmt=fmt):
                out = self.compress(f, fmt, "one")
                result, _, _ = self.extract(out, password=PASSWORD)
                self.assertEqual(result.read_bytes(), b"secret\n")
                self.clear_dest()


class InteropTests(CryptoBase):
    """其他工具用同一个密码能打开 CleanPack 生成的加密归档。"""

    def test_7zz_tests_every_archive(self) -> None:
        self.require("tar", "7z")
        src = self.make_source()
        seven = core.locate("7z")
        outs = [self.compress(src, "zip", f"i-{m}", zip_method=m) for m in core.ZIP_METHODS]
        outs += [self.compress(src, "7z", "i-7z")]
        for out in outs:
            with self.subTest(out=out.name):
                # 测试里可以把密码放进命令行；应用本身绝不这样做。
                ok = subprocess.run([seven, "t", "-p" + PASSWORD, str(out)],
                                    capture_output=True, stdin=subprocess.DEVNULL)
                self.assertEqual(ok.returncode, 0, ok.stdout[-500:])
                bad = subprocess.run([seven, "t", "-pwrong", str(out)],
                                     capture_output=True, stdin=subprocess.DEVNULL)
                self.assertNotEqual(bad.returncode, 0)

    def test_unzip_reads_zipcrypto(self) -> None:
        self.require("tar")
        unzip = shutil.which("unzip")
        if unzip is None:
            self.skipTest("缺少 unzip")
        out = self.compress(self.make_source(), "zip", "u", zip_method="zipcrypt")
        r = subprocess.run([unzip, "-tqP", PASSWORD, str(out)], capture_output=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_max_length_password_opens_in_7zz(self) -> None:
        """PASSWORD_MAX 个字符的密码：bsdtar 写的 AES zip，7-Zip 也要能打开。"""
        self.require("tar", "7z")
        pw = "k" * core.PASSWORD_MAX
        out = self.compress(self.make_source(), "zip", "long", password=pw)
        r = subprocess.run([core.locate("7z"), "t", "-p" + pw, str(out)], capture_output=True,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout[-300:])

    def test_extracts_7zz_made_zips(self) -> None:
        """别人用 7-Zip 生成的 AES 和 ZipCrypto zip 也能用密码解开。"""
        self.require("tar", "7z")
        src = self.make_source()
        for method in ("AES256", "ZipCrypto"):
            with self.subTest(method=method):
                out = self.arch / f"seven-{method}.zip"
                subprocess.run([core.locate("7z"), "a", "-tzip", f"-mem={method}", "-snl",
                                "-p" + PASSWORD, "-bso0", "-bsp0", str(out), src.name],
                               cwd=src.parent, check=True, stdin=subprocess.DEVNULL)
                result, _, _ = self.extract(out, password=PASSWORD)
                self.assertEqual(nfc_tree(result), self.expected(src))
                self.clear_dest()

    def test_7zz_zip_with_encrypted_links(self) -> None:
        """7-Zip 用 -snl 生成的加密 zip 会加密链接成员：bsdtar 对 AES 的报错，对 ZipCrypto 的
        却把密文写成链接目标并报告成功。这种归档交给 7-Zip；没有 7-Zip 时给出明确提示。"""
        self.require("tar", "7z")
        src = self.make_source()
        for method in ("AES256", "ZipCrypto"):
            with self.subTest(method=method):
                self.check_encrypted_links(src, method)

    def check_encrypted_links(self, src: Path, method: str) -> None:
        out = self.arch / f"links-{method}.zip"
        subprocess.run([core.locate("7z"), "a", "-tzip", f"-mem={method}", "-snl",
                        "-p" + PASSWORD, "-bso0", "-bsp0", str(out), src.name],
                       cwd=src.parent, check=True, stdin=subprocess.DEVNULL)
        self.assertEqual(core.zip_encryption(out), (True, True))
        result, _, lines = self.extract(out, password=PASSWORD)
        self.assertEqual(nfc_tree(result), self.expected(src))
        # 中央目录显示有加密的链接成员，直接交给 7-Zip，不先让 bsdtar 试。
        commands = [line for line in lines if line.startswith("$ ")]
        self.assertEqual(len(commands), 1)
        self.assertIn(" x ", commands[0])
        self.assertTrue(commands[0].startswith("$ " + core.locate("7z")))
        self.clear_dest()
        self.assert_extract_fails(out, "archive_wrong_password", "wrong")
        real = core.locate
        with patch.object(core, "locate", lambda k: None if k == "7z" else real(k)):
            # 没装 7-Zip 时不先要密码：没给密码、密码错误、密码正确都直接说明需要 7-Zip。
            for pw in (None, "wrong", PASSWORD):
                self.assert_extract_fails(out, "archive_zip_needs_7z", pw)


class WrongPasswordTests(CryptoBase):
    """没有密码时报需要密码，密码错误时报密码不正确，都不留下任何东西。"""

    def test_each_format(self) -> None:
        self.require("tar", "7z")
        src = self.make_source()
        outs = [self.compress(src, "zip", f"w-{m}", zip_method=m) for m in core.ZIP_METHODS]
        outs += [self.compress(src, "7z", f"w-7z-{n}", encrypt_names=n) for n in (True, False)]
        for out in outs:
            with self.subTest(out=out.name):
                self.assert_extract_fails(out, "archive_encrypted", None)
                self.assert_extract_fails(out, "archive_wrong_password", "wrong")
                self.assert_extract_fails(out, "archive_wrong_password", PASSWORD.strip())

    def test_wrong_password_is_fast_for_many_entries(self) -> None:
        """bsdtar 对密码错误的成员会重试上万次（AES 每个成员要几秒）；事先校验后应立即失败。"""
        self.require("tar")
        src = self.root / "many"
        src.mkdir()
        for i in range(60):
            (src / f"f{i}.txt").write_bytes(b"x" * i)
        for method in core.ZIP_METHODS:
            with self.subTest(method=method):
                out = self.compress(src, "zip", f"many-{method}", zip_method=method)
                start = time.monotonic()
                self.assert_extract_fails(out, "archive_wrong_password", "wrong")
                self.assertLess(time.monotonic() - start, 5)

    def test_wrong_zip_password_rejected_before_tools(self) -> None:
        """预检能确定密码错误时直接报告，不启动任何外部工具。"""
        self.require("tar")
        src = self.make_source()
        outs = [self.compress(src, "zip", f"pre-{m}", zip_method=m) for m in core.ZIP_METHODS]
        for out in outs:
            with self.subTest(out=out.name):
                with patch.object(subprocess, "Popen", side_effect=AssertionError("ran a tool")):
                    self.assert_extract_fails(out, "archive_wrong_password", "wrong")

    def test_password_check_across_producers(self) -> None:
        """事先校验不能误判正确的密码：bsdtar、7-Zip（AES-128/192/256、ZipCrypto）、Info-ZIP
        （包括从管道写入、带数据描述符的成员）生成的 zip 都要验证。"""
        self.require("tar", "7z", "zip")
        src = self.make_source()
        outs = [self.compress(src, "zip", f"p-{m}", zip_method=m) for m in core.ZIP_METHODS]
        for method in ("AES128", "AES192", "AES256", "ZipCrypto"):
            out = self.arch / f"p7-{method}.zip"
            subprocess.run([core.locate("7z"), "a", "-tzip", f"-mem={method}", "-p" + PASSWORD,
                            "-bso0", "-bsp0", str(out), src.name], cwd=src.parent, check=True,
                           stdin=subprocess.DEVNULL)
            outs.append(out)
        zip_tool = core.locate("zip")
        out = self.arch / "pz.zip"
        subprocess.run([zip_tool, "-q", "-r", "-P", PASSWORD, str(out), src.name],
                       cwd=src.parent, check=True, stdin=subprocess.DEVNULL)
        outs.append(out)
        out = self.arch / "pz-stream.zip"
        subprocess.run([zip_tool, "-q", "-P", PASSWORD, str(out), "-"], input=b"streamed" * 99,
                       check=True)
        with zipfile.ZipFile(out) as zf:
            self.assertTrue(zf.infolist()[0].flag_bits & 0x8)  # 确实带数据描述符
        outs.append(out)
        for out in outs:
            with self.subTest(out=out.name):
                self.assertTrue(core.zip_is_encrypted(out))
                strength = "strong" if "aes" in out.name.lower() else "weak"
                self.assertEqual(core.check_zip_password(out, PASSWORD), strength)
                self.assertEqual(core.check_zip_password(out, "wrong"), "wrong")
                result, _, _ = self.extract(out, password=PASSWORD)
                self.clear_dest()

    def zipcrypto_false_accepts(self, out: Path, count: int) -> list[str]:
        """找出能通过唯一成员的 ZipCrypto 校验字节、但并不正确的密码。"""
        with zipfile.ZipFile(out) as zf:
            (info,) = [i for i in zf.infolist() if not i.is_dir()]
        with open(out, "rb") as f:
            f.seek(info.header_offset)
            local = f.read(30)
            name_len, extra_len = struct.unpack("<HH", local[26:30])
            f.seek(info.header_offset + 30 + name_len + extra_len)
            header = f.read(12)
        # 用数据描述符（bit 3）时校验字节是修改时间的高字节，否则是 CRC 的高字节。
        expect = (info._raw_time >> 8) & 0xFF if info.flag_bits & 0x8 else info.CRC >> 24
        self.assertEqual(zipcrypto_check_byte(PASSWORD.encode(), header), expect)
        found = []
        for i in range(20000):
            pw = f"guess{i}"
            if zipcrypto_check_byte(pw.encode(), header) == expect:
                found.append(pw)
                if len(found) == count:
                    break
        self.assertEqual(len(found), count)
        return found

    def test_zipcrypto_false_accept_never_succeeds(self) -> None:
        """能通过校验字节的错误密码会解出乱码；任务必须失败，而不是“成功”交出乱码。
        装了 7-Zip 时交给它复核，明确报密码不正确；没装时只能说“密码或数据有问题”。"""
        self.require("tar", "7z")
        f = self.root / "single.txt"
        f.write_bytes(b"0123456789" * 500)
        real = core.locate
        for level in (0, 6):  # 0 走 CRC 校验，6 先在解压时出错
            with self.subTest(level=level):
                out = self.compress(f, "zip", f"fa-{level}", level=level, zip_method="zipcrypt")
                for pw in self.zipcrypto_false_accepts(out, 3):
                    self.assert_extract_fails(out, "archive_wrong_password", pw)
                    with patch.object(core, "locate", lambda k: None if k == "7z" else real(k)):
                        self.assert_extract_fails(out, "archive_password_or_damaged", pw)

    def test_zipcrypto_random_guesses(self) -> None:
        """几百个随机错误密码逐个尝试，没有一个能解压成功。"""
        self.require("tar")
        out = self.compress(self.make_source(), "zip", "rand", zip_method="zipcrypt")
        for i in range(300):
            job = core.ExtractJob(out, self.dest, log=silent, password=f"nope-{i}")
            with self.assertRaises(core.JobError) as caught:
                job.run()
            self.assertIn(caught.exception.key,
                          ("archive_wrong_password", "archive_password_or_damaged"))
        self.assert_dest_untouched()


class LeakTests(CryptoBase):
    """密码不能出现在任何子进程的命令行、日志或错误参数里。"""

    def test_password_never_in_argv_or_log(self) -> None:
        self.require("tar", "7z")
        src = self.make_source()
        argvs: list[list[str]] = []
        real = subprocess.Popen

        def recording(cmd: list[str], *a: object, **kw: object) -> subprocess.Popen:
            argvs.append(list(cmd))
            return real(cmd, *a, **kw)

        lines: list[str] = []
        errors: list[core.JobError] = []
        with patch.object(subprocess, "Popen", recording):
            for fmt, kw in (("zip", {"zip_method": "aes256"}), ("zip", {"zip_method": "zipcrypt"}),
                            ("7z", {"encrypt_names": True})):
                out = self.arch / (f"leak-{kw}" + core.FORMATS[fmt].ext)
                core.ArchiveJob(src, out, fmt, log=lines.append, password=PASSWORD, **kw).run()
                core.ExtractJob(out, self.dest, log=lines.append, password=PASSWORD).run()
                self.clear_dest()
                for pw in ("wrong", None):
                    try:
                        core.ExtractJob(out, self.dest, log=lines.append, password=pw).run()
                    except core.JobError as exc:
                        errors.append(exc)
        self.assertGreater(len(argvs), 6)
        for cmd in argvs:
            self.assertFalse(any(PASSWORD in arg or "wrong" in arg for arg in cmd), cmd)
        self.assertFalse([line for line in lines if PASSWORD in line])
        self.assertFalse([line for line in lines if "Enter pass" in line or "Enter password" in line])
        for exc in errors:
            self.assertNotIn(PASSWORD, repr(exc.kw))

    def test_controlling_terminal_does_not_hang(self) -> None:
        """从终端启动应用时，工具不能去 /dev/tty 等密码：用 script 提供伪终端运行一遍。"""
        self.require("tar", "7z")
        src = self.make_source()
        code = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(Path(core.__file__).parent)!r})
            import core
            src, arch, dest = map(core.Path, sys.argv[1:4])
            for fmt in ("zip", "7z"):
                out = arch / ("tty" + core.FORMATS[fmt].ext)
                core.ArchiveJob(src, out, fmt, log=lambda _: None, password={PASSWORD!r}).run()
                core.ExtractJob(out, dest, log=lambda _: None, password={PASSWORD!r}).run()
                try:
                    core.ExtractJob(out, dest, log=lambda _: None, password="wrong").run()
                except core.JobError as exc:
                    assert exc.key == "archive_wrong_password", exc.key
            print("TTY-OK")
        """)
        r = subprocess.run(["/usr/bin/script", "-q", "/dev/null", sys.executable, "-B", "-c", code,
                            str(src), str(self.arch), str(self.dest)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        self.assertIn("TTY-OK", r.stdout, r.stdout[-800:] + r.stderr[-800:])


class ValidationTests(CryptoBase):
    """密码校验、格式限制和工具需求。"""

    def test_check_password(self) -> None:
        ok = [("a", "zip"), ("k" * core.PASSWORD_MAX, "zip"), (" spaced ", "zip"),
              ("k" * core.EXTRACT_PASSWORD_MAX, None),
              ("~!@#$%^&*()", "zip"), (UNICODE_PASSWORD, "7z"), (UNICODE_PASSWORD, None)]
        for pw, fmt in ok:
            with self.subTest(pw=pw, fmt=fmt):
                core.check_password(pw, fmt)
        bad = [("", "zip", "password_empty"), ("", None, "password_empty"),
               ("k" * (core.PASSWORD_MAX + 1), "7z", "password_too_long"),
               ("k" * (core.EXTRACT_PASSWORD_MAX + 1), None, "password_too_long"),
               ("a\nb", "7z", "password_invalid_char"), ("a\tb", "zip", "password_invalid_char"),
               ("a\x00", None, "password_invalid_char"), ("a\ud800", "7z", "password_invalid_char"),
               ("a\x7f", "7z", "password_invalid_char"), ("café", "zip", "password_not_ascii"),
               ("全角", "zip", "password_not_ascii")]
        for pw, fmt, key in bad:
            with self.subTest(pw=pw, fmt=fmt):
                with self.assertRaises(core.JobError) as caught:
                    core.check_password(pw, fmt)
                self.assertEqual(caught.exception.key, key)
                self.assertNotIn(pw or "\x01", repr(caught.exception.kw))

    def test_tar_formats_refuse_password(self) -> None:
        src = self.make_source()
        for fmt in core.FORMATS:
            if fmt in core.ENCRYPTABLE:
                continue
            with self.subTest(fmt=fmt):
                out = self.arch / ("t" + core.FORMATS[fmt].ext)
                with self.assertRaises(core.JobError) as caught:
                    core.ArchiveJob(src, out, fmt, log=silent, password=PASSWORD).run()
                self.assertEqual(caught.exception.key, "encrypt_unsupported")
                self.assertFalse(out.exists())

    def test_bad_password_rejected_before_running_tools(self) -> None:
        src = self.make_source()
        with patch.object(subprocess, "Popen", side_effect=AssertionError("ran a tool")):
            with self.assertRaises(core.JobError) as caught:
                core.ArchiveJob(src, self.arch / "x.zip", "zip", log=silent,
                                password="naïve").run()
        self.assertEqual(caught.exception.key, "password_not_ascii")
        self.assertEqual(os.listdir(self.arch), [])

    def test_encrypted_zip_needs_tar_not_zip(self) -> None:
        paths = {"zip": None, "tar": "/usr/bin/tar", "7z": None}
        self.assertEqual(core.missing_tools("zip", paths), ["zip"])
        self.assertEqual(core.missing_tools("zip", paths, encrypted=True), [])
        self.assertEqual(core.missing_tools("7z", paths, encrypted=True), ["7z"])

    def test_encrypted_7z_without_7zz(self) -> None:
        self.require("tar", "7z")
        out = self.compress(self.make_source(), "7z", "no7")
        real = core.locate
        with patch.object(core, "locate", lambda k: None if k == "7z" else real(k)):
            # 不能先问密码再说缺 7-Zip：没给密码时就直接说明需要 7-Zip。
            self.assert_extract_fails(out, "archive_encrypted_needs_7z", None)
            self.assert_extract_fails(out, "archive_encrypted_needs_7z", PASSWORD)

    def test_password_ignored_for_unencrypted_archives(self) -> None:
        """界面只在需要时给密码，但即使给了，未加密的归档也照常解压。"""
        self.require("tar", "7z", "zip")
        src = self.make_source()
        for fmt in ("zip", "7z", "tar.gz"):
            with self.subTest(fmt=fmt):
                out = self.arch / ("plain" + core.FORMATS[fmt].ext)
                core.ArchiveJob(src, out, fmt, log=silent).run()
                result, _, _ = self.extract(out, password=PASSWORD)
                self.assertEqual(nfc_tree(result), self.expected(src))
                self.clear_dest()

    def test_prompt_prefix_is_stripped(self) -> None:
        """bsdtar 的提示不换行，会粘在报错前面；去掉后才能识别出密码错误。"""
        line = "Enter passphrase:" * 40 + "a b.txt: Too many incorrect passphrases: Unknown error: -1"
        stripped = core._PROMPTS.sub("", line)
        self.assertEqual(stripped, "a b.txt: Too many incorrect passphrases: Unknown error: -1")
        self.assertTrue(core._mentions_encryption(stripped, "tar"))
        self.assertEqual(core._PROMPTS.sub("", "Enter password:"), "")
        # 成员名本身以提示开头时只去掉提示，消息仍在行尾，判断不受影响。
        self.assertFalse(core._mentions_encryption(
            core._PROMPTS.sub("", "Enter passphrase:x.txt: Path contains '..'"), "tar"))


class ReviewRegressionTests(CryptoBase):
    """第一轮评审确认的问题，每个用例对应一个。"""

    def test_truncated_zip_wrong_password_is_fast(self) -> None:
        """中央目录读不了时预检无从判断；bsdtar 第二次要密码就应立即结束，而不是重试上万次。"""
        self.require("tar")
        src = self.root / "trunc"
        src.mkdir()
        for i in range(6):
            (src / f"f{i}.bin").write_bytes(os.urandom(3000))
        out = self.compress(src, "zip", "t", zip_method="aes256")
        data = out.read_bytes()
        out.write_bytes(data[:-300])
        self.assertIsNone(core.zip_encryption(out))
        start = time.monotonic()
        self.assert_extract_fails(out, "archive_encrypted", None)  # 假密码让 bsdtar 立即失败
        self.assertLess(time.monotonic() - start, 5)
        start = time.monotonic()
        self.assert_extract_fails(out, "archive_wrong_password", "wrong")
        self.assertLess(time.monotonic() - start, 5)

    def test_7z_crc_failed_means_wrong_password(self) -> None:
        """级别 0、不加密文件名的 7z，错误密码时 7-Zip 只报 CRC Failed in encrypted file。"""
        self.require("tar", "7z")
        out = self.compress(self.make_source(), "7z", "crc", level=0, encrypt_names=False)
        self.assert_extract_fails(out, "archive_wrong_password", "wrong")

    def test_deflate64_zip_falls_back_to_7z(self) -> None:
        """bsdtar 不支持 Deflate64：密码正确时不能报“密码可能不对”，应改用 7-Zip。"""
        self.require("tar", "7z")
        # 可压缩的大文件才会真的用上 Deflate64；不放链接，免得走“加密链接成员”那条路线。
        src = self.root / "d64src"
        (src / "sub").mkdir(parents=True)
        (src / "words.txt").write_bytes(b"compressible text line\n" * 4000)
        (src / "sub" / "more.txt").write_bytes(b"0123456789" * 3000)
        for name, args in (("aes", ["-mem=AES256", "-p" + PASSWORD]),
                           ("zc", ["-mem=ZipCrypto", "-p" + PASSWORD]), ("plain", [])):
            with self.subTest(kind=name):
                out = self.arch / f"d64-{name}.zip"
                subprocess.run([core.locate("7z"), "a", "-tzip", "-mm=Deflate64", *args,
                                "-bso0", "-bsp0", str(out), src.name], cwd=src.parent,
                               check=True, stdin=subprocess.DEVNULL)
                with zipfile.ZipFile(out) as zf:  # 确认真的用了 Deflate64（方法 9）
                    infos = [i for i in zf.infolist() if not i.is_dir()]
                if not args:
                    self.assertTrue(all(i.compress_type == 9 for i in infos))
                pw = PASSWORD if args else None
                result, _, lines = self.extract(out, password=pw)
                self.assertEqual(nfc_tree(result), self.expected(src))
                if args:  # 加密的成员 bsdtar 解不了，按中央目录直接交给 7-Zip
                    commands = [line for line in lines if line.startswith("$ ")]
                    self.assertTrue(commands[0].startswith("$ " + core.locate("7z")), commands)
                else:     # 未加密的先让 bsdtar 试，它报不支持后改用 7-Zip
                    self.assertIn("→ 7z", lines)
                self.clear_dest()
                real = core.locate
                with patch.object(core, "locate", lambda k: None if k == "7z" else real(k)):
                    self.assert_extract_fails(out, "archive_zip_needs_7z", pw)

    def test_aes_failure_reports_real_cause(self) -> None:
        """全是 AES 成员时预检已确认密码，bsdtar 因别的原因失败（这里伪造一个失败的 tar）
        应照实报告，而不是“密码可能不对”，否则界面会一直重新询问密码。"""
        self.require("tar")
        src = self.make_source()
        out = self.compress(src, "zip", "aesfail", zip_method="aes256")
        failing = self.root / "fake-tar"
        failing.write_text("#!/bin/sh\necho 'x: Write failed: No space left on device' >&2\nexit 1\n")
        failing.chmod(0o755)
        real = core.locate
        with patch.object(core, "locate", lambda k: str(failing) if k == "tar" else real(k)):
            self.assert_extract_fails(out, "tool_failed", PASSWORD)
        # ZipCrypto 也一样：与数据无关的失败照实报告；只有数据校验失败才是“密码或数据有问题”。
        zc = self.compress(src, "zip", "zcfail", zip_method="zipcrypt")
        with patch.object(core, "locate", lambda k: str(failing) if k == "tar" else real(k)):
            self.assert_extract_fails(zc, "tool_failed", PASSWORD)
        failing.write_text("#!/bin/sh\necho 'f.txt: ZIP bad CRC: 0x1 should be 0x2: "
                           "Unknown error: -1' >&2\nexit 1\n")
        # 没装 7-Zip：分不清原因，报“密码或数据有问题”。
        with patch.object(core, "locate",
                          lambda k: str(failing) if k == "tar" else None if k == "7z" else real(k)):
            self.assert_extract_fails(zc, "archive_password_or_damaged", PASSWORD)
        # 装了 7-Zip：交给它复核，归档本身没问题，于是正常解开。
        if real("7z"):
            with patch.object(core, "locate", lambda k: str(failing) if k == "tar" else real(k)):
                result, _, lines = self.extract(zc, password=PASSWORD)
            self.assertIn("→ 7z", lines)
            self.assertEqual(nfc_tree(result), self.expected(src))

    def test_forged_passphrase_message_in_tar(self) -> None:
        """tar 里伪造的 “Incorrect passphrase” 不能让任务报成已加密（界面会无休止地要密码）。"""
        self.require("tar")
        link = self.info("dd", b"2", linkname=".")
        arch = self.tar_with("forged.tar", [link, ("dd/x: Incorrect passphrase", b"x")])
        job = core.ExtractJob(arch, self.dest, log=silent)
        with self.assertRaises(core.JobError) as caught:
            job.run()
        self.assertNotIn(caught.exception.key, ("archive_encrypted", "archive_wrong_password"))
        self.assertFalse(core._mentions_encryption(
            "dd/x: Incorrect passphrase: Cannot extract through symlink dd/x: "
            "Incorrect passphrase: Undefined error: 0", "tar"))

    def test_tar_family_ignores_encryption_messages(self) -> None:
        """tar 系列不可能加密：即使某一行被当成加密提示，也只按工具失败报告。"""
        self.require("tar")
        link = self.info("dd", b"2", linkname=".")
        arch = self.tar_with("gate.tar", [link, ("dd/x", b"x")])
        with patch.object(core, "_mentions_encryption", lambda line, tool: True):
            job = core.ExtractJob(arch, self.dest, log=silent)
            with self.assertRaises(core.JobError) as caught:
                job.run()
        self.assertEqual(caught.exception.key, "tool_failed")

    def test_utf8_flag_without_locale(self) -> None:
        """从访达启动的应用没有区域设置；加密 zip 的非 ASCII 文件名仍要带 UTF-8 标志。"""
        self.require("tar")
        env = {k: v for k, v in core.ENV.items() if not k.startswith(("LANG", "LC_"))}
        with patch.dict(core.ENV, env, clear=True):
            out = self.compress(self.make_source(), "zip", "noloc", zip_method="aes256")
        with zipfile.ZipFile(out) as zf:
            names = [i for i in zf.infolist() if not i.filename.isascii()]
        self.assertTrue(names)
        self.assertTrue(all(i.flag_bits & 0x800 for i in names))

    def test_dash_and_at_names_have_no_dot_prefix(self) -> None:
        self.require("tar")
        for name in ("-dash", "@at"):
            with self.subTest(name=name):
                src = self.root / name
                (src / "in").mkdir(parents=True)
                (src / "in" / "f.txt").write_text("f")
                (src / "link").symlink_to("./in/f.txt")
                out = self.arch / f"{name.strip('-@')}.zip"
                core.ArchiveJob(src, out, "zip", log=silent, password=PASSWORD).run()
                with zipfile.ZipFile(out) as zf:
                    names = [i.filename for i in zf.infolist()]
                self.assertTrue(all(n.startswith(name + "/") for n in names), names)
                result, _, _ = self.extract(out, password=PASSWORD)
                self.assertEqual(os.readlink(result / "link"), "./in/f.txt")
                self.clear_dest()

    def test_precheck_is_cancellable(self) -> None:
        self.require("tar")
        out = self.compress(self.make_source(), "zip", "cancel", zip_method="aes256")

        def cancelled() -> None:
            raise core.Cancelled()

        with self.assertRaises(core.Cancelled):
            core.check_zip_password(out, PASSWORD, cancelled)

    def test_extraction_password_validation(self) -> None:
        """核心层也拒绝不合格的解压密码，不只依赖界面。"""
        self.require("tar")
        out = self.compress(self.make_source(), "zip", "val")
        for pw, key in (("", "password_empty"), ("a\nb", "password_invalid_char"),
                        ("k" * (core.EXTRACT_PASSWORD_MAX + 1), "password_too_long")):
            with self.subTest(key=key):
                self.assert_extract_fails(out, key, pw)

    def test_long_7z_password_from_elsewhere(self) -> None:
        """别处用 7-Zip 生成的 7z 可能用了超过 99 个字符的密码，解压时应能打开。"""
        self.require("tar", "7z")
        pw = "L" * 150
        src = self.make_source()
        out = self.arch / "long.7z"
        subprocess.run([core.locate("7z"), "a", "-t7z", "-p" + pw, "-mhe=on", "-snl",
                        "-bso0", "-bsp0",
                        str(out), src.name], cwd=src.parent, check=True,
                       stdin=subprocess.DEVNULL)
        result, _, _ = self.extract(out, password=pw)
        self.assertEqual(nfc_tree(result), self.expected(src))


class ReviewRound2Tests(CryptoBase):
    """第二轮评审确认的问题。"""

    def count_prompts(self, chunks: list[bytes]) -> int:
        """把 chunks 逐块写进管道，返回日志泵报告的提示总数。"""
        counted: list[int] = []
        job = core.Job(log=silent)
        job._on_prompts = counted.append  # type: ignore[method-assign]
        r, w = os.pipe()
        stream = os.fdopen(r, "rb")
        thread = job._pump(stream)
        for chunk in chunks:
            os.write(w, chunk)
            time.sleep(0.02)  # 让读取线程分块读到
        os.close(w)
        thread.join()
        return sum(counted)

    def test_prompt_counting(self) -> None:
        cases = [
            ([b"Enter passphrase:"], 1),
            ([b"Enter pass", b"phrase:Enter passphrase:"], 2),             # 提示被切成两块
            ([b"Enter passphrase:", b"x.txt: ok\n", b"Enter passphrase:"], 2),
            ([b"Enter passphrase:d/Enter passphrase:Enter passphrase:n: err\n"], 1),  # 名字中间
            ([b"a: b\nEnter passphrase:Enter passphrase:Enter passphrase:z: Too many\n"], 3),
        ]
        for chunks, expected in cases:
            with self.subTest(chunks=chunks):
                self.assertEqual(self.count_prompts(chunks), expected)

    def aes_zip_named(self, member: str, name: str) -> Path:
        """用 bsdtar 生成 AES zip，唯一的成员改名为 member（可含 ..，让 bsdtar 解压时报错）。"""
        f = self.root / "payload.txt"
        f.write_text("payload")
        out = self.arch / name
        subprocess.run(["/usr/bin/tar", "-c", "-f", str(out), "--format", "zip", "--options",
                        "zip:encryption=aes256", "-s", f",^payload.txt$,{member},",
                        "payload.txt"], cwd=self.root, input=(PASSWORD + "\n").encode(),
                       check=True, capture_output=True, start_new_session=True)
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(zf.namelist(), [member])
        return out

    def test_prompt_text_in_member_name(self) -> None:
        """成员名中间含提示文字、又让 bsdtar 报错：只数行首的提示，不能当成密码错误。"""
        self.require("tar")
        out = self.aes_zip_named("d/Enter passphrase:Enter passphrase:/../../x", "mid.zip")
        job = core.ExtractJob(out, self.dest, log=silent, password=PASSWORD)
        with self.assertRaises(core.JobError) as caught:
            job.run()
        self.assertEqual(caught.exception.key, "tool_failed")  # 真实原因：路径含 ..
        # bsdtar 在解密之前就拒绝了这个路径，没有真的要密码；名字中间的提示文字不计数。
        # “真提示 + 名字里的提示”这种组合由 test_prompt_counting 按字节流覆盖。
        self.assertEqual(job._prompts, 0)

    def test_prompt_text_at_start_of_member_name(self) -> None:
        """顶层成员名以提示文字开头：报错行在行首就是“提示”，与真正的第二次提示无法从文字上
        区分。预检已逐个确认密码（全是 AES）时不据此结束 bsdtar，照实报告路径错误。"""
        self.require("tar")
        out = self.aes_zip_named("Enter passphrase:Enter passphrase:/../x", "top.zip")
        job = core.ExtractJob(out, self.dest, log=silent, password=PASSWORD)
        with self.assertRaises(core.JobError) as caught:
            job.run()
        self.assertGreater(job._prompts, 1)  # 确实被数成了多次提示
        self.assertEqual(caught.exception.key, "tool_failed")

    def test_many_aes_entries_with_other_password(self) -> None:
        """前 33 个成员用一个密码、后面的成员用另一个：预检逐个校验，立即报密码错误，
        不能让 bsdtar 对后面的成员各重试上万次。"""
        self.require("tar", "7z")
        src = self.root / "many"
        src.mkdir()
        for i in range(34):
            (src / f"a{i:02}.txt").write_text(str(i))
        out = self.arch / "mixedpw.zip"
        seven = core.locate("7z")
        subprocess.run([seven, "a", "-tzip", "-mem=AES256", "-p" + PASSWORD, "-bso0", "-bsp0",
                        str(out), "many"], cwd=self.root, check=True, stdin=subprocess.DEVNULL)
        (src / "z.txt").write_text("other")
        subprocess.run([seven, "a", "-tzip", "-mem=AES256", "-pother", "-bso0", "-bsp0",
                        str(out), "many/z.txt"], cwd=self.root, check=True,
                       stdin=subprocess.DEVNULL)
        self.assertEqual(core.check_zip_password(out, PASSWORD), "wrong")
        start = time.monotonic()
        self.assert_extract_fails(out, "archive_wrong_password", PASSWORD)
        self.assertLess(time.monotonic() - start, 5)

    def test_encrypted_methods_bsdtar_cannot_decrypt(self) -> None:
        """libarchive 只为存储和 deflate 先解密；LZMA、BZip2、PPMd、XZ、Deflate64 的加密成员
        直接交给 7-Zip，密码正确时能解开，没装 7-Zip 时先说明而不是先要密码。"""
        self.require("tar", "7z")
        src = self.root / "methods"
        src.mkdir()
        (src / "t.txt").write_bytes(b"compressible line\n" * 3000)
        seven = core.locate("7z")
        real = core.locate
        for method in ("LZMA", "BZip2", "PPMd", "XZ", "Deflate64"):
            for enc in ("AES256", "ZipCrypto"):
                with self.subTest(method=method, enc=enc):
                    out = self.arch / f"m-{method}-{enc}.zip"
                    subprocess.run([seven, "a", "-tzip", f"-mm={method}", f"-mem={enc}",
                                    "-p" + PASSWORD, "-bso0", "-bsp0", str(out), "methods"],
                                   cwd=self.root, check=True, stdin=subprocess.DEVNULL)
                    self.assertEqual(core.zip_encryption(out), (True, True))
                    result, _, lines = self.extract(out, password=PASSWORD)
                    self.assertEqual((result / "t.txt").read_bytes(), b"compressible line\n" * 3000)
                    commands = [line for line in lines if line.startswith("$ ")]
                    self.assertTrue(commands[0].startswith("$ " + seven), commands)
                    self.clear_dest()
                    with patch.object(core, "locate", lambda k: None if k == "7z" else real(k)):
                        self.assert_extract_fails(out, "archive_zip_needs_7z", None)

    def test_long_non_ascii_password_counts_bytes(self) -> None:
        """7-Zip 的上限按字节算：40 个汉字（120 字节）也超出。"""
        self.require("tar", "7z")
        pw = "密" * 40
        f = self.root / "big.txt"
        f.write_bytes(b"text " * 4000)
        out = self.arch / "longcjk.zip"
        subprocess.run(["/usr/bin/tar", "-c", "-f", str(out), "--format", "zip", "--options",
                        "zip:encryption=aes256,zip:hdrcharset=UTF-8", "big.txt"], cwd=self.root,
                       input=(pw + "\n").encode(), check=True, capture_output=True,
                       start_new_session=True)
        data = bytearray(out.read_bytes())
        at = 0
        while (at := data.find(b"\x01\x99\x07\x00", at)) >= 0:
            struct.pack_into("<H", data, at + 9, 9)  # 真实方法改成 Deflate64，逼它交给 7-Zip
            at += 4
        out.write_bytes(bytes(data))
        self.assertLessEqual(len(pw), core.PASSWORD_MAX)
        self.assert_extract_fails(out, "archive_password_too_long_7z", pw)

    def test_zipcrypto_path_refusal_is_not_a_password_error(self) -> None:
        """ZipCrypto 归档因路径含 .. 被拒：正确密码下应报告真实原因，而不是“密码可能不对”。"""
        self.require("tar")
        f = self.root / "a.txt"
        f.write_text("a")
        out = self.arch / "dotdot.zip"
        subprocess.run(["/usr/bin/tar", "-c", "-f", str(out), "--format", "zip", "--options",
                        "zip:encryption=zipcrypt", "-s", ",^,../,", "a.txt"], cwd=self.root,
                       input=(PASSWORD + "\n").encode(), check=True, capture_output=True,
                       start_new_session=True)
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(zf.namelist(), ["../a.txt"])
        self.assert_extract_fails(out, "tool_failed", PASSWORD)

    def test_long_aes_password_with_7z_fallback(self) -> None:
        """预检确认了 120 个字符的 AES 密码，但归档需要 7-Zip（不接受超过 99 字节的密码）。"""
        self.require("tar", "7z")
        pw = "a" * 120
        f = self.root / "big.txt"
        f.write_bytes(b"text " * 4000)
        out = self.arch / "long.zip"
        subprocess.run(["/usr/bin/tar", "-c", "-f", str(out), "--format", "zip", "--options",
                        "zip:encryption=aes256", "big.txt"], cwd=self.root,
                       input=(pw + "\n").encode(), check=True, capture_output=True,
                       start_new_session=True)
        # 把 AES 扩展字段里的真实压缩方法改成 Deflate64（9），逼 bsdtar 交给 7-Zip。
        data = bytearray(out.read_bytes())
        at = 0
        while (at := data.find(b"\x01\x99\x07\x00", at)) >= 0:
            struct.pack_into("<H", data, at + 9, 9)
            at += 4
        out.write_bytes(bytes(data))
        self.assertEqual(core.check_zip_password(out, pw), "strong")
        self.assert_extract_fails(out, "archive_password_too_long_7z", pw)

    def test_mixed_aes_and_zipcrypto_is_weak(self) -> None:
        self.require("tar", "7z")
        for name in ("a.txt", "b.txt"):
            (self.root / name).write_text(name * 100)
        out = self.arch / "mixed.zip"
        seven = core.locate("7z")
        for name, method in (("a.txt", "AES256"), ("b.txt", "ZipCrypto")):
            subprocess.run([seven, "a", "-tzip", f"-mem={method}", "-p" + PASSWORD, "-bso0",
                            "-bsp0", str(out), name], cwd=self.root, check=True,
                           stdin=subprocess.DEVNULL)
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(sorted(i.compress_type for i in zf.infolist()), [8, 99])
        self.assertEqual(core.check_zip_password(out, PASSWORD), "weak")
        self.assertEqual(core.check_zip_password(out, "wrong"), "wrong")
        result, _, _ = self.extract(out, password=PASSWORD)
        self.assertEqual(sorted(os.listdir(result)), ["a.txt", "b.txt"])

    def test_encrypted_flag_on_directory_entries(self) -> None:
        """目录项也带加密标志时（有些工具会这样写），预检要跳过它们，不能误判正确的密码。"""
        self.require("tar")
        out = self.compress(self.make_source(), "zip", "dirflag", zip_method="zipcrypt")
        data = bytearray(out.read_bytes())
        with open(out, "rb") as stream:
            entries = core._read_central_directory(stream)
        dirs = [e for e in entries if e.name.endswith(b"/")]
        self.assertTrue(dirs)
        for e in dirs:
            for offset in (e.central + 8, e.local + 6):
                flags = struct.unpack_from("<H", data, offset)[0]
                struct.pack_into("<H", data, offset, flags | 0x1)
        out.write_bytes(bytes(data))
        self.assertEqual(core.check_zip_password(out, PASSWORD), "weak")
        self.assertEqual(core.check_zip_password(out, "wrong"), "wrong")

    def test_extract_job_precheck_honours_cancel(self) -> None:
        """ExtractJob 把自己的取消检查交给预检：预检途中取消，任务按取消结束。"""
        self.require("tar")
        out = self.compress(self.make_source(), "zip", "cx", zip_method="aes256")
        job = core.ExtractJob(out, self.dest, log=silent, password=PASSWORD)
        real = core.check_zip_password
        raised_inside: list[bool] = []

        def cancel_midway(path, password, check=None):
            job.cancel()
            try:
                return real(path, password, check)
            except core.Cancelled:
                raised_inside.append(True)  # 是预检自己响应了取消，而不是之后的步骤
                raise

        with patch.object(core, "check_zip_password", cancel_midway):
            with self.assertRaises(core.Cancelled):
                job.run()
        self.assertEqual(raised_inside, [True])
        self.assert_dest_untouched()

    def test_7z_wrong_password_line(self) -> None:
        self.assertTrue(core._mentions_encryption("ERROR: Wrong password : a/b.txt", "7z"))
        self.assertTrue(core._mentions_encryption(
            "ERROR: CRC Failed in encrypted file. Wrong password? : a/b.txt", "7z"))
        self.assertFalse(core._mentions_encryption("- a/Wrong password : b", "7z"))


class ReviewRound4Tests(CryptoBase):
    """第四轮评审确认的问题。"""

    def no_7z(self):
        real = core.locate
        return patch.object(core, "locate", lambda k: None if k == "7z" else real(k))

    def test_stored_aes_members_stay_with_bsdtar(self) -> None:
        """AES 成员的真实方法是 0（存储）时不需要 7-Zip：CleanPack 自己级别 0 的加密 zip、
        7-Zip 存储的成员，在没装 7-Zip 的 Mac 上都要能解开。"""
        self.require("tar")
        src = self.make_source()
        outs = [self.compress(src, "zip", f"l0-{m}", level=0, zip_method=m)
                for m in core.ZIP_METHODS]
        if core.locate("7z"):
            out = self.arch / "copy-aes.zip"
            subprocess.run([core.locate("7z"), "a", "-tzip", "-mm=Copy", "-mem=AES256",
                            "-p" + PASSWORD, "-bso0", "-bsp0", str(out), src.name],
                           cwd=src.parent, check=True, stdin=subprocess.DEVNULL)
            outs.append(out)
        for out in outs:
            with self.subTest(out=out.name):
                with open(out, "rb") as stream:
                    methods = {e.method for e in core._read_central_directory(stream)
                               if e.flags & 0x1 and not e.name.endswith(b"/")}
                self.assertEqual(methods, {0})
                self.assertEqual(core.zip_encryption(out), (True, False))
                with self.no_7z():
                    result, _, _ = self.extract(out, password=PASSWORD)
                self.assertEqual(nfc_tree(result)["keep.txt"], b"keep\n")
                self.clear_dest()

    def test_prefixed_zip_named_7z(self) -> None:
        """带前置数据的加密 zip 被命名为 .7z：按 zip 处理，没装 7-Zip 也能用密码解开。"""
        self.require("tar")
        zc = self.compress(self.make_source(), "zip", "pre", zip_method="zipcrypt")
        out = self.arch / "sfx.7z"
        out.write_bytes(os.urandom(4096) + zc.read_bytes())
        self.assertIsNone(core.sniff_family(out))
        self.assertEqual(core.extract_family(out), "7z")
        with self.no_7z():
            self.assert_extract_fails(out, "archive_encrypted", None)
            result, _, _ = self.extract(out, password=PASSWORD)
        self.assertEqual(nfc_tree(result)["keep.txt"], b"keep\n")

    def test_weak_zip_failure_on_7z_path_reports_real_cause(self) -> None:
        """ZipCrypto zip 走 7-Zip 时（含加密链接），7-Zip 因越界链接失败：照实报告工具失败，
        不能说“密码可能不对”（界面会反复要正确的密码）。"""
        self.require("tar", "7z")
        src = self.root / "esc"
        (src / "in").mkdir(parents=True)
        (src / "in" / "f.txt").write_text("f")
        (src / "in" / "up").symlink_to("../../../../etc/hosts")
        out = self.arch / "esc.zip"
        subprocess.run([core.locate("7z"), "a", "-tzip", "-mem=ZipCrypto", "-snl",
                        "-p" + PASSWORD, "-bso0", "-bsp0", str(out), "esc"], cwd=self.root,
                       check=True, stdin=subprocess.DEVNULL)
        self.assertEqual(core.zip_encryption(out), (True, True))
        self.assert_extract_fails(out, "tool_failed", PASSWORD)


def merge_zips(parts: list[Path], out: Path, hide_encryption_of: set[bytes]) -> None:
    """把几个 zip 的成员合并成一个，并清掉 hide_encryption_of 中成员在中央目录里的加密标志
    （本地头不动），构造“两处记录不一致”的归档。"""
    blob, central = bytearray(), bytearray()
    count = 0
    for part in parts:
        data = part.read_bytes()
        with open(part, "rb") as stream:
            entries = core._read_central_directory(stream)
        cd_start = min(e.central for e in entries)
        local_end = cd_start
        base = len(blob)
        blob += data[:local_end]
        for e in entries:
            h = core._CENTRAL.unpack_from(data, e.central)
            size = core._CENTRAL.size + h[10] + h[11] + h[12]
            rec = bytearray(data[e.central:e.central + size])
            struct.pack_into("<I", rec, 42, h[16] + base)
            if e.name in hide_encryption_of:
                struct.pack_into("<H", rec, 8, h[3] & ~0x1)
            central += rec
            count += 1
    eocd = struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, count, count, len(central),
                       len(blob), 0)
    out.write_bytes(bytes(blob + central) + eocd)


class ReviewRound5Tests(CryptoBase):
    """第五轮评审确认的问题。"""

    def no_7z(self):
        real = core.locate
        return patch.object(core, "locate", lambda k: None if k == "7z" else real(k))

    def bsdtar_zip(self, name: str, member: str, password: str, enc: str = "aes256") -> Path:
        (self.root / member).write_text(member * 50)
        out = self.arch / name
        subprocess.run(["/usr/bin/tar", "-c", "-f", str(out), "--format", "zip", "--options",
                        f"zip:encryption={enc}", member], cwd=self.root,
                       input=(password + "\n").encode(), check=True, capture_output=True,
                       start_new_session=True)
        return out

    def test_encryption_hidden_from_central_directory(self) -> None:
        """中央目录没标、本地头标了加密的成员也要校验；这种归档不给 "strong"，bsdtar 第二次
        要密码时照样结束它，不能对每个这样的成员重试上万次。"""
        self.require("tar")
        a = self.bsdtar_zip("a.zip", "a.txt", PASSWORD)
        b = self.bsdtar_zip("b.zip", "b.txt", "other")
        c = self.bsdtar_zip("c.zip", "c.txt", "other")
        out = self.arch / "hidden.zip"
        merge_zips([a, b, c], out, {b"b.txt", b"c.txt"})
        with zipfile.ZipFile(out) as zf:
            self.assertEqual([bool(i.flag_bits & 1) for i in zf.infolist()], [True, False, False])
        self.assertEqual(core.zip_encryption(out), (True, False))
        self.assertEqual(core.check_zip_password(out, PASSWORD), "wrong")
        start = time.monotonic()
        self.assert_extract_fails(out, "archive_wrong_password", PASSWORD)
        self.assertLess(time.monotonic() - start, 5)

    def test_hidden_encryption_with_same_password_is_weak(self) -> None:
        self.require("tar")
        a = self.bsdtar_zip("a.zip", "a.txt", PASSWORD)
        b = self.bsdtar_zip("b.zip", "b.txt", PASSWORD)
        out = self.arch / "hidden-same.zip"
        merge_zips([a, b], out, {b"b.txt"})
        self.assertEqual(core.check_zip_password(out, PASSWORD), "weak")
        result, _, _ = self.extract(out, password=PASSWORD)
        self.assertEqual(sorted(os.listdir(self.dest)), sorted(["hidden-same"]))

    def test_link_entry_unsupported_with_hyphenated_method(self) -> None:
        self.assertTrue(core._BSDTAR_ZIP_UNSUPPORTED.search(
            "tar: Unsupported ZIP compression method during decompression of link entry "
            "(9: deflation-64-bit)"))

    def test_prefixed_zip_named_zip_on_7z_route(self) -> None:
        """名为 .zip、开头带前置数据、又必须交给 7-Zip 的加密 zip：要以 -tzip 打开。"""
        self.require("tar", "7z")
        (self.root / "t.txt").write_bytes(b"compressible line\n" * 2000)
        inner = self.arch / "inner.zip"
        subprocess.run([core.locate("7z"), "a", "-tzip", "-mm=LZMA", "-mem=AES256",
                        "-p" + PASSWORD, "-bso0", "-bsp0", str(inner), "t.txt"], cwd=self.root,
                       check=True, stdin=subprocess.DEVNULL)
        out = self.arch / "prefixed.zip"
        out.write_bytes(os.urandom(4096) + inner.read_bytes())
        self.assertEqual(core.zip_encryption(out), (True, True))
        result, _, _ = self.extract(out, password=PASSWORD)
        self.assertEqual(result.read_bytes(), b"compressible line\n" * 2000)

    def test_wrong_password_on_zip_named_7z_without_7z(self) -> None:
        """名为 .7z 的 ZipCrypto zip、没装 7-Zip、错误密码碰巧通过预检的单字节校验：
        报密码问题，不能说“加密的 7z 只有 7-Zip 能解”。"""
        self.require("tar")
        zc = self.bsdtar_zip("one.zip", "one.txt", PASSWORD, enc="zipcrypt")
        out = self.arch / "one.7z"
        out.write_bytes(os.urandom(512) + zc.read_bytes())
        guesses = [f"g{i}" for i in range(5000)
                   if core.check_zip_password(out, f"g{i}") == "weak"][:4]
        self.assertTrue(guesses)
        with self.no_7z():
            for pw in guesses:
                job = core.ExtractJob(out, self.dest, log=silent, password=pw)
                with self.assertRaises(core.JobError) as caught:
                    job.run()
                self.assertIn(caught.exception.key,
                              ("archive_wrong_password", "archive_password_or_damaged"), pw)

    def test_wrong_long_password_on_real_7z(self) -> None:
        """真正的 7z 没有 99 字节的限制：错误的长密码报“密码不正确”，不能说 zip 密码太长。"""
        self.require("tar", "7z")
        pw = "L" * 150
        (self.root / "x.txt").write_text("x")
        out = self.arch / "long.7z"
        subprocess.run([core.locate("7z"), "a", "-t7z", "-p" + pw, "-mhe=on", "-bso0", "-bsp0",
                        str(out), "x.txt"], cwd=self.root, check=True, stdin=subprocess.DEVNULL)
        self.assert_extract_fails(out, "archive_wrong_password", "M" * 150)


def rename_in_central_directory(path: Path, old: bytes, new: bytes) -> None:
    """只改中央目录里的成员名（等长），本地头不动。"""
    assert len(old) == len(new)
    data = bytearray(path.read_bytes())
    with open(path, "rb") as stream:
        (entry,) = [e for e in core._read_central_directory(stream) if e.name == old]
    at = entry.central + core._CENTRAL.size
    data[at:at + len(old)] = new
    path.write_bytes(bytes(data))


def set_flags(path: Path, name: bytes, *, central: tuple[int, int] = (0, 0),
              local: tuple[int, int] = (0, 0)) -> None:
    """按 (要置位, 要清除) 修改成员在中央目录和本地头的通用标志位。"""
    data = bytearray(path.read_bytes())
    with open(path, "rb") as stream:
        (entry,) = [e for e in core._read_central_directory(stream) if e.name == name]
    for offset, (on, off) in ((entry.central + 8, central), (entry.local + 6, local)):
        flags = struct.unpack_from("<H", data, offset)[0]
        struct.pack_into("<H", data, offset, (flags | on) & ~off)
    path.write_bytes(bytes(data))


class ReviewRound6Tests(CryptoBase):
    """第六轮评审确认的问题。"""

    def no_7z(self):
        real = core.locate
        return patch.object(core, "locate", lambda k: None if k == "7z" else real(k))

    def bsdtar_zip(self, name: str, member: str, password: str, enc: str = "aes256") -> Path:
        (self.root / member).write_text(member * 50)
        out = self.arch / name
        subprocess.run(["/usr/bin/tar", "-c", "-f", str(out), "--format", "zip", "--options",
                        f"zip:encryption={enc}", member], cwd=self.root,
                       input=(password + "\n").encode(), check=True, capture_output=True,
                       start_new_session=True)
        return out

    def test_streamed_info_zip_zipcrypto(self) -> None:
        """Info-ZIP 流式写出的 ZipCrypto zip：bsdtar 把本地头里的校验值当成 CRC 而报错。
        装了 7-Zip 时改用它，正确密码能解开、错误密码明确报错；没装时报“密码或数据有问题”。"""
        self.require("tar", "7z", "zip")
        src = self.root / "streamed"
        src.mkdir()
        for i in range(3):
            (src / f"f{i}.txt").write_text(f"file {i}\n")
        out = self.arch / "streamed.zip"
        data = subprocess.run([core.locate("zip"), "-q", "-r", "-P", PASSWORD, "-", "streamed"],
                              cwd=self.root, check=True, capture_output=True,
                              stdin=subprocess.DEVNULL).stdout
        out.write_bytes(data)
        self.assertEqual(core.check_zip_password(out, PASSWORD), "weak")
        result, _, lines = self.extract(out, password=PASSWORD)
        self.assertIn("→ 7z", lines)
        self.assertEqual((result / "f1.txt").read_text(), "file 1\n")
        self.clear_dest()
        with self.no_7z():
            self.assert_extract_fails(out, "archive_password_or_damaged", PASSWORD)

    def test_directory_name_only_in_central_directory(self) -> None:
        """中央目录把成员名写成目录（b/）、本地头却是文件（bb）：仍要校验它的密码。"""
        self.require("tar")
        from test_crypto import merge_zips
        a = self.bsdtar_zip("a.zip", "a", PASSWORD)
        b = self.bsdtar_zip("b.zip", "bb", "other")
        out = self.arch / "trick.zip"
        merge_zips([a, b], out, set())
        rename_in_central_directory(out, b"bb", b"b/")
        self.assertEqual(core.check_zip_password(out, PASSWORD), "wrong")
        start = time.monotonic()
        self.assert_extract_fails(out, "archive_wrong_password", PASSWORD)
        self.assertLess(time.monotonic() - start, 5)
        # 只剩这一个成员时，也要识别出归档是加密的（而不是按未加密处理）。
        only = self.bsdtar_zip("only.zip", "bb", "other")
        rename_in_central_directory(only, b"bb", b"b/")
        self.assertEqual(core.zip_encryption(only), (True, False))
        self.assert_extract_fails(only, "archive_encrypted", None)

    def test_local_encryption_bit_alone(self) -> None:
        """本地头只有 bit 0（传统加密、方法 8），中央目录没标：算加密。"""
        self.require("tar")
        a = self.bsdtar_zip("a.zip", "a.txt", PASSWORD)
        b = self.bsdtar_zip("b.zip", "b.txt", "other", enc="zipcrypt")
        from test_crypto import merge_zips
        out = self.arch / "bit0.zip"
        merge_zips([a, b], out, {b"b.txt"})
        self.assertEqual(core.check_zip_password(out, "other"), "wrong")  # a.txt 不符
        same = self.arch / "bit0-same.zip"
        merge_zips([a, self.bsdtar_zip("c.zip", "b.txt", PASSWORD, enc="zipcrypt")], same,
                   {b"b.txt"})
        self.assertEqual(core.check_zip_password(same, PASSWORD), "weak")

    def test_local_aes_method_alone(self) -> None:
        """本地头是 AES 方法 99、两处的 bit 0 都清掉了：仍算加密并校验。"""
        self.require("tar")
        out = self.bsdtar_zip("m99.zip", "m.txt", PASSWORD)
        set_flags(out, b"m.txt", central=(0, 0x1), local=(0, 0x1))
        # 中央目录的 AES 扩展字段记着真实方法 deflate，bsdtar 能解，不需要 7-Zip。
        self.assertEqual(core.zip_encryption(out), (True, False))
        self.assertEqual(core.check_zip_password(out, "wrong"), "wrong")
        self.assertEqual(core.check_zip_password(out, PASSWORD), "weak")

    def test_unknown_aes_strength_is_weak(self) -> None:
        """AES 扩展字段的强度字节不认识时判断不了，按 weak 交给 bsdtar，不误判正确的密码。"""
        self.require("tar")
        out = self.bsdtar_zip("s.zip", "s.txt", PASSWORD)
        data = bytearray(out.read_bytes())
        at = 0
        while (at := data.find(b"\x01\x99\x07\x00", at)) >= 0:
            data[at + 8] = 0
            at += 4
        out.write_bytes(bytes(data))
        self.assertEqual(core.check_zip_password(out, PASSWORD), "weak")

    def test_pkware_strong_encryption_is_weak(self) -> None:
        self.require("tar")
        out = self.bsdtar_zip("p.zip", "p.txt", PASSWORD, enc="zipcrypt")
        set_flags(out, b"p.txt", central=(0x40, 0))
        self.assertEqual(core.check_zip_password(out, "wrong"), "weak")

    def test_truncated_data_counts_as_data_error(self) -> None:
        self.assertTrue(core._BSDTAR_DATA_ERROR.search("a.txt: Truncated ZIP file data"))
        self.assertTrue(core._BSDTAR_DATA_ERROR.search(
            "a.txt: Truncated ZIP file data: Unknown error: -1"))

    def test_zip_encryption_honours_cancel(self) -> None:
        self.require("tar")
        out = self.compress(self.make_source(), "zip", "zc", zip_method="zipcrypt")

        def cancelled() -> None:
            raise core.Cancelled()

        with self.assertRaises(core.Cancelled):
            core.zip_encryption(out, cancelled)
        job = core.ExtractJob(out, self.dest, log=silent, password=PASSWORD)
        real = core.zip_encryption
        raised_inside: list[bool] = []

        def cancel_midway(path, check=None):
            job.cancel()
            try:
                return real(path, check)
            except core.Cancelled:
                raised_inside.append(True)
                raise

        with patch.object(core, "zip_encryption", cancel_midway):
            with self.assertRaises(core.Cancelled):
                job.run()
        self.assertEqual(raised_inside, [True])


class CancelTests(CryptoBase):
    """加密任务取消后不留下暂存目录或产物。"""

    def test_cancel_encrypted_jobs(self) -> None:
        self.require("tar", "7z")
        src = self.make_source()
        (src / "big.bin").write_bytes(os.urandom(64 * 1024 * 1024))
        for fmt in core.ENCRYPTABLE:
            with self.subTest(fmt=fmt):
                out = self.arch / ("c" + core.FORMATS[fmt].ext)
                job = core.ArchiveJob(src, out, fmt, 9, log=silent, password=PASSWORD)
                real_spawn = job._spawn

                def spawn_then_cancel(cmd: list[str], **kw: object) -> subprocess.Popen:
                    p = real_spawn(cmd, **kw)
                    job.cancel()
                    return p

                with patch.object(job, "_spawn", spawn_then_cancel):
                    with self.assertRaises(core.Cancelled):
                        job.run()
                self.assertEqual(os.listdir(self.arch), [])


if __name__ == "__main__":
    unittest.main()
