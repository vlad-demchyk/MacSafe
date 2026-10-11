#!/bin/bash
# Збирає «MacSafe.app»: самодостатній бандл з кодом, генератором, шаблоном, іконкою і власним
# вбудованим Python і залежностями (pywebview, psutil). Після збірки репозиторій можна переносити або видаляти:
# застосунок працює з Contents/Resources і пише дані в ~/Library/Application Support/MacSafe.
#
#   ./build_app.sh                 зібрати в build/
#   ./build_app.sh --install       зібрати і встановити в /Applications (або ~/Applications, якщо немає прав)
#
# SIGN_IDENTITY="Developer ID Application: ..." ./build_app.sh підписує для розповсюдження (див. packaging/make_dmg.sh).
# Для запуску не потрібен root. Підпис ad-hoc: достатньо на цій машині, без сертифіката розробника.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="MacSafe"
BUILD="$HERE/build"
APP="$BUILD/$APP_NAME.app"
RES="$APP/Contents/Resources"
TMP="$BUILD/.work"
PY="/usr/bin/python3"
ID="com.user.macsafe"

fail() { echo "ПОМИЛКА: $*" >&2; exit 1; }

[ -f "$HERE/agent.py" ] || fail "не знайдено agent.py у $HERE"
[ -x "$PY" ] || fail "немає $PY"

rm -rf "$APP" "$TMP"
mkdir -p "$APP/Contents/MacOS" "$RES/core" "$TMP"

# 1. Код застосунку і генератор (без службових файлів)
for f in agent.py alerts.py bigfiles.py persistence.py trash.py leftovers.py engine.py app.py i18n.py metrics.py gpu_iokit.py menubar.py prefs.py autostart.py cleanup.py diff.py bar.js app.html requirements.txt; do
  cp "$HERE/$f" "$RES/" || fail "копіювання $f"
done
cp "$HERE/core/generate-report.sh" "$HERE/core/template.html" "$RES/core/" || fail "копіювання core/"

# 2. Іконка: PNG 1024 → набір розмірів → .icns
"$PY" "$HERE/packaging/make_icon.py" "$TMP/icon-1024.png" || fail "не вдалося намалювати іконку"
SET="$TMP/AppIcon.iconset"
mkdir -p "$SET"
for spec in 16:icon_16x16 32:icon_16x16@2x 32:icon_32x32 64:icon_32x32@2x \
            128:icon_128x128 256:icon_128x128@2x 256:icon_256x256 512:icon_256x256@2x \
            512:icon_512x512 1024:icon_512x512@2x; do
  px="${spec%%:*}"; name="${spec#*:}"
  sips -z "$px" "$px" "$TMP/icon-1024.png" --out "$SET/$name.png" >/dev/null || fail "sips: $name"
done
iconutil -c icns "$SET" -o "$RES/AppIcon.icns" || fail "iconutil"

# 3. Вбудований Python: копія Python3.framework із Command Line Tools усередині бандла (Contents/Frameworks)
# і пакунки в Resources/site-packages. Жодних посилань за межі бандла, тож працює строгий підпис.
FW_SRC="/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework"
[ -d "$FW_SRC" ] || fail "немає $FW_SRC (xcode-select --install)"
mkdir -p "$APP/Contents/Frameworks"
ditto "$FW_SRC" "$APP/Contents/Frameworks/Python3.framework" || fail "копіювання Python3.framework"
FWV="$APP/Contents/Frameworks/Python3.framework/Versions/3.9"
# Зайве для запуску: заголовки, man, тести, статичні бібліотеки й конфіг, утиліти з абсолютними посиланнями
rm -rf "$APP/Contents/Frameworks/Python3.framework/Headers" "$FWV/include" "$FWV/share" "$FWV/lib/pkgconfig" \
       "$FWV/lib/python3.9/config-3.9-darwin" "$FWV/lib/python3.9/test" "$FWV/lib/python3.9/idlelib" \
       "$FWV/lib/python3.9/tkinter" "$FWV/lib/python3.9/turtledemo" "$FWV/bin"
# Битих посилань у бандлі бути не повинно: строгий codesign їх відхиляє
find "$APP/Contents/Frameworks" -type l ! -exec test -e {} \; -print -delete >/dev/null
EMB_PY="$FWV/Resources/Python.app/Contents/MacOS/Python"
[ -x "$EMB_PY" ] || fail "немає вбудованого Python"
"$PY" -m pip install --quiet --upgrade --target "$RES/site-packages" -r "$RES/requirements.txt" psutil || fail "залежності (pip install)"
find "$RES" -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null
rm -f "$RES/requirements.txt"
"$EMB_PY" -c "import sys; sys.path.insert(0, '$RES/site-packages'); import psutil, webview" || fail "вбудований Python не бачить pywebview або psutil"

# 4. Запускач: експортує шлях до себе (для автозапуску) і запускає агент статус-бару вбудованим Python
LAUNCH="$APP/Contents/MacOS/launcher"
cat > "$LAUNCH" <<'LAUNCHER'
#!/bin/bash
# Запускач «MacSafe». Шлях до бандла визначається відносно самого запускача.
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
export MACSAFE_LAUNCHER="$HERE/Contents/MacOS/launcher"
export PYTHONPATH="$HERE/Contents/Resources/site-packages:$HERE/Contents/Resources"
export PYTHONNOUSERSITE=1
exec "$HERE/Contents/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python" "$HERE/Contents/Resources/agent.py" "$@"
LAUNCHER
chmod +x "$LAUNCH"

# 5. Info.plist. LSUIElement прибирає значок у Dock до старту; політику Accessory застосунок ставить сам.
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$ID</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppleEventsUsageDescription</key><string>MacSafe controls Finder to empty the Trash, and runs the system memory cleanup command after you confirm.</string>
</dict>
</plist>
PLIST
plutil -lint "$APP/Contents/Info.plist" >/dev/null || fail "Info.plist не пройшов plutil"

# 6. Ad-hoc підпис всього бандла
if [ -n "${SIGN_IDENTITY:-}" ]; then
  # Developer ID: hardened runtime і мітка часу, потрібні для notarization (SIGN_IDENTITY="Developer ID Application: Імʼя (TEAMID)")
  codesign --force --deep --options runtime --timestamp --sign "$SIGN_IDENTITY" "$APP" >/dev/null 2>&1 || fail "codesign ($SIGN_IDENTITY)"
else
  codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || fail "codesign"
fi
codesign --verify --deep --strict "$APP" >/dev/null 2>&1 || fail "перевірка підпису (строга)"
rm -rf "$TMP"
echo "BUILD_OK $APP"

if [ "${1:-}" = "--install" ]; then
  DEST="/Applications"
  [ -w "$DEST" ] || DEST="$HOME/Applications"
  mkdir -p "$DEST"
  rm -rf "$DEST/$APP_NAME.app"
  ditto "$APP" "$DEST/$APP_NAME.app" || fail "копіювання в $DEST"
  echo "INSTALL_OK $DEST/$APP_NAME.app"
fi
