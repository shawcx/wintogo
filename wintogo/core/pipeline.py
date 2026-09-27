"""The create pipeline: ISO + edition -> bootable Windows To Go drive or image."""

import os
import shutil
import stat
import tempfile
from pathlib import Path

from . import bootfiles, customize, disks, iso, wim
from .target import ESP_MIB, MIB, MSR_MIB, DeviceTarget, ImageTarget
from .util import WinToGoError, require

GIB = 1 << 30
# Room on top of the applied files for the pagefile, first-boot setup,
# updates and a little user data. A drive smaller than this is refused.
HEADROOM = 8 * GIB
RECOMMENDED_MIN = 64 * 1000**3

REQUIRED_COMMANDS = ("7z", "wimlib-imagex", "sgdisk", "mkfs.vfat", "mcopy", "mkntfs", "ntfscat", "ntfscp")


def is_block_device(path):
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except FileNotFoundError:
        return False


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


def required_bytes(edition):
    return (ESP_MIB + MSR_MIB + 2) * MIB + edition.installed_bytes + HEADROOM


def create(iso_path, edition_choice, output, reporter, *, options=None,
           image_size=None, workdir=None, allow_non_usb=False):
    require(*REQUIRED_COMMANDS)
    options = options or customize.Options()
    options.validate()
    os.environ["MTOOLS_SKIP_CHECK"] = "1"
    device = is_block_device(output)

    if device:
        disk = disks.check_target(output, allow_non_usb=allow_non_usb)
        base = workdir or "/var/tmp"
    else:
        disk = None
        # Image builds extract install.wim (several GB) and stage the Windows
        # partition, so work next to the output rather than in a tmpfs /tmp.
        base = workdir or Path(output).resolve().parent
    work = Path(tempfile.mkdtemp(prefix="wintogo.", dir=base))
    target = None

    try:
        reporter.step("source", "Reading the Windows image")
        with iso.open_install_image(iso_path, work, reporter) as wim_path:
            edition = pick_edition(wim.read_editions(wim_path), edition_choice)
            need = required_bytes(edition)
            reporter.log(f"{edition.name}, build {edition.build}, "
                         f"{edition.installed_bytes / GIB:.1f} GiB of files")

            if device:
                if disk.size < need:
                    raise WinToGoError(f"{output} is too small: {disks.human_size(disk.size)}, "
                                       f"need at least {disks.human_size(need)}")
                if disk.size < RECOMMENDED_MIN:
                    reporter.warning(f"{disks.human_size(disk.size)} leaves little free space; "
                                     "64 GB or more is recommended")
                target = DeviceTarget(output)
            else:
                size = image_size or need
                if size < need:
                    raise WinToGoError(f"image size {size / GIB:.1f} GiB is too small; "
                                       f"need at least {need / GIB:.1f} GiB")
                target = ImageTarget(output, size, work)

            reporter.step("partition", "Partitioning and formatting")
            target.partition()
            target.format_esp()
            target.format_windows()

            reporter.step("apply", f"Installing {edition.name}")
            wim.apply_image(wim_path, edition.index, target.windows_volume, reporter)

            reporter.step("customize", "Configuring Windows")
            customize.apply(target.windows_volume, edition, options, work, reporter)

            reporter.step("boot", "Installing boot files")
            bootfiles.install(wim_path, edition, target, work, reporter)

        reporter.step("finish", "Finishing")
        target.finish(reporter)
    except BaseException:
        if target is not None:
            target.cleanup()
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
