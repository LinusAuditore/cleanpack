"""CleanPack 压缩核心：不依赖 GUI，可单独测试。"""
from __future__ import annotations

import errno
import fnmatch
import os
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

APP_NAME = "CleanPack"
APP_VERSION = "0.1.0"
BUNDLE_ID = "io.github.linusauditore.cleanpack"

# 每个任务在输出目录下独占的暂存目录名形如 .cleanpack-XXXX.part
_STAGING_PREFIX, _STAGING_SUFFIX = ".cleanpack-", ".part"

# 永远不会被打包的 macOS 垃圾文件（按文件名/目录名匹配，任意层级）
JUNK = [
    ".DS_Store", "._*", "__MACOSX", ".Spotlight-V100", ".Trashes",
    ".fseventsd", ".TemporaryItems", ".DocumentRevisions-V100",
    ".AppleDouble", ".AppleDB", ".AppleDesktop", ".LSOverride", "Icon\r",
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


_SYSTEM_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/System")
# 可写的数据卷经 firmlink 挂在这里，其下的 /opt/homebrew 等目录不属于系统。
_DATA_VOLUME = "/System/Volumes/Data"


def is_system_path(path: str) -> bool:
    """判断工具是否来自 macOS 系统目录；先解析符号链接，避免把 Homebrew 的链接当成系统工具。"""
    real = os.path.realpath(path)
    if real == _DATA_VOLUME or real.startswith(_DATA_VOLUME + "/"):
        real = real[len(_DATA_VOLUME):] or "/"
    return any(real == d or real.startswith(d + "/") for d in _SYSTEM_DIRS)


def require_tool(key: str, fmt_key: str) -> str:
    """返回工具路径；任务开始后工具被移除时报告缺失，而不是把 None 传给子进程。"""
    path = locate(key)
    if path is None:
        raise JobError("missing_tools", fmt=fmt_key, tools=key)
    return path


def strip_ext(name: str) -> str:
    """移除最长的已知归档扩展名（不区分大小写），避免把 tar.gz 错当成单独 gz 后缀。"""
    for ext in sorted((f.ext for f in FORMATS.values()), key=len, reverse=True):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


_bsdtar_cache: dict[str, bool] = {}


def _is_bsdtar(tar: str) -> bool:
    """检测并缓存 tar 实现，避免把仅 bsdtar 支持的 macOS 参数传给 GNU tar。"""
    if tar not in _bsdtar_cache:
        try:
            out = subprocess.run([tar, "--version"], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, env=ENV).stdout
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


# ---------- 文件系统错误与输出预检 ----------
# 常见 errno 对应的翻译 key；其余错误统一归为 io_error，附带系统给出的原因。
_ERRNO_KEYS = {
    errno.ENOSPC: "no_space", errno.EDQUOT: "no_space", errno.EROFS: "read_only",
    errno.EACCES: "access_denied", errno.EPERM: "access_denied",  # EPERM 也覆盖 TCC 受保护目录
    errno.ENOENT: "not_found", errno.ENOTDIR: "not_found", errno.EISDIR: "out_is_dir",
    errno.ENAMETOOLONG: "name_too_long",
}
# 这些文案需要具体路径；系统没有给出文件名时退回 io_error，避免出现残缺句子。
_PATH_KEYS = {"read_only", "access_denied", "not_found", "out_is_dir"}


def _visible_path(name: str) -> str:
    """把暂存目录内的路径换成它所在的输出目录；暂存目录是隐藏的，任务结束后也已删除。"""
    parts = Path(name).parts
    for i, part in enumerate(parts):
        if part.startswith(_STAGING_PREFIX) and part.endswith(_STAGING_SUFFIX) and i > 0:
            return str(Path(*parts[:i]))
    return name


def job_error_from_os(exc: OSError) -> JobError:
    """把 OSError 转成可翻译的 JobError，界面不再直接显示英文系统消息。"""
    key = _ERRNO_KEYS.get(exc.errno or 0, "io_error")
    # os.replace 等双路径调用的 filename2 是目标，也就是用户选择的输出，优先报告它。
    name = exc.filename2 or exc.filename
    path = _visible_path(os.fsdecode(name)) if name else ""
    if key in _PATH_KEYS and not path:
        key = "io_error"
    return JobError(key, path=path, err=exc.strerror or str(exc))


def check_output_dir(path: Path) -> None:
    """确认输出目录存在、是目录且可写，否则抛出带路径的 JobError。"""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise JobError("out_dir_missing", path=str(path)) from None
    except NotADirectoryError:
        raise JobError("out_dir_not_dir", path=str(path)) from None
    except PermissionError:
        raise JobError("access_denied", path=str(path)) from None
    if not stat.S_ISDIR(st.st_mode):
        raise JobError("out_dir_not_dir", path=str(path))
    try:
        read_only = bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        read_only = False
    if read_only:
        raise JobError("read_only", path=str(path))
    if not os.access(path, os.W_OK | os.X_OK):
        raise JobError("access_denied", path=str(path))


def is_within(path: Path | str, ancestor: Path | str) -> bool:
    """判断 path 本身或其任一上级是否就是 ancestor。

    按 (st_dev, st_ino) 比较而不是比较字符串，因此不受 APFS 大小写不敏感和
    /System/Volumes/Data firmlink 别名影响；尚不存在的末端路径会跳过，继续比较上级。
    """
    try:
        target = os.stat(ancestor)
    except OSError:
        return False
    cur = os.path.realpath(path)
    while True:
        try:
            if os.path.samestat(os.stat(cur), target):
                return True
        except OSError:
            pass
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def check_compress_output(src: Path, out: Path) -> None:
    """压缩前检查输出位置：目录可写、不覆盖源文件、不在源目录内、不是已有文件夹。"""
    check_output_dir(out.parent)
    if out.resolve() == src.resolve() or (
        out.exists() and src.exists() and out.samefile(src)
    ):
        raise JobError("out_same_src")
    if src.is_dir() and is_within(out.parent, src):
        raise JobError("out_inside_src")
    # 指向目录的符号链接会被 os.replace 整体替换为归档，只有真实目录才无法替换。
    if out.is_dir() and not out.is_symlink():
        raise JobError("out_is_dir", path=str(out))
    # 暂存文件名是「输出名.part」，它也必须在单个文件名的长度上限之内。
    if _name_units(out.name + _PART_EXT) > _NAME_MAX:
        raise JobError("name_too_long")


# ---------- 键入路径的整理与校验 ----------
# APFS 按 UTF-16 码元计算单个名称长度，上限 255（实测：255 个汉字可以，128 个 emoji 不行）。
# HFS+ 先分解再计数，可能更严，那种情况由 ENAMETOOLONG 映射到同一条提示。
_NAME_MAX = 255
_PART_EXT = ".part"   # 暂存文件的后缀


def _name_units(name: str) -> int:
    return len(name.encode("utf-16-le", "surrogatepass")) // 2


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def _drop_terminal_escapes(s: str) -> str:
    """终端会把空格等字符写成「\\ 」。反斜杠也是合法的文件名字符，所以只在原文找不到、
    而去掉转义后能找到对应项（或其所在文件夹）时才去掉，后者用于键入新的输出文件名。"""
    if "\\" not in s or os.path.lexists(s):
        return s
    unescaped = _unescape(s)
    if os.path.lexists(unescaped):
        return unescaped
    if not os.path.isdir(os.path.dirname(s)) and os.path.isdir(os.path.dirname(unescaped)):
        return unescaped
    return s


def clean_typed_path(raw: str) -> str:
    """整理键入或粘贴的路径，返回去掉包装后的字符串；空白输入返回空字符串。

    依次处理：首尾空白、一对首尾相同的引号、file:// URL、~ 和终端拖入时的反斜杠转义。
    文件名本身可以以空格结尾或含引号、反斜杠，所以原文已经是存在的绝对路径时原样返回；
    拖放和文件对话框给出的路径也走这里，不会被误改。无法作为路径的输入抛出 path_invalid。
    """
    if os.path.isabs(raw) and os.path.lexists(raw):  # lexists 对 NUL 等无效输入返回 False
        return raw
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1]
    if s[:7].lower() == "file://":
        try:
            url = urllib.parse.urlsplit(s)
        except ValueError:  # 例如 file://[ 这样无法解析的主机名
            raise JobError("path_invalid", path=s) from None
        if url.netloc not in ("", "localhost"):
            raise JobError("path_invalid", path=s)  # 网络位置的 URL 不是本机路径
        s = urllib.parse.unquote(url.path)
    _check_encodable(s)
    return _drop_terminal_escapes(os.path.expanduser(s))


