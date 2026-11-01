# CleanPack

English | [简体中文](docs/README_zh.md)

**Create and extract clean archives on macOS, automatically excluding system files such as `.DS_Store`, `._*`, and `__MACOSX`.**

CleanPack is a desktop archiving tool built with Python and PySide6. Select or drag in a file or folder, choose an archive format and compression level, and create an archive ready to share without writing exclusion commands. In Extract mode, it unpacks archives from others and leaves their macOS junk behind. This project was developed with assistance from OpenAI Codex.

## Features

- Supports **tar, tar.gz, tar.xz, tar.zst, zip, and 7z**, with recursive exclusion of macOS junk files.
- Provides compression level sliders, xz extreme mode, and multithreaded compression for xz, zstd, and 7z.
- Supports file and folder selection, drag and drop of a single item, Save As, cancellation, and revealing the output in Finder. Paths can also be typed or pasted; they are checked as you type.
- **Extract mode** for the same six formats: junk files are skipped, a single top-level file or folder goes straight into the destination folder, and nothing is ever overwritten (existing names get a number, as in Finder).
- **Password protection** for zip (AES-256, or ZipCrypto for compatibility) and 7z (AES-256, with file names encrypted by default). Encrypted archives ask for their password when you extract them.
- Detects compression tools and warns when the selected format needs a missing one; at startup it also points out a missing Homebrew if a tool still needs it. The Toolchain window (header button or **Tools ▸ Toolchain…**) shows tool paths, the Homebrew command, a one-click install with live logs, and keeps installing in the background if you close it.
- Offers English, Simplified Chinese, Traditional Chinese, and French interfaces, with support for system light and dark appearance.
- Replaces the output atomically on success. On failure or cancellation, it cleans up temporary files and preserves existing output. It also prevents overwriting the source file or placing the output inside the source folder.

## Supported formats

| Format | Compression levels | Default level | Required commands |
| --- | --- | --- | --- |
| `tar` | None; archiving only | — | System `tar` |
| `tar.gz` | 1–9 | 6 | `tar`, `gzip` |
| `tar.xz` | 0–9, with optional extreme mode | 6 | `tar`, `xz` |
| `tar.zst` | 1–22 | 3 | `tar`, `zstd` |
| `zip` | 0–9; 0 stores files without compression | 6 | `zip`; encrypted zip uses the system `tar` |
| `7z` | 0–9; 0 stores files without compression | 5 | `7zz`, with fallback to `7z` |

zstd levels 20–22 enable `--ultra` and use substantially more memory. Switching formats resets the compression level to that format's default and turns off xz extreme mode.

Extraction uses the system `tar` for every format except `tar.zst`, which also needs `zstd`. 7-Zip is needed for encrypted 7z archives, for 7z archives that use a compression method the system `tar` cannot read, and for a few zip archives (for example Deflate64, or encrypted items compressed with LZMA or BZip2); CleanPack says so when it happens.

## Run from source

Requires macOS, Python 3.10 or later, and PySide6. The tools included with macOS support `tar`, `tar.gz`, and `zip`; the remaining formats require additional command-line tools.

From the project directory, launch with uv:

```bash
uv run --with PySide6 main.py
```

Alternatively, use a Python virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

To use the other three formats, install their dependencies with Homebrew or use the install button in the app's Toolchain window:

```bash
brew install xz zstd sevenzip
```

If Homebrew is missing, the app provides installation instructions. It does not install Homebrew automatically.

## Usage

1. Choose a file or folder, or drag it into the window.
2. Select an archive format and adjust the compression level and output location as needed.
3. Click **Compress**. If the output file already exists, the app asks before replacing it.
4. When compression finishes, click **Show in Finder** to locate the archive.

The app processes one input item at a time. To archive multiple files, place them in a folder first. The interface shows when a task is running but does not display a progress percentage.

### Encryption

Tick **Encrypt with a password** before compressing and enter the password twice. It is available for zip and 7z; tar formats have no standard encryption.

- **zip**: AES-256 is the default. ZipCrypto opens in more apps but is easy to crack. zip encryption protects file contents only: file names, and the targets of symbolic links, stay visible. zip passwords are limited to English letters, digits, spaces and ASCII symbols so that other apps can open the archive.
- **7z**: AES-256. File names are encrypted too unless you untick **Encrypt file names**.

Passwords are handed to the compression tools on standard input, never on the command line, and are not saved. The password fields are cleared after a successful compression.

### Extract

1. Switch to **Extract** and choose, drag in or type the path of an archive.
2. The destination defaults to the archive's folder; choose another one if needed.
3. Click **Extract**. If the archive is encrypted, CleanPack asks for its password, and asks again if it is wrong.

A single top-level file or folder is placed directly in the destination; otherwise everything goes into a folder named after the archive. Symbolic links that point outside the extracted items are kept, and the log lists them. If the archive came from the internet, the extracted items keep its "downloaded" mark (except on volumes such as exFAT, where the log says so).

## Build the macOS app

The repository includes the app icon, `icon.icns`, and the PyInstaller configuration, `CleanPack.spec`. Run on macOS:

```bash
uv run --with PySide6 --with pyinstaller pyinstaller --clean --noconfirm CleanPack.spec
```

The app is generated at `dist/CleanPack.app`. Git ignores `build/` and `dist/`, while the icon and `.spec` file are tracked with the source. The app bundles Python and Qt runtime dependencies; compression commands are still located in the target Mac's system or Homebrew environment.

## Exclusions and file protection

The complete exclusion list is defined in `core.py` as `JUNK` and applies at every directory level, both when compressing and when extracting. Ordinary hidden files and files with similar names are preserved.

- **tar**: Prefers `/usr/bin/tar` and uses `COPYFILE_DISABLE=1`, bsdtar-specific options, and exclusion rules to omit resource forks, extended attributes, and junk files.
- **zip**: Uses `-X` to strip extra attributes and recursively excludes junk files. On macOS, it adds UTF-8 filename flags to prevent Chinese paths from being decoded incorrectly.
- **7z**: Uses `-xr!` for recursive exclusions. Warning exit codes that may indicate omitted files are treated as failures.
- **Extraction**: Unpacks into a private temporary folder, removes junk at every level, refuses archives that write outside it or contain special files, and only then moves the result into place without replacing anything.

Each task creates its own `.cleanpack-*.part/` temporary directory in the output directory. The output is committed with `os.replace` only after the archive succeeds, preventing tasks from sharing temporary files or accidentally deleting an existing `.part` file with the same name.

## Project structure

```text
main.py             Main window and task coordination
extract_panel.py    Extract mode: archive and destination inputs
encrypt_panel.py    Password options for compression
tools_dialog.py     Toolchain window, tool detection, and Homebrew installation
gui_common.py       Shared Qt helpers: styles, Qt translations, localized numbers, dialogs
core.py             Archiving core, independent of Qt
i18n.py             Interface strings in four languages
requirements.txt    Runtime dependencies
CleanPack.spec      PyInstaller build configuration
icon.icns           macOS app icon
LICENSE             BSD 2-Clause license
licenses/           Third-party license texts bundled with the macOS app
```

## License

Copyright 2026 Xinghao Zuo. Licensed under the [BSD 2-Clause License](LICENSE)
(SPDX: `BSD-2-Clause`). Third-party dependencies retain their respective licenses; see
[licenses/THIRD_PARTY_NOTICES.md](licenses/THIRD_PARTY_NOTICES.md) for the components bundled in the macOS app.
