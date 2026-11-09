"""CleanPack 压缩与解压核心：不依赖 GUI，可单独测试。"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import fnmatch
import hashlib
import itertools
import math
import os
import re
import resource
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

APP_NAME = "CleanPack"
APP_VERSION = "0.2.0"
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


def compress_tools(fmt_key: str, encrypted: bool = False) -> tuple[str, ...]:
    """返回压缩该格式必需的工具；加密 zip 改用系统 tar（bsdtar），见 ArchiveJob._run_zip_encrypted。"""
    if encrypted and fmt_key == "zip":
        return ("tar",)
    return FORMATS[fmt_key].tools


def missing_tools(fmt_key: str, paths: dict[str, str | None] | None = None,
                  encrypted: bool = False) -> list[str]:
    """返回指定格式缺少的工具；paths 可提供已检测结果，未提供的项现场检测。
    encrypted 表示要加密，加密 zip 需要的工具与普通 zip 不同。"""
    paths = paths or {}
    return [t for t in compress_tools(fmt_key, encrypted)
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


# ---------- 加密 ----------
# 只有 zip 和 7z 有通用的标准加密方式；tar 系列没有，界面上停用加密选项。
ENCRYPTABLE = ("zip", "7z")
# zip 的加密方式，取值即 bsdtar 的 zip:encryption 参数；第一个是默认值。ZipCrypto 很容易被
# 破解，只为兼容只认传统加密的解压工具而保留。
ZIP_METHODS = ("aes256", "zipcrypt")
# 生成归档时的密码长度上限（字符数）。7-Zip 解 WinZip AES 的 zip 时拒绝超过 99 字节的密码
# （实测 100 个字符即报错）；统一取 99，保证生成的归档换个工具也能打开。
PASSWORD_MAX = 99
# 解压时的上限：别人的归档可能用了更长的密码。bsdtar 读密码的缓冲区是 1024 字节，
# 255 个字符即使每个占 4 字节也放得下，更长的会被悄悄截断。
EXTRACT_PASSWORD_MAX = 255


def check_password(password: str, fmt_key: str | None) -> None:
    """校验密码，不合格时抛出 JobError（错误参数里不含密码本身）。fmt_key 是要生成的格式；
    解压时为 None，只检查长度和控制字符，因为别人的归档可能用了非 ASCII 密码。

    密码经标准输入按行交给工具，所以不能含换行等控制字符。zip 对非 ASCII 密码没有统一的
    编码约定，换个解压工具可能就打不开，生成 zip 时只允许可打印 ASCII；7z 按 Unicode 处理密码。
    """
    if not password:
        raise JobError("password_empty")
    limit = EXTRACT_PASSWORD_MAX if fmt_key is None else PASSWORD_MAX
    if len(password) > limit:
        raise JobError("password_too_long", max=limit)
    if any(unicodedata.category(c) in ("Cc", "Cs") for c in password):
        raise JobError("password_invalid_char")
    if fmt_key == "zip" and not all(" " <= c <= "~" for c in password):
        raise JobError("password_not_ascii")


class JobError(Exception):
    """带翻译 key 的错误，GUI 负责本地化。

    denial 是访问被拒的原因（见 classify_denial），界面据此决定提供哪种补救按钮；其他错误
    为 None。它不放进 kw，kw 只放文案的占位符。
    """

    def __init__(self, key: str, *, denial: str | None = None, **kw: object) -> None:
        super().__init__(key)
        self.key, self.kw, self.denial = key, kw, denial


# ---------- 文件系统错误与输出预检 ----------
# 常见 errno 对应的翻译 key；其余错误统一归为 io_error，附带系统给出的原因。
_ERRNO_KEYS = {
    errno.ENOSPC: "no_space", errno.EDQUOT: "no_space", errno.EROFS: "read_only",
    errno.EACCES: "access_denied", errno.EPERM: "access_denied",  # 具体原因见 classify_denial
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


# 访问被拒的原因（classify_denial 的返回值）对应的专门文案；posix 沿用调用方给出的 key。
# system_read 不是 classify_denial 的返回值：读取被 SIP 拒绝时 _denied 改用它，因为
# system_denied 的文案说的是“不能修改”，而压缩源、读取归档只是读。
_DENIAL_KEYS = {"privacy": "privacy_denied", "locked": "locked_denied",
                "system": "system_denied", "system_read": "system_read_denied"}
# 访达「已锁定」对应 uchg；仅追加（append）同样让修改以 EPERM 失败。
_LOCK_FLAGS = stat.UF_IMMUTABLE | stat.SF_IMMUTABLE | stat.UF_APPEND | stat.SF_APPEND


def classify_denial(path: str | os.PathLike[str], err_no: int | None, *,
                    write: bool = True) -> str:
    """判断访问 path 被拒的原因，返回 privacy、locked、system 或 posix。

    err_no 是系统调用给出的 errno；为 None 表示只有 os.access 的结论。write 表示被拒的是
    修改（新建、写入、改名），读取时为 False。

    EACCES 只来自 POSIX 权限或 ACL。EPERM 的来源更多：macOS 隐私保护（TCC，例如未获授权的
    桌面、文稿、下载、外置卷）、文件被锁定、系统完整性保护（SIP）都会给出它，所以先看文件
    标志排除后两种，剩下的按隐私保护处理，文案里也只说“通常”。锁定只阻止修改，不阻止读取，
    所以只在 write 时考虑。os.access 不反映隐私保护，但 access(2) 会经过 SIP 的检查（例如
    /private/var/db/oah：权限位允许读取，access 却以 EPERM 失败），而 Python 丢掉了 errno；
    所以只有 os.access 的结论（err_no 为 None）时，带 restricted 标志的位置按 system 处理，
    其余按 POSIX 权限或锁定。

    路径存在时只看它自己的标志；不存在时（要在其中新建）看它所在的文件夹。先解析成真实
    路径：/tmp、/var 这些链接本身带 SIP 的 restricted 标志，指向的 /private/tmp 却是普通文件夹。
    """
    real = os.path.realpath(path)
    try:
        flags = os.lstat(real).st_flags
    except OSError:
        try:
            flags = os.lstat(os.path.dirname(real)).st_flags
        except OSError:
            flags = 0
    if err_no == errno.EACCES:
        return "posix"
    if write and flags & _LOCK_FLAGS:
        return "locked"
    if err_no is None and flags & stat.SF_RESTRICTED:
        return "system"
    if err_no != errno.EPERM:
        return "posix"
    if flags & stat.SF_RESTRICTED:  # 受 SIP 保护的系统位置，ls -lO 显示 restricted
        return "system"
    return "privacy"


def _denied(key: str, path: Path, err_no: int | None, *, write: bool) -> JobError:
    """访问 path 被拒时的错误：posix 用调用方的 key，其他原因用对应的专门文案。"""
    denial = classify_denial(path, err_no, write=write)
    lookup = "system_read" if denial == "system" and not write else denial
    return JobError(_DENIAL_KEYS.get(lookup, key), denial=denial, path=str(path))


def job_error_from_os(exc: OSError) -> JobError:
    """把 OSError 转成可翻译的 JobError，界面不再直接显示英文系统消息。"""
    key = _ERRNO_KEYS.get(exc.errno or 0, "io_error")
    # os.replace 等双路径调用的 filename2 是目标，也就是用户选择的输出，优先报告它。
    name = exc.filename2 or exc.filename
    path = _visible_path(os.fsdecode(name)) if name else ""
    if key in _PATH_KEYS and not path:
        key = "io_error"
    denial = None
    if key == "access_denied":
        # 按实际出错的路径（可能在暂存目录里）判断原因，文案里显示的仍是用户可见的位置。
        # 任务里经这里转换的多是写入（暂存、提交）；读取用户文件的地方自己调用 _denied。
        denial = classify_denial(os.fsdecode(name), exc.errno)
        key = _DENIAL_KEYS.get(denial, key)
    return JobError(key, denial=denial, path=path, err=exc.strerror or str(exc))


def check_output_dir(path: Path) -> None:
    """确认输出目录存在、是目录且可写，否则抛出带路径的 JobError。"""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise JobError("out_dir_missing", path=str(path)) from None
    except NotADirectoryError:
        raise JobError("out_dir_not_dir", path=str(path)) from None
    except PermissionError as exc:
        raise _denied("access_denied", path, exc.errno, write=True) from None
    if not stat.S_ISDIR(st.st_mode):
        raise JobError("out_dir_not_dir", path=str(path))
    try:
        read_only = bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        read_only = False
    if read_only:
        raise JobError("read_only", path=str(path))
    if not os.access(path, os.W_OK | os.X_OK):
        raise _denied("access_denied", path, None, write=True)


def same_location(a: Path | str | None, b: Path | str | None) -> bool:
    """a 和 b 是否指向同一个文件或文件夹（按 inode 比较）。键入的路径可能和原生面板返回的
    写法不同（APFS 不区分大小写、中间经过符号链接），按字符串比较会把同一位置当成新的选择。
    任一个不存在或读不了时返回 False。"""
    if a is None or b is None:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


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
    except PermissionError as exc:
        raise _denied("src_denied", p, exc.errno, write=False) from None
    except OSError as exc:
        raise job_error_from_os(exc) from exc
    # 顶层名称本身匹配排除规则时，工具会把整个源排除掉，得到一个“成功”的空归档。
    if is_junk_name(p.name):
        raise JobError("src_is_junk", name=p.name.replace("\r", "\\r"))
    is_dir = stat.S_ISDIR(st.st_mode)
    if not (is_dir or stat.S_ISREG(st.st_mode)):
        raise JobError("src_unsupported_type", path=str(p))  # FIFO、设备文件、套接字
    if not os.access(p, os.R_OK | (os.X_OK if is_dir else 0)):
        raise _denied("src_denied", p, None, write=False)
    return p


def check_source_readable(path: Path, check: Callable[[], None] | None = None, *,
                          name: str | None = None, sizes: bool = False) -> SourceStats:
    """实际读一次源：文件打开一次，文件夹则逐层列出每个子文件夹（不跟随链接，跳过垃圾项）；
    读不了时抛出带原因的 JobError。check 对每个文件夹调用一次，用于响应取消。

    同时统计工具要读的内容（进度条的总量），返回 SourceStats。name 是工具输出里源本身的
    名称（默认 path.name）；sizes 为真时还记下每一项的大小（按文件计进度时用）。

    os.access 和 stat 都看不出 macOS 隐私保护：它只拦读取内容，不拦元数据。Info-ZIP（zip 是
    默认格式）遇到读不了的子文件夹会静默跳过、照常成功，归档里悄悄少了内容（例如压缩个人
    文件夹时未获授权的“桌面”）；bsdtar 和 7-Zip 会以非 0 退出，但只能报“工具失败”。所以开始前
    先完整检查一遍，给出带原因、能补救的错误。首次读取受保护的位置时系统可能弹出同意框并
    阻塞当前线程，所以只在任务的工作线程里调用，不在界面线程的键入校验里调用。

    能列出但不能进入（有 r 没有 x）的文件夹也要拦下：列出不需要 x，Info-ZIP 却打不开其中的
    文件，同样静默跳过。路径超过 PATH_MAX 等与权限无关的错误不在这里报告：这一步按完整路径
    遍历，而 bsdtar 按相对路径遍历，能处理更深的树，交给工具自己判断。
    """
    def skip_unless_denied(exc: OSError) -> None:
        if isinstance(exc, PermissionError):
            raise exc

    top = name or path.name
    stats = SourceStats(sizes={} if sizes else None)
    try:
        if not path.is_dir():
            with open(path, "rb") as f:
                stats.add(top, os.fstat(f.fileno()).st_size)
            return stats
        for dirpath, dirnames, filenames in os.walk(path, topdown=True,
                                                    onerror=skip_unless_denied, followlinks=False):
            if check is not None:
                check()
            if not os.access(dirpath, os.X_OK):
                raise _denied("src_denied", Path(dirpath), None, write=False)
            dirnames[:] = [d for d in dirnames if not is_junk_name(d)]
            rel = os.path.relpath(dirpath, path)
            base = top if rel == "." else f"{top}/{rel}"
            stats.add(base, 0)
            for f in filenames:
                if is_junk_name(f):
                    continue
                try:
                    st = os.lstat(os.path.join(dirpath, f))
                except OSError:
                    continue  # 刚被删除或路径过长：只影响进度的估计，交给工具处理
                stats.add(f"{base}/{f}", st.st_size if stat.S_ISREG(st.st_mode) else 0)
    except PermissionError as exc:
        where = Path(os.fsdecode(exc.filename)) if exc.filename else path
        raise _denied("src_denied", where, exc.errno, write=False) from None
    return stats


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


def parse_folder(raw: str) -> Path | None:
    """解析键入的文件夹路径（解压目标）：留空返回 None，否则必须是绝对路径。

    文件夹是否存在、可写由 check_output_dir 检查，作为提示显示，不阻止保存设置。
    """
    s = clean_typed_path(raw)
    return _absolute_typed(s) if s else None


# ---------- 解压：格式识别与归档校验 ----------
# 归档内容的类别，决定用哪条命令解压。tar、gzip、xz、zip 都交给 bsdtar（libarchive
# 自带解码器），zstd 走管道（本机 bsdtar 未编译 libzstd），7z 用 7-Zip。
_MAGIC = [  # (偏移, 魔数, 类别)
    (0, b"\x1f\x8b", "gzip"),
    (0, b"\xfd7zXZ\x00", "xz"),
    (0, b"\x28\xb5\x2f\xfd", "zstd"),
    (0, b"7z\xbc\xaf\x27\x1c", "7z"),
    (0, b"PK\x03\x04", "zip"),
    (0, b"PK\x05\x06", "zip"),   # 空 zip 只有结束记录
    (257, b"ustar", "tar"),      # POSIX/GNU tar 头部的 magic 字段
]
_FAMILY_OF_FORMAT = {"tar": "tar", "tar.gz": "gzip", "tar.xz": "xz", "tar.zst": "zstd",
                     "zip": "zip", "7z": "7z"}
_FORMAT_OF_FAMILY = {family: fmt for fmt, family in _FAMILY_OF_FORMAT.items()}
# 解压各类别必需的工具；顺序与管道方向一致。7z 先用 bsdtar（链接原样保留），
# 7zz 只在 bsdtar 解不了时作为可选的后备，所以不算必需。
_EXTRACT_TOOLS = {"tar": ("tar",), "gzip": ("tar",), "xz": ("tar",), "zip": ("tar",),
                  "zstd": ("zstd", "tar"), "7z": ("tar",)}


def detect_format(name: str) -> str | None:
    """按最长扩展名（不区分大小写）返回格式 key，例如 a.TAR.GZ → tar.gz；不支持时返回 None。"""
    lower = name.lower()
    for fmt in sorted(FORMATS.values(), key=lambda f: len(f.ext), reverse=True):
        if lower.endswith(fmt.ext) and len(name) > len(fmt.ext):
            return fmt.key
    return None


def sniff_family(path: Path | str) -> str | None:
    """读取文件头魔数返回内容类别；识别不了时返回 None，由扩展名决定。"""
    with open(path, "rb") as f:
        head = f.read(262)
    for offset, magic, family in _MAGIC:
        if head[offset:offset + len(magic)] == magic:
            return family
    return None


def extract_family(path: Path) -> str:
    """决定解压路线：魔数能明确识别时以内容为准（扩展名可能写错），否则按扩展名。"""
    fmt = detect_format(path.name)
    sniffed = sniff_family(path)
    if sniffed is not None:
        return sniffed
    if fmt is None:
        raise JobError("archive_unsupported", name=path.name)
    return _FAMILY_OF_FORMAT[fmt]


_BSDTAR_DECRYPTABLE = (0, 8)  # libarchive 能解密后再解压的方法：存储、deflate


def zip_encryption(path: Path, check: Callable[[], None] | None = None) -> tuple[bool, bool] | None:
    """返回 (是否有加密的成员, 是否需要交给 7-Zip)；中央目录读不了时返回 None。
    check 用于响应取消（要逐个读本地头，成员很多又在慢速卷上时需要一些时间）。

    加密看通用标志位 bit 0（传统加密和 AES 都会设置），中央目录和本地头任一处标明即算，
    见 _encrypted_entries。必须事先判断：传统 ZipCrypto 只用
    一个校验字节验证密码，bsdtar 拿任何假密码去试，约有 1/256 的机会被当成正确密码，
    随后解出乱码并报 CRC 错误，而不是“需要密码”。

    以下加密成员 bsdtar 处理不了，归档要交给 7-Zip：
    - 符号链接（7-Zip 加 -snl 时会生成）：AES 的直接报错，传统加密的却把密文当成链接目标
      写出来并报告成功；
    - 存储和 deflate 以外的压缩方法（LZMA、BZip2、PPMd、XZ、Deflate64 等）：libarchive 只为
      这两种方法先解密，其他方法把密文直接交给解压器，报出与密码无关的错误。
    """
    try:
        with open(path, "rb") as stream:
            encrypted = [entry for entry, _ in _encrypted_entries(stream, check or _no_check)]
    except (OSError, JobError):
        return None  # 结构异常的 zip 交给 bsdtar 处理并报告
    return bool(encrypted), any(stat.S_ISLNK(entry.mode) or entry.method not in _BSDTAR_DECRYPTABLE
                                for entry in encrypted)


def _encrypted_entries(stream: BinaryIO,
                       check: Callable[[], None]) -> Iterator[tuple[_ZipEntry, bool]]:
    """逐个给出加密的非目录成员，以及它的中央目录与本地头是否不一致（True 表示不一致）。

    bsdtar 以本地头为准：名称、是否加密（bit 0 或 AES 方法 99）都看本地头。所以只有两处
    名称都以 / 结尾才算目录、跳过（目录项没有数据，标志位不影响解压）；只要任一处标明加密
    就算加密。两处记录不一（名称或加密标志不同）的归档只能是损坏或刻意构造的，不能因为
    中央目录的说法就跳过它的密码校验。
    """
    for entry in _read_central_directory(stream):
        check()
        stream.seek(entry.local)
        local = _LOCAL.unpack(_read_exact(stream, _LOCAL.size))
        if local[0] != b"PK\x03\x04":
            # 本地头读不出来：按中央目录的说法，交给密码校验和 bsdtar 去报告。
            if entry.flags & 0x1 and not entry.name.endswith(b"/"):
                yield entry, False
            continue
        local_name = _read_exact(stream, local[9])
        if entry.name.endswith(b"/") and local_name.endswith(b"/"):
            continue
        central_enc = bool(entry.flags & 0x1)
        local_enc = bool(local[2] & 0x1 or local[3] == 99)
        if central_enc or local_enc:
            yield entry, central_enc != local_enc or local_name != entry.name


def zip_is_encrypted(path: Path) -> bool:
    """zip 中是否有加密的成员，见 zip_encryption；读不了时返回 False。"""
    return (zip_encryption(path) or (False, False))[0]


_AES_EXTRA = 0x9901  # WinZip AES 扩展字段


def _zipcrypto_check(password: bytes, header: bytes) -> int:
    """按 PKWARE 传统加密算法解密 12 字节的加密头，返回最后一个字节（密码校验字节）。"""
    keys = [0x12345678, 0x23456789, 0x34567890]

    def crc(c: int, b: int) -> int:
        return zlib.crc32(bytes([b]), c ^ 0xFFFFFFFF) ^ 0xFFFFFFFF

    def update(b: int) -> None:
        keys[0] = crc(keys[0], b)
        keys[1] = ((keys[1] + (keys[0] & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
        keys[2] = crc(keys[2], keys[1] >> 24)

    for b in password:
        update(b)
    plain = 0
    for c in header:
        t = (keys[2] | 2) & 0xFFFF
        plain = c ^ (((t * (t ^ 1)) >> 8) & 0xFF)
        update(plain)
    return plain


def _aes_field(extra: bytes, offset: int, fmt: str) -> int | None:
    """从扩展字段里找 WinZip AES 记录（版本 2 字节、厂商 2 字节、强度 1 字节、真实压缩方法
    2 字节），按 fmt 读出记录内 offset 处的值（8 是强度，9 是真实压缩方法）；没有时返回 None。"""
    i = 0
    while i + 4 <= len(extra):
        tag, size = struct.unpack_from("<HH", extra, i)
        if tag == _AES_EXTRA and size >= 7 and i + 4 + size <= len(extra):
            return struct.unpack_from(fmt, extra, i + offset)[0]
        i += 4 + size
    return None


def _entry_password(stream: BinaryIO, entry: _ZipEntry, password: bytes) -> str:
    """校验一个加密成员的密码，返回 "wrong"、"aes"（AES 校验值相符）、
    "weak"（传统加密只有一个校验字节，或判断不了）。"""
    stream.seek(entry.local)
    local = _LOCAL.unpack(_read_exact(stream, _LOCAL.size))
    if local[0] != b"PK\x03\x04":
        return "weak"
    stream.seek(entry.local + _LOCAL.size + local[9])
    extra = _read_exact(stream, local[10])
    if local[3] == 99:  # AES：盐之后是 2 字节的密码校验值
        strength = _aes_field(extra, 8, "<B")
        if strength not in (1, 2, 3):
            return "weak"
        key_len, salt_len = 8 * (strength + 1), 4 * (strength + 1)
        salt = _read_exact(stream, salt_len)
        verifier = _read_exact(stream, 2)
        derived = hashlib.pbkdf2_hmac("sha1", password, salt, 1000, 2 * key_len + 2)
        return "wrong" if derived[-2:] != verifier else "aes"
    if (entry.flags | local[2]) & 0x40:  # PKWARE 强加密，不认识
        return "weak"
    # 传统加密：校验字节是 CRC 的高字节；用数据描述符（bit 3）时 CRC 写在后面，
    # 改用修改时间的高字节。两种都接受，不会因解压器的取舍不同而误判正确的密码。
    byte = _zipcrypto_check(password, _read_exact(stream, 12))
    return "weak" if byte in (local[6] >> 24, (local[4] >> 8) & 0xFF) else "wrong"


def check_zip_password(path: Path, password: str,
                       check: Callable[[], None] | None = None) -> str:
    """事先校验 zip 的密码，返回 "wrong"（一定不正确）、"strong"（所有加密成员都是 AES，
    每个的校验值都相符，且中央目录与本地头的记录一致）或 "weak"（有传统加密的成员、
    两处记录不一致，或判断不了；错误密码仍可能通过）。

    bsdtar 遇到密码错误的成员会重新派生密钥再试，最多上万次；AES 每次都要做一千轮 PBKDF2，
    一个成员就要几秒，成员多时等同于卡死。所以在交给 bsdtar 之前先自己逐个校验（每个 AES
    成员一千轮 PBKDF2，与 bsdtar 解压时自己的开销相当）。只有全部校验过，"strong" 才可信：
    ExtractJob 据此不再提防 bsdtar 的第二次提示。check 用于响应取消。
    判断不了时按 "weak" 处理，交给 bsdtar，避免把正确的密码误判为错误。
    """
    check = check or _no_check
    secret = password.encode("utf-8")
    verdicts = set()
    try:
        with open(path, "rb") as stream:
            for entry, inconsistent in _encrypted_entries(stream, check):
                verdict = _entry_password(stream, entry, secret)
                if verdict == "wrong":
                    return "wrong"
                # 两处记录不一致的归档不可信，不给 "strong"，bsdtar 第二次要密码时照样结束它。
                verdicts.add("weak" if inconsistent else verdict)
    except (OSError, JobError):
        return "weak"
    return "strong" if verdicts == {"aes"} else "weak"


def extract_tools(family: str) -> tuple[str, ...]:
    return _EXTRACT_TOOLS[family]


def archive_format(path: Path) -> str:
    """返回按内容（识别不了时按扩展名）判断出的格式 key，用于界面显示和缺工具提示。"""
    return _FORMAT_OF_FAMILY[extract_family(path)]


def validate_archive(path: Path | str) -> Path:
    """校验要解压的归档，返回绝对路径；必须是扩展名受支持的普通文件，否则抛出 JobError。

    与 validate_source 一样接受链接并解析到目标，解压结果默认放在归档所在的文件夹。
    """
    p = _absolute_typed(str(path))
    if p.name and os.path.islink(p):
        p = Path(os.path.realpath(p))
    try:
        st = os.stat(p)
    except (FileNotFoundError, NotADirectoryError):
        raise JobError("src_missing", path=str(p)) from None
    except PermissionError as exc:
        raise _denied("src_denied", p, exc.errno, write=False) from None
    except OSError as exc:
        raise job_error_from_os(exc) from exc
    if not stat.S_ISREG(st.st_mode):
        raise JobError("archive_not_file", path=str(p))
    if detect_format(p.name) is None:
        raise JobError("archive_unsupported", name=p.name)
    if not os.access(p, os.R_OK):
        raise _denied("src_denied", p, None, write=False)
    return p


# ---------- 解压：整理解出的文件树 ----------
def _walk_dirs_first(root: Path) -> Iterator[tuple[str, list[str], list[str]]]:
    """自顶向下遍历，不跟随符号链接；遇到读不了的目录直接抛出 OSError。"""
    def fail(exc: OSError) -> None:
        raise exc
    return os.walk(root, topdown=True, onerror=fail, followlinks=False)


_OPEN_MAX = 10240  # macOS 允许单个进程打开的文件数上限（<sys/syslimits.h> 的 OPEN_MAX）


def raise_fd_limit() -> None:
    """把可打开文件数的软上限提高到 OPEN_MAX（不超过硬上限）。

    从 Finder 启动的应用默认只有 256 个。遍历和删除目录树时每一层要占一个描述符，
    几百层深的归档（解压前无法预知）在默认上限下会失败，失败后的暂存目录也删不掉。
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = _OPEN_MAX if hard == resource.RLIM_INFINITY else min(hard, _OPEN_MAX)
    if soft < target:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))


