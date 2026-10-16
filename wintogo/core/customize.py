"""Offline changes to the applied Windows: registry settings and an answer file.

Nothing is mounted: hive files are read out of the unmounted NTFS volume with
ntfscat, edited with hivex and written back with ntfscp (which keeps the
file's ACLs and attributes). ntfs-3g path lookups are case-sensitive, so the
paths below are spelled exactly as in the image.
"""

import base64
import os
import re
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from .util import IS_MACOS, WinToGoError, run

SYSTEM_HIVE = "/Windows/System32/config/SYSTEM"
SOFTWARE_HIVE = "/Windows/System32/config/SOFTWARE"
# Windows looks for an answer file here when a generalized image first boots;
# \Windows\Panther would be preferred but doesn't exist in the image, and
# ntfs-3g's tools can't create directories.
UNATTEND_PATH = "/Windows/System32/Sysprep/unattend.xml"

REG_SZ, REG_DWORD = 1, 4


@dataclass
class Options:
    # Registry (offline).
    portable: bool = True              # mark as a Windows To Go install
    hide_internal_disks: bool = True   # SanPolicy 4: internal disks stay offline
    disable_bitlocker: bool = True     # no automatic device encryption
    disable_hibernation: bool = True   # no hibernate / Fast Startup on a roaming disk
    # First-time setup (unattend.xml + BypassNRO).
    skip_online_account: bool = True   # no Microsoft account / network requirement
    skip_privacy: bool = True          # accept defaults for EULA and privacy pages
    local_account: str = ""            # create this local admin account
    password: str = ""
    locale: str = ""                   # e.g. "en-GB"; sets region/keyboard/formats
    computer_name: str = ""

    def validate(self):
        if self.local_account:
            if len(self.local_account) > 20 or re.search(r'["/\\\[\]:;|=,+*?<>@]', self.local_account):
                raise WinToGoError("the account name must be at most 20 characters and "
                                   'contain none of " / \\ [ ] : ; | = , + * ? < > @')
            if self.local_account.lower() in ("administrator", "guest", "defaultaccount"):
                raise WinToGoError(f"{self.local_account!r} is a reserved account name")
        if self.computer_name and not re.fullmatch(r"[A-Za-z0-9-]{1,15}", self.computer_name):
            raise WinToGoError("the computer name must be 1-15 letters, digits or hyphens")
        if self.locale and not re.fullmatch(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})*", self.locale):
            raise WinToGoError(f"invalid locale {self.locale!r} (expected e.g. en-GB)")

    @property
    def wants_unattend(self):
        return bool(self.skip_online_account or self.skip_privacy or self.local_account
                    or self.locale or self.computer_name)


def host_locale():
    """The session's locale as a Windows tag, e.g. en_GB.UTF-8 -> en-GB."""
    values = [os.environ.get(var, "") for var in ("LC_ALL", "LC_CTYPE", "LANG")]
    if IS_MACOS:
        # Apps started from Finder get no LANG (Python then sets LC_CTYPE to
        # C.UTF-8); the region setting is here.
        values.append(run(["defaults", "read", "-g", "AppleLocale"], check=False).strip())
    for value in values:
        m = re.match(r"([a-z]{2,3})_([A-Z]{2})", value)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return ""


# ------------------------------------------------------------------ registry

class _HiveEditor:
    def __init__(self, path):
        import hivex  # python3-hivex
        self.h = hivex.Hivex(str(path), write=True)
        self.path = path

    def key(self, path):
        """Return the node for a backslash path, creating keys as needed."""
        node = self.h.root()
        for part in path.split("\\"):
            child = self.h.node_get_child(node, part)
            node = child if child is not None else self.h.node_add_child(node, part)
        return node

    def get_dword(self, path, name):
        node = self.h.root()
        for part in path.split("\\"):
            node = self.h.node_get_child(node, part)
            if node is None:
                return None
        value = self.h.node_get_value(node, name)
        return self.h.value_dword(value) if value is not None else None

    def set_dword(self, path, name, value):
        self.h.node_set_value(self.key(path), {
            "key": name, "t": REG_DWORD, "value": struct.pack("<I", value)})

    def commit(self):
        self.h.commit(str(self.path))


def _edit_hive(volume, hive_path, workdir, edit):
    local = Path(workdir) / Path(hive_path).name
    with open(local, "wb") as f:
        proc = subprocess.run(["ntfscat", volume, hive_path], stdout=f, stderr=subprocess.PIPE, text=False)
    if proc.returncode != 0:
        raise WinToGoError(f"could not read {hive_path}: {proc.stderr.decode(errors='replace').strip()}")
    editor = _HiveEditor(local)
    edit(editor)
    editor.commit()
    run(["ntfscp", "--quiet", "--force", volume, local, hive_path])
    local.unlink()


