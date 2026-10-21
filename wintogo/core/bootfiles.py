"""Populating the ESP, i.e. what `bcdboot <win> /s <esp> /f UEFI` would do.

bcdboot copies Windows\\Boot\\EFI (minus boot.pnd.stl), Fonts and Resources
into \\EFI\\Microsoft\\Boot, copies bootmgfw.efi to the removable-media
fallback path \\EFI\\Boot\\bootx64.efi, and writes the BCD store. The files
are taken straight from the WIM rather than from the applied volume.
"""

import shutil
from pathlib import Path

from . import bcd, wim
from .util import run

SKIP = {"boot.pnd.stl"}


def install(wim_path, edition, target, workdir, reporter, vdisk=None):
    src = Path(workdir) / "winboot"
    shutil.rmtree(src, ignore_errors=True)
    src.mkdir(parents=True)
    wim.extract_paths(wim_path, edition.index, [
        "/Windows/Boot/EFI",
        "/Windows/Boot/Fonts",
        "/Windows/Boot/Resources",
        "/Windows/System32/config/BCD-Template",
    ], src)
    # wimlib extracts each requested path into dest-dir under its basename.
    stage = Path(workdir) / "esp"
    shutil.rmtree(stage, ignore_errors=True)
    boot_dir = stage / "EFI" / "Microsoft" / "Boot"
    shutil.copytree(src / "EFI", boot_dir, ignore=lambda d, names: [n for n in names if n in SKIP])
    shutil.copytree(src / "Fonts", boot_dir / "Fonts")
    shutil.copytree(src / "Resources", boot_dir / "Resources")
    (stage / "EFI" / "Boot").mkdir(parents=True)
    shutil.copy2(boot_dir / "bootmgfw.efi", stage / "EFI" / "Boot" / "bootx64.efi")

    layout = target.layout
    win_device = None
    if vdisk:
        # Windows is in the virtual disk file on the drive's Windows partition.
        win_device = bcd.vhd_device(vdisk.win_guid, vdisk.disk_guid,
                                    layout.win_guid, layout.disk_guid, "\\" + vdisk.file_name)
    objects = bcd.build_objects(
        esp_part_guid=layout.esp_guid,
        win_part_guid=layout.win_guid,
        disk_guid=layout.disk_guid,
        description="Windows 11" if edition.is_windows_11 else "Windows 10",
        locale=edition.language.lower(),
        win_device=win_device,
    )
    base = src / "BCD-Template"
    bcd.write_store(base, boot_dir / "BCD", objects)
    reporter.log("generated BCD store")

    run(["mcopy", "-s", "-Q", "-o", "-i", target.esp_mtools, stage / "EFI", "::/"])
    shutil.rmtree(src)
    shutil.rmtree(stage)
