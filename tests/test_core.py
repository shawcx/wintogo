"""Unit and small integration tests: python3 -m unittest discover -s tests"""

import base64
import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wintogo.core import bcd, customize, disks, iso, pipeline, target, wim  # noqa: E402
from wintogo.core.util import Reporter, WinToGoError, setup_environment  # noqa: E402

setup_environment()  # macOS: find Homebrew's and .deps/'s tools

REF_BCD_TEMPLATE = Path(__file__).resolve().parents[1] / ".work" / "ref" / "BCD-Template"

SAMPLE_XML = """<WIM><TOTALBYTES>1</TOTALBYTES>
<IMAGE INDEX="1"><TOTALBYTES>2000</TOTALBYTES><HARDLINKBYTES>500</HARDLINKBYTES>
<WINDOWS><ARCH>9</ARCH><EDITIONID>Core</EDITIONID><LANGUAGES><DEFAULT>en-GB</DEFAULT></LANGUAGES>
<VERSION><BUILD>26200</BUILD><SPBUILD>6584</SPBUILD></VERSION></WINDOWS>
<NAME>Windows 11 Home</NAME><DISPLAYNAME>Windows 11 Home</DISPLAYNAME></IMAGE>
<IMAGE INDEX="2"><TOTALBYTES>3000</TOTALBYTES>
<WINDOWS><ARCH>12</ARCH><EDITIONID>Professional</EDITIONID>
<VERSION><BUILD>19045</BUILD></VERSION></WINDOWS><NAME>Windows 10 Pro</NAME></IMAGE></WIM>"""


class BcdTests(unittest.TestCase):
    # From a store written by bcdboot for a disk with these GUIDs.
    DISK = "3CD38A35-EF7C-4772-84C5-0F84C66AD993"
    WIN = "B587EEF4-EC4B-4D1D-BC5A-0DA1A45F6D18"
    WIN_DEVICE = ("00000000000000000000000000000000060000000000000048000000000000"
                  "00f4ee87b54bec1d4dbc5a0da1a45f6d180000000000000000358ad33c7cef72"
                  "4784c50f84c66ad99300000000000000000000000000000000")

    def test_partition_device_matches_bcdboot(self):
        self.assertEqual(bcd.partition_device(self.WIN, self.DISK).hex(), self.WIN_DEVICE)

    def test_objects_reference_each_other(self):
        objs = bcd.build_objects(esp_part_guid=self.WIN, win_part_guid=self.WIN,
                                 disk_guid=self.DISK, description="Windows 11")
        bootmgr = objs[bcd.BOOTMGR][1]
        loader = bootmgr[0x23000003][1]
        resume = bootmgr[0x23000006][1]
        self.assertEqual(objs[loader][0], bcd.TYPE_OSLOADER)
        self.assertEqual(objs[resume][0], bcd.TYPE_RESUME)
        self.assertEqual(bootmgr[0x24000001][1], [loader])
        self.assertEqual(objs[loader][1][0x23000003][1], resume)
        # Every inherited object exists.
        for _, elements in objs.values():
            for obj_id in elements.get(0x14000006, (None, []))[1]:
                self.assertIn(obj_id, objs)

    def test_encoding(self):
        self.assertEqual(bcd._encode(bcd.REG_SZ, "ab"), b"a\0b\0\0\0")
        self.assertEqual(bcd._encode(bcd.REG_MULTI_SZ, ["a", "b"]), b"a\0\0\0b\0\0\0\0\0")

    @unittest.skipUnless(REF_BCD_TEMPLATE.exists(), "needs a BCD-Template as base hive")
    def test_write_store_round_trip(self):
        import hivex
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "base"
            shutil.copy(REF_BCD_TEMPLATE, base)
            objs = bcd.build_objects(esp_part_guid=self.WIN, win_part_guid=self.WIN,
                                     disk_guid=self.DISK, description="Windows 11")
            bcd.write_store(base, Path(d) / "BCD", objs)
            h = hivex.Hivex(str(Path(d) / "BCD"))
            objects = h.node_get_child(h.root(), "Objects")
            self.assertEqual(len(h.node_children(objects)), len(objs))
            desc = h.node_get_child(h.root(), "Description")
            key_name = h.value_value(h.node_get_value(desc, "KeyName"))[1]
            self.assertEqual(key_name.decode("utf-16-le").rstrip("\0"), "BCD00000000")