def _no_check() -> None:
    """默认的取消检查：什么都不做。"""


def make_tree_accessible(root: Path, check: Callable[[], None] = _no_check) -> None:
    """给每个目录补上 u+rwx。归档里可能有 000 或 500 权限的目录，不补的话无法清理垃圾、
    检查内容，失败时也删不掉暂存目录。--no-same-permissions 只会去掉组和其他人的位。

    用 os.fwalk 按目录描述符操作，成员路径超过 PATH_MAX 也能处理；每层占一个描述符，
    调用方需先 raise_fd_limit。check 对每个子目录调用一次，用于响应取消。
    """
    os.chmod(root, stat.S_IMODE(os.lstat(root).st_mode) | stat.S_IRWXU)
    def fail(exc: OSError) -> None:
        raise exc
    for _, dirnames, _, dir_fd in os.fwalk(root, topdown=True, onerror=fail,
                                           follow_symlinks=False):
        check()
        for name in dirnames:
            check()
            st = os.lstat(name, dir_fd=dir_fd)
            if stat.S_ISDIR(st.st_mode):  # dirnames 里也包括指向目录的符号链接，不能跟随
                os.chmod(name, stat.S_IMODE(st.st_mode) | stat.S_IRWXU, dir_fd=dir_fd)


def sweep_junk(root: Path, check: Callable[[], None] = _no_check) -> int:
    """删除树中任意层级名称匹配 JUNK 的项，返回删除个数；不跟随符号链接。

    这是解压垃圾过滤的最终保证：不依赖各工具 exclude 规则的通配细节。check 用于响应取消，
    对每一项调用一次。
    """
    removed = 0
    for dirpath, dirnames, filenames in _walk_dirs_first(root):
        check()
        for name in [n for n in dirnames if is_junk_name(n)]:
            check()
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                os.unlink(path)
            else:
                shutil.rmtree(path)
            dirnames.remove(name)  # 已删除，不再进入
            removed += 1
        for name in filenames:
            check()
            if is_junk_name(name):
                os.unlink(os.path.join(dirpath, name))
                removed += 1
    return removed


