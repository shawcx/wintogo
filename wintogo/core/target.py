"""Partitioning and formatting a target, which is a block device or an image file.

Layout (GPT, UEFI only), matching what Windows Setup creates:

    1  EFI system partition     300 MiB  FAT32
    2  Microsoft reserved        16 MiB  (unformatted)
    3  Basic data (Windows)     rest     NTFS

The disk and partition GUIDs are chosen here, because the BCD store refers to
partitions by them.

For image files nothing is mounted and no root is needed: the ESP is written
in place with mtools, and the NTFS partition is built as a separate sparse
file (mkntfs and wimlib can't write at an offset) and copied in at the end.
On macOS a drive is built the same way and then written out in one pass.
"""

import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .disks import human_size
from .util import WinToGoError, run

MIB = 1 << 20
SECTOR = 512
ESP_MIB = 300
MSR_MIB = 16

TYPE_ESP = "ef00"
TYPE_MSR = "0c01"
TYPE_BASIC_DATA = "0700"


@dataclass
class Layout:
    disk_guid: str = field(default_factory=lambda: str(uuid.uuid4()).upper())
    esp_guid: str = field(default_factory=lambda: str(uuid.uuid4()).upper())
    msr_guid: str = field(default_factory=lambda: str(uuid.uuid4()).upper())
    win_guid: str = field(default_factory=lambda: str(uuid.uuid4()).upper())
    # Filled in after partitioning: (start sector, sector count) per partition.
    extents: dict = field(default_factory=dict)


class Target:
    """Common partitioning logic; subclasses say where the bytes go."""

    def __init__(self, path):
        self.path = str(path)
        self.layout = Layout()

    def partition(self):
        run(["sgdisk", "--zap-all", self.path], check=False)
        lay = self.layout
        run(["sgdisk",
             "--clear",
             f"--disk-guid={lay.disk_guid}",
             f"--new=1:2048:+{ESP_MIB}M", f"--typecode=1:{TYPE_ESP}",
             f"--partition-guid=1:{lay.esp_guid}", "--change-name=1:EFI system partition",
             f"--new=2:0:+{MSR_MIB}M", f"--typecode=2:{TYPE_MSR}",
             f"--partition-guid=2:{lay.msr_guid}", "--change-name=2:Microsoft reserved partition",
             "--new=3:0:0", f"--typecode=3:{TYPE_BASIC_DATA}",
             f"--partition-guid=3:{lay.win_guid}", "--change-name=3:Basic data partition",
             self.path])
        for number in (1, 2, 3):
            info = run(["sgdisk", f"--info={number}", self.path])
            first = last = None
            for line in info.splitlines():
                if line.startswith("First sector:"):
                    first = int(line.split()[2])
                elif line.startswith("Last sector:"):
                    last = int(line.split()[2])
            if first is None or last is None:
                raise WinToGoError(f"could not read partition {number} of {self.path}")
            lay.extents[number] = (first, last - first + 1)

    def format_esp(self):
        raise NotImplementedError

    @property
    def esp_mtools(self):
        """The mtools -i argument that addresses the ESP."""
        raise NotImplementedError

    @property
    def windows_volume(self):
        """Path to hand to mkntfs/wimlib for the Windows partition."""
        raise NotImplementedError

    def format_windows(self, label="Windows"):
        raise NotImplementedError

    def finish(self, reporter):
        pass

    def cleanup(self):
        pass