class WimTests(unittest.TestCase):
    def test_parse_editions(self):
        home, pro = wim.parse_editions(SAMPLE_XML)
        self.assertEqual((home.index, home.name, home.arch, home.build), (1, "Windows 11 Home", "x64", "26200.6584"))
        self.assertEqual((home.installed_bytes, home.language), (1500, "en-GB"))
        self.assertTrue(home.is_windows_11)
        self.assertEqual((pro.arch, pro.language, pro.is_windows_11), ("arm64", "en-US", False))

    def fake_wim(self, padding=5000):
        xml = ("﻿" + SAMPLE_XML).encode("utf-16-le")
        header = bytearray(wim.WIM_HEADER_SIZE)
        header[:8] = wim.WIM_MAGIC
        offset = wim.WIM_HEADER_SIZE + padding
        struct.pack_into("<QQQ", header, wim.XML_RESHDR_OFFSET, len(xml), offset, len(xml))
        return bytes(header) + b"\xaa" * padding + xml + b"tail"

    def test_read_editions_from_stream_and_file(self):
        data = self.fake_wim()
        self.assertEqual(len(wim.read_editions_from_stream(io.BytesIO(data))), 2)
        with tempfile.NamedTemporaryFile() as f:
            f.write(data)
            f.flush()
            self.assertEqual(len(wim.read_editions(f.name)), 2)

    def test_rejects_non_wim(self):
        with self.assertRaises(WinToGoError):
            wim.read_editions_from_stream(io.BytesIO(b"not a wim" * 100))

    def test_truncated_stream(self):
        with self.assertRaises(WinToGoError):
            wim.read_editions_from_stream(io.BytesIO(self.fake_wim()[:1000]))


class CustomizeTests(unittest.TestCase):
    NS = {"u": "urn:schemas-microsoft-com:unattend"}

    def test_password_encoding(self):
        encoded = customize._encode_password("secret", "Password")
        self.assertEqual(base64.b64decode(encoded).decode("utf-16-le"), "secretPassword")

    def test_unattend_contents(self):
        opts = customize.Options(local_account="alice", password="pw", locale="en-GB",
                                 computer_name="PC-1")
        root = ET.fromstring(customize.build_unattend(opts).encode())
        passes = {s.get("pass") for s in root.findall("u:settings", self.NS)}
        self.assertEqual(passes, {"specialize", "oobeSystem"})
        text = customize.build_unattend(opts)
        for needle in ("<ComputerName>PC-1</ComputerName>", "<Name>alice</Name>",
                       "<UserLocale>en-GB</UserLocale>", "HideOnlineAccountScreens",
                       "<PlainText>false</PlainText>", r"Sysprep\unattend.xml"):
            self.assertIn(needle, text)
        self.assertNotIn(">pw<", text)

    def test_minimal_unattend_has_no_account(self):
        text = customize.build_unattend(customize.Options())
        self.assertNotIn("LocalAccount", text)
        self.assertNotIn("specialize", text)

    def test_escaping(self):
        text = customize.build_unattend(customize.Options(local_account="a&b"))
        ET.fromstring(text.encode())  # well-formed despite the ampersand

    def test_validation(self):
        for bad in (dict(local_account="x" * 21), dict(local_account="a/b"),
                    dict(local_account="Administrator"), dict(computer_name="has space"),
                    dict(computer_name="x" * 16), dict(locale="english")):
            with self.subTest(**bad), self.assertRaises(WinToGoError):
                customize.Options(**bad).validate()
        customize.Options(local_account="matt", computer_name="WTG-1", locale="en-US").validate()

    @mock.patch.object(customize, "IS_MACOS", False)
    def test_host_locale(self):
        env = {k: os.environ.pop(k) for k in ("LC_ALL", "LC_CTYPE", "LANG") if k in os.environ}
        try:
            os.environ["LANG"] = "de_AT.UTF-8"
            self.assertEqual(customize.host_locale(), "de-AT")
            os.environ["LANG"] = "C.UTF-8"
            self.assertEqual(customize.host_locale(), "")
        finally:
            os.environ.pop("LANG", None)
            os.environ.update(env)


