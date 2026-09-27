#!/usr/bin/env bash
#
# Install launchers for wintogo from this source checkout.
#
#   ./install.sh          add "Windows To Go" to your applications menu
#   sudo ./install.sh     also install a polkit policy, so the password prompt
#                         says what it is for instead of "run python3 as root"
#   ./install.sh --remove / sudo ./install.sh --remove

set -euo pipefail

HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
APP_ID=io.github.wintogo
PYTHON=$(command -v python3)

if [[ $EUID -eq 0 ]]; then
    apps=/usr/share/applications
    policy=/usr/share/polkit-1/actions/$APP_ID.policy
else
    apps=${XDG_DATA_HOME:-$HOME/.local/share}/applications
    policy=
fi
desktop=$apps/$APP_ID.desktop

if [[ ${1:-} == --remove ]]; then
    rm -f "$desktop" ${policy:+"$policy"}
    echo "removed"
    exit 0
fi

mkdir -p "$apps"
cat >"$desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Windows To Go
Comment=Install Windows onto a USB drive
Exec=$HERE/bin/wintogo-gui %f
Icon=drive-removable-media
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
    <icon_name>drive-removable-media</icon_name>
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
