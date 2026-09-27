"""Finding and reading the Windows image inside an installation ISO."""

import contextlib
import os
import subprocess
import tempfile
from pathlib import Path

from . import wim
from .util import WinToGoError, run

INSTALL_IMAGES = ("sources/install.wim", "sources/install.esd")


def list_files(iso_path):
    out = run(["7z", "l", "-slt", "-ba", iso_path])
    return [line[len("Path = "):] for line in out.splitlines() if line.startswith("Path = ")]


def find_install_image(iso_path):
    """Return the install image's path inside the ISO, as the ISO spells it.

    Windows ISOs are UDF and letter case varies between releases, so match
    case-insensitively.
    """
    if not Path(iso_path).is_file():
        raise WinToGoError(f"ISO not found: {iso_path}")
    by_lower = {p.lower(): p for p in list_files(iso_path)}
    for candidate in INSTALL_IMAGES:
        if candidate in by_lower:
            return by_lower[candidate]
    raise WinToGoError(f"{iso_path} has no sources/install.wim or install.esd; "
                       "is it a Windows installation ISO?")


def read_editions(iso_path):
    """List the editions in the ISO without extracting or mounting anything."""
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

    As root the ISO is loop-mounted read-only, which is instant. Otherwise
    (image-file builds) the image is extracted into workdir with 7z.
    """
    inner = find_install_image(iso_path)
    if os.geteuid() == 0:
        mountpoint = tempfile.mkdtemp(prefix="iso.", dir=workdir)
        run(["mount", "-o", "loop,ro", iso_path, mountpoint])
        try:
            # The mounted UDF volume can spell the path differently again.
            path = _find_case_insensitive(Path(mountpoint), inner)
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


def _find_case_insensitive(base, relative):
    path = base
    for part in Path(relative).parts:
        matches = [p for p in path.iterdir() if p.name.lower() == part.lower()]
        if not matches:
            raise WinToGoError(f"{relative} not found in mounted ISO")
        path = matches[0]
    return path