def _check_encodable(s: str) -> None:
    """NUL 和无法编码成文件系统路径的字符（例如孤立的代理项）会让 os 函数抛出
    ValueError 而不是 OSError，在这里统一报告为无效路径。"""
    try:
        if "\0" in s:
            raise ValueError
        os.fsencode(s)
    except ValueError:
        # 提示里显示的路径也要能正常渲染：去掉 NUL，代理项换成替换字符。
        shown = s.replace("\0", "").encode("utf-8", "replace").decode()
        raise JobError("path_invalid", path=shown) from None


def _absolute_typed(s: str) -> Path:
    """要求绝对路径且不含 ..，返回 Path；含 .. 的路径经过符号链接时由系统解析，
    结果可能和界面上按字面理解的位置不同，所以直接拒绝。"""
    _check_encodable(s)
    if not os.path.isabs(s):
        raise JobError("path_not_absolute")
    path = Path(s)
    if ".." in path.parts:
        raise JobError("path_invalid", path=s)
    return path


def is_junk_name(name: str) -> bool:
    """名称是否匹配 JUNK 中的任一模式；区分大小写，与 tar、7z 的排除规则一致。"""
    return any(fnmatch.fnmatchcase(name, p) for p in JUNK)


def validate_source(path: Path | str) -> Path:
    """校验压缩源，返回要压缩的绝对路径；不合格时抛出 JobError。

    path 应已经过 clean_typed_path。源本身是符号链接时返回解析后的目标：tar、zip、7z
    都只保存顶层链接本身，否则会得到一个只含链接、没有任何内容的“成功”归档
    （/tmp、/etc 在 macOS 上都是链接）。键入、拖放和文件对话框都调用这里，ArchiveJob
    开始时也会再查一次，因为源可能在选择之后被删除或替换。
    """
    p = _absolute_typed(str(path))
    if p.name and os.path.islink(p):
        p = Path(os.path.realpath(p))
    if not p.name:
        raise JobError("src_is_root")  # tar 等工具无法以空名称打包整个磁盘
    try:
        st = os.stat(p)  # 悬空链接按不存在处理
    except (FileNotFoundError, NotADirectoryError):
        raise JobError("src_missing", path=str(p)) from None
    except PermissionError:
        raise JobError("src_denied", path=str(p)) from None
    except OSError as exc:
        raise job_error_from_os(exc) from exc
    # 顶层名称本身匹配排除规则时，工具会把整个源排除掉，得到一个“成功”的空归档。
    if is_junk_name(p.name):
        raise JobError("src_is_junk", name=p.name.replace("\r", "\\r"))
    is_dir = stat.S_ISDIR(st.st_mode)
    if not (is_dir or stat.S_ISREG(st.st_mode)):
        raise JobError("src_unsupported_type", path=str(p))  # FIFO、设备文件、套接字
    if not os.access(p, os.R_OK | (os.X_OK if is_dir else 0)):
        raise JobError("src_denied", path=str(p))
    return p


