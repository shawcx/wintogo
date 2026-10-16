"""The main window: a single Rufus-style form, in numbered sections."""

import getpass
import importlib.util
import shutil
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ..core import customize, disks, iso, pipeline  # noqa: E402
from ..core.util import DEPS_PREFIX, IS_MACOS, WinToGoError  # noqa: E402
from . import style  # noqa: E402
from .job import Job, log_path  # noqa: E402

if IS_MACOS:
    # Command -> Homebrew formula that provides it.
    DEPENDENCIES = {"sgdisk": "gptfdisk", "mkfs.vfat": "dosfstools", "mcopy": "mtools"}
    # Homebrew has no ntfs-3g tools and no NTFS-3G support in wimlib, so
    # macos/build-deps.sh builds them (and hivex's Python module) into .deps/,
    # with these formulae.
    BUILT_COMMANDS = ("wimlib-imagex", "mkntfs", "ntfscat", "ntfscp")
    BUILD_DEPENDENCIES = {"autoreconf": "autoconf", "aclocal": "automake",
                          "glibtoolize": "libtool", "pkg-config": "pkgconf"}
    BUILD_SCRIPT = DEPS_PREFIX.parent / "macos" / "build-deps.sh"
else:
    # Command (or Python module) -> Debian/Ubuntu package that provides it.
    DEPENDENCIES = {
        "7z": "7zip", "wimlib-imagex": "wimtools", "sgdisk": "gdisk", "mkfs.vfat": "dosfstools",
        "mcopy": "mtools", "mkntfs": "ntfs-3g", "ntfscat": "ntfs-3g", "ntfscp": "ntfs-3g",
        "pkexec": "pkexec", "lsblk": "util-linux",
    }

# How much of the overall progress bar each backend step takes up.
STEP_WEIGHTS = {"source": 3, "partition": 2, "apply": 82, "customize": 3, "boot": 2, "finish": 8}
# On macOS a drive is built in a file, then written out in a "write" step.
STAGED_STEP_WEIGHTS = {"source": 2, "partition": 1, "apply": 46, "customize": 2, "boot": 1,
                       "finish": 0, "write": 48}

DISK_POLL_SECONDS = 3

_style_installed = False


