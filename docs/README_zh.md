# CleanPack

[English](../README.md) | 简体中文

**在 macOS 上创建和解压干净的归档，自动排除 `.DS_Store`、`._*`、`__MACOSX` 等系统文件。**

CleanPack 是一个用 Python 和 PySide6 编写的桌面压缩工具。选择或拖入一个文件或文件夹，选好归档格式和压缩级别，就能得到一个可以直接分享的归档，不用手写排除命令。在解压模式下，它可以解开别人发来的归档，并把其中的 macOS 垃圾文件留在外面。本项目在 OpenAI Codex 的辅助下开发。

## 功能

- 支持 **tar、tar.gz、tar.xz、tar.zst、zip 和 7z**，并在每一层目录递归排除 macOS 垃圾文件。
- 提供压缩级别滑块、xz 极限模式，以及 xz、zstd 和 7z 的多线程压缩。
- 支持选择文件和文件夹、拖放单个项目、另存为、取消任务，以及在访达中显示输出。路径也可以直接键入或粘贴，输入时即时校验。
- 同样六种格式的**解压模式**：跳过垃圾文件；只有一个顶层文件或文件夹时直接放进目标文件夹；绝不覆盖已有内容（重名时像访达一样加上编号）。
- zip（AES-256，或为兼容性选择 ZipCrypto）和 7z（AES-256，默认同时加密文件名）支持**密码保护**。解压加密归档时会询问密码。
- 检测压缩工具，所选格式缺少工具时给出提示；启动时，如果还有工具需要用 Homebrew 安装而 Homebrew 不存在，也会指出来。工具链窗口（标题栏按钮或「工具 ▸ 工具链…」）显示工具路径和 Homebrew 命令，提供一键安装和实时日志；关闭窗口后安装会在后台继续。
- 提供英文、简体中文、繁体中文和法文界面，并适配系统的浅色和深色外观。
- 成功时以原子方式替换输出。失败或取消时清理临时文件，并保留已有的输出。还会防止覆盖源文件，或把输出放进源文件夹内。

## 支持的格式

| 格式 | 压缩级别 | 默认级别 | 所需命令 |
| --- | --- | --- | --- |
| `tar` | 无，只打包 | — | 系统 `tar` |
| `tar.gz` | 1–9 | 6 | `tar`、`gzip` |
| `tar.xz` | 0–9，可选极限模式 | 6 | `tar`、`xz` |
| `tar.zst` | 1–22 | 3 | `tar`、`zstd` |
| `zip` | 0–9；0 表示仅存储、不压缩 | 6 | `zip`；加密 zip 使用系统 `tar` |
| `7z` | 0–9；0 表示仅存储、不压缩 | 5 | `7zz`，找不到时退回 `7z` |

zstd 的 20–22 级会启用 `--ultra`，内存占用明显更高。切换格式时，压缩级别会恢复为该格式的默认值，并关闭 xz 极限模式。

除 `tar.zst` 还需要 `zstd` 外，所有格式都用系统 `tar` 解压。以下情况需要 7-Zip：加密的 7z 归档、使用了系统 `tar` 无法读取的压缩方法的 7z 归档，以及少数 zip 归档（例如 Deflate64，或用 LZMA、BZip2 压缩的加密项目）；遇到时 CleanPack 会说明。

## 从源码运行

需要 macOS、Python 3.10 或更高版本，以及 PySide6。macOS 自带的工具支持 `tar`、`tar.gz` 和 `zip`；其余格式需要额外的命令行工具。

在项目目录中用 uv 启动：

```bash
uv run --with PySide6 main.py
```

也可以使用 Python 虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

要使用另外三种格式，请用 Homebrew 安装所需工具，或使用应用工具链窗口里的安装按钮：

```bash
brew install xz zstd sevenzip
```

如果没有安装 Homebrew，应用会给出安装说明，但不会自动安装 Homebrew。

## 使用方法

1. 选择一个文件或文件夹，或把它拖进窗口。
2. 选择归档格式，并按需调整压缩级别和输出位置。
3. 点按「压缩」。如果输出文件已经存在，应用会先询问是否替换。
4. 压缩完成后，点按「在访达中显示」找到归档。