_LINK_HOPS_MAX = 32          # macOS 的 MAXSYMLINKS：一次路径解析最多展开 32 个链接
_LINK_PARTS_MAX = 16384      # 解析单个链接的工作量上限
_LINK_WORK_MAX = 2_000_000   # 一棵树里解析全部链接的工作量上限（约一两秒）
# 工作量按实际开销计：每个分量拼路径、查链接的开销与当前深度成正比，复制缓存的位置与其长度
# 成正比。只按分量计数的话，恶意归档能让深度不断增长，使总时间随链接数平方增长。
_LOOP = object()     # 展开的链接数超限：系统也解析不了（ELOOP），不会指向任何地方
_UNKNOWN = object()  # 处理量超限：无法判断，按可能越界提示


class _LinkChecker:
    """判断树中的符号链接是否指到 boundary（最终放进目标文件夹的那一项）之外。

    逐个分量解析目标，经过树内的其他链接时继续展开；只要某一步的 .. 越过 boundary 就算
    越界，即使之后又走回来。原因：提交时 boundary 会改名（例如变成「名称 2」）或离开暂存
    目录，经由上级目录绕回来的路径在提交后会指到别处，可能正是用户已有的同名文件。

    恶意归档可以构造互相引用的长链接，所以总处理量有硬上限：超出后剩下的链接不再解析，
    判为“无法判断”。缓存只保存与剩余预算无关的结果：最终位置和真正越界随时可缓存；
    预算耗尽（_LOOP、_UNKNOWN）只在从链接自己所在目录、以完整预算解析时缓存——从别处
    经过它时剩余预算只会更少，结果不会变好。嵌套解析中途耗尽的结果不缓存。
    """

    def __init__(self, boundary: str) -> None:
        self.boundary = boundary
        self.memo: dict[str, object] = {}  # 链接路径 -> 最终位置、True、_LOOP 或 _UNKNOWN
        self.work = _LINK_WORK_MAX

    def escapes(self, link: str) -> bool | None:
        """True 表示越界，False 表示留在 boundary 内或系统也解析不了，None 表示无法判断。"""
        result = self.memo.get(link)
        if result is None:
            rel = os.path.relpath(os.path.dirname(link), self.boundary)
            stack = [] if rel == "." else rel.split(os.sep)
            result = self._resolve(stack, os.readlink(link), [_LINK_HOPS_MAX, _LINK_PARTS_MAX])
            # 总处理量耗尽（self.work < 0）造成的 _UNKNOWN 与这个链接本身无关，不缓存。
            if result is not _UNKNOWN or self.work >= 0:
                self.memo[link] = result
        if result is _UNKNOWN:
            return None
        return result is True

    def _resolve(self, stack: list[str], target: str, budget: list[int]) -> object:
        """从 stack 所在目录出发解析 target；返回最终位置、True（越界）、_LOOP 或 _UNKNOWN。

        budget 是这次顶层解析剩余的 [链接展开数, 分量数]。
        """
        if os.path.isabs(target):
            return True
        stack = list(stack)
        for part in target.split("/"):
            if part in ("", "."):
                cost = 1
            elif part == "..":
                cost = 1
            else:
                cost = len(stack) + 1  # 下面要拼出完整路径并查询一次
            budget[1] -= cost
            self.work -= cost
            if budget[1] < 0 or self.work < 0:
                return _UNKNOWN
            if part in ("", "."):
                continue
            if part == "..":
                if not stack:
                    return True
                stack.pop()
                continue
            path = os.path.join(self.boundary, *stack, part)
            if not os.path.islink(path):
                stack.append(part)
                continue
            result = self.memo.get(path)
            if result is None:
                budget[0] -= 1
                if budget[0] < 0:
                    return _LOOP
                result = self._resolve(stack, os.readlink(path), budget)
                if result is True or isinstance(result, list):
                    self.memo[path] = result
            if not isinstance(result, list):
                return result
            budget[1] -= len(result)  # 复制缓存的位置
            self.work -= len(result)
            if budget[1] < 0 or self.work < 0:
                return _UNKNOWN
            stack = list(result)
        return stack


def _link_escapes(boundary: str, link: str) -> bool:
    """单独判断一个链接是否越界；无法判断时按越界处理。"""
    return _LinkChecker(boundary).escapes(link) is not False


def verify_tree(root: Path, boundary: Path | None = None,
                check: Callable[[], None] = _no_check) -> list[JobError]:
    """检查解出的文件树，返回需要告知用户的警告；发现不安全的项时抛出 JobError。

    - FIFO、套接字、设备文件：unsafe_entry，整个任务失败；
    - 与树外文件共享 inode 的硬链接：unsafe_link，修改它会改到树外的文件；
    - 普通文件去掉 setuid、setgid 和 sticky 位；
    - 指向 boundary（默认 root，即最终放进目标文件夹的那一项）之外的符号链接保留
      （CleanPack 自己的归档也可能包含），但逐个警告；复杂到无法在处理量上限内判断的
      链接合并为一条 links_unchecked 提示。

    check 对每一项调用一次，用于响应取消。
    """
    root_s = os.path.normpath(str(root))
    bound = os.path.normpath(str(boundary or root))
    warnings: list[JobError] = []
    inode_seen: dict[tuple[int, int], int] = {}
    inode_nlink: dict[tuple[int, int], tuple[int, str]] = {}
    links = _LinkChecker(bound)
    unchecked = 0
    for dirpath, dirnames, filenames in _walk_dirs_first(root):
        for name in dirnames + filenames:
            check()  # 每一项检查一次：一个目录里可能有成千上万个需要解析的链接
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root_s)
            st = os.lstat(path)
            mode = st.st_mode
            if stat.S_ISLNK(mode):
                escapes = links.escapes(path)
                if escapes:
                    warnings.append(JobError("link_outside", name=rel, target=os.readlink(path)))
                elif escapes is None:
                    unchecked += 1
            elif stat.S_ISREG(mode):
                key = (st.st_dev, st.st_ino)
                inode_seen[key] = inode_seen.get(key, 0) + 1
                inode_nlink[key] = (st.st_nlink, rel)
                if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                    os.chmod(path, stat.S_IMODE(mode) & ~(stat.S_ISUID | stat.S_ISGID
                                                          | stat.S_ISVTX))
            elif not stat.S_ISDIR(mode):
                raise JobError("unsafe_entry", name=rel)
    for key, (nlink, rel) in inode_nlink.items():
        if nlink > inode_seen[key]:
            raise JobError("unsafe_link", name=rel)
    if unchecked:
        warnings.append(JobError("links_unchecked", n=unchecked))
    return warnings


def unique_names(folder: Path, name: str, is_dir: bool) -> Iterator[Path]:
    """依次给出 Finder 风格的候选路径：名称、名称 2、名称 3……

    文件的序号插在最后一个扩展名之前（report 2.pdf）；文件夹和以点开头的名称直接追加。
    """
    stem, ext = (name, "") if is_dir else os.path.splitext(name)
    yield folder / name
    n = 2
    while True:
        yield folder / f"{stem} {n}{ext}"
        n += 1


def plan_layout(root: Path, archive_name: str) -> tuple[Path, str, bool]:
    """决定提交方式，返回 (要移动的路径, 首选名称, 是否为目录)。

    智能放置：只有一个顶层项且不是符号链接时，直接放进目标文件夹；否则把整个
    解出的树包进以归档名（去掉扩展名）命名的文件夹。没有内容时抛出 extract_empty。
    """
    entries = sorted(os.listdir(root))
    if not entries:
        raise JobError("extract_empty")
    if len(entries) == 1:
        only = root / entries[0]
        if not only.is_symlink():
            return only, entries[0], only.is_dir()
    return root, strip_ext(archive_name) or archive_name, True


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
    """中央目录中的一项：中央头与本地头的绝对偏移、原始文件名字节、通用标志位，
    外部属性高 16 位里的 Unix 文件类型和权限（不是 Unix 生成的归档通常为 0）、压缩方法
    （AES 成员取扩展字段里记录的真实方法），以及解压后的大小（估算解压进度用）。"""
    central: int
    local: int
    name: bytes
    flags: int = 0
    mode: int = 0
    method: int = 0
    usize: int = 0


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


def _zip64_usize(extra: bytes, usize: int) -> int:
    """返回解压后的大小；32 位字段溢出时从 ZIP64 扩展字段的第一项读取。只用于估算进度，
    读不到时返回 0，不判为损坏（损坏与否由解压工具和中央目录的其他检查决定）。"""
    if usize != _U32_MAX:
        return usize
    i = 0
    while i + 4 <= len(extra):
        tag, length = struct.unpack_from("<HH", extra, i)
        if tag == 0x0001 and length >= 8:
            return struct.unpack_from("<Q", extra, i + 4)[0]
        i += 4 + length
    return 0


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
        method = h[4]
        if method == 99:  # AES：真实的压缩方法记在扩展字段里，读不到时保留 99
            real = _aes_field(extra, 9, "<H")
            # 不能写成 “or method”：真实方法 0（存储）是合法值，会被当成没读到。
            method = real if real is not None else method
        entries.append(_ZipEntry(cd_start + i, local, name, h[3], h[15] >> 16, method,
                                 _zip64_usize(extra, h[9])))
        i = end
    if len(entries) != n_total:
        raise JobError("zip_invalid")
    return entries


