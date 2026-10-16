"""Finding candidate USB disks and refusing unsafe targets."""

import json
import plistlib
import re
from dataclasses import dataclass, field

from .util import IS_MACOS, WinToGoError, run

LSBLK_COLUMNS = "NAME,PATH,TYPE,SIZE,MODEL,VENDOR,SERIAL,TRAN,RM,RO,HOTPLUG,MOUNTPOINTS"
LINUX_SYSTEM_MOUNTS = ("/", "/boot", "/boot/efi", "/home", "/usr", "/var")


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
    sector_size: int = 512

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
    return _list_disks_macos() if IS_MACOS else _list_disks_linux()


def _list_disks_linux():
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


def _diskutil(verb, *args):
    return plistlib.loads(run(["diskutil", verb, "-plist", *args]).encode())


def _list_disks_macos():
    listing = _diskutil("list")
    whole = _diskutil("list", "physical")["WholeDisks"]
    return parse_diskutil(listing, {d: _diskutil("info", d) for d in whole})


def _whole_disk(identifier):
    m = re.match(r"disk\d+", identifier)
    return m.group(0) if m else identifier


def parse_diskutil(listing, infos):
    """Disks from `diskutil list -plist` and `diskutil info -plist` of each
    whole physical disk ({"disk4": info, ...})."""
    mounts = {}
    for entry in listing.get("AllDisksAndPartitions", []):
        # An APFS container is a disk of its own; its volumes count as
        # mounted on the disk that holds its physical store.
        stores = [s["DeviceIdentifier"] for s in entry.get("APFSPhysicalStores", [])]
        owners = {_whole_disk(s) for s in stores} or {entry["DeviceIdentifier"]}
        points = [entry.get("MountPoint")]
        points += [p.get("MountPoint") for p in entry.get("Partitions", []) + entry.get("APFSVolumes", [])]
        for owner in owners:
            mounts.setdefault(owner, []).extend(p for p in points if p)

    disks = []
    for identifier, info in infos.items():
        model = info.get("MediaName") or re.sub(r" Media$", "", info.get("IORegistryEntryName") or "")
        disks.append(Disk(
            path=info.get("DeviceNode") or f"/dev/{identifier}",
            size=int(info.get("TotalSize") or info.get("Size") or 0),
            model=model.strip(),
            vendor="",
            serial="",
            transport=(info.get("BusProtocol") or "").lower(),
            removable=bool(info.get("RemovableMedia")),
            read_only=not info.get("WritableMedia", True),
            hotplug=bool(info.get("Ejectable")),
            mountpoints=mounts.get(identifier, []),
            sector_size=int(info.get("DeviceBlockSize") or 512),
        ))
    return disks


def list_usb_disks():
    return [d for d in list_disks() if d.is_usb and not d.read_only and d.size > 0]


def whole_disk_path(path):
    """macOS has a raw node per disk; /dev/rdisk4 is the same disk as /dev/disk4."""
    return re.sub(r"^/dev/rdisk", "/dev/disk", path) if IS_MACOS else path


def get_disk(path):
    path = whole_disk_path(path)
    for disk in list_disks():
        if disk.path == path:
            return disk
    example = "/dev/disk4, not /dev/disk4s1" if IS_MACOS else "/dev/sdb, not /dev/sdb1"
    raise WinToGoError(f"{path} is not a whole disk (pass e.g. {example})")


def _is_system_mount(mountpoint):
    if IS_MACOS:
        return mountpoint == "/" or mountpoint.startswith("/System/Volumes/")
    return mountpoint in LINUX_SYSTEM_MOUNTS or mountpoint.startswith("[SWAP]")


def check_target(path, *, allow_non_usb=False):
    """Return the Disk for path, or raise if it must not be written."""
    disk = get_disk(path)
    path = disk.path
    if disk.read_only:
        raise WinToGoError(f"{path} is read-only")
    if disk.mountpoints:
        if any(_is_system_mount(m) for m in disk.mountpoints):
            raise WinToGoError(f"{path} holds this system's filesystems; refusing to touch it")
        hint = f" (diskutil unmountDisk {path})" if IS_MACOS else ""
        raise WinToGoError(f"{path} has mounted filesystems ({', '.join(disk.mountpoints)}); "
                           f"unmount them first{hint}")
    if not disk.is_usb and not allow_non_usb:
        raise WinToGoError(f"{path} is not a USB disk (transport: {disk.transport or 'unknown'})")
    return disk
