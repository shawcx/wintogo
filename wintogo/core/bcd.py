"""Generating a UEFI BCD store without bcdboot.

The objects and elements below reproduce what `bcdboot <win> /s <esp> /f UEFI`
writes for a GPT disk (verified against a store created by bcdboot for the
same disk). Everything is fixed except the two partition devices, which are
derived from GPT GUIDs chosen when partitioning, and the loader/resume object
IDs, which are random per install. When Windows is in a VHDX, bcdboot writes
the same store except that the loader and resume objects' devices are a
partition inside the virtual disk (verified the same way, with the VHDX
attached in WinPE).
"""

import struct
import uuid

# Well-known BCD object IDs.
BOOTMGR = "{9dea862c-5cdd-4e70-acc1-f32b344d4795}"
MEMDIAG = "{b2721d73-1db4-4c62-bf78-c548a880142d}"
GLOBALSETTINGS = "{7ea2e1ac-2e61-4728-aaa3-896d9d0a9f0e}"
BOOTLOADERSETTINGS = "{6efb52bf-1766-41db-a6b3-0ee5eff72bd7}"
RESUMELOADERSETTINGS = "{1afa9c49-16ab-4a5c-901b-212802da9460}"
HYPERVISORSETTINGS = "{7ff607e0-4395-11db-b0de-0800200c9a66}"
DBGSETTINGS = "{4636856e-540f-4170-a130-a84776f4c654}"
EMSSETTINGS = "{0ce4991b-e6b3-4b16-b23c-5e0d9250e5d9}"
BADMEMORY = "{5189b25c-5558-4bf2-bca4-289b11bd29e2}"

# Object types.
TYPE_BOOTMGR = 0x10100002
TYPE_OSLOADER = 0x10200003
TYPE_RESUME = 0x10200004
TYPE_MEMDIAG = 0x10200005
TYPE_INHERIT_ALL = 0x20100000
TYPE_INHERIT_OSLOADER = 0x20200003
TYPE_INHERIT_RESUME = 0x20200004

# Registry value types.
REG_SZ, REG_BINARY, REG_DWORD, REG_MULTI_SZ = 1, 3, 4, 7


# Device types in a device element's header.
DEVICE_BLOCK_IO = 0
DEVICE_PARTITION = 6       # extended form, identified by GUIDs
# Kinds of block I/O device, and the value that marks a partition's disk as
# the virtual disk in its parent device.
BLOCK_IO_FILE = 5
DISK_VIRTUAL = 6


def _device(device_type, body):
    """A device: header (type, flags 0, length including the header, reserved 0), body."""
    return struct.pack("<IIII", device_type, 0, 16 + len(body), 0) + body


def _guid(value):
    # Windows' mixed-endian byte order.
    return uuid.UUID(str(value)).bytes_le


def _gpt_partition(partition_guid, disk_guid, parent=None):
    """A partition on a GPT disk: the partition's unique GUID, the disk kind,
    partition style 0 (GPT), the disk GUID, then the device the disk is in
    (an empty header for a physical disk)."""
    return _device(DEVICE_PARTITION,
                   _guid(partition_guid)
                   + struct.pack("<II", DISK_VIRTUAL if parent else 0, 0)
                   + _guid(disk_guid)
                   + (parent or bytes(16)))


def partition_device(partition_guid, disk_guid):
    """The binary device element for a partition on a GPT disk.

    A zero "additional options" GUID, then the partition: 88 bytes in all.
    """
    return bytes(16) + _gpt_partition(partition_guid, disk_guid)


def vhd_device(partition_guid, disk_guid, host_partition_guid, host_disk_guid, path):
    """The device element for a partition inside a virtual disk file.

    This is what bcdedit shows as vhd=[X:]\\path: the partition in the VHD,
    whose disk is a block I/O device of the file kind, which holds a
    version (1), its length, the kind again, the host partition the file is
    on and the file's path.
    """
    file = _gpt_partition(host_partition_guid, host_disk_guid) + (path + "\0").encode("utf-16-le")
    file = struct.pack("<III", 1, 12 + len(file), BLOCK_IO_FILE) + file
    disk = _device(DEVICE_BLOCK_IO, struct.pack("<I", BLOCK_IO_FILE) + file)
    return bytes(16) + _gpt_partition(partition_guid, disk_guid, parent=disk)


# Element values are tagged with how they are stored in the hive.
def _str(s): return (REG_SZ, s)
def _objs(*ids): return (REG_MULTI_SZ, list(ids))
def _int(n): return (REG_BINARY, struct.pack("<Q", n))
def _bool(b): return (REG_BINARY, b"\x01" if b else b"\x00")
def _bin(b): return (REG_BINARY, b)


