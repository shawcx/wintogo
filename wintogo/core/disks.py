"""Finding candidate USB disks and refusing unsafe targets."""

import json
from dataclasses import dataclass, field

from .util import WinToGoError, run

LSBLK_COLUMNS = "NAME,PATH,TYPE,SIZE,MODEL,VENDOR,SERIAL,TRAN,RM,RO,HOTPLUG,MOUNTPOINTS"


@dataclass
class Disk:
    path: str
    size: int
    model: str
    vendor: str
    serial: str
    transport: str
    removable: bool
    read_only: bool
    hotplug: bool
    mountpoints: list = field(default_factory=list)

    @property
    def is_usb(self):
        return self.transport == "usb"

    @property
    def label(self):
        name = " ".join(x for x in (self.vendor, self.model) if x) or self.path
        return f"{name} — {human_size(self.size)} ({self.path})"


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def _mountpoints(node):
    points = [m for m in node.get("mountpoints") or [] if m]
    for child in node.get("children") or []:
        points += _mountpoints(child)
    return points


def list_disks():
    out = run(["lsblk", "--json", "--bytes", "--tree", "-o", LSBLK_COLUMNS])
    disks = []
    for node in json.loads(out)["blockdevices"]:
        if node.get("type") != "disk":
            continue
        disks.append(Disk(
            path=node["path"],
            size=int(node.get("size") or 0),
            model=(node.get("model") or "").strip(),
            vendor=(node.get("vendor") or "").strip(),
            serial=(node.get("serial") or "").strip(),
            transport=node.get("tran") or "",
            removable=bool(node.get("rm")),
            read_only=bool(node.get("ro")),
            hotplug=bool(node.get("hotplug")),
            mountpoints=_mountpoints(node),
        ))
    return disks


def list_usb_disks():
    return [d for d in list_disks() if d.is_usb and not d.read_only and d.size > 0]


def get_disk(path):
    for disk in list_disks():
        if disk.path == path:
            return disk
    raise WinToGoError(f"{path} is not a whole disk (pass e.g. /dev/sdb, not /dev/sdb1)")


def check_target(path, *, allow_non_usb=False):
    """Return the Disk for path, or raise if it must not be written."""
    disk = get_disk(path)
    if disk.read_only:
        raise WinToGoError(f"{path} is read-only")
    if disk.mountpoints:
        if any(m in ("/", "/boot", "/boot/efi", "/home", "/usr", "/var") or m.startswith("[SWAP]")
               for m in disk.mountpoints):
            raise WinToGoError(f"{path} holds this system's filesystems; refusing to touch it")
        raise WinToGoError(f"{path} has mounted filesystems ({', '.join(disk.mountpoints)}); "
                           "unmount them first")
    if not disk.is_usb and not allow_non_usb:
        raise WinToGoError(f"{path} is not a USB disk (transport: {disk.transport or 'unknown'})")
    return disk