_LOG_LINE_MAX = 2000  # 单行日志的最大字符数
# 工具报告读取被拒（EPERM）的行，按工具分开：bsdtar「tar: Can't open `x': Operation not
# permitted」、Info-ZIP「zip warning: Operation not permitted」、7-Zip「WARNING: errno=1 :
# Operation not permitted」及其汇总行「x : errno=1 : …」。只用产生这一行的工具自己的格式：
# Info-ZIP 列出成员时整行以成员名结尾（「  adding: 名称」），名称恰好以 7-Zip 的汇总格式
# 结尾时，共用一个模式就会误判。
_EPERM_TEXT = re.escape(os.strerror(errno.EPERM))
_EPERM_LINES = {
    "tar": re.compile(r"^tar: .*: " + _EPERM_TEXT + "$"),
    "zip": re.compile(r"^zip warning: " + _EPERM_TEXT + "$"),
    "7z": re.compile(r"^(?:WARNING: |.* : )errno=1 : " + _EPERM_TEXT + "$"),
}
# 工具向标准输入要密码时打印的提示。bsdtar 的提示不换行，会粘在下一条消息前面；
# 去掉后报错消息才能按原格式识别，日志也不会被重复的提示刷屏。
_PROMPTS = re.compile(r"^(?:(?:Enter passphrase|Enter password):\s?)+")
_PROMPT_TAIL = re.compile(r"(?:Enter passphrase:)+$")  # bsdtar -v 时粘在名称后面的提示
_PROMPT_BYTES = b"Enter passphrase:"  # bsdtar 每次向标准输入要密码时打印一次
# 行首连续的提示。bsdtar 的错误行以成员名开头，名字中间的提示文字不计数。
_LEADING_PROMPTS = re.compile(rb"^(?:Enter passphrase:)+")


def _leading_prompts(data: bytes) -> int:
    """返回 data 开头连续出现的 bsdtar 密码提示个数。"""
    m = _LEADING_PROMPTS.match(data)
    return len(m.group()) // len(_PROMPT_BYTES) if m else 0


def _send_password(proc: subprocess.Popen, password: str) -> None:
    """把密码写成一行送进工具的标准输入后立即关闭；工具提前退出时忽略管道错误。
    密码很短，写入不会因管道缓冲区满而阻塞。"""
    try:
        proc.stdin.write((password + "\n").encode("utf-8"))
    except OSError:
        pass  # 工具已经退出（例如参数错误），失败原因由退出码报告
    with contextlib.suppress(OSError):
        proc.stdin.close()


# 工具写管道遇到 EPIPE（下游已退出）时自己退出所用的退出码。zstd 先打印
# “zstd: error 70 : Write error : cannot write block : Broken pipe”，随后要么被 SIGPIPE
# 杀死，要么在信号送达前以 70 退出，两种都会出现（2026-11-09 实测，机器越忙后者越多）。
_EPIPE_EXIT = {"zstd": 70}


def _broken_pipe(tool: str, rc: int) -> bool:
    """这个退出码是否表示工具只是因为下游关闭了管道才失败。"""
    return rc == -signal.SIGPIPE or rc == _EPIPE_EXIT.get(tool)


def _blame(results: list[tuple[str, int]], downstream: bool) -> tuple[str, int] | None:
    """从管道各段 (工具, 退出码) 中找出应报告的失败段；全部成功时返回 None。

    downstream 为 True 时，多段同时失败归咎最下游：压缩管道里压缩器先退出后，
    tar 写管道会因 SIGPIPE 或 EPIPE（bsdtar 以退出码 1 结束）失败，这只是连带结果。
    为 False 时归咎最上游：解压管道里数据损坏会让下游的 tar 也跟着失败。
    """
    if not downstream:
        # 解压方向：tar 读到结束标记或出错就会退出，上游的解码器再写管道只会被拒，这只是
        # 连带结果。数据本身是否完整由调用方事先校验（例如 zstd -t）。
        results = [(tool, rc) for tool, rc in results[:-1] if not _broken_pipe(tool, rc)] \
            + results[-1:]
    failed = [(tool, rc) for tool, rc in results if rc]
    if not failed:
        return None
    return failed[-1] if downstream else failed[0]


def _remove_tree(path: Path) -> None:
    """尽力删除暂存目录，不抛出异常（清理失败不能盖过任务本身的结果）。

    先补权限再用 shutil.rmtree；它每层占一个文件描述符，极深的树在描述符上限很低时会失败，
    这时退回系统的 chmod -R 和 rm -rf：它们基于 fts 逐级进入目录，描述符用量与深度无关，
    也不跟随符号链接。
    """
    try:
        make_tree_accessible(path)
        shutil.rmtree(path)
        return
    except OSError:
        pass
    for cmd in (["/bin/chmod", "-R", "-P", "u+rwx", str(path)], ["/bin/rm", "-rf", str(path)]):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, env=ENV)


# ---------- 进度与剩余时间 ----------
PROGRESS_INTERVAL = 0.1  # 进度回调的最短间隔（秒），避免每个数据块都刷新界面
_RELAY_CHUNK = 1 << 20   # 中转和喂数据时每次读写的字节数
_ZIP_DOT = 1 << 20       # Info-ZIP「-ds 1m」每个点代表的输入字节数
# 7-Zip「-bsp1」的进度：百分比后面跟着要用退格擦掉的内容（不是终端时照样输出退格符）。
# 要求后面紧跟退格，才不会把成员名里的「50%」当成进度（成员名那一行以换行结束）。
_SEVENZIP_PERCENT = re.compile(rb"(\d{1,3})%[^\n\x08]*\x08")


class _Progress:
    """任务进度：线程安全、节流、单调不减。

    phase 为 scan（统计源文件）、prepare（解压前的检查）、work（工具运行中）、finish（整理
    和提交）；只有 work 有总量。unit 为 bytes、items（按项数）或 percent（7-Zip 只给百分比）。
    callback(phase, done, total, unit) 在工作线程或读取线程调用，total 为 None 表示不确定。
    """
    def __init__(self, callback: Callable[[str, int, int | None, str], None] | None) -> None:
        self._cb = callback
        self._lock = threading.Lock()
        self._last = 0.0
        self.phase = ""
        self.done = 0
        self.total: int | None = None
        self.unit = "bytes"

    def start(self, phase: str, total: int | None = None, unit: str = "bytes") -> None:
        """进入新阶段，立即通知（不节流）。"""
        with self._lock:
            self.phase, self.total, self.unit, self.done = phase, total, unit, 0
            self._emit(force=True)

    def update(self, done: int) -> None:
        """当前阶段已完成 done；比之前少的值忽略，超过总量的按总量算。"""
        with self._lock:
            if self.total is not None:
                done = min(done, self.total)
            if done > self.done:
                self.done = done
                self._emit(force=False)

    def complete(self) -> None:
        """当前阶段做完了：进度补满并立即通知（预估的总量可能偏大）。"""
        with self._lock:
            if self.total is not None:
                self.done = self.total
            self._emit(force=True)

    def _emit(self, force: bool) -> None:
        if self._cb is None:
            return
        now = time.monotonic()
        if not force and now - self._last < PROGRESS_INTERVAL:
            return
        self._last = now
        self._cb(self.phase, self.done, self.total, self.unit)


class ProgressEstimator:
    """根据进度估算速度和剩余时间；界面在主线程使用，不依赖 Qt，便于测试。

    速度取指数滑动平均（时间常数 TAU 秒），抹平工具输出的忽快忽慢；开始 WARMUP 秒内或完成
    不足 1% 时还估不准，不给剩余时间。进度一段时间没变时，界面定时用同样的值再调用一次，
    速度随之下降、剩余时间变长，不会停在一个过时的估计上。
    """
    TAU = 3.0
    WARMUP = 2.0
    FIRST_SAMPLE = 0.5

    def __init__(self, now: float) -> None:
        self._start = now
        self._t: float | None = None
        self._done = 0
        self.rate: float | None = None  # 每秒完成的单位数

    def update(self, done: int, total: int | None, now: float) -> tuple[float | None, float | None]:
        """记录 now 时刻完成了 done，返回 (速度, 剩余秒数)；还估不出时为 None。"""
        if self._t is None or done < self._done:
            self._t, self._done = now, done  # 第一次，或进入了新阶段
            return self.rate, None
        dt = now - self._t
        if self.rate is None and dt < self.FIRST_SAMPLE:
            # 第一份速度至少用这么长时间的数据：几毫秒内的跳变会算出离谱的速度。
            return None, None
        if dt > 0:
            instant = (done - self._done) / dt
            alpha = 1 - math.exp(-dt / self.TAU)
            self.rate = instant if self.rate is None else self.rate + alpha * (instant - self.rate)
            self._t, self._done = now, done
        if (total is None or not self.rate or now - self._start < self.WARMUP
                or done < total * 0.01):
            return self.rate, None
        return self.rate, max(0.0, (total - done) / self.rate)


def eta_parts(seconds: float) -> tuple[int, int, int]:
    """把剩余秒数取整成 (时, 分, 秒) 以减少跳动：一分钟内按 5 秒向上取整，一小时内按
    10 秒，更长时按分钟。"""
    s = max(0.0, seconds)
    if s < 60:
        s = max(5, math.ceil(s / 5) * 5)
    elif s < 3600:
        s = round(s / 10) * 10
    else:
        s = round(s / 60) * 60
    s = int(s)
    return s // 3600, s % 3600 // 60, s % 60


def _strip_backspaces(line: str) -> str:
    """去掉 7-Zip 用退格覆盖的进度：保留最后一个退格之后的内容。"""
    return line[line.rfind("\x08") + 1:].lstrip() if "\x08" in line else line


