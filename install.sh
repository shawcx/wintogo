#!/usr/bin/env bash
#
# Install launchers for wintogo from this source checkout.
#
#   ./install.sh          add "Windows To Go" to your applications menu
#   sudo ./install.sh     also install a polkit policy, so the password prompt
#                         says what it is for instead of "run python3 as root"
#   ./install.sh --remove / sudo ./install.sh --remove
#
# On macOS, ./install.sh adds "Windows To Go" to ~/Applications instead.

set -euo pipefail

HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
APP_ID=io.github.wintogo
PYTHON=$(command -v python3)

if [[ $(uname -s) == Darwin ]]; then
    [[ $EUID -ne 0 ]] || { echo "install.sh: run it without sudo on macOS" >&2; exit 1; }
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
        echo "install.sh: rsvg-convert not found (brew install librsvg), so no app icon" >&2
    fi
    echo "installed $app"
    exit 0
fi

if [[ $EUID -eq 0 ]]; then
    data=/usr/share
    policy=/usr/share/polkit-1/actions/$APP_ID.policy
else
    data=${XDG_DATA_HOME:-$HOME/.local/share}
    policy=
fi
apps=$data/applications
icons=$data/icons/hicolor
desktop=$apps/$APP_ID.desktop
icon_files=("scalable/apps/$APP_ID.svg" "symbolic/apps/$APP_ID-symbolic.svg")

refresh_icon_cache() {
    # The system theme has an icon cache; a user's normally doesn't.
    if [[ $EUID -eq 0 ]]; then
        gtk-update-icon-cache -q "$icons" 2>/dev/null ||
            gtk4-update-icon-cache -q "$icons" 2>/dev/null || true
    fi
}

if [[ ${1:-} == --remove ]]; then
    rm -f "$desktop" "${icon_files[@]/#/$icons/}" ${policy:+"$policy"}
    refresh_icon_cache
    echo "removed"
    exit 0
fi

for file in "${icon_files[@]}"; do
    install -Dm644 "$HERE/data/icons/hicolor/$file" "$icons/$file"
done
refresh_icon_cache

mkdir -p "$apps"
cat >"$desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Windows To Go
Comment=Install Windows onto a USB drive
Exec=$HERE/bin/wintogo-gui %f
Icon=$APP_ID
Terminal=false
Categories=System;
MimeType=application/x-cd-image;application/x-iso9660-image;
Keywords=windows;usb;wintogo;rufus;
EOF
echo "installed $desktop"

if [[ -n $policy ]]; then
    # pkexec matches the policy by program and first argument, so it only
    # covers running this checkout's backend.
    cat >"$policy" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
  "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <action id="$APP_ID.write-drive">
    <description>Install Windows onto a USB drive</description>
    <message>Authentication is required to erase the drive and install Windows on it</message>
    <icon_name>$APP_ID</icon_name>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>auth_admin</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">$PYTHON</annotate>
    <annotate key="org.freedesktop.policykit.exec.argv1">$HERE/bin/wintogo</annotate>
  </action>
</policyconfig>
EOF
    echo "installed $policy"
fi