class DeviceTarget(Target):
    """A real drive. Needs root.

    While we write, desktop automounters must not mount the partitions we
    are creating, so (like GParted) a temporary udev rule marks the drive's
    block devices UDISKS_IGNORE until finish()/cleanup().
    """

    RULES_DIR = Path("/run/udev/rules.d")

    def __init__(self, path):
        super().__init__(path)
        self._rule = self.RULES_DIR / f"90-wintogo-{Path(path).name}.rules"

    def inhibit_automount(self):
        name = Path(self.path).name
        self.RULES_DIR.mkdir(parents=True, exist_ok=True)
        self._rule.write_text(
            "# Temporary: wintogo is writing to this drive.\n"
            f'SUBSYSTEM=="block", KERNEL=="{name}*", ENV{{UDISKS_IGNORE}}="1"\n')
        run(["udevadm", "control", "--reload"], check=False)
        run(["udevadm", "trigger", "--settle", "--subsystem-match=block",
             f"--sysname-match={name}*"], check=False)

    def release_automount(self):
        if self._rule.exists():
            self._rule.unlink()
            run(["udevadm", "control", "--reload"], check=False)
            run(["udevadm", "trigger", "--subsystem-match=block",
                 f"--sysname-match={Path(self.path).name}*"], check=False)

    def partition(self):
        self.inhibit_automount()
        run(["wipefs", "--all", "--quiet", self.path], check=False)
        super().partition()
        run(["udevadm", "settle"], check=False)
        run(["blockdev", "--rereadpt", self.path], check=False)
        run(["udevadm", "settle"], check=False)
        self._parts = self._partition_nodes()

    def _partition_nodes(self):
        # sdb -> sdb1, nvme0n1/mmcblk0 -> ...p1; ask lsblk rather than guess.
        for _ in range(20):
            out = run(["lsblk", "-lnpo", "NAME,TYPE", self.path])
            parts = [line.split()[0] for line in out.splitlines() if line.split()[1:] == ["part"]]
            if len(parts) == 3:
                return parts
            time.sleep(0.25)
        raise WinToGoError(f"partitions on {self.path} did not appear")

    @property
    def esp_mtools(self):
        return self._parts[0]

    def format_esp(self):
        run(["mkfs.vfat", "-F", "32", "-s", "1", "-n", "SYSTEM", self._parts[0]])

    @property
    def windows_volume(self):
        return self._parts[2]

    def format_windows(self, label="Windows"):
        run(["mkntfs", "--quick", "--label", label, self._parts[2]])

    def finish(self, reporter):
        reporter.log("flushing writes to the drive (this can take a while)")
        run(["sync"])
        run(["blockdev", "--flushbufs", self.path], check=False)
        self.release_automount()

    def cleanup(self):
        self.release_automount()


