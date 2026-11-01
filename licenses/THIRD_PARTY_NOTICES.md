# Third-party notices

The CleanPack source code is licensed under the BSD 2-Clause License (see `LICENSE`).
The macOS app built with `CleanPack.spec` also bundles the following third-party
components. Each component remains under its own license; the license texts listed
below are included in this folder.

| Component | License | Text in this folder | Source |
| --- | --- | --- | --- |
| Qt for Python (PySide6, Shiboken6) | LGPL-3.0-only (also offered under GPL-2.0-only or GPL-3.0-only) | `LGPL-3.0.txt`, `GPL-3.0.txt` | https://code.qt.io/cgit/pyside/pyside-setup.git |
| Qt 6 modules QtCore, QtGui, QtWidgets, QtDBus, QtNetwork and QtSvg, with their plugins | LGPL-3.0-only | `LGPL-3.0.txt`, `GPL-3.0.txt` | https://download.qt.io/official_releases/qt/ |
| Third-party code built into those Qt modules (for example PCRE2 in QtCore, HarfBuzz and libpng in QtGui) | Mostly BSD-, MIT- or zlib-style licenses, listed by Qt | — | https://doc.qt.io/qt-6/licenses-used-in-qt.html |
| Python runtime | Python Software Foundation License | `Python/LICENSE.txt` (copied from the build interpreter) | https://www.python.org/downloads/source/ |
| Libraries used by the Python runtime (OpenSSL `libcrypto`, libffi, zlib, bzip2, liblzma, mpdecimal, expat, SQLite) | OpenSSL: Apache-2.0; the others: MIT-, BSD-style or public-domain licenses, listed by Python | `Apache-2.0.txt` (OpenSSL) | https://docs.python.org/3/license.html |
| PyInstaller bootloader | GPL-2.0-or-later with the PyInstaller bootloader exception, which permits distributing the bootloader with applications under any license | — | https://github.com/pyinstaller/pyinstaller |
| PyInstaller run-time hooks embedded in the executable | Apache-2.0 | `Apache-2.0.txt` | https://github.com/pyinstaller/pyinstaller |

The GNU Lesser General Public License version 3 builds on the GNU General Public
License version 3, so both texts are included. The build excludes Qt modules that
CleanPack does not use, including Qt Virtual Keyboard, which is licensed under GPL-3.0
only.

The Qt libraries are dynamically linked framework files inside
`CleanPack.app/Contents/Frameworks`. You may replace them with modified versions
of the same Qt release. The app is code-signed, so after replacing a framework you
need to sign the app again, for example with `codesign --force --deep --sign - CleanPack.app`.
The corresponding source code is available from the links above.

The compression tools CleanPack runs (`tar`, `gzip`, `zip`, `xz`, `zstd`, `7zz`) are
not bundled. They come from macOS or from Homebrew on the user's Mac.

The exact component versions depend on the environment used to build the app. After
each build, check the `CFBundleVersion` in each Qt framework's `Resources/Info.plist`
under `Contents/Frameworks/PySide6/Qt/lib`, and the version embedded in the
`libpyside6` and `libshiboken6` file names.
