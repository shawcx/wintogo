# wintogo

Install a full, bootable Windows 10/11 onto a USB drive — Windows To Go —
from Linux or macOS. No Windows machine or VM needed.

**Status: experimental.** Drives it builds have been booted in QEMU (UEFI,
Secure Boot), not yet on a range of real PCs, and only Pro editions have
been tried. It rewrites the whole target drive, so check `--target` twice.

## Install

On Debian, Ubuntu and derivatives, download the `.deb` from the
[latest release](https://github.com/shawcx/wintogo/releases/latest) and
`sudo apt install ./wintogo_*_all.deb`, which pulls in everything it needs.
Or build the package yourself:

```
sudo apt install debhelper dh-python     # once, to build
dpkg-buildpackage -b -us -uc
sudo apt install ../wintogo_0.1.0_all.deb
```

That adds `wintogo`, `wintogo-gui` and `wintogo-vm` to `/usr/bin`, "Windows
To Go" to the applications menu, and a polkit policy so the password prompt
says what it's for. Installing into a VHDX needs `qemu-utils`, which apt
installs by default as a recommendation.

To run from the checkout instead, install the dependencies and use the
programs in `bin/`:

```
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-hivex \
                 wimtools ntfs-3g gdisk dosfstools mtools 7zip qemu-utils
```

(`7zip` is called `p7zip-full` on older releases.)

On macOS, run it from the checkout:

```
brew install gptfdisk dosfstools mtools autoconf automake libtool \
             gtk4 libadwaita pygobject3 adwaita-icon-theme
macos/build-deps.sh
macos/install-app.sh     # optional: add the app to ~/Applications
```

(Add `qemu` for installing into a VHDX.) Homebrew's ntfs-3g is Linux-only
(on macOS it needs macFUSE) and its wimlib can't write NTFS, so
`build-deps.sh` builds ntfs-3g's library and tools (no FUSE driver), wimlib
with NTFS-3G support and hivex's Python module into `.deps/` in this
checkout; delete that to undo. Rerun it if you move the checkout or
Homebrew's `python3` changes version.

Target machines must boot in UEFI mode; Secure Boot can stay on.

A 64 GB or larger USB SSD is recommended. Flash sticks work but Windows runs
slowly from them.

## GUI

```
wintogo-gui [windows.iso]
```

Choose the ISO, edition and USB drive, adjust the first-time setup options,
and press Install. Writing to a drive asks for your password (pkexec; on
macOS, sudo with a password dialog). The "Create a disk image file instead"
switch builds a raw image without root, e.g. for testing in QEMU.

The last run's log is kept in `~/.cache/wintogo/last-run.log`.

## Command line

```
wintogo editions Win11.iso
wintogo disks
sudo wintogo create --iso Win11.iso --edition "Windows 11 Pro" --target /dev/sdX \
     --account alice --locale auto
wintogo create --iso Win11.iso --edition 6 --target test.img     # no root needed
sudo wintogo create --iso Win11.iso --edition 6 --target /dev/sdX --vhdx
```

From a checkout (and on macOS) the programs are `bin/wintogo`,
`bin/wintogo-gui` and `bin/wintogo-vm`. On macOS drives are `/dev/diskN`
(`bin/wintogo disks` lists USB ones).

Options that include a password go in a JSON file (`--config opts.json`, or
`--config -` for stdin), using the field names of `customize.Options`.

## What it does

1. Reads `sources/install.wim|esd` from the ISO (loop-mounted as root,
   extracted with 7z otherwise; attached read-only on macOS).
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

With `--vhdx` (in the GUI, "Install into a virtual disk file"), Windows goes
into `\windows.vhdx` on the NTFS partition instead, and the BCD boots the
file (native VHD boot, as bcdboot sets it up from an attached VHDX). The
virtual disk is a dynamic VHDX nearly as large as the partition, holding one
NTFS partition; Windows is told not to expand it to full size at boot. It is
built in the work directory first (about twice the installed size; on Linux
`/var/tmp` for a drive, `--workdir` moves it), then copied onto the drive.

Nothing is mounted during the build; desktop automounting of the target is
suppressed while it is written.

macOS mounts filesystems as soon as they appear on a drive, so there the
drive is first built as a sparse image in `/var/tmp` (it needs about the size
of the installed files free; `--workdir` moves it), then written to the raw
device in one pass with the drive unmounted, and ejected.

## Testing a drive in QEMU

```
wintogo-vm test.img          # an image built with "Create a disk image file instead"
sudo wintogo-vm /dev/sdX     # a real drive (unmount it first)
```

The disk is attached as a USB drive with Secure Boot on (needs `qemu-system-x86`
and `ovmf`; on macOS `brew install qemu`, whose firmware has no Microsoft keys,
so Secure Boot isn't enforced, and Apple silicon emulates x86, very slowly).
The image itself is not modified: changes go to `test.img.overlay.qcow2`, kept between runs so first-boot setup happens once.
`--reset` starts over, `--snapshot` discards changes on exit, `--write` writes
into the image. For a real drive, changes are discarded on exit unless you
pass `--write`.

Devices are emulated hardware Windows has drivers for. For speed, once the
[virtio-win drivers](https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso)
are installed in the guest:

```
wintogo-vm --virtio test.img                  # virtio display/net/input/balloon/RNG
wintogo-vm --virtio --disk virtio test.img    # ...and the Windows disk on virtio-blk
```

`--virtio` attaches the driver ISO as a CD if it's in `~/Downloads` (or pass
`--virtio-win ISO`). Windows only boots from a virtio disk after its storage
driver has been bound to one, so boot once with `--virtio` on the USB disk
first: that attaches a small spare virtio disk for the driver to bind to.

## Tests

```
python3 -m unittest discover -s tests
```

## Licence

MIT; see [LICENSE](LICENSE). Not affiliated with or endorsed by Microsoft.
Windows and Windows To Go are trademarks of Microsoft Corporation.