def parse_output(raw: str, out_dir: Path) -> tuple[Path, str | None] | None:
    """解析键入的输出位置，返回 (输出目录, 不含扩展名的名称)，名称为 None 表示沿用当前名称。

    留空返回 None，表示恢复自动生成的路径。只写名称时放在 out_dir；以 / 结尾或指向
    已有文件夹时只更换目录；其余必须是绝对路径。已知的归档扩展名会被去掉，
    由界面接上当前格式的扩展名，切换格式时不会得到 a.zip.7z 这样的名称。
    """
    s = clean_typed_path(raw)
    if not s:
        return None
    if "/" not in s:
        directory, name = out_dir, s
    else:
        path = _absolute_typed(s)
        try:
            is_dir = path.is_dir()  # Python 3.13 起，权限不足或名称过长会抛出 OSError
        except OSError as exc:
            raise job_error_from_os(exc) from exc
        if s.endswith("/") or is_dir:
            return path, None
        directory, name = path.parent, path.name
    stem = strip_ext(name)
    if stem in ("", ".", ".."):
        raise JobError("path_invalid", path=s)
    return directory, stem


# ---------- ZIP 中央目录解析 ----------
# 按 PKWARE APPNOTE 的固定头部布局解析，不依赖 zipfile 的内部属性。
_EOCD = struct.Struct("<4sHHHHIIH")                  # 中央目录结束记录，22 字节
_ZIP64_LOCATOR = struct.Struct("<4sIQI")             # ZIP64 结束记录定位器，20 字节
_ZIP64_EOCD = struct.Struct("<4sQHHIIQQQQ")          # ZIP64 中央目录结束记录，56 字节
_CENTRAL = struct.Struct("<4sHHHHHHIIIHHHHHII")      # 中央目录文件头，46 字节
_LOCAL = struct.Struct("<4sHHHHHIIIHH")              # 本地文件头，30 字节
_U16_MAX, _U32_MAX = 0xFFFF, 0xFFFFFFFF


