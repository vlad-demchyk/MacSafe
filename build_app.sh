#!/bin/bash
# Builds “MacSafe.app”: a self-contained bundle with the code, generator, template, icon and its own
# the embedded Python and dependencies (pywebview, psutil). After the build the repository can be moved or deleted:
# the app works from Contents/Resources and writes data to ~/Library/Application Support/MacSafe.
#
#   ./build_app.sh                 build into build/
#   ./build_app.sh --install       build and install into /Applications (or ~/Applications without permission)
#
# SIGN_IDENTITY=“Developer ID Application: ...” ./build_app.sh signs for distribution (see packaging/make_dmg.sh).
# No root is needed to run. Ad-hoc signature: enough on this machine, without a developer certificate.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="MacSafe"
BUILD="$HERE/build"
APP="$BUILD/$APP_NAME.app"
RES="$APP/Contents/Resources"
TMP="$BUILD/.work"
PY="/usr/bin/python3"
ID="com.user.macsafe"

fail() { echo "ERROR: $*" >&2; exit 1; }

[ -f "$HERE/agent.py" ] || fail "agent.py not found in $HERE"
[ -x "$PY" ] || fail "missing $PY"

rm -rf "$APP" "$TMP"
mkdir -p "$APP/Contents/MacOS" "$RES/core" "$TMP"

# 1. App code and generator (without service files)
for f in agent.py history.py thermal_hid.py actions.py alerts.py apps.py bigfiles.py persistence.py trash.py leftovers.py llm.py engine.py app.py i18n.py metrics.py gpu_iokit.py menubar.py prefs.py autostart.py cleanup.py diff.py bar.js app.html requirements.txt; do
  cp "$HERE/$f" "$RES/" || fail "copying $f"
done
cp "$HERE/core/generate-report.sh" "$HERE/core/template.html" "$RES/core/" || fail "copying core/"

# 2. Icon: PNG 1024 → set of sizes → .icns
"$PY" "$HERE/packaging/make_icon.py" "$TMP/icon-1024.png" || fail "could not draw the icon"
SET="$TMP/AppIcon.iconset"
mkdir -p "$SET"
for spec in 16:icon_16x16 32:icon_16x16@2x 32:icon_32x32 64:icon_32x32@2x \
            128:icon_128x128 256:icon_128x128@2x 256:icon_256x256 512:icon_256x256@2x \
            512:icon_512x512 1024:icon_512x512@2x; do
  px="${spec%%:*}"; name="${spec#*:}"
  sips -z "$px" "$px" "$TMP/icon-1024.png" --out "$SET/$name.png" >/dev/null || fail "sips: $name"
done
iconutil -c icns "$SET" -o "$RES/AppIcon.icns" || fail "iconutil"

# 3. Embedded Python: a copy of Python3.framework from the Command Line Tools inside the bundle (Contents/Frameworks)
# and packages in Resources/site-packages. No links outside the bundle, so the strict signature works.
FW_SRC="/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework"
[ -d "$FW_SRC" ] || fail "missing $FW_SRC (xcode-select --install)"
mkdir -p "$APP/Contents/Frameworks"
ditto "$FW_SRC" "$APP/Contents/Frameworks/Python3.framework" || fail "copying Python3.framework"
FWV="$APP/Contents/Frameworks/Python3.framework/Versions/3.9"
# Not needed to run: headers, man pages, tests, static libraries and config, utilities with absolute links
rm -rf "$APP/Contents/Frameworks/Python3.framework/Headers" "$FWV/include" "$FWV/share" "$FWV/lib/pkgconfig" \
       "$FWV/lib/python3.9/config-3.9-darwin" "$FWV/lib/python3.9/test" "$FWV/lib/python3.9/idlelib" \
       "$FWV/lib/python3.9/tkinter" "$FWV/lib/python3.9/turtledemo" "$FWV/bin"
