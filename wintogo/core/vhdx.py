"""Windows inside a VHDX file on the drive (native VHD boot).

The drive keeps the usual layout, but its NTFS partition holds a single
\\windows.vhdx. Inside that is a GPT disk with one NTFS partition, which is
where Windows is applied. The BCD store boots the file in place, so the
whole installation can be backed up, replaced or kept in several copies.

The virtual disk is built without mounting anything: its partition is a
sparse file formatted and filled like the Windows partition of an image
build, and qemu-img writes it into a dynamic VHDX behind the partition
table. ntfscp then copies the VHDX onto the drive's NTFS partition.

ntfs-3g spreads files over the whole volume, which would allocate nearly
every block of the dynamic VHDX. So Windows is applied to a volume just big
enough for it, which is then grown to fill the partition.
"""

import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from .disks import human_size
from .target import MIB, SECTOR, TYPE_BASIC_DATA
from .util import IS_MACOS, WinToGoError, run

GIB = 1 << 30
FILE_NAME = "windows.vhdx"
# The label of the drive's partition that holds it.
HOST_LABEL = "WinToGo"
# Where the Windows partition starts inside the virtual disk.
PARTITION_START = 2048
# Kept free on the drive's partition around the virtual disk: the dynamic
# VHDX can grow to its full size plus its own metadata, and if the host
# volume fills up first Windows stops with a bug check.
RESERVE_FRACTION = 100
RESERVE_MIN = GIB
# What the drive needs on top of a plain install.
OVERHEAD = 2 * GIB

REQUIRED_COMMANDS = ("qemu-img", "ntfsresize", "ntfsfix")


