# wintogo

Install a full, bootable Windows 10/11 onto a USB drive — Windows To Go —
from Linux. No Windows machine or VM needed.

## Requirements

```
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-hivex \
                 wimtools ntfs-3g gdisk dosfstools mtools 7zip
```

(`7zip` is called `p7zip-full` on older releases.) Target machines must boot
in UEFI mode; Secure Boot can stay on.

A 64 GB or larger USB SSD is recommended. Flash sticks work but Windows runs
slowly from them.

## GUI

```
bin/wintogo-gui [windows.iso]
./install.sh           # optional: add it to the applications menu
sudo ./install.sh      # optional: nicer password prompt (polkit policy)
```

Choose the ISO, edition and USB drive, adjust the first-time setup options,
and press Install. Writing to a drive asks for your password (pkexec). The
"Create a disk image file instead" switch builds a raw image without root,
e.g. for testing in QEMU.

The last run's log is kept in `~/.cache/wintogo/last-run.log`.

## Command line

```
bin/wintogo editions Win11.iso
bin/wintogo disks
sudo bin/wintogo create --iso Win11.iso --edition "Windows 11 Pro" --target /dev/sdX \
     --account alice --locale auto
bin/wintogo create --iso Win11.iso --edition 6 --target test.img     # no root needed
```

Options that include a password go in a JSON file (`--config opts.json`, or
`--config -` for stdin), using the field names of `customize.Options`.

## What it does

1. Reads `sources/install.wim|esd` from the ISO (loop-mounted as root,
   extracted with 7z otherwise).
2. Partitions the target GPT: 300 MiB ESP (FAT32), 16 MiB MSR, NTFS for
   Windows, with GUIDs chosen up front.
3. Applies the edition with wimlib straight onto the unmounted NTFS volume.
4. Edits the offline registry with hivex (portable OS flag, internal disks
   offline, no BitLocker auto-encryption, no hibernation, BypassNRO) and
   writes an `unattend.xml` for first-time setup (local account, skipped
   Microsoft account and privacy pages, region, computer name). If an account
   is created, the answer file deletes itself at first logon.
5. Populates the ESP the way `bcdboot /f UEFI` does and generates the BCD
   store itself (`wintogo/core/bcd.py`; byte-identical to bcdboot's elements).

Nothing is mounted during the build; desktop automounting of the target is
suppressed while it is written.

## Testing a drive in QEMU

```
bin/wintogo-vm test.img          # an image built with "Create a disk image file instead"
sudo bin/wintogo-vm /dev/sdX     # a real drive (unmount it first)
```

The disk is attached as a USB drive with Secure Boot on (needs `qemu-system-x86`
and `ovmf`). The image itself is not modified: changes go to
`test.img.overlay.qcow2`, kept between runs so first-boot setup happens once.
`--reset` starts over, `--snapshot` discards changes on exit, `--write` writes
into the image. For a real drive, changes are discarded on exit unless you
pass `--write`.

Devices are emulated hardware Windows has drivers for. For speed, once the
[virtio-win drivers](https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso)
are installed in the guest:

```
bin/wintogo-vm --virtio test.img                  # virtio display/net/input/balloon/RNG
bin/wintogo-vm --virtio --disk virtio test.img    # ...and the Windows disk on virtio-blk
```

`--virtio` attaches the driver ISO as a CD if it's in `~/Downloads` (or pass
`--virtio-win ISO`). Windows only boots from a virtio disk after its storage
driver has been bound to one, so boot once with `--virtio` on the USB disk
first: that attaches a small spare virtio disk for the driver to bind to.

## Tests

```
python3 -m unittest discover -s tests
```
