"""Reading WIM metadata and applying images with wimlib."""

import re
import struct
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .util import IS_MACOS, WinToGoError, run

# WIM header layout (little-endian): magic at 0, XML resource header at 72.
# A resource header is 7 bytes of stored size, 1 flag byte, 8 bytes of
# offset and 8 bytes of original size. The XML is always stored uncompressed.
WIM_MAGIC = b"MSWIM\0\0\0"
WIM_HEADER_SIZE = 208
XML_RESHDR_OFFSET = 72

ARCHITECTURES = {0: "x86", 5: "arm", 9: "x64", 12: "arm64"}


@dataclass
class Edition:
    index: int
    name: str
    edition_id: str
    arch: str
    build: str
    total_bytes: int
    hardlink_bytes: int
    language: str

    @property
    def installed_bytes(self):
        # TOTALBYTES counts every hard link separately; WinSxS links a lot.
        return self.total_bytes - self.hardlink_bytes

    @property
    def is_windows_11(self):
        return int(self.build.split(".")[0] or 0) >= 22000

    @property
    def label(self):
        return f"{self.name} ({self.arch})"


def parse_editions(xml_text):
    root = ET.fromstring(xml_text)
    editions = []
    for image in root.findall("IMAGE"):
        def text(path, default=""):
            node = image.find(path)
            return node.text if node is not None and node.text else default

        arch_code = int(text("WINDOWS/ARCH", "-1"))
        build = text("WINDOWS/VERSION/BUILD")
        sp_build = text("WINDOWS/VERSION/SPBUILD")
        editions.append(Edition(
            index=int(image.get("INDEX")),
            name=text("DISPLAYNAME") or text("NAME") or f"Image {image.get('INDEX')}",
            edition_id=text("WINDOWS/EDITIONID"),
            arch=ARCHITECTURES.get(arch_code, f"arch {arch_code}"),
            build=f"{build}.{sp_build}" if sp_build else build,
            total_bytes=int(text("TOTALBYTES", "0")),
            hardlink_bytes=int(text("HARDLINKBYTES", "0")),
            language=text("WINDOWS/LANGUAGES/DEFAULT", "en-US"),
        ))
    return editions


def _xml_location(header):
    if len(header) < WIM_HEADER_SIZE or header[:8] != WIM_MAGIC:
        raise WinToGoError("not a WIM/ESD file (bad header)")
    size_and_flags, offset, _ = struct.unpack_from("<QQQ", header, XML_RESHDR_OFFSET)
    return offset, size_and_flags & 0x00FF_FFFF_FFFF_FFFF


def _decode_xml(raw):
    return raw.decode("utf-16-le").lstrip("﻿")


def read_editions(wim_path):
    """List the images in a WIM/ESD file on disk."""
    with open(wim_path, "rb") as f:
        offset, size = _xml_location(f.read(WIM_HEADER_SIZE))
        f.seek(offset)
        return parse_editions(_decode_xml(f.read(size)))


def read_editions_from_stream(stream):
    """List the images in a WIM/ESD streamed sequentially (e.g. from 7z -so).

    The XML sits near the end of the file, so this reads through the whole
    stream, but keeps only the header and the XML in memory.
    """
    header = stream.read(WIM_HEADER_SIZE)
    offset, size = _xml_location(header)
    remaining = offset - WIM_HEADER_SIZE
    chunk = 8 << 20
    while remaining > 0:
        data = stream.read(min(chunk, remaining))
        if not data:
            raise WinToGoError("WIM file ended before its metadata")
        remaining -= len(data)
    raw = stream.read(size)
    if len(raw) != size:
        raise WinToGoError("WIM file ended inside its metadata")
    return parse_editions(_decode_xml(raw))


_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?) (\w+) of (\d+(?:\.\d+)?) (\w+) \((\d+)%\) done")


def apply_image(wim_path, index, target, reporter):
    """Apply one image to an unmounted NTFS volume (block device or file).

    wimlib's NTFS-3G mode writes the volume directly, keeping security
    descriptors, reparse points, short names and alternate data streams.
    """
    proc = subprocess.Popen(
        ["wimlib-imagex", "apply", str(wim_path), str(index), str(target)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    tail = []
    buf = ""
    try:
        # wimlib redraws its progress line with \r, so split on both.
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                break
            if ch not in "\r\n":
                buf += ch
                continue
            line, buf = buf.strip(), ""
            if not line:
                continue
            m = _PROGRESS_RE.search(line)
            if m and line.startswith("Extracting file data"):
                reporter.progress(int(m.group(5)) / 100,
                                  f"{m.group(1)} {m.group(2)} of {m.group(3)} {m.group(4)}")
            else:
                tail = (tail + [line])[-20:]
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    if proc.wait() != 0:
        detail = "\n".join(tail)
        if IS_MACOS and "without support for NTFS-3G" in detail:
            # Homebrew's wimlib is built that way.
            detail += "\nBuild one that can with macos/build-deps.sh."
        raise WinToGoError("applying the Windows image failed:\n" + detail)


def extract_paths(wim_path, index, paths, dest_dir):
    """Extract files or directories from an image, without ACLs, into dest_dir."""
    run(["wimlib-imagex", "extract", wim_path, str(index), *paths,
         f"--dest-dir={dest_dir}", "--no-acls", "--no-attributes"])