def apply_size(installed_bytes):
    """Room to apply Windows in: its files plus NTFS metadata and slack."""
    return (installed_bytes + installed_bytes // 10 + GIB) // MIB * MIB


def virtual_size(host_bytes):
    """The largest virtual disk that can never outgrow a host volume of this size."""
    reserve = max(RESERVE_MIN, host_bytes // RESERVE_FRACTION)
    return (host_bytes - reserve) // MIB * MIB


class VirtualDisk:
    file_name = FILE_NAME

    def __init__(self, size_bytes, workdir):
        self.size_bytes = size_bytes
        self.workdir = Path(workdir)
        self.disk_guid = str(uuid.uuid4()).upper()
        self.win_guid = str(uuid.uuid4()).upper()
        self.path = self.workdir / FILE_NAME
        self._table = self.workdir / "vhdx-table.img"
        self._win_file = self.workdir / "vhdx-partition.img"
        self._win_sectors = None

    @property
    def windows_volume(self):
        return str(self._win_file)

    def create(self, fill_bytes):
        """Partition the virtual disk and format its Windows partition, at
        first only fill_bytes big (see grow())."""
        # sgdisk writes both partition tables into a sparse file of the full
        # size, which costs nothing; only those sectors end up in the VHDX.
        self._table.unlink(missing_ok=True)
        with open(self._table, "wb") as f:
            f.truncate(self.size_bytes)
        run(["sgdisk", "--clear", f"--disk-guid={self.disk_guid}",
             f"--new=1:{PARTITION_START}:0", f"--typecode=1:{TYPE_BASIC_DATA}",
             f"--partition-guid=1:{self.win_guid}", "--change-name=1:Basic data partition",
             self._table])
        info = run(["sgdisk", "--info=1", self._table])
        last = next(int(line.split()[2]) for line in info.splitlines()
                    if line.startswith("Last sector:"))
        self._win_sectors = last - PARTITION_START + 1

        self._win_file.unlink(missing_ok=True)
        with open(self._win_file, "wb") as f:
            f.truncate(min(fill_bytes, self._win_sectors * SECTOR))
        run(["mkntfs", "--quick", "--force", "--label", "Windows", "--sector-size", "512",
             "--partition-start", str(PARTITION_START), "--heads", "255",
             "--sectors-per-track", "63", self._win_file])

    def grow(self):
        """Grow the filled volume to the whole partition.

        ntfsresize marks the volume for a check by Windows as a precaution;
        clear that, so the first boot doesn't start with chkdsk.
        """
        with open(self._win_file, "r+b") as f:
            f.truncate(self._win_sectors * SECTOR)
        run(["ntfsresize", "--force", "--no-progress-bar", self._win_file], input="")
        run(["ntfsfix", "--clear-dirty", self._win_file])

    def build(self, reporter):
        """Turn the partition table and the filled partition into the VHDX."""
        self.grow()
        self.path.unlink(missing_ok=True)
        run(["qemu-img", "convert", "-f", "raw", "-O", "vhdx", "-o", "subformat=dynamic",
             self._table, self.path])
        self._table.unlink()
        # Written through a raw layer at the partition's offset, so the
        # partition never has to be copied into a whole-disk file first. The
        # new VHDX reads as zeros, so the holes in the partition are skipped.
        target = ",".join([
            "driver=raw", f"offset={PARTITION_START * SECTOR}",
            f"size={self._win_sectors * SECTOR}",
            # qemu's option syntax doubles commas in values.
            "file.driver=vhdx", "file.file.filename=" + str(self.path).replace(",", ",,")])
        _run_with_progress(["qemu-img", "convert", "-p", "-n", "--target-is-zero",
                            "-f", "raw", self._win_file, "--target-image-opts", target],
                           reporter)
        self._win_file.unlink()

    def copy_to(self, host_volume, reporter):
        """Copy the VHDX into the root of an unmounted NTFS volume."""
        size = self.path.stat().st_size
        proc = subprocess.Popen(["ntfscp", "--quiet", host_volume, str(self.path), "/" + FILE_NAME],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        done = threading.Event()
        if not IS_MACOS:
            threading.Thread(target=_watch_reads, args=(proc.pid, size, done, reporter),
                             daemon=True).start()
        try:
            _, err = proc.communicate()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            done.set()
        if proc.returncode != 0:
            raise WinToGoError(f"copying {FILE_NAME} to the drive failed: "
                               f"{err.decode(errors='replace').strip()}")
        self.path.unlink()


def _watch_reads(pid, size, done, reporter):
    # ntfscp has no progress output; how much of the file it has read so
    # far is in /proc/PID/io (rchar also counts a little volume metadata).
    while not done.wait(1):
        try:
            text = Path(f"/proc/{pid}/io").read_text()
        except OSError:
            return
        for line in text.splitlines():
            if line.startswith("rchar:"):
                read = int(line.split()[1])
                reporter.progress(min(read / size, 1.0), _gib_progress(read, size))


def _gib_progress(done, total):
    return f"{min(done, total) / GIB:.1f} GiB of {total / GIB:.1f} GiB"


def _run_with_progress(cmd, reporter):
    """Run qemu-img -p, relaying its "(12.34/100%)" progress lines."""
    proc = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail, buf = [], ""
    try:
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                break
            if ch not in "\r\n":
                buf += ch
                continue
            line, buf = buf.strip(), ""
            if line.startswith("(") and line.endswith("%)"):
                reporter.progress(float(line[1:-2].split("/")[0]) / 100)
            elif line:
                tail = (tail + [line])[-20:]
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        proc.stdout.close()
    if proc.wait() != 0:
        raise WinToGoError(f"{cmd[0]} failed (exit {proc.returncode}): " + "\n".join(tail))


def staging_bytes(installed_bytes):
    """Work space a build needs: the partition and the VHDX made from it."""
    return 2 * installed_bytes + 2 * GIB


def check_space(work, installed_bytes):
    need = staging_bytes(installed_bytes)
    free = shutil.disk_usage(work).free
    if free < need:
        raise WinToGoError(f"the virtual disk is built in {Path(work).parent} first, which needs "
                           f"{human_size(need)} free but has {human_size(free)}; "
                           "use --workdir to put it elsewhere")

