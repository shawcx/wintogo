"""The create pipeline: ISO + edition -> bootable Windows To Go drive or image.

With vhdx=True, Windows goes into a virtual disk file on the drive instead
of straight onto its NTFS partition (see vhdx.py).
"""

import os
import shutil
import stat
import tempfile
from pathlib import Path

from . import bootfiles, customize, disks, iso, vhdx as vhdx_mod, wim
from .target import ESP_MIB, MIB, MSR_MIB, SECTOR, DeviceTarget, ImageTarget, StagedDeviceTarget
from .util import IS_MACOS, WinToGoError, require

GIB = 1 << 30
# Room on top of the applied files for the pagefile, first-boot setup,
# updates and a little user data. A drive smaller than this is refused.
HEADROOM = 8 * GIB
RECOMMENDED_MIN = 64 * 1000**3

REQUIRED_COMMANDS = ("wimlib-imagex", "sgdisk", "mkfs.vfat", "mcopy", "mkntfs", "ntfscat", "ntfscp")
# macOS attaches the ISO itself; Linux reads it with 7z unless running as root.
REQUIRED_COMMANDS += ("diskutil",) if IS_MACOS else ("7z",)


def is_device(path):
    """A disk node: a block device, or on macOS also its raw /dev/rdiskN twin."""
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISBLK(mode) or stat.S_ISCHR(mode)


def pick_edition(editions, choice):
    """choice is an index ("6") or a case-insensitive name ("Windows 11 Pro")."""
    choice = str(choice).strip()
    if choice.isdigit():
        for e in editions:
            if e.index == int(choice):
                return e
    else:
        for e in editions:
            if e.name.lower() == choice.lower():
                return e
    names = "\n".join(f"  {e.index}: {e.name}" for e in editions)
    raise WinToGoError(f"no edition {choice!r} in this ISO; available:\n{names}")


def required_bytes(edition, vhdx=False):
    need = (ESP_MIB + MSR_MIB + 2) * MIB + edition.installed_bytes + HEADROOM
    return need + vhdx_mod.OVERHEAD if vhdx else need


def _check_staging_space(work, edition, vhdx=False):
    # The drive is built in a sparse file first, which grows to about the
    # size of the installed files; a virtual disk is built before that.
    need = edition.installed_bytes + 2 * GIB
    if vhdx:
        need += vhdx_mod.staging_bytes(edition.installed_bytes)
    free = shutil.disk_usage(work).free
    if free < need:
        raise WinToGoError(f"the drive is assembled in {work.parent} first, which needs "
                           f"{disks.human_size(need)} free but has {disks.human_size(free)}; "
                           "use --workdir to put it elsewhere")


def create(iso_path, edition_choice, output, reporter, *, options=None,
           image_size=None, workdir=None, allow_non_usb=False, vhdx=False):
    require(*REQUIRED_COMMANDS, *(vhdx_mod.REQUIRED_COMMANDS if vhdx else ()))
    options = options or customize.Options()
    options.validate()
    os.environ["MTOOLS_SKIP_CHECK"] = "1"
    device = is_device(output)

    if device:
        disk = disks.check_target(output, allow_non_usb=allow_non_usb)
        base = workdir or "/var/tmp"
        if IS_MACOS and disk.sector_size != 512:
            raise WinToGoError(f"{disk.path} has {disk.sector_size}-byte sectors; "
                               "only drives with 512-byte sectors are supported")
    else:
        disk = None
        if Path(output).resolve().is_relative_to("/dev") or (
                os.path.lexists(output) and not os.path.isfile(output)):
            raise WinToGoError(f"{output} is neither a disk nor an image file")
        # Image builds extract install.wim (several GB) and stage the Windows
        # partition, so work next to the output rather than in a tmpfs /tmp.
        base = workdir or Path(output).resolve().parent
    work = Path(tempfile.mkdtemp(prefix="wintogo.", dir=base))
    target = vdisk = None

    try:
        reporter.step("source", "Reading the Windows image")
        with iso.open_install_image(iso_path, work, reporter) as wim_path:
            edition = pick_edition(wim.read_editions(wim_path), edition_choice)
            need = required_bytes(edition, vhdx)
            reporter.log(f"{edition.name}, build {edition.build}, "
                         f"{edition.installed_bytes / GIB:.1f} GiB of files")

            if device:
                if disk.size < need:
                    raise WinToGoError(f"{output} is too small: {disks.human_size(disk.size)}, "
                                       f"need at least {disks.human_size(need)}")
                if disk.size < RECOMMENDED_MIN:
                    reporter.warning(f"{disks.human_size(disk.size)} leaves little free space; "
                                     "64 GB or more is recommended")
                if IS_MACOS:
                    _check_staging_space(work, edition, vhdx)
                    target = StagedDeviceTarget(disk, work)
                else:
                    if vhdx:
                        vhdx_mod.check_space(work, edition.installed_bytes)
                    target = DeviceTarget(output)
            else:
                size = image_size or need
                if size < need:
                    raise WinToGoError(f"image size {size / GIB:.1f} GiB is too small; "
                                       f"need at least {need / GIB:.1f} GiB")
                if vhdx:
                    vhdx_mod.check_space(work, edition.installed_bytes)
                target = ImageTarget(output, size, work)

            reporter.step("partition", "Partitioning and formatting")
            target.partition()
            target.format_esp()
            # With a virtual disk, Windows' own C: is also labelled Windows.
            target.format_windows(label=vhdx_mod.HOST_LABEL if vhdx else "Windows")
            windows_volume = target.windows_volume
            if vhdx:
                _, host_sectors = target.layout.extents[3]
                vdisk = vhdx_mod.VirtualDisk(vhdx_mod.virtual_size(host_sectors * SECTOR), work)
                reporter.log(f"creating a {disks.human_size(vdisk.size_bytes)} virtual disk, "
                             f"\\{vhdx_mod.FILE_NAME}")
                vdisk.create(vhdx_mod.apply_size(edition.installed_bytes))
                windows_volume = vdisk.windows_volume

            reporter.step("apply", f"Installing {edition.name}")
            wim.apply_image(wim_path, edition.index, windows_volume, reporter)

            reporter.step("customize", "Configuring Windows")
            customize.apply(windows_volume, edition, options, work, reporter)
            if vdisk:
                customize.prepare_vhd_boot(windows_volume, work, reporter)

            reporter.step("boot", "Installing boot files")
            bootfiles.install(wim_path, edition, target, work, reporter, vdisk=vdisk)

        if vdisk:
            reporter.step("vhdx", "Building the virtual disk")
            vdisk.build(reporter)
            reporter.step("copy", "Copying the virtual disk into place")
            vdisk.copy_to(target.windows_volume, reporter)

        reporter.step("finish", "Finishing")
        target.finish(reporter)
    except BaseException:
        if target is not None:
            target.cleanup()
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