@dataclass(frozen=True)
class _ZipEntry:
    """中央目录中的一项：中央头与本地头的绝对偏移，以及原始文件名字节。"""
    central: int
    local: int
    name: bytes


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise JobError("zip_invalid")
    return data


def _find_eocd(stream: BinaryIO) -> tuple[int, tuple[Any, ...]]:
    """从文件末尾向前查找 EOCD，返回其偏移和字段；注释长度必须与剩余字节一致。"""
    size = stream.seek(0, os.SEEK_END)
    tail_len = min(size, _EOCD.size + _U16_MAX)
    stream.seek(size - tail_len)
    tail = stream.read(tail_len)
    pos = tail.rfind(b"PK\x05\x06")
    while pos >= 0:
        # 注释里也可能出现签名字节，只接受注释恰好延伸到文件末尾的那一处。
        if pos + _EOCD.size <= len(tail):
            fields = _EOCD.unpack_from(tail, pos)
            if pos + _EOCD.size + fields[7] == len(tail):
                return size - tail_len + pos, fields
        pos = tail.rfind(b"PK\x05\x06", 0, pos)
    raise JobError("zip_invalid")


def _zip64_local_offset(extra: bytes, csize: int, usize: int, offset: int) -> int:
    """返回本地头偏移；32 位字段溢出时从 ZIP64 扩展字段（tag 0x0001）中读取。"""
    if offset != _U32_MAX:
        return offset
    i = 0
    while i + 4 <= len(extra):
        tag, length = struct.unpack_from("<HH", extra, i)
        body = extra[i + 4:i + 4 + length]
        if tag == 0x0001:
            # 扩展字段只包含溢出的项，顺序固定为：原始大小、压缩大小、本地头偏移。
            skip = 8 * ((usize == _U32_MAX) + (csize == _U32_MAX))
            if len(body) < skip + 8:
                break
            return struct.unpack_from("<Q", body, skip)[0]
        i += 4 + length
    raise JobError("zip_invalid")


def _read_zip64_end(stream: BinaryIO, eocd_at: int) -> tuple[int, tuple[int, ...]] | None:
    """读取 EOCD 之前的 ZIP64 定位器和结束记录；没有定位器时返回 None。

    返回 (ZIP64 结束记录偏移, (本卷号, 中央目录卷号, 本卷条目数, 总条目数, 中央目录大小, 偏移))。
    """
    loc_at = eocd_at - _ZIP64_LOCATOR.size
    if loc_at < 0:
        return None
    stream.seek(loc_at)
    if _ZIP64_LOCATOR.unpack(_read_exact(stream, _ZIP64_LOCATOR.size))[0] != b"PK\x06\x07":
        return None
    # 与 CPython 相同的假设：ZIP64 结束记录紧挨在定位器之前，且不带扩展数据。
    record_at = loc_at - _ZIP64_EOCD.size
    if record_at < 0:
        raise JobError("zip_invalid")
    stream.seek(record_at)
    z64 = _ZIP64_EOCD.unpack(_read_exact(stream, _ZIP64_EOCD.size))
    if z64[0] != b"PK\x06\x06":
        raise JobError("zip_invalid")
    return record_at, z64[4:10]