class PipelineTests(unittest.TestCase):
    def test_pick_edition(self):
        editions = wim.parse_editions(SAMPLE_XML)
        self.assertEqual(pipeline.pick_edition(editions, "2").name, "Windows 10 Pro")
        self.assertEqual(pipeline.pick_edition(editions, "windows 11 home").index, 1)
        with self.assertRaises(WinToGoError):
            pipeline.pick_edition(editions, "7")


class IsoTests(unittest.TestCase):
    def test_failed_attach_cleans_up(self):
        # diskutil creates the mount point and a marker in it, then fails to
        # mount the volume.
        def attach(cmd, **_):
            mountpoint = next(arg for arg in cmd if arg.endswith("/mnt"))
            os.mkdir(mountpoint)
            Path(mountpoint, ".autodiskmounted").touch()
            raise WinToGoError("diskutil failed (exit 1): Error: Failed to mount the volume")

        with tempfile.TemporaryDirectory() as work, \
                mock.patch.object(iso, "_check_iso"), mock.patch.object(iso, "run", attach):
            with self.assertRaisesRegex(WinToGoError, "could not open .*Failed to mount"):
                with iso._attached("bogus.iso", work):
                    pass
            self.assertEqual(os.listdir(work), [])


class MacDiskTests(unittest.TestCase):
    # Trimmed `diskutil list -plist` and `diskutil info -plist` output: the
    # internal disk, and a USB drive with a FAT partition and an APFS container.
    LISTING = {"AllDisksAndPartitions": [
        {"DeviceIdentifier": "disk0", "Partitions": [{"DeviceIdentifier": "disk0s2"}]},
        {"DeviceIdentifier": "disk3", "APFSPhysicalStores": [{"DeviceIdentifier": "disk0s2"}],
         "APFSVolumes": [{"DeviceIdentifier": "disk3s1s1", "MountPoint": "/"},
                         {"DeviceIdentifier": "disk3s5", "MountPoint": "/System/Volumes/Data"}]},
        {"DeviceIdentifier": "disk4", "Partitions": [
            {"DeviceIdentifier": "disk4s1", "MountPoint": "/Volumes/STICK"},
            {"DeviceIdentifier": "disk4s2"}]},
        {"DeviceIdentifier": "disk5", "APFSPhysicalStores": [{"DeviceIdentifier": "disk4s2"}],
         "APFSVolumes": [{"DeviceIdentifier": "disk5s1", "MountPoint": "/Volumes/Backup"}]},
    ]}
    INFOS = {
        "disk0": {"DeviceNode": "/dev/disk0", "TotalSize": 1000555581440, "MediaName": "APPLE SSD",
                  "BusProtocol": "Apple Fabric", "WritableMedia": True, "DeviceBlockSize": 4096},
        "disk4": {"DeviceNode": "/dev/disk4", "TotalSize": 128035676160,
                  "IORegistryEntryName": "Samsung PSSD T7 Media", "BusProtocol": "USB",
                  "WritableMedia": True, "RemovableMedia": False, "Ejectable": True,
                  "DeviceBlockSize": 512},
    }

    def test_parse_diskutil(self):
        internal, usb = disks.parse_diskutil(self.LISTING, self.INFOS)
        self.assertFalse(internal.is_usb)
        self.assertIn("/", internal.mountpoints)
        self.assertEqual(internal.sector_size, 4096)
        self.assertTrue(usb.is_usb)
        self.assertEqual((usb.path, usb.model, usb.size), ("/dev/disk4", "Samsung PSSD T7", 128035676160))
        self.assertEqual(sorted(usb.mountpoints), ["/Volumes/Backup", "/Volumes/STICK"])

    @mock.patch.object(disks, "IS_MACOS", True)
    def test_mac_target_checks(self):
        with mock.patch.object(disks, "list_disks",
                               lambda: disks.parse_diskutil(self.LISTING, self.INFOS)):
            with self.assertRaisesRegex(WinToGoError, "this system's"):
                disks.check_target("/dev/rdisk0", allow_non_usb=True)
            with self.assertRaisesRegex(WinToGoError, "unmountDisk"):
                disks.check_target("/dev/rdisk4")
            with self.assertRaisesRegex(WinToGoError, "/dev/disk4, not"):
                disks.check_target("/dev/disk4s1")