@dataclass
class SourceStats:
    """开始压缩前统计的源：普通文件的总字节数、项数（文件、文件夹、链接；tar 每项一个
    512 字节的头）、按 tar 格式估算的流大小，以及可选的「名称 → 大小」表（名称是工具在
    输出里使用的相对路径，以源本身的名称开头；文件夹为 0）。"""
    total: int = 0
    entries: int = 0
    tar_size: int = 1024  # tar 以两个 512 字节的空块结束
    sizes: dict[str, int] | None = None

    def add(self, name: str, size: int) -> None:
        self.total += size
        self.entries += 1
        self.tar_size += 512 + -(-size // 512) * 512
        if self.sizes is not None:
            self.sizes[name] = size


class Job:
    """一次性后台任务的公共部分：进程登记与取消、日志排空、暂存目录和错误转换。

    run 在工作线程执行，cancel 可由主线程调用。
    """

    # 是否让工具脱离应用的控制终端。从终端启动应用时，bsdtar 遇到加密 zip 会直接读
    # /dev/tty 等密码，stdin=DEVNULL 挡不住；只有解压需要，代价是应用被强行杀掉时
    # 工具进程不会随终端会话一起收到信号（正常退出和取消仍会结束它们）。
    _new_session = False

    def __init__(self, log: Callable[[str], None] = print,
                 progress: Callable[[str, int, int | None, str], None] | None = None) -> None:
        """log 接收命令行和工具输出，每次一行；progress 接收进度，含义见 _Progress。"""
        self.log = log
        self._progress = _Progress(progress)
        # 按 bsdtar -v 的输出计进度（见 _track_verbose）：「名称 → 大小」、行首的「a 」或「x 」、
        # 已处理完的大小，以及名称已打印、还没处理完的那一项。
        self._verbose: dict[str, int] | None = None
        self._verbose_prefix = ""
        self._verbose_done = 0
        self._current: str | None = None
        self._relay_error: OSError | None = None  # 中转或喂数据的线程遇到的文件系统错误
        self._cancel = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self._procs_lock = threading.Lock()
        self._pumps: list[threading.Thread] = []
        self._eperm_tools: set[str] = set()  # 报告过读取被拒（EPERM）的工具，见 _tool_failure

    # ---------- 公共接口 ----------
    def set_progress(self, callback: Callable[[str, int, int | None, str], None] | None) -> None:
        """在 run 之前设置进度回调（界面在启动任务时统一接上）。"""
        self._progress = _Progress(callback)

    def cancel(self) -> None:
        """设置取消标记并终止已登记进程；清理和等待由 run 所在线程完成。"""
        self._cancel.set()
        self._kill_processes()

    def _kill_processes(self) -> None:
        """结束所有仍在运行的已登记进程；可在任意线程调用。"""
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

    @contextlib.contextmanager
    def _staging(self, parent: Path) -> Iterator[Path]:
        """在 parent 下创建独占暂存目录；退出前先回收子进程，再删除目录。"""
        # 暂存目录与输出在同一文件系统，os.replace 才能保证原子提交。
        staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, suffix=_STAGING_SUFFIX,
                                        dir=parent))
        try:
            yield staging
        finally:
            self._cleanup_processes()
            _remove_tree(staging)

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

    def _spawn(self, cmd: list[str], *, tool: str, password: str | None = None,
               **kw: Any) -> subprocess.Popen:
        """记录 cmd 并以统一环境启动进程；tool 是 TOOLS 的 key，用于错误提示。

        password 不为 None 时经标准输入交给工具（一行），不放进命令行：命令行参数其他本地
        用户用 ps 也能看到。kw 传给 Popen，返回已登记的进程。
        """
        self._check_cancel()
        # 排除规则里的 Icon\r 含回车符，原样记录会把日志行截断，这里显示成转义形式。
        self.log("$ " + shlex.join(cmd).replace("\r", "\\r"))
        if password is not None:
            # bsdtar 和 7-Zip 在没有控制终端时才从标准输入读密码，否则会打开 /dev/tty 等待输入。
            kw["stdin"] = subprocess.PIPE
            kw["start_new_session"] = True
        # 不继承应用的标准输入：工具意外读取输入时立即读到 EOF，而不是让任务一直等待。
        kw.setdefault("stdin", subprocess.DEVNULL)
        kw.setdefault("start_new_session", self._new_session)
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
        if password is not None:
            _send_password(p, password)
        return p

    # 工具报告读取被拒时是否按隐私保护报告失败（见 _tool_failure）；只有读用户文件的压缩需要。
    _report_privacy = False

    def _pump(self, stream: BinaryIO, tool: str | None = None,
              on_chunk: Callable[[bytes], None] | None = None,
              log_lines: bool = True) -> threading.Thread:
        """异步排空二进制日志流，避免管道写满阻塞子进程；返回读取线程。

        按块而不是按行读取：bsdtar 的密码提示不换行，按行读要等到报错那一行才看得到。
        每出现新的提示就调用 _on_prompts，子类据此及早发现密码被拒。on_chunk 收到每个原始
        数据块（7-Zip 的百分比、Info-ZIP 的进度点都不换行）；log_lines 为假时只调用 on_chunk，
        不记日志（Info-ZIP 的进度点单独走 stdout）。
        """
        def run() -> None:
            pending, seen = b"", 0  # 未成行的数据，以及其开头已经报告过的提示数
            while chunk := stream.read1(65536):
                if on_chunk is not None:
                    on_chunk(chunk)
                if not log_lines:
                    continue
                pending += chunk
                *lines, pending = pending.split(b"\n")
                # 提示总在行首（前一条消息以换行结束），连续的提示连在一起；只数行首那一串，
                # 成员名里夹带的提示文字不算。在整段数据上计数，提示被切在两块之间也不会漏。
                new = 0
                if lines:
                    new += _leading_prompts(lines[0]) - seen  # 第一行就是之前未成行的数据
                    new += sum(_leading_prompts(line) for line in lines[1:])
                    seen = 0
                current = _leading_prompts(pending)
                new += current - seen
                seen = current
                if new > 0:
                    self._on_prompts(new)
                for raw in lines:
                    self._emit_line(raw, tool)
                if self._verbose is not None:
                    self._note_pending(pending)
            if pending:
                self._emit_line(pending, tool)
            stream.close()
        t = threading.Thread(target=run, daemon=True)
        self._pumps.append(t)
        t.start()
        return t

    def _emit_line(self, raw: bytes, tool: str | None = None) -> None:
        """记录 tool 输出的一行：先去掉 7-Zip 用退格覆盖的进度和密码提示再截断，bsdtar 的
        提示会粘在报错前面。按 -v 计进度时，列出的已知项只计进度、不记日志。"""
        line = _PROMPTS.sub("", _strip_backspaces(raw.decode("utf-8", "replace").rstrip()))
        if self._verbose is not None and self._consume_verbose(line):
            return
        pattern = _EPERM_LINES.get(tool) if tool is not None and self._report_privacy else None
        if pattern is not None and pattern.match(line):
            self._eperm_tools.add(tool)
        if len(line) > _LOG_LINE_MAX:
            # 例如工具反复输出同一行时，一行可达上百 KB，会拖慢日志控件。
            line = line[:_LOG_LINE_MAX] + "…"
        if line:
            self.log(line)

    def _track_verbose(self, prefix: str, sizes: dict[str, int], total: int | None,
                       unit: str = "bytes") -> None:
        """按 bsdtar -v 的输出计进度：它在开始处理一项时打印「a 名称」（创建）或「x 名称」
        （解压），处理完才换行。sizes 是已知的「名称 → 大小」（按项数计时每项为 1）。
        只认得 sizes 里的名称：恰好以「x 」开头的报错行照常记日志、照常识别。"""
        self._verbose, self._verbose_prefix = sizes, prefix
        self._verbose_done, self._current = 0, None
        self._progress.start("work", total, unit)

    def _verbose_name(self, line: str) -> str | None:
        if not line.startswith(self._verbose_prefix):
            return None
        # 加密 zip 创建时，bsdtar 的密码提示会粘在第一个名称后面。
        name = _PROMPT_TAIL.sub("", line[len(self._verbose_prefix):])
        return name if self._verbose is not None and name in self._verbose else None

    def _consume_verbose(self, line: str) -> bool:
        """完整的一行：这一项处理完了。返回是否是 -v 的列表行（不再记日志）。"""
        name = self._verbose_name(line)
        if name is None:
            return False
        self._verbose_done += self._verbose[name]
        self._current = None
        self._progress.update(self._verbose_done)
        return True

    def _percent_watcher(self, to_done: Callable[[int], int]) -> Callable[[bytes], None]:
        """返回一个数据块回调：解析 7-Zip「-bsp1」的百分比，经 to_done 换算后更新进度。
        一个百分比可能被切在两个数据块之间，所以带上前一块的结尾一起找（重复的值不影响，
        进度只增不减）。"""
        tail = [b""]

        def on_chunk(chunk: bytes) -> None:
            data = tail[0] + chunk
            if found := _SEVENZIP_PERCENT.findall(data):
                self._progress.update(to_done(min(100, int(found[-1]))))
            tail[0] = data[-64:]
        return on_chunk

    def _note_pending(self, pending: bytes) -> None:
        """还没换行的数据：名称已打印、正在处理的那一项（解压时据此轮询已写的大小）。"""
        self._current = self._verbose_name(pending.decode("utf-8", "replace"))

    def _on_prompts(self, count: int) -> None:
        """工具又打印了 count 次 bsdtar 的密码提示；在读取线程调用，默认不处理。"""

    def _run_simple(self, cmd: list[str], *, tool: str, cwd: Path,
                    stdout: BinaryIO | int | None = None, password: str | None = None,
                    on_chunk: Callable[[bytes], None] | None = None,
                    on_stdout: Callable[[bytes], None] | None = None,
                    feed: Path | None = None,
                    meter: Callable[[int], None] | None = None) -> None:
        """运行单个命令并排空日志，任何非 0 退出码都按失败处理。

        stdout 为 None 时合并 stdout/stderr 写入日志，on_chunk 另外收到其中的原始数据块；
        否则把 stdout 写入该文件，只记录 stderr，meter 不为 None 时经中转线程写入并计数。
        on_stdout 不为 None 时 stdout 单独读、只交给它（Info-ZIP 的进度点），stderr 记日志。
        feed 不为 None 时从这个文件喂工具的标准输入，meter 收到已喂的字节数。
        password 的含义见 _spawn。
        """
        stdin = {"stdin": subprocess.PIPE} if feed is not None else {}
        threads = []
        if on_stdout is not None:
            p = self._spawn(cmd, tool=tool, password=password, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **stdin)
            threads += [self._pump(p.stdout, tool, on_stdout, log_lines=False),
                        self._pump(p.stderr, tool)]
        elif stdout is None:
            p = self._spawn(cmd, tool=tool, password=password, cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **stdin)
            threads.append(self._pump(p.stdout, tool, on_chunk))
        elif meter is not None and feed is None:
            p = self._spawn(cmd, tool=tool, password=password, cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            threads += [self._relay(p.stdout, stdout, meter, close_dst=False),
                        self._pump(p.stderr, tool)]
        else:
            p = self._spawn(cmd, tool=tool, password=password, cwd=cwd, stdout=stdout,
                            stderr=subprocess.PIPE, **stdin)
            threads.append(self._pump(p.stderr, tool))
        if feed is not None:
            threads.append(self._feed(p, feed, meter))
        rc = p.wait()
        for t in threads:
            t.join()
        self._check_cancel()
        self._raise_relay_error()
        # 警告类退出码（如 7z 的 1）也可能意味着遗漏文件，不能当作成功。
        if rc:
            raise self._tool_failure(tool, rc)

    def _run_pipeline(self, first: list[str], second: list[str], *, tools: tuple[str, str],
                      cwd: Path, stdout: BinaryIO | int, blame_downstream: bool,
                      meter: Callable[[int], None] | None = None,
                      feed: Path | None = None) -> None:
        """运行 first | second，second 的输出写入 stdout；检查两端退出码。

        两端都失败时，blame_downstream 决定报告哪一端，含义见 _blame。meter 不为 None 时
        两端之间经中转线程连接，meter 收到经过的字节数（压缩时 tar 输出的未压缩字节）；
        feed 不为 None 时从这个文件喂 first 的标准输入，meter 收到已喂的字节数（解压）。
        """
        relay = meter is not None and feed is None
        p1 = self._spawn(first, tool=tools[0], cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE,
                         **({"stdin": subprocess.PIPE} if feed is not None else {}))
        threads = [self._pump(p1.stderr, tools[0])]
        if feed is not None:
            threads.append(self._feed(p1, feed, meter))
        p2 = self._spawn(second, tool=tools[1], cwd=cwd,
                         stdin=subprocess.PIPE if relay else p1.stdout, stdout=stdout,
                         stderr=subprocess.PIPE)
        if relay:
            threads.append(self._relay(p1.stdout, p2.stdin, meter, close_dst=True))
        else:
            # 父进程不持有读取端，下游退出时上游才能收到管道关闭信号。
            p1.stdout.close()
        threads.append(self._pump(p2.stderr, tools[1]))
        results = [(tools[0], p1.wait()), (tools[1], p2.wait())]
        for t in threads:
            t.join()
        self._check_cancel()
        if (failed := _blame(results, blame_downstream)):
            raise self._tool_failure(*failed)
        self._raise_relay_error()

    def _relay(self, src: BinaryIO, dst: BinaryIO, meter: Callable[[int], None], *,
               close_dst: bool) -> threading.Thread:
        """把 src（上游的 stdout）原样转给 dst（下游的 stdin 或输出文件），每块之后用累计字节
        数调用 meter。下游已退出（EPIPE）时停止并关闭读取端：上游再写管道就会因 SIGPIPE
        结束，与两端直接相连时一样，_blame 的归因不变。写文件失败（例如磁盘已满）记下，由
        调用方在进程结束后抛出。close_dst 为真时（下游进程的 stdin）结束后关闭它，下游读到 EOF。"""
        def run() -> None:
            count = 0
            try:
                while data := os.read(src.fileno(), _RELAY_CHUNK):
                    dst.write(data)
                    count += len(data)
                    meter(count)
                dst.flush()
            except (BrokenPipeError, ValueError):
                pass  # 下游已退出或被取消；原因由退出码报告
            except OSError as exc:
                self._relay_error = exc
            finally:
                src.close()
                if close_dst:
                    with contextlib.suppress(OSError, ValueError):
                        dst.close()
        t = threading.Thread(target=run, daemon=True)
        self._pumps.append(t)
        t.start()
        return t

    def _feed(self, proc: subprocess.Popen, path: Path,
              meter: Callable[[int], None] | None) -> threading.Thread:
        """把 path 的内容喂给 proc 的标准输入，每块之后用累计字节数调用 meter。工具提前退出
        （tar 读到结束标记、出错或被取消）时停止；读归档本身出错时记下，由调用方抛出。"""
        def run() -> None:
            count = 0
            try:
                with open(path, "rb") as f:
                    while data := f.read(_RELAY_CHUNK):
                        proc.stdin.write(data)
                        count += len(data)
                        if meter is not None:
                            meter(count)
            except (BrokenPipeError, ValueError):
                pass
            except OSError as exc:
                self._relay_error = exc
            finally:
                with contextlib.suppress(OSError, ValueError):
                    proc.stdin.close()
        t = threading.Thread(target=run, daemon=True)
        self._pumps.append(t)
        t.start()
        return t

    def _raise_relay_error(self) -> None:
        """中转或喂数据的线程遇到的文件系统错误（磁盘已满等）比工具的退出码更能说明原因。"""
        if (exc := self._relay_error) is not None:
            self._relay_error = None
            raise exc

    def _tool_failure(self, tool: str, code: int) -> JobError:
        """工具以 code 失败时的错误。压缩时，失败的正是报告过读取被拒（EPERM）的那个工具，
        多半是 macOS 隐私保护拦住了源里的某些文件（开始前的检查只看得到文件夹），改用能引导
        用户授权的文案；被拒的项目已在日志里。别的工具失败（例如压缩器写满磁盘）照实报告。"""
        if tool in self._eperm_tools:
            return JobError("privacy_partial", denial="privacy", tool=tool)
        return JobError("tool_failed", tool=tool, code=code)


class ArchiveJob(Job):
    """一次性归档任务；run 在工作线程执行，cancel 可由主线程调用。"""
    _report_privacy = True  # 读的是用户的文件，工具读取被拒多半是隐私保护
    def __init__(self, src: Path | str, out: Path | str, fmt: str, level: int | None = None,
                 extreme: bool = False, log: Callable[[str], None] = print, *,
                 password: str | None = None, zip_method: str = ZIP_METHODS[0],
                 encrypt_names: bool = True,
                 progress: Callable[[str, int, int | None, str], None] | None = None) -> None:
        """src/out 为输入输出路径；fmt 选择格式，level/extreme 控制压缩，log 接收日志，
        progress 接收进度（见 _Progress）。

        password 不为 None 时加密（只支持 ENCRYPTABLE 中的格式）：zip_method 是 ZIP_METHODS
        之一，encrypt_names 决定 7z 是否连文件名一起加密（zip 的文件名总是可见）。
        """
        super().__init__(log, progress)
        self._stats = SourceStats()
        self.src = Path(src).absolute()
        self.out = Path(out).absolute()
        self.fmt = FORMATS[fmt]
        self.level = level if level is not None else (self.fmt.level or (0, 0, 0))[2]
        self.extreme = extreme
        self.password = password
        self.zip_method = zip_method
        self.encrypt_names = encrypt_names

    def _execute(self) -> Path:
        """验证输出位置，创建归档并原子提交，返回产物路径；异常时清理暂存数据。"""
        self._check_cancel()
        if self.password is not None:
            if self.fmt.key not in ENCRYPTABLE:
                raise JobError("encrypt_unsupported", fmt=self.fmt.key)
            if self.zip_method not in ZIP_METHODS:
                raise ValueError(f"unknown zip method: {self.zip_method}")
            check_password(self.password, self.fmt.key)
        missing = missing_tools(self.fmt.key, encrypted=self.password is not None)
        if missing:
            raise JobError("missing_tools", fmt=self.fmt.key, tools=", ".join(missing))
        # 源可能在选择之后被删除或替换；源是链接时改用解析后的目标。
        self.src = validate_source(self.src)
        check_compress_output(self.src, self.out)
        self._progress.start("scan")
        # 加密 zip 只能按文件计进度，要记下每一项的大小。
        per_file = self.fmt.key == "zip" and self.password is not None
        self._stats = check_source_readable(self.src, self._check_cancel, name=self._name,
                                            sizes=per_file)

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
            self._progress.complete()
            self._progress.start("finish")
            if self.fmt.key == "zip" and self.password is None and sys.platform == "darwin":
                self._mark_zip_utf8(tmp)
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
        """把 tar 输出写入 tmp，或经压缩器管道写入；任一端失败都报告对应工具。

        tar 的输出经中转线程计数：进度是已打包的未压缩字节，总量按 tar 格式从源的统计估算
        （每项一个 512 字节的头，内容补齐到 512 字节）。"""
        comp = self._compressor()
        self._progress.start("work", self._stats.tar_size)
        meter = self._progress.update
        with open(tmp, "wb") as f:
            if comp:
                self._run_pipeline(self._tar_cmd(), comp, tools=("tar", self.fmt.tools[-1]),
                                   cwd=self.src.parent, stdout=f, blame_downstream=True,
                                   meter=meter)
            else:
                self._run_simple(self._tar_cmd(), tool="tar", cwd=self.src.parent, stdout=f,
                                 meter=meter)

    def _run_zip(self, tmp: Path) -> None:
        """生成 zip 递归排除参数并写入 tmp，保留符号链接本身。

        进度：-q -dg -ds 1m 让 Info-ZIP 只在 stdout 上每读 1 MB 输入打一个点（不再逐个列出
        加入的文件），警告仍写在 stderr，读取被拒的识别不受影响。"""
        if self.password is not None:
            self._run_zip_encrypted(tmp)
            return
        excludes = []
        for p in JUNK:  # zip 的 * 能跨越 /，四种写法覆盖任意层级
            excludes += [p, f"*/{p}", f"{p}/*", f"*/{p}/*"]
        # -X 去掉额外属性，-y 存储链接而不沿链接遍历源目录之外的文件。
        cmd = [require_tool("zip", self.fmt.key), "-r", "-X", "-y", f"-{self.level}",
               "-q", "-dg", "-ds", "1m", str(tmp), self._name, "-x", *excludes]
        self._progress.start("work", self._stats.total)
        dots = [0]

        def on_dots(chunk: bytes) -> None:
            dots[0] += chunk.count(b".")
            self._progress.update(dots[0] * _ZIP_DOT)
        self._run_simple(cmd, tool="zip", cwd=self.src.parent, on_stdout=on_dots)

    def _run_zip_encrypted(self, tmp: Path) -> None:
        """用 bsdtar 写加密 zip。Info-ZIP 的 zip 只能从终端（-e）或命令行（-P）拿密码，
        而且只有 ZipCrypto；bsdtar 支持 AES-256，并在没有终端时从标准输入读密码。

        hdrcharset=UTF-8 让 bsdtar 为非 ASCII 文件名设置 UTF-8 标志：从访达启动的应用没有
        区域设置，bsdtar 在 C 区域下默认不设。排除规则与 tar 格式相同，符号链接按链接本身存储。
        """
        tar = require_tool("tar", self.fmt.key)
        if not _is_bsdtar(tar):  # GNU tar 写不了 zip
            raise JobError("missing_tools", fmt=self.fmt.key, tools="tar")
        # 级别 0 存储不压缩；zip:compression-level 没写进 man 页，但 libarchive 3.7 支持。
        level = "zip:compression=store" if self.level == 0 else \
            f"zip:compression-level={self.level}"
        # -v：bsdtar 开始处理每一项时打印「a 名称」，按文件大小计进度（只能做到按文件）。
        cmd = [tar, "-c", "-v", "-f", str(tmp), "--format", "zip",
               "--options", f"zip:encryption={self.zip_method},{level},zip:hdrcharset=UTF-8",
               "--no-mac-metadata", "--no-xattrs"]
        if self._name.startswith("./"):
            # 为避开选项解析加的 ./ 不应留在成员名里（Info-ZIP 和 7-Zip 都会去掉）；
            # H、S 表示不改硬链接和符号链接的目标。
            cmd += ["-s", r",^\./,,HS"]
        for p in JUNK:
            cmd += ["--exclude", p]
        self._track_verbose("a ", self._stats.sizes or {}, self._stats.total)
        try:
            self._run_simple(cmd + [self._name], tool="tar", cwd=self.src.parent,
                             password=self.password)
        finally:
            self._verbose = None

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
        # -snl 保存符号链接，-mmt=on 启用多线程，-bsp1 输出百分比（用退格覆盖，记日志前去掉）。
        cmd = [require_tool("7z", self.fmt.key), "a", "-t7z", f"-mx={self.level}", "-mmt=on",
               "-snl", "-bb1", "-bsp1", "-y", str(tmp), self._name]
        # -xr! 对任意层级的文件名递归匹配，避免只排除顶层垃圾。
        cmd += [f"-xr!{p}" for p in JUNK]
        if self.password is not None:
            # 不带值的 -p 让 7-Zip 从标准输入读密码（AES-256）；-mhe 决定是否加密文件名。
            cmd[2:2] = ["-p", f"-mhe={'on' if self.encrypt_names else 'off'}"]
        # 退出码 1 可能表示部分文件未读到，_run_simple 会把它当作失败，不提交不完整的归档。
        # 7-Zip 的百分比按源的总字节换算，界面才能显示速度。
        total = self._stats.total
        self._progress.start("work", total)
        self._run_simple(cmd, tool="7z", cwd=self.src.parent, password=self.password,
                         on_chunk=self._percent_watcher(lambda pct: pct * total // 100))


# ---------- 解压：提交与隔离属性 ----------
_RENAME_EXCL = 0x00000004  # <sys/stdio.h>：目标已存在时以 EEXIST 失败


def _load_renamex_np() -> Callable[[bytes, bytes, int], int] | None:
    """返回 libc 的 renamex_np；不是 macOS 或找不到时返回 None。"""
    try:
        func = ctypes.CDLL(None, use_errno=True).renamex_np
    except (OSError, AttributeError):
        return None
    func.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    func.restype = ctypes.c_int
    return func


_renamex_np = _load_renamex_np()


def rename_noreplace(src: Path, dst: Path) -> None:
    """把 src 改名为 dst，绝不覆盖已有内容；dst 已存在时抛出 FileExistsError。

    os.rename 会静默替换空文件夹，在不区分大小写的卷上连只差大小写的名称也会替换。
    renamex_np 的 RENAME_EXCL 由内核原子判断，大小写和 Unicode 规范化变体都算已存在。
    不支持它的文件系统（exFAT、部分网络卷）退回到先独占创建占位、再替换自己占位的做法。
    """
    if _renamex_np is not None:
        if _renamex_np(os.fsencode(src), os.fsencode(dst), _RENAME_EXCL) == 0:
            return
        err = ctypes.get_errno()
        if err not in (errno.ENOTSUP, errno.EINVAL):
            raise OSError(err, os.strerror(err), str(src), None, str(dst))
    is_dir = src.is_dir() and not src.is_symlink()
    if is_dir:
        os.mkdir(dst, 0o700)  # 已存在时抛 FileExistsError
    else:
        os.close(os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    try:
        # 目标是刚创建的空占位：目录改名到空目录、文件替换文件都是原子的；
        # 若有人往占位目录里放了东西，rename 会以 ENOTEMPTY 失败而不是覆盖。
        os.rename(src, dst)
    except OSError:
        with contextlib.suppress(OSError):
            os.rmdir(dst) if is_dir else os.unlink(dst)
        raise


_QUARANTINE = "com.apple.quarantine"
_XATTR = "/usr/bin/xattr"
_XATTR_NOFOLLOW = 0x0001  # <sys/xattr.h>：作用于链接本身
_PROBE_NAME = ".cleanpack-xattr-probe"
_PROBE_ATTR = b"io.github.linusauditore.cleanpack.probe"


def _load_setxattr() -> Callable[..., int] | None:
    """返回 libc 的 setxattr；不是 macOS 或找不到时返回 None。"""
    try:
        func = ctypes.CDLL(None, use_errno=True).setxattr
    except (OSError, AttributeError):
        return None
    func.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t,
                     ctypes.c_uint32, ctypes.c_int]
    func.restype = ctypes.c_int
    return func


_setxattr = _load_setxattr()


def uses_sidecar_xattrs(directory: Path) -> bool:
    """判断 directory 所在的卷是否只能用「._名称」文件保存扩展属性（exFAT、FAT 等）。

    在 directory 里建一个探测文件并写一个属性：旁边出现 ._ 文件就说明是。不能看暂存目录里
    有没有 ._x 来判断：系统只在进程带 com.apple.provenance 属性时才给新建的项写属性，带不带
    取决于应用的启动方式（GitHub 的运行器上就不带）。写属性失败时返回 False，后面写隔离属性
    的步骤会自己报告问题。探测文件和它的 ._ 文件用完即删。
    """
    probe = directory / _PROBE_NAME
    sidecar = directory / ("._" + _PROBE_NAME)
    os.close(os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    try:
        if _setxattr is None or _setxattr(os.fsencode(probe), _PROBE_ATTR, b"1", 1, 0,
                                          _XATTR_NOFOLLOW) != 0:
            return False
        return os.path.lexists(sidecar)
    finally:
        for path in (probe, sidecar):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)


def read_quarantine(path: Path) -> str | None:
    """返回文件的隔离属性值（从浏览器等处下载的文件才有）；没有或读取失败时返回 None。"""
    try:
        r = subprocess.run([_XATTR, "-p", _QUARANTINE, str(path)], capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, env=ENV, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    value = r.stdout.strip()
    return value if r.returncode == 0 and value else None


def _run_xattr(cmd: list[str]) -> tuple[int, str]:
    """独立运行 xattr，返回 (退出码, 合并后的输出)；任务内改用可取消的 Job._run_capture。"""
    r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=ENV)
    return r.returncode, r.stderr or r.stdout


def apply_quarantine(root: Path, value: str,
                     run: Callable[[list[str]], tuple[int, str]] = _run_xattr,
                     check: Callable[[], None] = _no_check) -> JobError | None:
    """把隔离属性递归写到 root 下的每一项，返回失败时的警告；不跟随符号链接。

    让 Gatekeeper 像对待下载的归档一样检查解出的程序。-s 作用于链接本身，否则归档里
    指向外部的链接会让属性写到解压目录之外。写属性需要文件可写，只读文件临时补上 u+w。
    run 执行 xattr 命令，check 对每个目录调用一次，二者都用于让取消及时生效。
    """
    restore: list[tuple[str, int]] = []
    try:
        for dirpath, _, filenames in _walk_dirs_first(root):
            check()
            for name in filenames:
                path = os.path.join(dirpath, name)
                st = os.lstat(path)
                if stat.S_ISREG(st.st_mode) and not st.st_mode & stat.S_IWUSR:
                    mode = stat.S_IMODE(st.st_mode)
                    os.chmod(path, mode | stat.S_IWUSR)
                    restore.append((path, mode))
        try:
            code, output = run([_XATTR, "-w", "-r", "-s", _QUARANTINE, value, str(root)])
            failure = (output.strip().splitlines() or [f"exit {code}"])[0] if code else None
        except OSError as exc:
            failure = exc.strerror or str(exc)
        except JobError as exc:
            # 任务内经 Job._spawn 启动时，启动失败表现为 tool_start_failed；隔离属性只是附加
            # 步骤，失败只提示，不让整个解压失败。取消（Cancelled）不在此列，照常传出。
            if exc.key != "tool_start_failed":
                raise
            failure = str(exc.kw.get("err", ""))
    finally:
        for path, mode in restore:
            os.chmod(path, mode)
    return JobError("quarantine_failed", err=failure) if failure else None


# ---------- 解压任务 ----------
# 解压时 bsdtar 的公共选项：不恢复属主、权限位（按 umask）、扩展属性、ACL、文件标志和
# 资源分叉，避免归档把这些元数据强加到用户的文件上。
_BSDTAR_EXTRACT = ["--no-same-owner", "--no-same-permissions", "--no-xattrs", "--no-acls",
                   "--no-fflags", "--no-mac-metadata"]
# 没有密码时给 bsdtar 一个假密码：加密的 zip 立即以“密码错误”失败，不会反复提示上万次。
# 有密码时不加这一项，bsdtar 改从标准输入读真正的密码。
_NO_PASSWORD = ["--passphrase", "cleanpack-no-password"]
# 工具报告“归档已加密”的原话。成员名会出现在同一行里，所以按各工具的行格式锚定：
# bsdtar 是「成员: 消息[: Unknown error: -1]」，消息在行尾，其他错误行的末尾总是 bsdtar 自己
# 加的系统错误说明；7-Zip 的消息在行首，成员名在「 : 」之后。另外只对可能加密的归档采信
# （ExtractJob._encryption_possible），tar 系列里伪造的提示一律忽略。
_BSDTAR_ENCRYPTED = re.compile(
    r"(^|: )(Incorrect passphrase|Too many incorrect passphrases|Passphrase required for this entry"
    r"|The (archive header|file content) is encrypted, but currently not supported"
    r"|Encrypted file is unsupported)(: Unknown error: -1)?$")
_SEVENZIP_ENCRYPTED = re.compile(
    r"^(ERROR: )?((Data Error|CRC Failed) in encrypted file\. Wrong password\? : "
    r"|Wrong password : "
    r"|Cannot open encrypted archive\. Wrong password\?$)")
# bsdtar 解出的数据不对：错误密码偶然通过 ZipCrypto 的单字节校验，或者数据本身损坏。
_BSDTAR_DATA_ERROR = re.compile(
    r": (ZIP bad CRC: 0x[0-9a-f]+ should be 0x[0-9a-f]+|ZIP decompression failed \(-?\d+\)"
    r"|Truncated ZIP file data)(: Unknown error: -1)?$")
# bsdtar 解不了的 zip 成员：AES 加密的符号链接（7-Zip 用 -snl 生成的加密 zip 里会有），
# 以及 Deflate64 等它不支持的压缩方法。这不是密码错误，改用 7-Zip 重试。成员名即使伪造出
# 这句话，后果也只是改用 7-Zip，安全检查照常进行。
_BSDTAR_ZIP_UNSUPPORTED = re.compile(
    r"(^tar: Unsupported ZIP compression method during decompression of link entry "
    r"\(\d+: [a-z0-9-]+\)"
    r"|: Unsupported ZIP compression method \(\d+: [a-z0-9-]+\): "
    r"Inappropriate file type or format)$")


def _zip_member_sizes(archive: Path) -> dict[str, int] | None:
    """「成员名 → 解压后大小」，用于按 bsdtar -v 的输出计进度；名称与 bsdtar 打印的一致
    （文件夹带结尾的 /）。工具排除的垃圾项不计入。中央目录读不了时返回 None。"""
    try:
        with open(archive, "rb") as f:
            entries = _read_central_directory(f)
    except (OSError, JobError):
        return None
    sizes: dict[str, int] = {}
    for e in entries:
        name = e.name.decode("utf-8", "replace")
        if any(is_junk_name(part) for part in name.rstrip("/").split("/")):
            continue
        sizes[name] = e.usize
    return sizes


def _mentions_encryption(line: str, tool: str) -> bool:
    """判断 tool（"tar" 或 "7z"）输出的这一行是否在报告归档已加密。"""
    if tool == "7z":
        return bool(_SEVENZIP_ENCRYPTED.search(line))
    return bool(_BSDTAR_ENCRYPTED.search(line))


class ExtractJob(Job):
    """一次性解压任务：解到输出目录下的独占暂存目录，清理垃圾、检查安全后智能放置。

    run 返回最终放置的路径；warnings 收集指向外部的链接等提示。skipped 是最终清理删掉的
    垃圾项数，不含工具排除规则已经挡掉的部分，只用于测试和排查。
    """
    _new_session = True

    def __init__(self, archive: Path | str, dest: Path | str,
                 log: Callable[[str], None] = print, *, password: str | None = None,
                 progress: Callable[[str, int, int | None, str], None] | None = None) -> None:
        """archive 为归档路径，dest 为放置结果的文件夹，log 接收命令行和工具输出，progress
        接收进度（见 _Progress）。

        password 为 None 时遇到加密归档报 archive_encrypted，界面据此询问密码后带密码重试；
        密码不正确时报 archive_wrong_password，无法区分密码错误和数据损坏时报
        archive_password_or_damaged。加密的 7z 需要 7-Zip，没装时报 archive_encrypted_needs_7z。
        """
        self._user_log = log
        self.password = password
        self._encrypted = False
        self._family = ""
        self._encryption_possible = False  # 这个归档可能加密，工具的加密提示才可信
        self._zip_encrypted = False  # 中央目录显示有加密成员
        self._zip_needs_7z = False  # 有 bsdtar 解不了的加密成员（链接或少见的压缩方法）
        self._zip_weak = False  # 预检无法排除错误密码（有传统加密的成员或判断不了）
        self._zip_strong = False  # 预检已确认密码（全是 AES 成员，校验值相符）
        self._data_error = False  # bsdtar 报告解出的数据校验失败
        self._decoder = "tar"  # 当前产生输出的解压工具，决定按哪种格式识别加密提示
        self._damaged = False
        self._zip_unsupported = False  # bsdtar 报告有它解不了、7-Zip 能解的 zip 成员
        self._prompts = 0  # bsdtar 向标准输入要密码的次数
        self.archive = Path(archive).absolute()
        self.dest = Path(dest).absolute()
        super().__init__(self._log_line, progress)
        self.warnings: list[JobError] = []
        self.skipped = 0

    def _log_line(self, line: str) -> None:
        """转发日志，同时留意工具报告的加密提示，失败时给出更明确的原因。"""
        if (self._encryption_possible and not line.startswith("$ ")
                and _mentions_encryption(line, self._decoder)):
            self._encrypted = True
        # bsdtar 跳过损坏的 tar 头时只打印这一行并继续，退出码仍可能是 0，跳过的项会悄悄丢失。
        if self._decoder == "tar" and line == "tar: Damaged tar archive":
            self._damaged = True
        if self._decoder == "tar" and _BSDTAR_ZIP_UNSUPPORTED.search(line):
            self._zip_unsupported = True
        if self._decoder == "tar" and _BSDTAR_DATA_ERROR.search(line):
            self._data_error = True
        self._user_log(line)

    def _on_prompts(self, count: int) -> None:
        """标准输入里只有一行密码，bsdtar 第二次要密码说明它拒绝了这个密码。它接下来会对每个
        成员重试上万次（AES 每个成员要几秒），所以立即结束它，按密码错误报告。"""
        self._prompts += count
        # 预检已确认密码时不必提防：第二次提示只可能来自伪造的成员名，不能据此误报密码错误。
        if (self.password is not None and self._decoder == "tar" and not self._zip_strong
                and self._prompts > 1):
            self._encrypted = True
            self._kill_processes()

    def _execute(self) -> Path:
        """校验、解压、整理并提交，返回放置后的路径；失败或取消时暂存目录整个删除。"""
        self._check_cancel()
        raise_fd_limit()
        self._progress.start("prepare")
        if self.password is not None:
            check_password(self.password, None)
        self.archive = validate_archive(self.archive)
        try:
            # 第一次实际读取归档内容：被隐私保护拦住时在这里暴露（stat 和 os.access 都看不出）。
            family = extract_family(self.archive)
        except PermissionError as exc:
            raise _denied("src_denied", self.archive, exc.errno, write=False) from None
        fmt = _FORMAT_OF_FAMILY[family]
        missing = [t for t in extract_tools(family) if locate(t) is None]
        if missing:
            raise JobError("missing_tools", fmt=fmt, tools=", ".join(missing))
        check_output_dir(self.dest)
        # 开头有额外数据（例如自解压程序）的 zip 没有魔数，按扩展名路由到 bsdtar 后它仍会当作
        # zip 读取；所以扩展名决定路线时也要检查，避免假密码被偶然接受。
        self._family = family
        if family == "zip" or sniff_family(self.archive) is None:
            info = zip_encryption(self.archive, self._check_cancel)
            self._zip_encrypted, self._zip_needs_7z = info or (False, False)
            # 中央目录读不了（例如下载不完整）时无法预先判断，只能相信 bsdtar 的提示。
            self._encryption_possible = info is None or self._zip_encrypted
        else:
            self._encryption_possible = family == "7z"
        if self._zip_encrypted:
            if self._zip_needs_7z:
                # 只有 7-Zip 能解，没装时先说明，不要让用户白输密码。
                self._require_7z("archive_zip_needs_7z")
            if self.password is None:
                raise JobError("archive_encrypted")
            verdict = check_zip_password(self.archive, self.password, self._check_cancel)
            if verdict == "wrong":
                raise JobError("archive_wrong_password")
            self._zip_weak = verdict == "weak"
            self._zip_strong = verdict == "strong"
        quarantine = read_quarantine(self.archive)

        with self._staging(self.dest) as staging:
            root = staging / "x"
            root.mkdir()
            self._extract(family, root)
            self._check_cancel()
            self._progress.complete()
            self._progress.start("finish")
            make_tree_accessible(root, self._check_cancel)
            self.skipped = sweep_junk(root, self._check_cancel)
            # exFAT、FAT 等不支持扩展属性的卷上，系统用「._名称」文件保存属性（例如
            # com.apple.provenance），暂存目录里会多出 ._x。它不是越界写入的内容。
            sidecar = "._x"
            if set(os.listdir(staging)) - {"x", sidecar}:
                raise JobError("unsafe_path")  # 有内容写到了解压根目录之外
            src, name, is_dir = plan_layout(root, self.archive.name)
            # 链接是否越界以最终放进目标文件夹的那一项为界。
            self.warnings += verify_tree(root, src if is_dir else root, self._check_cancel)
            if quarantine:
                self._copy_quarantine(root, quarantine, staging)
            if src == root:
                # 整个 x 改名放进目标时，exFAT 驱动会把 ._x 一起带过去，变成用户文件夹里的
                # ._名称；提交前删掉它。
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(staging / sidecar)
            self._check_cancel()
            return self._commit(src, name, is_dir)

    def _copy_quarantine(self, root: Path, value: str, staging: Path) -> None:
        """把归档的隔离属性写到解出的内容上；卷只能用 ._ 文件保存属性时跳过并提示，
        否则每个文件旁边都会多出一个 AppleDouble 文件，正是本应用要避免的垃圾。
        staging 是暂存目录，与目标在同一个卷上，用来探测卷的行为。"""
        if uses_sidecar_xattrs(staging):
            self.warnings.append(JobError("quarantine_skipped"))
        elif problem := apply_quarantine(root, value, self._run_capture, self._check_cancel):
            self.warnings.append(problem)

    def _run_capture(self, cmd: list[str]) -> tuple[int, str]:
        """以登记的进程运行 cmd（取消时会被结束），返回 (退出码, 合并后的输出)。"""
        p = self._spawn(cmd, tool=os.path.basename(cmd[0]), stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT)
        output = p.communicate()[0].decode("utf-8", "replace")
        self._check_cancel()
        return p.returncode, output

    def _extract(self, family: str, root: Path) -> None:
        """按类别运行解压命令并检查结果；失败的工具会留下补零的残缺文件，所以任何非 0 退出码
        都算失败，bsdtar 报告跳过了损坏的头部时也算失败。"""
        self._run_decoder(family, root)
        if self._damaged:
            raise JobError("archive_damaged")

    def _run_decoder(self, family: str, root: Path) -> None:
        """运行解压命令：tar.zst 走管道，其余先用 bsdtar，7z 解不了时改用 7-Zip。"""
        tar = require_tool("tar", _FORMAT_OF_FAMILY[family])
        excludes = [arg for p in JUNK for arg in ("--exclude", p)]
        size = os.path.getsize(self.archive)
        if family == "zstd":
            # 本机 bsdtar 没有 libzstd，它会自己去 PATH 找 zstd；显式管道才能分别检查两端退出码。
            # tar 读到结束标记就会退出，zstd 可能读不到流末尾的校验和；先完整校验一遍，
            # 损坏的数据不会被当成成功解出（zstd 解码很快，这一遍的代价不大）。
            # 两遍都由这里喂 zstd 的标准输入并计数，进度总量是两倍的归档大小。
            zstd = require_tool("zstd", "tar.zst")
            self._progress.start("work", 2 * size)
            self._run_simple([zstd, "-t", "-q", "-"], tool="zstd", cwd=root, feed=self.archive,
                             meter=self._progress.update)
            self._run_pipeline([zstd, "-d", "-c", "-q", "-"],
                               [tar, "-x", "-f", "-", *_BSDTAR_EXTRACT, *_NO_PASSWORD, *excludes],
                               tools=("zstd", "tar"), cwd=root, stdout=subprocess.DEVNULL,
                               blame_downstream=False, feed=self.archive,
                               meter=lambda n: self._progress.update(size + n))
            return
        if family in ("tar", "gzip", "xz") and self.password is None:
            # 流式格式：由这里从标准输入喂归档并计数，进度是已读的归档字节（与从文件读相比，
            # 报错原文相同，2026-11-09 实测）。
            self._progress.start("work", size)
            self._run_simple([tar, "-x", "-f", "-", *_BSDTAR_EXTRACT, *_NO_PASSWORD, *excludes],
                             tool="tar", cwd=root, stdout=subprocess.DEVNULL, feed=self.archive,
                             meter=self._progress.update)
            return
        # 名为 .7z、实为带前置数据的 zip 时，中央目录已经说明它是加密 zip，按 zip 处理。
        if family == "7z" and self.password is not None and not self._zip_encrypted:
            # bsdtar 解不了 AES 加密的 7z，有密码时直接交给 7-Zip。
            self._run_7z_fallback(self._require_7z("archive_encrypted_needs_7z"), root)
            return
        if self._zip_needs_7z:
            self._run_7z_fallback(self._require_7z("archive_zip_needs_7z"), root)
            return
        try:
            verbose = self._watch_members(family, tar, root)
            try:
                with self._polling(root):
                    self._run_simple([tar, "-x", *(["-v"] if verbose else []), "-f",
                                      str(self.archive), *_BSDTAR_EXTRACT,
                                      *(_NO_PASSWORD if self.password is None else []),
                                      *excludes],
                                     tool="tar", cwd=root, stdout=subprocess.DEVNULL,
                                     password=self.password)
            finally:
                self._verbose = None
            return
        except JobError as exc:
            if self._encrypted:
                raise self._password_error() from exc
            if self._zip_unsupported:
                # 密码没问题，只是 libarchive 不支持其中的压缩方法或加密的链接成员；7-Zip 支持。
                seven = self._require_7z("archive_zip_needs_7z")
            elif self._zip_weak and self._data_error:
                # bsdtar 报数据校验失败。可能是错误密码偶然通过了 ZipCrypto 的单字节校验，
                # 也可能是 bsdtar 自己误判：Info-ZIP 流式写出的 zip 在本地头的 CRC 字段里放的是
                # 它的校验值，libarchive 会当成真实 CRC。7-Zip 自己校验数据，密码错误时会明确
                # 报告，所以装了 7-Zip 就交给它；没装时分不清原因，只能如实说“密码或数据有问题”。
                # 其他失败（例如磁盘已满、成员路径含 ..）与密码无关，照实报告，免得界面反复要密码。
                seven = locate("7z")
                if seven is None:
                    raise JobError("archive_password_or_damaged") from exc
            elif family == "7z":
                # 系统 tar 读不了这个 7z 的编码方式，装上 7-Zip 就能解开，要明确告诉用户。
                seven = self._require_7z("archive_needs_7z")
            else:
                raise
            self.log("→ 7z")
        self._run_7z_fallback(seven, root)

    def _watch_members(self, family: str, tar: str, root: Path) -> bool:
        """准备按 bsdtar -v 计进度（zip、7z 不能从标准输入读，只能这样），返回是否加 -v。

        zip 按中央目录里每个成员解压后的大小计；7z 先列一次成员（目录在文件尾，很快），按项数
        计。有密码时不加 -v：bsdtar 的密码提示会粘在「x 名称」后面，「第二次要密码就结束它」
        的防护按行首数提示（设计决策 25），不能冒这个险，这时进度不确定。"""
        if self.password is None:
            sizes = _zip_member_sizes(self.archive) if family != "7z" else None
            if sizes:
                self._track_verbose("x ", sizes, sum(sizes.values()) or None)
                return True
            if family == "7z":
                excludes = [arg for p in JUNK for arg in ("--exclude", p)]
                rc, out = self._run_capture([tar, "-t", "-f", str(self.archive), *_NO_PASSWORD,
                                             *excludes])
                names = [line for line in out.splitlines() if line]
                if rc == 0 and names:
                    self._track_verbose("x ", dict.fromkeys(names, 1), len(names), "items")
                    return True
        self._progress.start("work")
        return False

    @contextlib.contextmanager
    def _polling(self, root: Path) -> Iterator[None]:
        """按 -v 的字节计进度时，定时查看正在写的那一项已写了多少：bsdtar 写完一项才换行，
        解一个大文件期间进度要靠这个推进。"""
        if self._verbose is None or self._progress.unit != "bytes":
            yield
            return
        stop = threading.Event()

        def run() -> None:
            while not stop.wait(0.2):
                name, sizes = self._current, self._verbose
                if name is None or sizes is None:
                    continue
                try:
                    written = os.lstat(root / name).st_size
                except OSError:
                    continue
                self._progress.update(self._verbose_done + min(written, sizes.get(name, 0)))
        t = threading.Thread(target=run, daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join()

    def _require_7z(self, key: str) -> str:
        """返回 7-Zip 的路径；没有安装时以 key 报告为什么需要它。"""
        seven = locate("7z")
        if seven is None:
            raise JobError(key)
        return seven

    def _password_error(self) -> JobError:
        """工具报告了加密提示时的错误：没给密码就是需要密码，给了就是密码不正确。
        加密的 7z 只有 7-Zip 能解，没装时先说明这一点，不要让用户白输一次密码。"""
        if self._family == "7z" and not self._zip_encrypted and locate("7z") is None:
            return JobError("archive_encrypted_needs_7z")
        return JobError("archive_encrypted" if self.password is None
                        else "archive_wrong_password")

    def _run_7z_fallback(self, seven: str, root: Path) -> None:
        """用 7-Zip 解压 bsdtar 解不了的归档：少见编码组合或 AES 加密的 7z，以及含加密链接成员
        或 Deflate64 等压缩方法的 zip。在清空的解压根目录里重来。

        -snld 只保留解析后仍在输出目录内的相对链接，越界的相对链接会被拒绝并以 2 退出；
        -spd 关闭文件名通配，否则名为 a*.7z 的归档会连同旁边匹配的归档一起解开。
        没有密码时用不带值的 -p 给出空密码，加密归档立即失败而不是等待输入；有密码时省略 -p，
        7-Zip 需要密码时从标准输入读取。
        """
        make_tree_accessible(root, self._check_cancel)
        shutil.rmtree(root)
        root.mkdir()
        self._decoder = "7z"
        # 解的是 zip 时指定 -tzip：7-Zip 按扩展名选格式，名为 .zip、开头带前置数据的归档
        # 不指定就打不开（名为 .7z 的反而会按内容找到 zip）。
        is_zip = self._family == "zip" or self._zip_encrypted or self._zip_unsupported
        # -bsp1：7-Zip 自己的百分比（用退格覆盖，记日志前去掉），按百分比计进度。
        self._progress.start("work", 100, "percent")
        cmd = [seven, "x", "-y", "-bsp1", "-bb1", "-snld", "-spd",
               *(["-tzip"] if is_zip else []),
               *(["-p"] if self.password is None else []), f"-o{root}",
               *[f"-xr!{p}" for p in JUNK], str(self.archive)]
        try:
            self._run_simple(cmd, tool="7z", cwd=root, password=self.password,
                             on_chunk=self._percent_watcher(lambda pct: pct))
        except JobError as exc:
            if self._encrypted and self._zip_strong and self.password is not None \
                    and len(self.password.encode("utf-8")) > PASSWORD_MAX:
                # 预检已确认密码，7-Zip 却拒绝：它解 WinZip AES 时不接受超过 99 字节的密码。
                raise JobError("archive_password_too_long_7z", max=PASSWORD_MAX) from exc
            if self._encrypted:
                raise self._password_error() from exc
            raise
        _restore_rerooted_links(root)

    def _commit(self, src: Path, name: str, is_dir: bool) -> Path:
        """按「名称」「名称 2」……依次尝试，用不覆盖的改名把结果放进目标文件夹。"""
        for candidate in itertools.islice(unique_names(self.dest, name, is_dir), 10000):
            try:
                rename_noreplace(src, candidate)
                return candidate
            except FileExistsError:
                continue
        raise JobError("io_error", err=os.strerror(errno.EEXIST))


def _restore_rerooted_links(root: Path) -> None:
    """7-Zip 解压时把绝对路径的链接目标改写到输出目录之下（/etc/hosts 变成 <root>/etc/hosts），
    提交后它们会悬空。按用户的决定，链接应原样保留并警告，这里去掉前缀恢复原目标。"""
    prefixes = {str(root), os.path.realpath(root)}
    for dirpath, dirnames, filenames in _walk_dirs_first(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                continue
            target = os.readlink(path)
            for prefix in prefixes:
                if target == prefix or target.startswith(prefix + "/"):
                    os.unlink(path)
                    os.symlink(target[len(prefix):] or "/", path)
                    break
