"""CleanPack 压缩核心：不依赖 GUI，可单独测试。"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import struct
import sys
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

APP_NAME = "CleanPack"

# 永远不会被打包的 macOS 垃圾文件（按文件名/目录名匹配，任意层级）
JUNK = [
    ".DS_Store", "._*", "__MACOSX", ".Spotlight-V100", ".Trashes",
    ".fseventsd", ".TemporaryItems", ".DocumentRevisions-V100",
    ".AppleDouble", ".AppleDB", ".AppleDesktop", "Icon\r",
]

# 从 Finder 启动的 .app 拿不到 shell 的 PATH，这里手动补上 Homebrew 路径
_EXTRA_PATHS = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
                "/usr/bin", "/bin", "/usr/sbin", "/sbin"]


def _make_env() -> dict[str, str]:
    """返回子进程统一环境，补全 Finder 启动时缺失的 PATH 并禁用 AppleDouble。"""
    env = os.environ.copy()
    parts = [p for p in env.get("PATH", "").split(":") if p]
    for p in reversed(_EXTRA_PATHS):
        if p not in parts:
            parts.insert(0, p)
    env["PATH"] = ":".join(parts)
    env["COPYFILE_DISABLE"] = "1"  # 让 macOS 的 tar 不生成 ._* 文件
    return env


ENV = _make_env()


def which(name: str) -> str | None:
    return shutil.which(name, path=ENV["PATH"])


def find_brew() -> str | None:
    """优先查找 Apple Silicon 与 Intel 的默认安装位置，返回可执行路径或 None。"""
    for c in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if os.access(c, os.X_OK):
            return c
    return which("brew")


BREW_INSTALL_CMD = ('/bin/bash -c "$(curl -fsSL '
                    'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')


@dataclass(frozen=True)
class Tool:
    """工具查找候选、Homebrew formula 和 macOS 内建标记。"""
    key: str
    candidates: tuple[str, ...]
    formula: str | None  # Homebrew formula 名
    builtin: bool        # macOS 是否自带


TOOLS: dict[str, Tool] = {t.key: t for t in [
    Tool("tar", ("/usr/bin/tar", "tar"), None, True),
    Tool("gzip", ("gzip",), "gzip", True),
    Tool("xz", ("xz",), "xz", False),
    Tool("zstd", ("zstd",), "zstd", False),
    Tool("zip", ("zip",), "zip", True),
    Tool("7z", ("7zz", "7z"), "sevenzip", False),
]}


def locate(key: str) -> str | None:
    """按工具 key 的候选顺序返回路径；系统 tar 优先，避免 PATH 中 GNU tar 改变行为。"""
    for c in TOOLS[key].candidates:
        if os.path.isabs(c):
            if os.access(c, os.X_OK):
                return c
        elif (p := which(c)):
            return p
    return None


@dataclass(frozen=True)
class Format:
    """归档格式的扩展名、必需工具和级别范围（最小、最大、默认）。"""
    key: str
    ext: str
    tools: tuple[str, ...]
    level: tuple[int, int, int] | None  # (最小, 最大, 默认)


FORMATS: dict[str, Format] = {f.key: f for f in [
    Format("tar.gz", ".tar.gz", ("tar", "gzip"), (1, 9, 6)),
    Format("tar.xz", ".tar.xz", ("tar", "xz"), (0, 9, 6)),
    Format("tar.zst", ".tar.zst", ("tar", "zstd"), (1, 22, 3)),
    Format("zip", ".zip", ("zip",), (0, 9, 6)),
    Format("7z", ".7z", ("7z",), (0, 9, 5)),
    Format("tar", ".tar", ("tar",), None),
]}


def formats_using(tool_key: str) -> list[str]:
    return [f.key for f in FORMATS.values() if tool_key in f.tools]


def missing_tools(fmt_key: str, paths: dict[str, str | None] | None = None) -> list[str]:
    """返回指定格式缺少的工具；paths 可提供已检测结果，未提供的项现场检测。"""
    paths = paths or {}
    return [t for t in FORMATS[fmt_key].tools
            if not (paths.get(t) if t in paths else locate(t))]


def missing_formulas(paths: dict[str, str | None]) -> list[str]:
    return sorted({TOOLS[k].formula for k, p in paths.items()
                   if not p and TOOLS[k].formula})


def strip_ext(name: str) -> str:
    """移除最长的已知归档扩展名，避免把 tar.gz 错当成单独 gz 后缀。"""
    for ext in sorted((f.ext for f in FORMATS.values()), key=len, reverse=True):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


_bsdtar_cache: dict[str, bool] = {}


def _is_bsdtar(tar: str) -> bool:
    """检测并缓存 tar 实现，避免把仅 bsdtar 支持的 macOS 参数传给 GNU tar。"""
    if tar not in _bsdtar_cache:
        try:
            out = subprocess.run([tar, "--version"], capture_output=True,
                                 text=True, env=ENV).stdout
        except OSError:
            out = ""
        _bsdtar_cache[tar] = "bsdtar" in out
    return _bsdtar_cache[tar]


class Cancelled(Exception):
    """用户取消任务；GUI 将其显示为正常取消而非压缩失败。"""


class JobError(Exception):
    """带翻译 key 的错误，GUI 负责本地化。"""

    def __init__(self, key: str, **kw: object) -> None:
        super().__init__(key)
        self.key, self.kw = key, kw


class ArchiveJob:
    """一次性归档任务；run 在工作线程执行，cancel 可由主线程调用。"""
    def __init__(self, src: Path | str, out: Path | str, fmt: str, level: int | None = None,
                 extreme: bool = False, log: Callable[[str], None] = print) -> None:
        """src/out 为输入输出路径；fmt 选择格式，level/extreme 控制压缩，log 接收日志。"""
        self.src = Path(src).absolute()
        self.out = Path(out).absolute()
        self.fmt = FORMATS[fmt]
        self.level = level if level is not None else (self.fmt.level or (0, 0, 0))[2]
        self.extreme = extreme
        self.log = log
        self._cancel = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self._procs_lock = threading.Lock()
        self._pumps: list[threading.Thread] = []

    # ---------- 公共接口 ----------
    def cancel(self) -> None:
        """设置取消标记并终止已登记进程；清理和等待由 run 所在线程完成。"""
        self._cancel.set()
        with self._procs_lock:
            for proc in self._procs:
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass

    def run(self) -> Path:
        """验证输出位置，创建归档并原子提交，返回产物路径；异常时清理暂存数据。"""
        self._check_cancel()
        missing = missing_tools(self.fmt.key)
        if missing:
            raise JobError("missing_tools", fmt=self.fmt.key, tools=", ".join(missing))
        if self.out.resolve() == self.src.resolve() or (
            self.out.exists() and self.src.exists() and self.out.samefile(self.src)
        ):
            raise JobError("out_same_src")
        if self.src.is_dir() and self.out.resolve().is_relative_to(self.src.resolve()):
            raise JobError("out_inside_src")

        # 每个任务独占暂存目录，避免删除已有 .part 文件或与另一个任务互相覆盖。
        # 暂存目录与输出在同一文件系统，os.replace 才能保证原子提交。
        with tempfile.TemporaryDirectory(prefix=".cleanpack-", suffix=".part",
                                         dir=self.out.parent) as staging:
            tmp = Path(staging) / (self.out.name + ".part")
            try:
                if self.fmt.key.startswith("tar"):
                    self._run_tar(tmp)
                elif self.fmt.key == "zip":
                    self._run_zip(tmp)
                else:
                    self._run_7z(tmp)
                self._check_cancel()
                os.replace(tmp, self.out)
                return self.out
            finally:
                self._cleanup_processes()

    # ---------- 内部实现 ----------
    def _cleanup_processes(self) -> None:
        """回收管道两端和日志线程，保证失败或取消后才开始清理暂存目录。"""
        # 第二个管道进程启动失败时，tar 也必须退出，不能留下孤立进程。
        for proc in self._procs:
            if proc.poll() is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        for proc in self._procs:
            proc.wait()
        for thread in self._pumps:
            thread.join()
        for proc in self._procs:
            if proc.stdout and not proc.stdout.closed:
                proc.stdout.close()

    @property
    def _name(self) -> str:
        n = self.src.name
        # 文件名以短横线开头时加相对路径前缀，防止被工具当成选项。
        return "./" + n if n.startswith("-") else n

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def _spawn(self, cmd: list[str], **kw: Any) -> subprocess.Popen:
        """记录 cmd 并以统一环境启动进程，kw 传给 Popen，返回已登记的进程。"""
        self._check_cancel()
        self.log("$ " + shlex.join(cmd))
        # 与 cancel 共用锁，防止取消落在进程创建和登记之间而漏杀进程。
        with self._procs_lock:
            self._check_cancel()
            p = subprocess.Popen(cmd, env=ENV, **kw)
            self._procs.append(p)
        return p

    def _pump(self, stream: BinaryIO) -> threading.Thread:
        """异步排空二进制日志流，避免管道写满阻塞子进程；返回读取线程。"""
        def run() -> None:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self.log(line)
            stream.close()
        t = threading.Thread(target=run, daemon=True)
        self._pumps.append(t)
        t.start()
        return t

    def _tar_cmd(self) -> list[str]:
        """返回写入标准输出的 tar 命令，统一屏蔽磁盘垃圾和 macOS 元数据。"""
        tar = locate("tar")
        cmd = [tar, "-c", "-f", "-"]
        if _is_bsdtar(tar):  # macOS 自带 bsdtar：不写入扩展属性/资源分叉
            cmd += ["--no-mac-metadata", "--no-xattrs"]
        for p in JUNK:
            cmd += ["--exclude", p]
        return cmd + [self._name]

    def _compressor(self) -> list[str] | None:
        """按格式返回读取标准输入的压缩命令；纯 tar 返回 None。"""
        lv = self.level
        if self.fmt.key == "tar.gz":
            return [locate("gzip"), f"-{lv}", "-c"]
        # -c 写标准输出；xz 的 -e 增加搜索强度，-T0 让工具自动选线程数。
        if self.fmt.key == "tar.xz":
            return [locate("xz"), f"-{lv}{'e' if self.extreme else ''}", "-T0", "-c"]
        if self.fmt.key == "tar.zst":
            cmd = [locate("zstd"), f"-{lv}", "-T0", "-q", "-c"]
            if lv > 19:
                # zstd 默认限制到 19，20–22 需要显式开启高内存模式。
                cmd.insert(1, "--ultra")
            return cmd
        return None

    def _run_tar(self, tmp: Path) -> None:
        """把 tar 输出写入 tmp，或经压缩器管道写入；检查管道两端退出码。"""
        comp = self._compressor()
        with open(tmp, "wb") as f:
            p1 = self._spawn(self._tar_cmd(), cwd=self.src.parent,
                             stdout=subprocess.PIPE if comp else f,
                             stderr=subprocess.PIPE)
            threads = [self._pump(p1.stderr)]
            p2 = None
            if comp:
                p2 = self._spawn(comp, stdin=p1.stdout, stdout=f, stderr=subprocess.PIPE)
                # 父进程不持有读取端，压缩器退出时 tar 才能收到管道关闭信号。
                p1.stdout.close()
                threads.append(self._pump(p2.stderr))
            rc1 = p1.wait()
            rc2 = p2.wait() if p2 else 0
            for t in threads:
                t.join()
        self._check_cancel()
        if rc1:
            raise JobError("tool_failed", tool="tar", code=rc1)
        if rc2:
            raise JobError("tool_failed", tool=comp[0], code=rc2)

    def _run_simple(self, cmd: list[str], ok_codes: tuple[int, ...] = (0,)) -> int:
        """运行单个归档命令并排空日志，返回允许的退出码，否则抛本地化错误。"""
        p = self._spawn(cmd, cwd=self.src.parent, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT)
        t = self._pump(p.stdout)
        rc = p.wait()
        t.join()
        self._check_cancel()
        if rc not in ok_codes:
            raise JobError("tool_failed", tool=Path(cmd[0]).name, code=rc)
        return rc

    def _run_zip(self, tmp: Path) -> None:
        """生成 zip 递归排除参数并写入 tmp，保留符号链接本身。"""
        excludes = []
        for p in JUNK:  # zip 的 * 能跨越 /，四种写法覆盖任意层级
            excludes += [p, f"*/{p}", f"{p}/*", f"*/{p}/*"]
        # -X 去掉额外属性，-y 存储链接而不沿链接遍历源目录之外的文件。
        cmd = [locate("zip"), "-r", "-X", "-y", f"-{self.level}",
               str(tmp), self._name, "-x", *excludes]
        self._run_simple(cmd)
        if sys.platform == "darwin":
            self._mark_zip_utf8(tmp)

    def _mark_zip_utf8(self, path: Path) -> None:
        """为 macOS zip 的 UTF-8 文件名补标记；只改头部，不重新压缩文件内容。"""
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            # zipfile 已处理 ZIP64 和中央目录偏移；start_dir 是其解析后的目录位置。
            central_offset = archive.start_dir
        with path.open("r+b") as stream:
            for member in members:
                self._check_cancel()
                stream.seek(central_offset)
                header = stream.read(46)
                if len(header) != 46 or header[:4] != b"PK\x01\x02":
                    raise JobError("zip_invalid")
                name_len, extra_len, comment_len = struct.unpack_from("<HHH", header, 28)
                raw_name = stream.read(name_len)
                # Apple 的 zip 缺少 Unicode 编译选项，但 macOS 路径字节本身是 UTF-8。
                # 校验原始字节后同步设置中央目录和本地头的 bit 11，兼容不同解压器。
                try:
                    raw_name.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise JobError("zip_invalid") from exc
                if not raw_name.isascii():
                    for flag_offset in (central_offset + 8, member.header_offset + 6):
                        stream.seek(flag_offset)
                        flags = struct.unpack("<H", stream.read(2))[0]
                        stream.seek(flag_offset)
                        stream.write(struct.pack("<H", flags | 0x800))
                central_offset += 46 + name_len + extra_len + comment_len

    def _run_7z(self, tmp: Path) -> None:
        """生成 7z 递归排除参数并写入 tmp，拒绝有遗漏警告的归档。"""
        # -snl 保存符号链接，-mmt=on 启用多线程，-bsp0 关闭百分比刷屏。
        cmd = [locate("7z"), "a", "-t7z", f"-mx={self.level}", "-mmt=on",
               "-snl", "-bb1", "-bsp0", "-y", str(tmp), self._name]
        # -xr! 对任意层级的文件名递归匹配，避免只排除顶层垃圾。
        cmd += [f"-xr!{p}" for p in JUNK]
        # 退出码 1 可能表示部分文件未读到，不能把不完整的归档当作成功提交。
        self._run_simple(cmd)