def _system_edits(opts, reporter):
    def edit(reg):
        current = reg.get_dword("Select", "Current") or 1
        ccs = f"ControlSet{current:03d}"
        if opts.portable:
            reg.set_dword(f"{ccs}\\Control", "PortableOperatingSystem", 1)
            reporter.log("marked as a portable (Windows To Go) installation")
        if opts.hide_internal_disks:
            reg.set_dword(f"{ccs}\\Services\\partmgr\\Parameters", "SanPolicy", 4)
            reporter.log("internal disks will stay offline")
        if opts.disable_bitlocker:
            reg.set_dword(f"{ccs}\\Control\\BitLocker", "PreventDeviceEncryption", 1)
            reporter.log("automatic BitLocker device encryption disabled")
        if opts.disable_hibernation:
            reg.set_dword(f"{ccs}\\Control\\Power", "HibernateEnabled", 0)
            reg.set_dword(f"{ccs}\\Control\\Power", "HibernateEnabledDefault", 0)
            reg.set_dword(f"{ccs}\\Control\\Session Manager\\Power", "HiberbootEnabled", 0)
            reporter.log("hibernation and Fast Startup disabled")
    return edit


def _software_edits(opts, reporter):
    def edit(reg):
        # Lets OOBE continue without a network connection / Microsoft account.
        reg.set_dword(r"Microsoft\Windows\CurrentVersion\OOBE", "BypassNRO", 1)
    return edit


# ------------------------------------------------------------------ unattend

def _encode_password(password, kind):
    """Windows answer-file password obfuscation: base64(UTF-16LE(pw + kind))."""
    return base64.b64encode((password + kind).encode("utf-16-le")).decode()


_COMPONENT = ('<component name="{name}" processorArchitecture="{arch}" '
              'publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" '
              'xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" '
              'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">')


def build_unattend(opts, arch="amd64"):
    def component(name, body):
        return _COMPONENT.format(name=name, arch=arch) + body + "</component>"

    specialize = []
    if opts.computer_name:
        specialize.append(component("Microsoft-Windows-Shell-Setup",
                                    f"<ComputerName>{escape(opts.computer_name)}</ComputerName>"))

    oobe_system = []
    if opts.locale:
        loc = escape(opts.locale)
        oobe_system.append(component("Microsoft-Windows-International-Core",
                                     f"<InputLocale>{loc}</InputLocale><SystemLocale>{loc}</SystemLocale>"
                                     f"<UserLocale>{loc}</UserLocale>"))

    shell = []
    oobe = []
    if opts.skip_privacy:
        oobe += ["<HideEULAPage>true</HideEULAPage>", "<ProtectYourPC>3</ProtectYourPC>"]
    if opts.skip_online_account or opts.local_account:
        oobe += ["<HideOnlineAccountScreens>true</HideOnlineAccountScreens>"]
    if opts.local_account:
        oobe += ["<HideLocalAccountScreen>true</HideLocalAccountScreen>"]
    if oobe:
        oobe += ["<HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>"]
        shell.append("<OOBE>" + "".join(oobe) + "</OOBE>")
    if opts.local_account:
        name = escape(opts.local_account)
        pw = _encode_password(opts.password, "Password")
        shell.append(
            "<UserAccounts><LocalAccounts>"
            '<LocalAccount wcm:action="add">'
            f"<Name>{name}</Name><DisplayName>{name}</DisplayName>"
            "<Group>Administrators</Group>"
            f"<Password><Value>{pw}</Value><PlainText>false</PlainText></Password>"
            "</LocalAccount></LocalAccounts></UserAccounts>")
        # The answer file (and Windows' cached copy) can contain the password
        # in reversible form, so remove both at first logon.
        shell.append(
            "<FirstLogonCommands>"
            '<SynchronousCommand wcm:action="add"><Order>1</Order>'
            r"<CommandLine>cmd /c del /f /q %WINDIR%\System32\Sysprep\unattend.xml "
            r"%WINDIR%\Panther\unattend.xml</CommandLine>"
            "<Description>Remove answer file</Description></SynchronousCommand>"
            "</FirstLogonCommands>")
    if shell:
        oobe_system.append(component("Microsoft-Windows-Shell-Setup", "".join(shell)))

    passes = ""
    if specialize:
        passes += '<settings pass="specialize">' + "".join(specialize) + "</settings>"
    if oobe_system:
        passes += '<settings pass="oobeSystem">' + "".join(oobe_system) + "</settings>"
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           '<unattend xmlns="urn:schemas-microsoft-com:unattend">' + passes + "</unattend>")
    return _pretty(xml)


def _pretty(text):
    from xml.dom import minidom
    return minidom.parseString(text).toprettyxml(indent="  ", encoding="utf-8").decode()


# ------------------------------------------------------------------ entry point

WIM_ARCH_TO_UNATTEND = {"x64": "amd64", "x86": "x86", "arm64": "arm64"}


def apply(volume, edition, opts, workdir, reporter):
    opts.validate()
    if opts.portable or opts.hide_internal_disks or opts.disable_bitlocker or opts.disable_hibernation:
        _edit_hive(volume, SYSTEM_HIVE, workdir, _system_edits(opts, reporter))
    if opts.skip_online_account:
        _edit_hive(volume, SOFTWARE_HIVE, workdir, _software_edits(opts, reporter))
        reporter.log("first-time setup won't require a network or Microsoft account")
    if opts.wants_unattend:
        local = Path(workdir) / "unattend.xml"
        local.write_text(build_unattend(opts, WIM_ARCH_TO_UNATTEND.get(edition.arch, "amd64")),
                         encoding="utf-8")
        run(["ntfscp", "--quiet", "--force", volume, local, UNATTEND_PATH])
        local.unlink()
        if opts.local_account:
            reporter.log(f"local account {opts.local_account!r} will be created at first boot")
