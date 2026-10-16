"""Finding and reading the Windows image inside an installation ISO."""

import contextlib
import os
import platform
import plistlib
import subprocess
import tempfile
from pathlib import Path

from . import wim
from .util import IS_MACOS, WinToGoError, run

INSTALL_IMAGES = ("sources/install.wim", "sources/install.esd")


def _check_iso(iso_path):
    if not Path(iso_path).is_file():
        raise WinToGoError(f"ISO not found: {iso_path}")


def _no_install_image(iso_path):
    return WinToGoError(f"{iso_path} has no sources/install.wim or install.esd; "
                        "is it a Windows installation ISO?")


def list_files(iso_path):
    out = run(["7z", "l", "-slt", "-ba", iso_path])
    return [line[len("Path = "):] for line in out.splitlines() if line.startswith("Path = ")]


def find_install_image(iso_path):
    """Return the install image's path inside the ISO, as the ISO spells it.

    Windows ISOs are UDF and letter case varies between releases, so match
    case-insensitively.
    """
    _check_iso(iso_path)
    by_lower = {p.lower(): p for p in list_files(iso_path)}
    for candidate in INSTALL_IMAGES:
        if candidate in by_lower:
            return by_lower[candidate]
    raise _no_install_image(iso_path)


def read_editions(iso_path):
    """List the editions in the ISO without extracting anything."""
    if IS_MACOS:
        # Attaching is quick and needs no root, and the WIM's metadata can
        # then be read directly instead of streaming the whole file.
        with _attached(iso_path) as root:
            return wim.read_editions(_install_image_in(root, iso_path))
    inner = find_install_image(iso_path)
    proc = subprocess.Popen(["7z", "x", "-so", str(iso_path), inner],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        return wim.read_editions_from_stream(proc.stdout)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


@contextlib.contextmanager
def open_install_image(iso_path, workdir, reporter):
    """Yield a filesystem path to the ISO's install image.

    On macOS the ISO is attached read-only, and on Linux as root it is
    loop-mounted, which is instant. Otherwise (image-file builds on Linux)
    the image is extracted into workdir with 7z.
    """
    if IS_MACOS:
        with _attached(iso_path, workdir) as root:
            yield _install_image_in(root, iso_path)
        return
    inner = find_install_image(iso_path)
    if os.geteuid() == 0:
        mountpoint = tempfile.mkdtemp(prefix="iso.", dir=workdir)
        run(["mount", "-o", "loop,ro", iso_path, mountpoint])
        try:
            # The mounted UDF volume can spell the path differently again.
            path = _find_case_insensitive(Path(mountpoint), inner)
            if path is None:
                raise WinToGoError(f"{inner} not found in mounted ISO")
            yield path
        finally:
            run(["umount", mountpoint], check=False)
            os.rmdir(mountpoint)
    else:
        reporter.log(f"extracting {inner} from the ISO")
        dest = Path(workdir) / "iso"
        run(["7z", "x", "-y", "-bso0", "-bsp0", f"-o{dest}", iso_path, inner])
        path = dest / inner
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)


@contextlib.contextmanager
def _attached(iso_path, workdir=None):
    """macOS: attach the ISO read-only, hidden from Finder; yield its root."""
    _check_iso(iso_path)
    parent = tempfile.mkdtemp(prefix="iso.", dir=workdir)
    mountpoint = os.path.join(parent, "mnt")  # created and removed by the attach
    # hdiutil is deprecated from macOS 26, which has `diskutil image` instead.
    if int(platform.mac_ver()[0].split(".")[0] or 0) >= 26:
        cmd = ["diskutil", "image", "attach", "--plist", "--readOnly", "--nobrowse",
               "--mountPoint", mountpoint, iso_path]
    else:
        cmd = ["hdiutil", "attach", "-plist", "-readonly", "-nobrowse", "-noautoopen",
               "-noverify", "-mountpoint", mountpoint, iso_path]
    try:
        entities = plistlib.loads(run(cmd).encode())["system-entities"]
    except WinToGoError as e:
        os.rmdir(parent)
        raise WinToGoError(f"could not open {iso_path}: {e}") from None
    device = next(e["dev-entry"] for e in entities if "dev-entry" in e)
    try:
        yield Path(mountpoint)
    finally:
        run(["diskutil", "eject", device], check=False)
        with contextlib.suppress(OSError):
            os.rmdir(parent)


def _install_image_in(root, iso_path):
    for candidate in INSTALL_IMAGES:
        path = _find_case_insensitive(root, candidate)
        if path is not None:
            return path
    raise _no_install_image(iso_path)


def _find_case_insensitive(base, relative):
    path = base
    for part in Path(relative).parts:
        matches = [p for p in path.iterdir() if p.name.lower() == part.lower()]
        if not matches:
            return None
        path = matches[0]
    return path
