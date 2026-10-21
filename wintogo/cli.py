"""Command-line interface; the GUI runs `create --json` through pkexec."""

import argparse
import json
import os
import signal
import sys
import threading

from .core import customize, disks, iso, pipeline
from .core.util import IS_MACOS, JsonReporter, Reporter, WinToGoError, setup_environment


def cmd_editions(args):
    editions = iso.read_editions(args.iso)
    if args.json:
        print(json.dumps([e.__dict__ for e in editions], indent=2))
        return
    for e in editions:
        print(f"{e.index:3d}  {e.name} ({e.arch}, build {e.build}, "
              f"{e.installed_bytes / pipeline.GIB:.1f} GiB)")


def cmd_disks(args):
    found = disks.list_disks() if args.all else disks.list_usb_disks()
    if args.json:
        print(json.dumps([d.__dict__ for d in found], indent=2))
        return
    if not found:
        print("no USB disks found" + ("" if args.all else " (use --all to list every disk)"))
    for d in found:
        flags = ", ".join(f for f, on in (("mounted", d.mountpoints), ("read-only", d.read_only)) if on)
        print(f"{d.path:12s} {disks.human_size(d.size):>9s}  {d.transport or '-':5s} "
              f"{d.vendor} {d.model}" + (f"  [{flags}]" if flags else ""))


def load_options(args):
    """Customization options from --config JSON (file or '-' for stdin) plus flags."""
    opts = customize.Options()
    data = None
    if args.stdin_control:
        data = json.loads(sys.stdin.readline() or "{}")
    elif args.config:
        data = json.load(sys.stdin if args.config == "-" else open(args.config))
    if data:
        for key, value in data.items():
            if not hasattr(opts, key):
                raise WinToGoError(f"unknown option in config: {key}")
            setattr(opts, key, value)
    if args.no_customize:
        opts = customize.Options(portable=False, hide_internal_disks=False,
                                 disable_bitlocker=False, disable_hibernation=False,
                                 skip_online_account=False, skip_privacy=False)
    if args.account:
        opts.local_account = args.account
    if args.locale:
        opts.locale = customize.host_locale() if args.locale == "auto" else args.locale
    if args.computer_name:
        opts.computer_name = args.computer_name
    return opts


def watch_for_cancel():
    """GUI protocol: after the config line, a "cancel" line (or the GUI going
    away and closing stdin) interrupts the run like Ctrl-C would."""
    def watch():
        for line in sys.stdin:
            if line.strip() == "cancel":
                break
        os.kill(os.getpid(), signal.SIGINT)
    threading.Thread(target=watch, daemon=True).start()


def cmd_create(args):
    reporter = JsonReporter() if args.json else Reporter()
    options = load_options(args)
    if args.stdin_control:
        watch_for_cancel()
    if pipeline.is_device(args.target) and not args.yes:
        disk = disks.check_target(args.target, allow_non_usb=args.allow_non_usb)
        print(f"ALL DATA on {disk.label} will be destroyed.", file=sys.stderr)
        reply = input("Type the device path to continue: ")
        if reply.strip() != args.target:
            raise WinToGoError("aborted")
    size = int(args.size * pipeline.GIB) if args.size else None
    try:
        pipeline.create(args.iso, args.edition, args.target, reporter,
                        options=options, image_size=size, workdir=args.workdir,
                        allow_non_usb=args.allow_non_usb, vhdx=args.vhdx)
    except WinToGoError as e:
        if args.json:
            reporter.error(str(e))
        raise
    except KeyboardInterrupt:
        if args.json:
            reporter.error("cancelled")
        raise
    if args.json:
        reporter.done()
    else:
        print(f"==> done: {args.target}", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wintogo", description="Install Windows To Go onto a USB drive.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("editions", help="list the Windows editions in an ISO")
    p.add_argument("iso")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_editions)

    p = sub.add_parser("disks", help="list USB disks that can be used as targets")
    p.add_argument("--all", action="store_true", help="list every disk, not just USB")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_disks)

    p = sub.add_parser("create", help="install Windows onto a drive or into an image file")
    p.add_argument("--iso", required=True)
    p.add_argument("--edition", required=True, help="edition index or exact name")
    p.add_argument("--target", required=True,
                   help=f"whole-disk device (e.g. {'/dev/disk4' if IS_MACOS else '/dev/sdb'}) or image file")
    p.add_argument("--vhdx", action="store_true",
                   help="install Windows into a virtual disk file (\\windows.vhdx) on the drive "
                        "and boot that")
    p.add_argument("--size", type=float, help="image file size in GiB (default: just big enough)")
    p.add_argument("--workdir", help="where to put temporary files")
    p.add_argument("--allow-non-usb", action="store_true", help="allow a target that isn't a USB disk")
    p.add_argument("--account", help="create this local administrator account (no password; "
                                      "use --config for one)")
    p.add_argument("--locale", help="region and keyboard, e.g. en-GB, or 'auto' for this system's")
    p.add_argument("--computer-name")
    p.add_argument("--config", help="JSON file (or '-' for stdin) with customization options, "
                                    "including 'password'")
    p.add_argument("--no-customize", action="store_true",
                   help="apply the image as-is: no registry changes and no answer file")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before wiping a device")
    p.add_argument("--json", action="store_true", help="report progress as JSON lines")
    p.add_argument("--stdin-control", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_create)

    args = parser.parse_args(argv)
    setup_environment()
    try:
        args.func(args)
    except WinToGoError as e:
        if not getattr(args, "json", False):
            print(f"wintogo: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