def _read_central_directory(stream: BinaryIO) -> list[_ZipEntry]:
    """解析 EOCD（含 ZIP64）与整个中央目录，返回各项偏移；结构不自洽时抛 zip_invalid。"""
    eocd_at, fields = _find_eocd(stream)
    _, disk, cd_disk, n_disk, n_total, cd_size, cd_offset, _ = fields
    record_at = eocd_at
    # 与 CPython 一样总是探测 ZIP64 记录，而不是看 EOCD 字段是否溢出：Info-ZIP 遇到
    # 大于 4 GiB 的条目也会写 ZIP64 记录，而 EOCD 字段未必溢出；反过来，字段恰好等于
    # 上限（例如正好 65535 项）时也可能没有 ZIP64 记录。
    if (z64 := _read_zip64_end(stream, eocd_at)) is not None:
        record_at, (disk, cd_disk, n_disk, n_total, cd_size, cd_offset) = z64
    if disk != 0 or cd_disk != 0 or n_disk != n_total:
        raise JobError("zip_invalid")  # 不处理分卷归档
    # 归档前若有额外数据（例如自解压程序），所有记录的偏移都要整体后移。
    prefix = record_at - (cd_offset + cd_size)
    if prefix < 0:
        raise JobError("zip_invalid")
    cd_start = cd_offset + prefix
    stream.seek(cd_start)
    data = _read_exact(stream, cd_size)
    entries: list[_ZipEntry] = []
    i = 0
    while i < cd_size:
        if i + _CENTRAL.size > cd_size:
            raise JobError("zip_invalid")
        h = _CENTRAL.unpack_from(data, i)
        if h[0] != b"PK\x01\x02":
            raise JobError("zip_invalid")
        name_at = i + _CENTRAL.size
        end = name_at + h[10] + h[11] + h[12]
        if end > cd_size:
            raise JobError("zip_invalid")
        name = data[name_at:name_at + h[10]]
        extra = data[name_at + h[10]:name_at + h[10] + h[11]]
        local = _zip64_local_offset(extra, h[8], h[9], h[16]) + prefix
        # 本地头必须位于中央目录之前；越界的偏移说明归档已损坏。
        if not 0 <= local < cd_start:
            raise JobError("zip_invalid")
        entries.append(_ZipEntry(cd_start + i, local, name))
        i = end
    if len(entries) != n_total:
        raise JobError("zip_invalid")
    return entries


def _blame(results: list[tuple[str, int]], downstream: bool) -> tuple[str, int] | None:
    """从管道各段 (工具, 退出码) 中找出应报告的失败段；全部成功时返回 None。

    downstream 为 True 时，多段同时失败归咎最下游：压缩管道里压缩器先退出后，
    tar 写管道会因 SIGPIPE 或 EPIPE（bsdtar 以退出码 1 结束）失败，这只是连带结果。
    为 False 时归咎最上游：解压管道里数据损坏会让下游的 tar 也跟着失败。
    """
    failed = [(tool, rc) for tool, rc in results if rc]
    if not failed:
        return None
    return failed[-1] if downstream else failed[0]