class ImageTarget(Target):
    def __init__(self, path, size_bytes, workdir):
        super().__init__(path)
        self.size_bytes = size_bytes
        self.workdir = Path(workdir)
        self._win_file = self.workdir / "windows-partition.img"

    def partition(self):
        Path(self.path).unlink(missing_ok=True)
        with open(self.path, "wb") as f:
            f.truncate(self.size_bytes)
        super().partition()

    @property
    def esp_mtools(self):
        start, _ = self.layout.extents[1]
        return f"{self.path}@@{start * SECTOR}"

    def format_esp(self):
        start, count = self.layout.extents[1]
        # mkfs.vfat warns that the file is larger than the block count we
        # give it, which is intended; only its exit status matters.
        run(["mkfs.vfat", "-F", "32", "-s", "1", "-n", "SYSTEM",
             f"--offset={start}", self.path, str(count * SECTOR // 1024)])

    @property
    def windows_volume(self):
        return str(self._win_file)

    def format_windows(self, label="Windows"):
        start, count = self.layout.extents[3]
        self._win_file.unlink(missing_ok=True)
        with open(self._win_file, "wb") as f:
            f.truncate(count * SECTOR)
        # -p/-H/-S fill in the boot sector geometry; -p must match the real
        # start sector or Windows won't mount the volume.
        run(["mkntfs", "--quick", "--force", "--label", label, "--sector-size", "512",
             "--partition-start", str(start), "--heads", "255", "--sectors-per-track", "63",
             self._win_file])

    def finish(self, reporter):
        start, _ = self.layout.extents[3]
        reporter.log("copying the Windows partition into the image")
        sparse_copy(self._win_file, self.path, start * SECTOR, reporter)
        self._win_file.unlink()

    def cleanup(self):
        # A half-built image is useless; don't leave a multi-GB file behind.
        self._win_file.unlink(missing_ok=True)
        Path(self.path).unlink(missing_ok=True)


class StagedDeviceTarget(ImageTarget):
    """A real drive on macOS. Needs root.

    macOS mounts filesystems as soon as they appear on a drive, and new
    partitions only appear once the drive is closed after writing. So the
    drive is built as a sparse image of the same size, its data is written
    to the raw device in one pass while the drive is unmounted, and then the
    drive is ejected.
    """

    def __init__(self, disk, workdir):
        super().__init__(Path(workdir) / "disk.img", disk.size, workdir)
        self.device = disk.path
        # The raw node bypasses the buffer cache, which is much faster.
        self.raw_device = disk.path.replace("/dev/disk", "/dev/rdisk", 1)

    def finish(self, reporter):
        reporter.step("write", "Writing to the drive")
        win_start, _ = self.layout.extents[3]
        # The image holds the partition tables and the ESP; the Windows
        # partition is still in its own file.
        sources = [(Path(self.path), 0), (self._win_file, win_start * SECTOR)]
        total = sum(length for src, _ in sources for _, length in file_extents(src))
        done = 0

        def progress(n):
            nonlocal done
            done += n
            reporter.progress(done / total, f"{human_size(done)} of {human_size(total)}")

        run(["diskutil", "unmountDisk", "force", self.device])
        fd = os.open(self.raw_device, os.O_WRONLY)
        try:
            for src, offset in sources:
                sparse_copy_fd(src, fd, offset, progress)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._win_file.unlink()
        Path(self.path).unlink()
        # Closing the device made macOS read the new partition table, and it
        # may mount what it found; ejecting unmounts and flushes the drive.
        for _ in range(5):
            try:
                run(["diskutil", "eject", self.device])
                reporter.log("ejected the drive; it can be unplugged")
                return
            except WinToGoError as e:
                error = e
                time.sleep(1)
        reporter.warning(f"could not eject {self.device}; eject it before unplugging it ({error})")


CHUNK = 8 << 20


def file_extents(path):
    """The (offset, length) of each run of data in a sparse file."""
    with open(path, "rb") as f:
        return list(_data_extents(f.fileno(), os.path.getsize(path)))


def _data_extents(fd, size):
    pos = 0
    while pos < size:
        try:
            data = os.lseek(fd, pos, os.SEEK_DATA)
        except OSError:  # ENXIO: only a hole remains
            return
        hole = os.lseek(fd, data, os.SEEK_HOLE)
        yield data, hole - data
        pos = hole


def _copy_range(sfd, dfd, offset, length, dst_offset, progress=None):
    """Copy length bytes at offset in sfd to dst_offset + offset in dfd."""
    while length > 0:
        if hasattr(os, "copy_file_range"):  # Linux: done in the kernel
            n = os.copy_file_range(sfd, dfd, min(length, 1 << 30), offset, dst_offset + offset)
        else:
            data = os.pread(sfd, min(length, CHUNK), offset)
            n = os.pwrite(dfd, data, dst_offset + offset) if data else 0
        if n == 0:
            raise WinToGoError("short copy while assembling the image")
        length -= n
        offset += n
        if progress:
            progress(n)


def sparse_copy_fd(src, dfd, dst_offset, progress=None):
    """Copy the data in src to dfd at dst_offset, skipping holes."""
    with open(src, "rb") as fin:
        sfd = fin.fileno()
        for offset, length in _data_extents(sfd, os.path.getsize(src)):
            _copy_range(sfd, dfd, offset, length, dst_offset, progress)


def sparse_copy(src, dst, dst_offset, reporter=None):
    """Copy src into dst at dst_offset, skipping holes so dst stays sparse."""
    size = os.path.getsize(src)
    with open(src, "rb") as fin, open(dst, "r+b") as fout:
        sfd, dfd = fin.fileno(), fout.fileno()
        for offset, length in _data_extents(sfd, size):
            _copy_range(sfd, dfd, offset, length, dst_offset)
            if reporter:
                reporter.progress((offset + length) / size)