def missing_dependencies():
    """(what's missing, the command that installs it), or None."""
    packages = {pkg for cmd, pkg in DEPENDENCIES.items() if shutil.which(cmd) is None}
    no_hivex = importlib.util.find_spec("hivex") is None
    if not IS_MACOS:
        if no_hivex:
            packages.add("python3-hivex")
        packages = sorted(packages)
        return ("Missing packages: " + " ".join(packages),
                "sudo apt install " + " ".join(packages)) if packages else None

    build = no_hivex or not all(any((DEPS_PREFIX / d / c).exists() for d in ("bin", "sbin"))
                                for c in BUILT_COMMANDS)
    missing = sorted(packages) + (["NTFS tools"] if build else [])
    if build:
        packages |= {pkg for cmd, pkg in BUILD_DEPENDENCIES.items() if shutil.which(cmd) is None}
    steps = (["brew install " + " ".join(sorted(packages))] if packages else []) + \
        ([str(BUILD_SCRIPT)] if build else [])
    return ("Missing: " + ", ".join(missing), " && ".join(steps)) if steps else None


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Windows To Go")
        self.set_default_size(640, 860)
        global _style_installed
        if not _style_installed:
            style.install()
            _style_installed = True

        self.iso_path = None
        self.editions = []
        self.disks = []
        self.image_path = None
        self.job = None
        self.close_after_job = False
        self._step = None
        self._step_base = 0.0
        self._weights = STEP_WEIGHTS

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        # The hero below carries the title, so the header bar stays empty.
        header = Adw.HeaderBar(title_widget=Gtk.Box())
        view.add_top_bar(header)

        self.banner = Adw.Banner()
        view.add_top_bar(self.banner)

        self.form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=26,
                            margin_top=4, margin_bottom=28, margin_start=18, margin_end=18)
        self.form.append(self._build_hero())
        self._build_source_group()
        self._build_target_group()
        self._build_setup_group()
        self._build_wtg_group()
        clamp = Adw.Clamp(maximum_size=600, tightening_threshold=480, child=self.form)
        view.set_content(Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER))
        view.add_bottom_bar(self._build_action_bar())

        self.toasts.set_child(view)
        self.set_content(self.toasts)

        self.connect("close-request", self._on_close_request)
        self._check_dependencies()
        self.refresh_disks()
        GLib.timeout_add_seconds(DISK_POLL_SECONDS, self._poll_disks)
        self.update_state()

    # ------------------------------------------------------------ building

    def _build_hero(self):
        icon = Gtk.Image(icon_name="drive-removable-media-symbolic", pixel_size=34,
                         halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        tile = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        tile.add_css_class("hero-icon")
        icon.set_hexpand(True)
        tile.append(icon)

        title = Gtk.Label(label="Windows To Go", xalign=0)
        title.add_css_class("hero-title")
        subtitle = Gtk.Label(label="Install a portable Windows that boots from a USB drive",
                             xalign=0, wrap=True)
        subtitle.add_css_class("hero-subtitle")
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        text.append(title)
        text.append(subtitle)

        hero = Gtk.Box(spacing=18, margin_bottom=4)
        hero.append(tile)
        hero.append(text)
        return hero

    def _section(self, number, title, description=None):
        """Append a numbered section to the form; return its list for rows."""
        badge = Gtk.Label(label=str(number), halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        badge.add_css_class("step-badge")
        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class("section-title")
        header = Gtk.Box(spacing=10)
        header.append(badge)
        header.append(heading)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(header)
        if description:
            desc = Gtk.Label(label=description, xalign=0, wrap=True, margin_start=36)
            desc.add_css_class("section-description")
            box.append(desc)
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=6)
        rows.add_css_class("boxed-list")
        box.append(rows)
        self.form.append(box)
        return rows

    def _build_source_group(self):
        group = self._section(1, "Windows image")
        self.iso_row = Adw.ActionRow(title="Installation ISO",
                                     subtitle="Choose a Windows 10 or 11 ISO")
        button = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        button.connect("clicked", self._choose_iso)
        self.iso_row.add_suffix(button)
        self.iso_row.set_activatable_widget(button)
        group.append(self.iso_row)

        self.edition_row = Adw.ComboRow(title="Edition", model=Gtk.StringList())
        self.edition_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.edition_row.add_suffix(self.edition_spinner)
        self.edition_row.connect("notify::selected", lambda *_: self.update_state())
        group.append(self.edition_row)

    def _build_target_group(self):
        group = self._section(2, "Target drive")
        # use-subtitle shows the chosen drive under the title, where a long
        # model name plus its size has room.
        self.drive_row = Adw.ComboRow(title="USB drive", model=Gtk.StringList(), use_subtitle=True)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text="Look for drives again")
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_: self.refresh_disks())
        self.drive_row.add_suffix(refresh)
        self.drive_row.connect("notify::selected", lambda *_: self.update_state())
        group.append(self.drive_row)

        self.image_switch = Adw.SwitchRow(
            title="Create a disk image file instead",
            subtitle="For testing in a virtual machine, or writing to a drive later")
        self.image_switch.connect("notify::active", lambda *_: self.update_state())
        group.append(self.image_switch)

        self.image_row = Adw.ActionRow(title="Image file", subtitle="Not chosen")
        button = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        button.connect("clicked", self._choose_image)
        self.image_row.add_suffix(button)
        self.image_row.set_activatable_widget(button)
        group.append(self.image_row)

        self.size_row = Adw.ActionRow()
        self.size_row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
        self.size_row.add_css_class("warning-row")
        group.append(self.size_row)

    def _build_setup_group(self):
        group = self._section(3, "First-time setup",
                              "Answers Windows would otherwise ask the first time it starts")
        self.account_row = Adw.EntryRow(title="Local account name", text=_default_account())
        self.account_row.connect("changed", lambda *_: self.update_state())
        group.append(self.account_row)
        self.password_row = Adw.PasswordEntryRow(title="Password (optional)")
        group.append(self.password_row)

        self.online_switch = Adw.SwitchRow(
            title="Skip Microsoft account sign-in",
            subtitle="Also lets setup finish without an internet connection", active=True)
        group.append(self.online_switch)
        self.privacy_switch = Adw.SwitchRow(
            title="Skip license and privacy questions",
            subtitle="Accepts the license and sends only required diagnostic data", active=True)
        group.append(self.privacy_switch)

        locale = customize.host_locale()
        self.region_switch = Adw.SwitchRow(
            title="Use this computer's region settings",
            subtitle=f"Language format and keyboard: {locale}" if locale else "Not available",
            active=bool(locale), sensitive=bool(locale))
        self._host_locale = locale
        group.append(self.region_switch)

        self.name_row = Adw.EntryRow(title="Computer name (optional)")
        self.name_row.connect("changed", lambda *_: self.update_state())
        group.append(self.name_row)

    def _build_wtg_group(self):
        group = self._section(4, "Windows To Go", "Settings for a Windows that moves between PCs")
        self.disks_switch = Adw.SwitchRow(
            title="Hide the host computer's internal disks",
            subtitle="Windows won't mount or change the drives of the PC it runs on", active=True)
        group.append(self.disks_switch)
        self.bitlocker_switch = Adw.SwitchRow(
            title="Prevent automatic BitLocker encryption",
            subtitle="Otherwise Windows may encrypt the drive and tie it to one PC", active=True)
        group.append(self.bitlocker_switch)
        self.hibernate_switch = Adw.SwitchRow(
            title="Turn off hibernation and Fast Startup",
            subtitle="Resuming on a different PC can corrupt the drive", active=True)
        group.append(self.hibernate_switch)

    def _build_action_bar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        bar.add_css_class("action-bar")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                        margin_top=14, margin_bottom=14, margin_start=18, margin_end=18)
        bar.append(Adw.Clamp(maximum_size=600, tightening_threshold=480, child=inner))

        # Progress card: big percentage, current step, bar, and the log.
        self.progress_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False)
        self.progress_card.add_css_class("progress-card")
        top = Gtk.Box(spacing=14)
        self.percent_label = Gtk.Label(label="0%", xalign=0, valign=Gtk.Align.CENTER)
        self.percent_label.add_css_class("percent")
        top.append(self.percent_label)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.status_label = Gtk.Label(xalign=0, ellipsize=3)
        self.status_label.add_css_class("status")
        self.detail_label = Gtk.Label(xalign=0, ellipsize=3)
        self.detail_label.add_css_class("status-detail")
        texts.append(self.status_label)
        texts.append(self.detail_label)
        top.append(texts)
        self.progress_card.append(top)

        self.progress = Gtk.ProgressBar(show_text=False)
        self.progress.add_css_class("big")
        self.progress_card.append(self.progress)

        self.log_view = Gtk.TextView(editable=False, cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.add_css_class("log")
        scroller = Gtk.ScrolledWindow(child=self.log_view, min_content_height=110)
        scroller.add_css_class("log-frame")
        self.log_expander = Gtk.Expander(label="Details", child=scroller)
        self.progress_card.append(self.log_expander)
        inner.append(self.progress_card)

        buttons = Gtk.Box(spacing=10, halign=Gtk.Align.END)
        self.cancel_button = Gtk.Button(label="Cancel", visible=False)
        self.cancel_button.add_css_class("cancel-button")
        self.cancel_button.connect("clicked", lambda *_: self._cancel())
        buttons.append(self.cancel_button)
        self.start_button = Gtk.Button(child=Adw.ButtonContent(
            label="Install", icon_name="document-save-symbolic"))
        self.start_button.add_css_class("install-button")
        self.start_button.connect("clicked", lambda *_: self._confirm_start())
        buttons.append(self.start_button)
        inner.append(buttons)
        return bar

    # ------------------------------------------------------------ state

    @property
    def edition(self):
        i = self.edition_row.get_selected()
        return self.editions[i] if self.editions and 0 <= i < len(self.editions) else None

    @property
    def disk(self):
        i = self.drive_row.get_selected()
        return self.disks[i] if self.disks and 0 <= i < len(self.disks) else None

    def _problem(self):
        """The first thing stopping an install, or None."""
        if self.job:
            return "Installing…"
        if not self.iso_path:
            return "Choose a Windows ISO"
        if not self.edition:
            return "Waiting for the edition list"
        if self.image_switch.get_active():
            if not self.image_path:
                return "Choose where to save the image"
        elif not self.disk:
            return "Plug in a USB drive"
        try:
            self._options().validate()
        except WinToGoError as e:
            return str(e)
        return None

    def update_state(self):
        image_mode = self.image_switch.get_active()
        self.drive_row.set_visible(not image_mode)
        self.image_row.set_visible(image_mode)

        warning = None
        if self.edition and not image_mode and self.disk:
            need = pipeline.required_bytes(self.edition)
            if self.disk.size < need:
                warning = (f"This drive is too small; {self.edition.name} needs at least "
                           f"{disks.human_size(need)}")
            elif self.disk.size < pipeline.RECOMMENDED_MIN:
                warning = "Little space will be left for apps and files; 64 GB or more is recommended"
            elif self.disk.mountpoints:
                warning = "This drive is mounted; it will be unmounted when you install"
        self.size_row.set_visible(bool(warning))
        self.size_row.set_title(warning or "")

        problem = self._problem()
        too_small = warning and "too small" in warning
        self.start_button.set_sensitive(problem is None and not too_small)
        self.start_button.set_tooltip_text(problem)
        self.form.set_sensitive(self.job is None)
        # While installing, Cancel is the only action.
        self.start_button.set_visible(self.job is None)

    def _check_dependencies(self):
        missing = missing_dependencies()
        if missing:
            title, command = missing
            self.banner.set_title(title)
            self.banner.set_button_label("Copy install command")
            self.banner.connect("button-clicked",
                                lambda *_: (self.get_clipboard().set(command),
                                            self.toast("Command copied")))
            self.banner.set_revealed(True)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    # ------------------------------------------------------------ source

    def _choose_iso(self, *_):
        dialog = Gtk.FileDialog(title="Choose a Windows ISO")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        iso_filter = Gtk.FileFilter(name="Disc images (*.iso)")
        iso_filter.add_suffix("iso")
        filters.append(iso_filter)
        dialog.set_filters(filters)
        dialog.open(self, None, self._iso_chosen)

    def _iso_chosen(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # dismissed
        self.load_iso(file.get_path())

    def load_iso(self, path):
        self.iso_path = path
        self.iso_row.set_subtitle(Path(path).name)
        self.editions = []
        self.edition_row.set_model(Gtk.StringList())
        self.edition_row.set_subtitle("Reading editions…")
        self.edition_spinner.set_visible(True)
        self.edition_spinner.start()
        self.update_state()

        def work():
            try:
                result = iso.read_editions(path)
                error = None
            except (WinToGoError, OSError) as e:
                result, error = [], str(e)
            GLib.idle_add(self._editions_loaded, path, result, error)
        threading.Thread(target=work, daemon=True).start()

    def _editions_loaded(self, path, editions, error):
        if path != self.iso_path:
            return  # a different ISO was chosen meanwhile
        self.edition_spinner.stop()
        self.edition_spinner.set_visible(False)
        if error:
            self.iso_path = None
            self.iso_row.set_subtitle("Choose a Windows 10 or 11 ISO")
            self.edition_row.set_subtitle("")
            self.alert("Can't use this ISO", error)
        else:
            self.editions = editions
            self.edition_row.set_model(Gtk.StringList.new(
                [f"{e.name} ({e.arch})" for e in editions]))
            # Default to Pro, the usual choice, when present.
            pro = next((i for i, e in enumerate(editions) if e.edition_id == "Professional"), 0)
            self.edition_row.set_selected(pro)
            self.edition_row.set_subtitle(f"Build {editions[0].build}" if editions else "")
        self.update_state()

    # ------------------------------------------------------------ target

    def refresh_disks(self):
        # diskutil takes a noticeable moment, so look in the background.
        def work():
            try:
                found = disks.list_usb_disks()
            except WinToGoError:
                found = []
            GLib.idle_add(self._disks_found, found)
        threading.Thread(target=work, daemon=True).start()

    def _disks_found(self, found):
        if [(d.path, d.size, bool(d.mountpoints)) for d in found] == \
                [(d.path, d.size, bool(d.mountpoints)) for d in self.disks]:
            return
        selected = self.disk.path if self.disk else None
        self.disks = found
        if found:
            self.drive_row.set_model(Gtk.StringList.new([d.label for d in found]))
            index = next((i for i, d in enumerate(found) if d.path == selected), 0)
            self.drive_row.set_selected(index)
        else:
            self.drive_row.set_model(Gtk.StringList.new(["No USB drives found"]))
        self.update_state()

    def _poll_disks(self):
        if self.job is None:
            self.refresh_disks()
        return GLib.SOURCE_CONTINUE

    def _choose_image(self, *_):
        dialog = Gtk.FileDialog(title="Save disk image", initial_name="windows-to-go.img")
        dialog.save(self, None, self._image_chosen)

    def _image_chosen(self, dialog, result):
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        self.image_path = file.get_path()
        self.image_row.set_subtitle(self.image_path)
        self.update_state()

    # ------------------------------------------------------------ running

    def _options(self):
        return customize.Options(
            portable=True,
            hide_internal_disks=self.disks_switch.get_active(),
            disable_bitlocker=self.bitlocker_switch.get_active(),
            disable_hibernation=self.hibernate_switch.get_active(),
            skip_online_account=self.online_switch.get_active(),
            skip_privacy=self.privacy_switch.get_active(),
            local_account=self.account_row.get_text().strip(),
            password=self.password_row.get_text(),
            locale=self._host_locale if self.region_switch.get_active() else "",
            computer_name=self.name_row.get_text().strip(),
        )

    def _confirm_start(self):
        if self.image_switch.get_active():
            self._start()
            return
        disk = self.disk
        body = (f"Everything on {disk.path} ({disks.human_size(disk.size)}) will be permanently "
                f"deleted and replaced with {self.edition.name}.")
        if disk.mountpoints:
            body += ("\n\nThis drive is in use right now: it is mounted at "
                     + ", ".join(disk.mountpoints) + ". Make sure nothing on it is needed.")
        name = " ".join(x for x in (disk.vendor, disk.model) if x) or disk.path
        dialog = Adw.AlertDialog(heading=f"Erase {name}?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("erase", "Erase and Install")
        dialog.set_response_appearance("erase", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "erase" and self._start())
        dialog.present(self)

    def _start(self):
        image_mode = self.image_switch.get_active()
        target = self.image_path if image_mode else self.disk.path
        if not image_mode and self.disk.mountpoints:
            if not self._unmount(self.disk):
                return
        self.log_view.get_buffer().set_text("")
        self._step, self._step_base = None, 0.0
        self._set_progress(0)
        self._set_outcome(None)
        self.progress_card.set_visible(True)
        self.cancel_button.set_visible(True)
        self._weights = STAGED_STEP_WEIGHTS if IS_MACOS and not image_mode else STEP_WEIGHTS
        self.status_label.set_text("Starting…")
        self.detail_label.set_text("Waiting for authorization" if not image_mode else "")
        self.cancel_button.set_sensitive(True)
        self.job = Job(iso=self.iso_path, edition_index=self.edition.index, target=target,
                       as_root=not image_mode, options=self._options().__dict__,
                       on_event=self._on_event, on_exit=self._on_exit, log_path=log_path())
        self._pulse_source = GLib.timeout_add(150, self._pulse)
        self.update_state()

    def _unmount(self, disk):
        """Unmount the drive's partitions through udisks or diskutil (no root needed)."""
        try:
            if IS_MACOS:
                disks.run(["diskutil", "unmountDisk", disk.path])
                return True
            out = disks.run(["lsblk", "-lnpo", "NAME,MOUNTPOINTS", disk.path])
            for line in out.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[1].strip():
                    disks.run(["udisksctl", "unmount", "-b", parts[0]])
        except WinToGoError as e:
            self.alert("Can't unmount the drive", str(e))
            return False
        return True

    def _pulse(self):
        # Steps without their own progress (extracting, flushing) animate.
        if self.job and self._step in (None, "source", "partition", "finish"):
            self.progress.pulse()
        return GLib.SOURCE_CONTINUE if self.job else GLib.SOURCE_REMOVE

    def _log(self, text):
        buffer = self.log_view.get_buffer()
        buffer.insert(buffer.get_end_iter(), text + "\n")
        self.log_view.scroll_to_mark(buffer.get_insert(), 0, False, 0, 0)

    def _on_event(self, event):
        kind = event.get("type")
        if kind == "step":
            if self._step:
                self._step_base += self._weights.get(self._step, 0) / 100
            self._step = event["id"]
            self.status_label.set_text(event["label"])
            self.detail_label.set_text("")
            self._set_progress(self._step_base)
            self._log("▸ " + event["label"])
        elif kind == "progress":
            weight = self._weights.get(self._step, 0) / 100
            self._set_progress(min(1.0, self._step_base + weight * event["fraction"]))
            if event.get("detail"):
                self.detail_label.set_text(event["detail"])
        elif kind == "log":
            self.detail_label.set_text(event["message"])
            self._log("  " + event["message"])
        elif kind == "warning":
            self._log("  ⚠ " + event["message"])
        elif kind == "done":
            self._finished_ok = True
        elif kind == "error":
            self._error = event["message"]
            self._log("✗ " + event["message"])

    def _set_progress(self, fraction):
        self.progress.set_fraction(fraction)
        self.percent_label.set_text(f"{int(fraction * 100)}%")

    def _set_outcome(self, outcome):
        """Colour the progress card for "success"/"error", or reset it (None)."""
        for widget in (self.percent_label, self.progress):
            widget.remove_css_class("success")
            widget.remove_css_class("error")
            if outcome:
                widget.add_css_class(outcome)

    def _cancel(self):
        if self.job:
            self.cancel_button.set_sensitive(False)
            self.status_label.set_text("Cancelling…")
            self.job.cancel()

    def _on_exit(self, status):
        ok = getattr(self, "_finished_ok", False)
        error = getattr(self, "_error", None)
        self._finished_ok, self._error = False, None
        image_mode = self.image_switch.get_active()
        self.job = None
        self.cancel_button.set_visible(False)
        self.update_state()
        if self.close_after_job:
            self.close()
            return
        if ok:
            self._set_progress(1)
            self._set_outcome("success")
            self.status_label.set_text("Done")
            self.detail_label.set_text("The image is ready" if image_mode else "The drive is ready")
            if image_mode:
                self.alert("The image is ready",
                           f"Saved to {self.image_path}. Write it to a drive of at least the same "
                           "size, or boot it in a virtual machine as a USB disk.")
            else:
                self.alert("Windows is ready",
                           "Plug the drive into a PC and choose it in the boot menu (usually F12, "
                           "F11, F9 or Esc at power-on). The first start takes a few minutes "
                           "while Windows finishes setting up.")
        elif error == "cancelled":
            self._set_outcome("error")
            self.status_label.set_text("Cancelled")
            self.detail_label.set_text("")
            self.toast("Installation cancelled")
        else:
            self._set_outcome("error")
            self.status_label.set_text("Failed")
            self.detail_label.set_text(error or "")
            self.alert("Installation failed",
                       f"{error or 'Unknown error'}\n\nA full log is in {log_path()}.")

    def alert(self, heading, body):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def _on_close_request(self, *_):
        if self.job is None:
            return False
        dialog = Adw.AlertDialog(
            heading="Stop installing?",
            body="The drive will be left unusable until you install again.")
        dialog.add_response("continue", "Keep Installing")
        dialog.add_response("stop", "Stop")
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("continue")

        def respond(_d, response):
            if response == "stop" and self.job:
                self.close_after_job = True
                self._cancel()
        dialog.connect("response", respond)
        dialog.present(self)
        return True


def _default_account():
    try:
        name = getpass.getuser()
    except Exception:
        return ""
    return name if name and name != "root" and len(name) <= 20 else ""
