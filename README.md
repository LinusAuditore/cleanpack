# CleanPack

**Create clean archives on macOS, automatically excluding system files such as `.DS_Store`, `._*`, and `__MACOSX`.**

CleanPack is a desktop archiving tool built with Python and PySide6. Select or drag in a file or folder, choose an archive format and compression level, and create an archive ready to share without writing exclusion commands. This project was developed with assistance from OpenAI Codex.

## Features

- Supports **tar, tar.gz, tar.xz, tar.zst, zip, and 7z**, with recursive exclusion of macOS junk files.
- Provides compression level sliders, xz extreme mode, and multithreaded compression for xz, zstd, and 7z.
- Supports file and folder selection, drag and drop of a single item, Save As, cancellation, and revealing the output in Finder.
- Detects compression tools, shows missing dependencies and Homebrew installation commands, and provides an install button with live logs.
- Offers English, Simplified Chinese, Traditional Chinese, and French interfaces, with support for system light and dark appearance.
- Replaces the output atomically on success. On failure or cancellation, it cleans up temporary files and preserves existing output. It also prevents overwriting the source file or placing the output inside the source folder.

## Supported formats

| Format | Compression levels | Default level | Required commands |
| --- | --- | --- | --- |
| `tar` | None; archiving only | — | System `tar` |
| `tar.gz` | 1–9 | 6 | `tar`, `gzip` |
| `tar.xz` | 0–9, with optional extreme mode | 6 | `tar`, `xz` |
| `tar.zst` | 1–22 | 3 | `tar`, `zstd` |
| `zip` | 0–9; 0 stores files without compression | 6 | `zip` |
| `7z` | 0–9; 0 stores files without compression | 5 | `7zz`, with fallback to `7z` |

zstd levels 20–22 enable `--ultra` and use substantially more memory. Switching formats resets the compression level to that format's default.

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

To use the other three formats, install their dependencies with Homebrew or use the install button in the app:

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

## Build the macOS app

The repository includes the app icon, `icon.icns`, and the PyInstaller configuration, `CleanPack.spec`. Run on macOS:

```bash
uv run --with PySide6 --with pyinstaller pyinstaller --clean --noconfirm CleanPack.spec
```

The app is generated at `dist/CleanPack.app`. Git ignores `build/` and `dist/`, while the icon and `.spec` file are tracked with the source. The app bundles Python and Qt runtime dependencies; compression commands are still located in the target Mac's system or Homebrew environment.

## Exclusions and file protection

The complete exclusion list is defined in `core.py` as `JUNK` and applies at every directory level. Ordinary hidden files and files with similar names are preserved.

- **tar**: Prefers `/usr/bin/tar` and uses `COPYFILE_DISABLE=1`, bsdtar-specific options, and exclusion rules to omit resource forks, extended attributes, and junk files.
- **zip**: Uses `-X` to strip extra attributes and recursively excludes junk files. On macOS, it adds UTF-8 filename flags to prevent Chinese paths from being decoded incorrectly.
- **7z**: Uses `-xr!` for recursive exclusions. Warning exit codes that may indicate omitted files are treated as failures.

Each task creates its own `.cleanpack-*.part/` temporary directory in the output directory. The output is committed with `os.replace` only after the archive succeeds, preventing tasks from sharing temporary files or accidentally deleting an existing `.part` file with the same name.

## Project structure

```text
main.py             GUI, task coordination, and tool installation
core.py             Archiving core, independent of Qt
i18n.py             Interface strings in four languages
requirements.txt    Runtime dependencies
CleanPack.spec      PyInstaller build configuration
icon.icns           macOS app icon
```