应用每次处理一个输入项目。要把多个文件打包在一起，请先把它们放进一个文件夹。界面会显示任务正在运行，但不显示进度百分比。

### 加密

压缩前勾选「使用密码加密」，并输入两次密码。此选项适用于 zip 和 7z；tar 系列格式没有标准的加密方式。

- **zip**：默认使用 AES-256。ZipCrypto 能被更多应用打开，但很容易被破解。zip 加密只保护文件内容：文件名和符号链接的目标仍然可见。zip 密码只能使用英文字母、数字、空格和 ASCII 符号，以便其他应用能打开归档。
- **7z**：AES-256。除非取消勾选「加密文件名」，否则文件名也会被加密。

密码通过标准输入交给压缩工具，从不放在命令行上，也不会被保存。压缩成功后，密码框会被清空。

### 解压

1. 切换到「解压」，然后选择、拖入或键入一个归档的路径。
2. 目标文件夹默认是归档所在的文件夹；需要时可以另选。
3. 点按「解压」。如果归档已加密，CleanPack 会询问密码，密码错误时会再次询问。

只有一个顶层文件或文件夹时，它会直接放进目标文件夹；否则所有内容会放进一个以归档命名的文件夹。指向解压内容之外的符号链接会被保留，并在日志中列出。如果归档是从互联网下载的，解压出的项目会保留它的“已下载”标记（exFAT 等卷除外，日志里会说明）。

## 构建 macOS 应用

仓库包含应用图标 `icon.icns` 和 PyInstaller 配置 `CleanPack.spec`。在 macOS 上运行：

```bash
uv run --with PySide6 --with pyinstaller pyinstaller --clean --noconfirm CleanPack.spec
```

应用生成在 `dist/CleanPack.app`。Git 忽略 `build/` 和 `dist/`，图标和 `.spec` 文件与源码一起纳入版本控制。应用打包了 Python 和 Qt 的运行时依赖；压缩命令仍然从目标 Mac 的系统或 Homebrew 环境中查找。

## 排除规则与文件保护

完整的排除列表定义在 `core.py` 的 `JUNK` 中，压缩和解压时都作用于每一层目录。普通的隐藏文件和名称相近的文件会被保留。

- **tar**：优先使用 `/usr/bin/tar`，并通过 `COPYFILE_DISABLE=1`、bsdtar 专用选项和排除规则省去资源分叉、扩展属性和垃圾文件。
- **zip**：使用 `-X` 去除额外属性，并递归排除垃圾文件。在 macOS 上会加上 UTF-8 文件名标志，防止中文路径被错误解码。
- **7z**：使用 `-xr!` 递归排除。可能表示有文件被遗漏的警告退出码按失败处理。
- **解压**：先解到一个私有的临时文件夹，在每一层删除垃圾文件，拒绝向外部写入或包含特殊文件的归档，最后才把结果移到目标位置，不替换任何已有内容。

每个任务都会在输出目录中创建自己的 `.cleanpack-*.part/` 临时目录。只有归档成功后，才用 `os.replace` 提交输出，从而避免任务之间共用临时文件，或误删同名的已有 `.part` 文件。

## 项目结构

```text
main.py             主窗口与任务协调
extract_panel.py    解压模式：归档与目标文件夹输入
encrypt_panel.py    压缩时的密码选项
tools_dialog.py     工具链窗口、工具检测与 Homebrew 安装
gui_common.py       共用的 Qt 辅助：样式、Qt 翻译、本地化数字、对话框
core.py             压缩核心，独立于 Qt
i18n.py             四种语言的界面文字
requirements.txt    运行依赖
CleanPack.spec      PyInstaller 构建配置
icon.icns           macOS 应用图标
LICENSE             BSD 2-Clause 许可证
licenses/           随 macOS 应用分发的第三方许可证文本
```

## 许可证

Copyright 2026 Xinghao Zuo。采用 [BSD 2-Clause 许可证](../LICENSE)
（SPDX：`BSD-2-Clause`）。第三方依赖保留各自的许可证；macOS 应用中打包的组件见
[licenses/THIRD_PARTY_NOTICES.md](../licenses/THIRD_PARTY_NOTICES.md)。
