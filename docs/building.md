# Building and distribution

## Build

You need the Xcode Command Line Tools (`xcode-select --install`). The build copies the Python 3.9 framework from them into the app, so the app runs without a separate Python.

```sh
./build_app.sh                 # builds build/MacSafe.app
./build_app.sh --install       # builds and installs to /Applications (or ~/Applications)
```

The build:

1. creates the icon from `packaging/make_icon.py`,
2. copies the code, the report generator and the template into `Contents/Resources`,
3. copies the Python 3.9 framework into `Contents/Frameworks`,
4. installs the dependencies (`requirements.txt`, which is pywebview, and psutil) into `Contents/Resources/site-packages`,
5. writes `Info.plist` and a launcher that sets the Python path,
6. signs the bundle and checks it with `codesign --verify --strict`.

The app is self-contained. You can delete the repository after the build.

## Signing

By default the bundle is signed ad hoc, which is enough on the Mac that built it.

For distribution, sign with a Developer ID and the hardened runtime:

```sh
SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" ./build_app.sh
```

## DMG and notarization

```sh
./packaging/make_dmg.sh                                    # build/MacSafe.dmg
NOTARY_PROFILE=macsafe ./packaging/make_dmg.sh             # also notarizes and staples
```

`NOTARY_PROFILE` is a keychain profile created once with `xcrun notarytool store-credentials`. Notarization needs a paid Apple Developer Program membership. It has not been done for this release.

## Tests before a release

```sh
python3 -m unittest discover -s tests
```

Then build, install, and check the menu bar and the report window in both languages.

## Data

The build does not touch your data. Reports, settings and history stay in `~/Library/Application Support/MacSafe`.