def build_objects(*, esp_part_guid, win_part_guid, disk_guid, description,
                  locale="en-us", loader_id=None, resume_id=None, win_device=None):
    """Return {object id: (object type, {element id: (reg type, value)})}.

    win_device replaces the Windows partition's device, e.g. with vhd_device().
    """
    loader_id = loader_id or "{%s}" % uuid.uuid4()
    resume_id = resume_id or "{%s}" % uuid.uuid4()
    esp = _bin(partition_device(esp_part_guid, disk_guid))
    win = _bin(win_device or partition_device(win_part_guid, disk_guid))
    allowed_in_memory = _bin(struct.pack("<Q", 0x15000075))

    return {
        BOOTMGR: (TYPE_BOOTMGR, {
            0x11000001: esp,                                   # device
            0x12000002: _str(r"\EFI\Microsoft\Boot\bootmgfw.efi"),  # path
            0x12000004: _str("Windows Boot Manager"),          # description
            0x12000005: _str(locale),                          # locale
            0x14000006: _objs(GLOBALSETTINGS),                 # inherit
            0x23000003: _str(loader_id),                       # default
            0x23000006: _str(resume_id),                       # resumeobject
            0x24000001: _objs(loader_id),                      # displayorder
            0x24000010: _objs(MEMDIAG),                        # toolsdisplayorder
            0x25000004: _int(30),                              # timeout
        }),
        loader_id: (TYPE_OSLOADER, {
            0x11000001: win,                                   # device
            0x12000002: _str(r"\Windows\system32\winload.efi"),
            0x12000004: _str(description),
            0x12000005: _str(locale),
            0x14000006: _objs(BOOTLOADERSETTINGS),
            0x16000060: _bool(True),                           # isolatedcontext
            0x17000077: allowed_in_memory,                     # allowedinmemorysettings
            0x21000001: win,                                   # osdevice
            0x22000002: _str(r"\Windows"),                     # systemroot
            0x23000003: _str(resume_id),                       # resumeobject
            0x25000020: _int(0),                               # nx OptIn
            0x250000C2: _int(1),                               # bootmenupolicy Standard
        }),
        resume_id: (TYPE_RESUME, {
            0x11000001: win,
            0x12000002: _str(r"\Windows\system32\winresume.efi"),
            0x12000004: _str("Windows Resume Application"),
            0x12000005: _str(locale),
            0x14000006: _objs(RESUMELOADERSETTINGS),
            0x16000060: _bool(True),
            0x17000077: allowed_in_memory,
            0x22000002: _str(r"\hiberfil.sys"),                # filepath
            0x25000008: _int(1),                               # bootmenupolicy Standard
        }),
        MEMDIAG: (TYPE_MEMDIAG, {
            0x11000001: esp,
            0x12000002: _str(r"\EFI\Microsoft\Boot\memtest.efi"),
            0x12000004: _str("Windows Memory Diagnostic"),
            0x12000005: _str(locale),
            0x14000006: _objs(GLOBALSETTINGS),
            0x1600000B: _bool(True),                           # badmemoryaccess
        }),
        EMSSETTINGS: (TYPE_INHERIT_ALL, {
            0x16000020: _bool(False),                          # bootems
        }),
        RESUMELOADERSETTINGS: (TYPE_INHERIT_RESUME, {
            0x14000006: _objs(GLOBALSETTINGS),
        }),
        DBGSETTINGS: (TYPE_INHERIT_ALL, {
            0x15000011: _int(4),                               # debugtype Local
        }),
        BADMEMORY: (TYPE_INHERIT_ALL, {}),
        GLOBALSETTINGS: (TYPE_INHERIT_ALL, {
            0x14000006: _objs(DBGSETTINGS, EMSSETTINGS, BADMEMORY),
        }),
        BOOTLOADERSETTINGS: (TYPE_INHERIT_OSLOADER, {
            0x14000006: _objs(GLOBALSETTINGS, HYPERVISORSETTINGS),
        }),
        HYPERVISORSETTINGS: (TYPE_INHERIT_OSLOADER, {
            0x250000F3: _int(0),                               # hypervisordebugtype Serial
            0x250000F4: _int(1),                               # hypervisordebugport
            0x250000F5: _int(115200),                          # hypervisorbaudrate
        }),
    }


def _encode(reg_type, value):
    if reg_type == REG_SZ:
        return (value + "\0").encode("utf-16-le")
    if reg_type == REG_MULTI_SZ:
        return "".join(v + "\0" for v in value).encode("utf-16-le") + b"\0\0"
    if reg_type == REG_DWORD:
        return struct.pack("<I", value)
    return value


def write_store(base_hive, output, objects):
    """Write a BCD store to output.

    hivex can only edit existing hives, so base_hive (any hive file; we use
    the image's own BCD-Template) supplies the file structure and everything
    under its root is replaced.
    """
    import hivex  # python3-hivex; imported late so the GUI can start without it

    h = hivex.Hivex(str(base_hive), write=True)
    root = h.root()
    for child in h.node_children(root):
        h.node_delete_child(child)
    h.node_set_values(root, [])

    desc = h.node_add_child(root, "Description")
    h.node_set_values(desc, [
        {"key": "KeyName", "t": REG_SZ, "value": _encode(REG_SZ, "BCD00000000")},
        {"key": "System", "t": REG_DWORD, "value": _encode(REG_DWORD, 1)},
    ])

    objects_node = h.node_add_child(root, "Objects")
    for object_id in sorted(objects, key=str.lower):
        object_type, elements = objects[object_id]
        obj = h.node_add_child(objects_node, object_id)
        obj_desc = h.node_add_child(obj, "Description")
        h.node_set_value(obj_desc, {"key": "Type", "t": REG_DWORD,
                                    "value": _encode(REG_DWORD, object_type)})
        elements_node = h.node_add_child(obj, "Elements")
        for element_id in sorted(elements):
            reg_type, value = elements[element_id]
            el = h.node_add_child(elements_node, f"{element_id:08x}")
            h.node_set_value(el, {"key": "Element", "t": reg_type,
                                  "value": _encode(reg_type, value)})
    h.commit(str(output))