# There must be no broken links in the bundle: strict codesign rejects them
find "$APP/Contents/Frameworks" -type l ! -exec test -e {} \; -print -delete >/dev/null
EMB_PY="$FWV/Resources/Python.app/Contents/MacOS/Python"
[ -x "$EMB_PY" ] || fail "no embedded Python"
"$PY" -m pip install --quiet --upgrade --target "$RES/site-packages" -r "$RES/requirements.txt" psutil || fail "dependencies (pip install)"
find "$RES" -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null
rm -f "$RES/requirements.txt"
"$EMB_PY" -c "import sys; sys.path.insert(0, '$RES/site-packages'); import psutil, webview" || fail "embedded Python cannot see pywebview or psutil"

# 4. Launcher: exports its own path (for autostart) and starts the menu bar agent with the embedded Python
LAUNCH="$APP/Contents/MacOS/launcher"
cat > "$LAUNCH" <<'LAUNCHER'
#!/bin/bash
# The “MacSafe” launcher. The bundle path is resolved relative to the launcher itself.
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
export MACSAFE_LAUNCHER="$HERE/Contents/MacOS/launcher"
export PYTHONPATH="$HERE/Contents/Resources/site-packages:$HERE/Contents/Resources"
export PYTHONNOUSERSITE=1
# Python runs as a child, not through exec: when the app is opened by Launch Services, a process that replaces itself with the
# executable of another bundle (Python.app) loses its place in the menu bar and its status item never appears.
PY="$HERE/Contents/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python"
"$PY" "$HERE/Contents/Resources/agent.py" "$@" &
CHILD=$!
trap 'kill "$CHILD" 2>/dev/null' TERM INT HUP
wait "$CHILD"
exit $?
LAUNCHER
chmod +x "$LAUNCH"

# 5. Info.plist. LSUIElement hides the Dock icon until start; the app sets the Accessory policy itself.
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
plutil -lint "$APP/Contents/Info.plist" >/dev/null || fail "Info.plist failed plutil"

# 6. Ad-hoc signature of the whole bundle
if [ -n "${SIGN_IDENTITY:-}" ]; then
  # Developer ID: hardened runtime and a timestamp, needed for notarization (SIGN_IDENTITY=“Developer ID Application: Name (TEAMID)”)
  codesign --force --deep --options runtime --timestamp --sign "$SIGN_IDENTITY" "$APP" >/dev/null 2>&1 || fail "codesign ($SIGN_IDENTITY)"
else
  codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || fail "codesign"
fi
codesign --verify --deep --strict "$APP" >/dev/null 2>&1 || fail "signature check (strict)"
rm -rf "$TMP"
echo "BUILD_OK $APP"

if [ "${1:-}" = "--install" ]; then
  DEST="/Applications"
  [ -w "$DEST" ] || DEST="$HOME/Applications"
  mkdir -p "$DEST"
  # Stop the running copy first: its code would keep running, and its instance lock would stop the new build from starting
  RUNNING='MacSafe[.]app/Contents/(MacOS/launcher|Resources/)'
  launchctl bootout "gui/$(id -u)/com.user.macsafe" >/dev/null 2>&1 || true
  pkill -f "$RUNNING" >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -f "$RUNNING" >/dev/null 2>&1 || break
    sleep 0.5
  done
  pkill -9 -f "$RUNNING" >/dev/null 2>&1 || true
  rm -rf "$DEST/$APP_NAME.app"
  ditto "$APP" "$DEST/$APP_NAME.app" || fail "copying to $DEST"
  echo "INSTALL_OK $DEST/$APP_NAME.app"

  # The status bar starts now and at every login: a LaunchAgent with RunAtLoad, registered by autostart.py.
  # It is the same plist the switch in the menu writes, so turning it off there keeps working.
  DATA_DIR="$HOME/Library/Application Support/MacSafe"
  mkdir -p "$DATA_DIR/дані"
  "/usr/bin/python3" -c 'import sys; sys.path.insert(0, sys.argv[1]); import autostart; ok, err = autostart.enable([sys.argv[2]], sys.argv[3], sys.argv[4]); print("STATUSBAR_OK" if ok else "STATUSBAR_FAIL " + err)' \
    "$DEST/$APP_NAME.app/Contents/Resources" "$DEST/$APP_NAME.app/Contents/MacOS/launcher" "$DATA_DIR" "$DATA_DIR/дані/автозапуск.log" \
    || echo "STATUSBAR_FAIL (python)"
fi