class Job:
    """一次性后台任务的公共部分：进程登记与取消、日志排空、暂存目录和错误转换。

    run 在工作线程执行，cancel 可由主线程调用。
    """

    def __init__(self, log: Callable[[str], None] = print) -> None:
        """log 接收命令行和工具输出，每次一行。"""
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
        """执行任务并返回产物路径；文件系统错误统一转换为可翻译的 JobError。"""
        try:
            return self._execute()
        except OSError as exc:
            # 取消时被终止的进程或被删除的暂存文件也会引发 OSError，仍按取消处理。
            if self._cancel.is_set():
                raise Cancelled() from exc
            raise job_error_from_os(exc) from exc

    def _execute(self) -> Path:
        raise NotImplementedError

    # ---------- 内部实现 ----------
    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    @contextmanager
    def _staging(self, parent: Path) -> Iterator[Path]:
        """在 parent 下创建独占暂存目录；退出前先回收子进程，再删除目录。"""
        # 暂存目录与输出在同一文件系统，os.replace 才能保证原子提交。
        with tempfile.TemporaryDirectory(prefix=_STAGING_PREFIX, suffix=_STAGING_SUFFIX,
                                         dir=parent, ignore_cleanup_errors=True) as staging:
            try:
                yield Path(staging)
            finally:
                self._cleanup_processes()

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

    def _spawn(self, cmd: list[str], *, tool: str, **kw: Any) -> subprocess.Popen:
        """记录 cmd 并以统一环境启动进程；tool 是 TOOLS 的 key，用于错误提示。

        kw 传给 Popen，返回已登记的进程。
        """
        self._check_cancel()
        self.log("$ " + shlex.join(cmd))
        # 不继承应用的标准输入：工具意外读取输入时立即读到 EOF，而不是让任务一直等待。
        kw.setdefault("stdin", subprocess.DEVNULL)
        # 与 cancel 共用锁，防止取消落在进程创建和登记之间而漏杀进程。
        with self._procs_lock:
            self._check_cancel()
            try:
                p = subprocess.Popen(cmd, env=ENV, **kw)
            except OSError as exc:
                cwd = kw.get("cwd")
                # 切换工作目录失败（例如源所在的卷已推出）时 filename 是该目录，
                # 这不是工具本身的问题，按文件系统错误报告并带上路径。
                if cwd is not None and exc.filename is not None and str(exc.filename) == str(cwd):
                    raise job_error_from_os(exc) from exc
                raise JobError("tool_start_failed", tool=tool,
                               err=exc.strerror or str(exc)) from exc
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

    def _run_simple(self, cmd: list[str], *, tool: str, cwd: Path,
                    stdout: BinaryIO | None = None) -> None:
        """运行单个命令并排空日志，任何非 0 退出码都按失败处理。

        stdout 为 None 时合并 stdout/stderr 写入日志；否则把 stdout 写入该文件，只记录 stderr。
        """
        if stdout is None:
            p = self._spawn(cmd, tool=tool, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
            t = self._pump(p.stdout)
        else:
            p = self._spawn(cmd, tool=tool, cwd=cwd, stdout=stdout, stderr=subprocess.PIPE)
            t = self._pump(p.stderr)
        rc = p.wait()
        t.join()
        self._check_cancel()
        # 警告类退出码（如 7z 的 1）也可能意味着遗漏文件，不能当作成功。
        if rc:
            raise JobError("tool_failed", tool=tool, code=rc)

    def _run_pipeline(self, first: list[str], second: list[str], *, tools: tuple[str, str],
                      cwd: Path, stdout: BinaryIO | int, blame_downstream: bool) -> None:
        """运行 first | second，second 的输出写入 stdout；检查两端退出码。

        两端都失败时，blame_downstream 决定报告哪一端，含义见 _blame。
        """
        p1 = self._spawn(first, tool=tools[0], cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
        threads = [self._pump(p1.stderr)]
        p2 = self._spawn(second, tool=tools[1], cwd=cwd, stdin=p1.stdout, stdout=stdout,
                         stderr=subprocess.PIPE)
        # 父进程不持有读取端，下游退出时上游才能收到管道关闭信号。
        p1.stdout.close()
        threads.append(self._pump(p2.stderr))
        results = [(tools[0], p1.wait()), (tools[1], p2.wait())]
        for t in threads:
            t.join()
        self._check_cancel()
        if (failed := _blame(results, blame_downstream)):
            raise JobError("tool_failed", tool=failed[0], code=failed[1])


class ArchiveJob(Job):
    """一次性归档任务；run 在工作线程执行，cancel 可由主线程调用。"""
    def __init__(self, src: Path | str, out: Path | str, fmt: str, level: int | None = None,
                 extreme: bool = False, log: Callable[[str], None] = print) -> None:
        """src/out 为输入输出路径；fmt 选择格式，level/extreme 控制压缩，log 接收日志。"""
        super().__init__(log)
        self.src = Path(src).absolute()
        self.out = Path(out).absolute()
        self.fmt = FORMATS[fmt]
        self.level = level if level is not None else (self.fmt.level or (0, 0, 0))[2]
        self.extreme = extreme

    def _execute(self) -> Path:
        """验证输出位置，创建归档并原子提交，返回产物路径；异常时清理暂存数据。"""
        self._check_cancel()
        missing = missing_tools(self.fmt.key)
        if missing:
            raise JobError("missing_tools", fmt=self.fmt.key, tools=", ".join(missing))
        # 源可能在选择之后被删除或替换；源是链接时改用解析后的目标。
        self.src = validate_source(self.src)
        check_compress_output(self.src, self.out)

        # 每个任务独占暂存目录，避免删除已有 .part 文件或与另一个任务互相覆盖。
        with self._staging(self.out.parent) as staging:
            tmp = staging / (self.out.name + _PART_EXT)
            if self.fmt.key.startswith("tar"):
                self._run_tar(tmp)
            elif self.fmt.key == "zip":
                self._run_zip(tmp)
            else:
                self._run_7z(tmp)
            self._check_cancel()
            os.replace(tmp, self.out)
            return self.out

    @property
    def _name(self) -> str:
        n = self.src.name
        # 以短横线开头会被当成选项，以 @ 开头会被 bsdtar/7z 当成归档或列表文件，
        # 这两种情况都加相对路径前缀。
        return "./" + n if n.startswith(("-", "@")) else n

    def _tar_cmd(self) -> list[str]:
        """返回写入标准输出的 tar 命令，统一屏蔽磁盘垃圾和 macOS 元数据。"""
        tar = require_tool("tar", self.fmt.key)
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
            return [require_tool("gzip", self.fmt.key), f"-{lv}", "-c"]
        # -c 写标准输出；xz 的 -e 增加搜索强度，-T0 让工具自动选线程数。
        if self.fmt.key == "tar.xz":
            return [require_tool("xz", self.fmt.key), f"-{lv}{'e' if self.extreme else ''}",
                    "-T0", "-c"]
        if self.fmt.key == "tar.zst":
            cmd = [require_tool("zstd", self.fmt.key), f"-{lv}", "-T0", "-q", "-c"]
            if lv > 19:
                # zstd 默认限制到 19，20–22 需要显式开启高内存模式。
                cmd.insert(1, "--ultra")
            return cmd
        return None

    def _run_tar(self, tmp: Path) -> None:
        """把 tar 输出写入 tmp，或经压缩器管道写入；任一端失败都报告对应工具。"""
        comp = self._compressor()
        with open(tmp, "wb") as f:
            if comp:
                self._run_pipeline(self._tar_cmd(), comp, tools=("tar", self.fmt.tools[-1]),
                                   cwd=self.src.parent, stdout=f, blame_downstream=True)
            else:
                self._run_simple(self._tar_cmd(), tool="tar", cwd=self.src.parent, stdout=f)

    def _run_zip(self, tmp: Path) -> None:
        """生成 zip 递归排除参数并写入 tmp，保留符号链接本身。"""
        excludes = []
        for p in JUNK:  # zip 的 * 能跨越 /，四种写法覆盖任意层级
            excludes += [p, f"*/{p}", f"{p}/*", f"*/{p}/*"]
        # -X 去掉额外属性，-y 存储链接而不沿链接遍历源目录之外的文件。
        cmd = [require_tool("zip", self.fmt.key), "-r", "-X", "-y", f"-{self.level}",
               str(tmp), self._name, "-x", *excludes]
        self._run_simple(cmd, tool="zip", cwd=self.src.parent)
        if sys.platform == "darwin":
            self._mark_zip_utf8(tmp)

    def _mark_zip_utf8(self, path: Path) -> None:
        """为 macOS zip 的 UTF-8 文件名补标记；只改头部，不重新压缩文件内容。"""
        with path.open("r+b") as stream:
            entries = _read_central_directory(stream)
            targets = []
            for entry in entries:
                self._check_cancel()
                stream.seek(entry.local)
                local = _LOCAL.unpack(_read_exact(stream, _LOCAL.size))
                # 先确认本地头签名和文件名与中央目录一致，再决定是否写入。
                if local[0] != b"PK\x03\x04" or _read_exact(stream, local[9]) != entry.name:
                    raise JobError("zip_invalid")
                if entry.name.isascii():
                    continue
                # Apple 的 zip 缺少 Unicode 编译选项，但 macOS 路径字节本身是 UTF-8。
                try:
                    entry.name.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise JobError("zip_invalid") from exc
                targets.append(entry)
            # 全部校验通过后才写入，同步设置中央目录和本地头的 bit 11，兼容不同解压器。
            for entry in targets:
                self._check_cancel()
                for flag_offset in (entry.central + 8, entry.local + 6):
                    stream.seek(flag_offset)
                    flags = struct.unpack("<H", _read_exact(stream, 2))[0]
                    stream.seek(flag_offset)
                    stream.write(struct.pack("<H", flags | 0x800))

    def _run_7z(self, tmp: Path) -> None:
        """生成 7z 递归排除参数并写入 tmp，拒绝有遗漏警告的归档。"""
        # -snl 保存符号链接，-mmt=on 启用多线程，-bsp0 关闭百分比刷屏。
        cmd = [require_tool("7z", self.fmt.key), "a", "-t7z", f"-mx={self.level}", "-mmt=on",
               "-snl", "-bb1", "-bsp0", "-y", str(tmp), self._name]
        # -xr! 对任意层级的文件名递归匹配，避免只排除顶层垃圾。
        cmd += [f"-xr!{p}" for p in JUNK]
        # 退出码 1 可能表示部分文件未读到，_run_simple 会把它当作失败，不提交不完整的归档。
        self._run_simple(cmd, tool="7z", cwd=self.src.parent)