class TargetTests(unittest.TestCase):
    def test_sparse_copy(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d) / "src", Path(d) / "dst"
            # APFS fills in holes smaller than about 16 MiB, so keep them big.
            with open(src, "wb") as f:
                f.truncate(1 << 30)
                f.seek(100 << 20)
                f.write(b"A" * 4096)
                f.seek(700 << 20)
                f.write(b"B" * 4096)
            with open(dst, "wb") as f:
                f.truncate(1100 << 20)
            target.sparse_copy(src, dst, 1 << 20)
            with open(dst, "rb") as f:
                f.seek((1 << 20) + (100 << 20))
                self.assertEqual(f.read(4096), b"A" * 4096)
                f.seek((1 << 20) + (700 << 20))
                self.assertEqual(f.read(4096), b"B" * 4096)
            self.assertLess(os.stat(dst).st_blocks * 512, 4 << 20, "destination should stay sparse")

    def test_staged_device_write(self):
        """The image's data and the Windows partition land at the right offsets."""
        with tempfile.TemporaryDirectory() as d:
            device = Path(d) / "device"
            device.write_bytes(b"\xee" * (64 << 20))  # stale data that holes must leave alone
            disk = disks.Disk(path="/dev/disk9", size=64 << 20, model="", vendor="", serial="",
                              transport="usb", removable=False, read_only=False, hotplug=True)
            t = target.StagedDeviceTarget(disk, d)
            self.assertEqual(t.raw_device, "/dev/rdisk9")
            t.raw_device = str(device)
            t.layout.extents[3] = (40960, 81920)  # Windows partition at 20 MiB
            with open(t.path, "wb") as f:
                f.truncate(disk.size)
                f.write(b"GPT!")
                f.seek(disk.size - 4)
                f.write(b"BAK!")
            with open(t.windows_volume, "wb") as f:
                f.truncate(40 << 20)
                f.write(b"NTFS")
            commands = []
            with mock.patch.object(target, "run", lambda cmd, **kw: commands.append(cmd[:2]) or ""):
                t.finish(Reporter(io.StringIO()))
            data = device.read_bytes()
            self.assertEqual(data[:4], b"GPT!")
            self.assertEqual(data[-4:], b"BAK!")
            self.assertEqual(data[20 << 20:(20 << 20) + 4], b"NTFS")
            self.assertEqual(commands, [["diskutil", "unmountDisk"], ["diskutil", "eject"]])
            self.assertFalse(Path(t.path).exists() or Path(t.windows_volume).exists())

    @unittest.skipUnless(all(shutil.which(c) for c in ("sgdisk", "mkfs.vfat", "mkntfs", "minfo", "ntfsinfo")),
                         "needs gdisk, dosfstools, mtools and ntfs-3g")
    def test_image_target_layout(self):
        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "disk.img"
            t = target.ImageTarget(img, 512 << 20, d)
            t.partition()
            t.format_esp()
            t.format_windows()
            (esp_start, esp_len), (_, msr_len), (win_start, win_len) = (t.layout.extents[i] for i in (1, 2, 3))
            self.assertEqual(esp_start, 2048)
            self.assertEqual(esp_len * 512, 300 << 20)
            self.assertEqual(msr_len * 512, 16 << 20)
            info = subprocess.run(["sgdisk", "-p", str(img)], capture_output=True, text=True).stdout
            self.assertIn(t.layout.disk_guid, info)
            env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
            out = subprocess.run(["minfo", "-i", t.esp_mtools, "::"], capture_output=True, text=True, env=env)
            self.assertIn("FAT32", out.stdout)
            out = subprocess.run(["ntfsinfo", "-m", t.windows_volume], capture_output=True, text=True)
            self.assertIn("Windows", out.stdout)
            t.finish(Reporter(io.StringIO()))
            self.assertFalse(Path(t.windows_volume).exists())


if __name__ == "__main__":
    unittest.main()
