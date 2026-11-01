#!/bin/bash
# 检查 PyInstaller 产物：版本与 bundle id、许可证、签名、只打包了用到的 Qt 模块。
# 用法：check_app.sh <CleanPack.app 路径>；在仓库根目录运行（需要导入 core）。
set -euo pipefail

app="${1:?usage: check_app.sh path/to/CleanPack.app}"
plist="$app/Contents/Info.plist"
fail() { echo "FAIL: $*" >&2; exit 1; }

read -r version bundle_id < <(python3 -B -c 'import core; print(core.APP_VERSION, core.BUNDLE_ID)')
[[ "$(plutil -extract CFBundleShortVersionString raw "$plist")" == "$version" ]] || fail "CFBundleShortVersionString"
[[ "$(plutil -extract CFBundleVersion raw "$plist")" == "$version" ]] || fail "CFBundleVersion"
[[ "$(plutil -extract CFBundleIdentifier raw "$plist")" == "$bundle_id" ]] || fail "CFBundleIdentifier"
echo "LSMinimumSystemVersion: $(plutil -extract LSMinimumSystemVersion raw "$plist")"

# 与 CleanPack.spec 的 LICENSE_FILES 以及打包的 Python 许可证对应。
for f in LICENSE licenses/THIRD_PARTY_NOTICES.md licenses/LGPL-3.0.txt licenses/GPL-3.0.txt \
         licenses/Apache-2.0.txt licenses/Python/LICENSE.txt; do
    [[ -f "$app/Contents/Resources/$f" ]] || fail "missing $f"
done

codesign --verify --deep --strict "$app" || fail "codesign --verify"

# spec 排除了虚拟键盘、Quick/QML/PDF 等模块，这里确认实际只剩这六个。
expected="QtCore.framework QtDBus.framework QtGui.framework QtNetwork.framework QtSvg.framework QtWidgets.framework"
actual="$(ls "$app/Contents/Frameworks/PySide6/Qt/lib" | sort | tr '\n' ' ' | sed 's/ $//')"
[[ "$actual" == "$expected" ]] || fail "Qt modules: $actual"

echo "OK: $app ($version, $bundle_id)"
