#!/usr/bin/env bash
#
# Add "Windows To Go" to ~/Applications, as an app that runs this checkout.
#
#   macos/install-app.sh
#   macos/install-app.sh --remove
#
# On Linux, build and install the Debian package instead (see README.md).

set -euo pipefail

HERE=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
APP_ID=io.github.wintogo
PYTHON=$(command -v python3)

[[ $(uname -s) == Darwin ]] || { echo "install-app.sh: this is for macOS" >&2; exit 1; }
[[ $EUID -ne 0 ]] || { echo "install-app.sh: run it without sudo on macOS" >&2; exit 1; }
app="$HOME/Applications/Windows To Go.app"
if [[ ${1:-} == --remove ]]; then
    rm -rf "$app"
    echo "removed"
    exit 0
fi
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cat >"$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>wintogo</string>
  <key>CFBundleIconFile</key><string>wintogo</string>
  <key>CFBundleIdentifier</key><string>$APP_ID</string>
  <key>CFBundleName</key><string>Windows To Go</string>
  <key>CFBundlePackageType</key><string>APPL</string>
</dict>
</plist>
EOF
# Apps don't get Homebrew on their PATH, so name the python3 that has gi.
cat >"$app/Contents/MacOS/wintogo" <<EOF
#!/bin/sh
exec "$PYTHON" "$HERE/bin/wintogo-gui" "\$@"
EOF
chmod +x "$app/Contents/MacOS/wintogo"

# rsvg-convert comes with librsvg, which GTK needs for its icons anyway.
if command -v rsvg-convert >/dev/null; then
    iconset=$(mktemp -d)/wintogo.iconset
    mkdir "$iconset"
    for size in 16 32 128 256 512; do
        rsvg-convert -w $size -h $size -o "$iconset/icon_${size}x${size}.png" \
            "$HERE/macos/icon.svg"
        rsvg-convert -w $((size * 2)) -h $((size * 2)) \
            -o "$iconset/icon_${size}x${size}@2x.png" "$HERE/macos/icon.svg"
    done
    iconutil -c icns -o "$app/Contents/Resources/wintogo.icns" "$iconset"
    rm -r "$(dirname "$iconset")"
    # Finder notices a new icon when the bundle's modification time changes.
    touch "$app"
else
    echo "install-app.sh: rsvg-convert not found (brew install librsvg), so no app icon" >&2
fi
echo "installed $app"
