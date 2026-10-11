#!/bin/bash
# Packs build/MacSafe.app into a DMG and, if NOTARY_PROFILE is set, submits it for notarization.
#
#   SIGN_IDENTITY="Developer ID Application: ..." ./build_app.sh
#   NOTARY_PROFILE=macsafe ./packaging/make_dmg.sh
#
# The profile is created once: xcrun notarytool store-credentials macsafe --apple-id ... --team-id ...
# A paid Apple Developer account is needed. Without it the app works only with an ad-hoc signature on this Mac.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HERE/build/MacSafe.app"
DMG="$HERE/build/MacSafe.dmg"
fail() { echo "ERROR: $*" >&2; exit 1; }
[ -d "$APP" ] || fail "run ./build_app.sh first"
codesign --verify --deep "$APP" 2>/dev/null || fail "bundle signature is invalid"
if [ -n "${NOTARY_PROFILE:-}" ]; then
  # Notarization requires a strict check. The bundle's venv has symbolic links to Python from the Command Line Tools
  # outside the bundle, so it will not pass until Python is embedded in the bundle (see README, “Distribution”).
  codesign --verify --deep --strict "$APP" 2>/dev/null || fail "strict signature check fails: the venv points to Python outside the bundle"
fi
rm -f "$DMG"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
ditto "$APP" "$STAGE/MacSafe.app"; ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname MacSafe -srcfolder "$STAGE" -ov -format UDZO "$DMG" || fail "hdiutil"
if [ -n "${NOTARY_PROFILE:-}" ]; then
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait || fail "notarization"
  xcrun stapler staple "$DMG" || fail "stapler"
fi
echo "DMG_OK $DMG"
