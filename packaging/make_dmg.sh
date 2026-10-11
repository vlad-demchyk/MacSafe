#!/bin/bash
# Пакує build/MacSafe.app у DMG і, якщо задано NOTARY_PROFILE, віддає на notarization.
#
#   SIGN_IDENTITY="Developer ID Application: ..." ./build_app.sh
#   NOTARY_PROFILE=macsafe ./packaging/make_dmg.sh
#
# Профіль створюється один раз: xcrun notarytool store-credentials macsafe --apple-id ... --team-id ...
# Потрібен платний Apple Developer акаунт. Без нього застосунок працює лише з ad-hoc підписом на цьому Mac.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HERE/build/MacSafe.app"
DMG="$HERE/build/MacSafe.dmg"
fail() { echo "ПОМИЛКА: $*" >&2; exit 1; }
[ -d "$APP" ] || fail "спершу ./build_app.sh"
codesign --verify --deep "$APP" 2>/dev/null || fail "підпис бандла недійсний"
if [ -n "${NOTARY_PROFILE:-}" ]; then
  # Notarization вимагає строгої перевірки. venv бандла має символічні посилання на Python із Command Line Tools
  # поза бандлом, тому вона не пройде, доки Python не вбудований у бандл (див. README, «Розповсюдження»).
  codesign --verify --deep --strict "$APP" 2>/dev/null || fail "строгий підпис не проходить: venv посилається на Python поза бандлом"
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
