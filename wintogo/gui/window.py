"""The main window: the two choices that matter, the options folded away
below them, and a progress page that takes over while installing."""

import getpass
import importlib.util
import shutil
import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

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

# How much of the overall progress bar each backend step takes up, in the
# order the backend runs them.
STEP_WEIGHTS = {"source": 3, "partition": 2, "apply": 82, "customize": 3, "boot": 2, "finish": 8}
# On macOS a drive is built in a file, then written out in a "write" step.
STAGED_STEP_WEIGHTS = {"source": 2, "partition": 1, "apply": 46, "customize": 2, "boot": 1,
                       "finish": 0, "write": 48}
# What the progress page lists before the backend reports each step (it
# uses the same words; "apply" names the edition).
STEP_LABELS = {"source": "Reading the Windows image", "partition": "Partitioning and formatting",
               "apply": "Installing Windows", "customize": "Configuring Windows",
               "boot": "Installing boot files", "finish": "Finishing",
               "write": "Writing to the drive"}
# Steps this big show their own percentage as well.
STEP_PERCENT_WEIGHT = 10

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


class ChoiceTile(Gtk.Box):
    """A card for one of the two choices: a caption, an icon with two lines
    of text, an optional warning chip, and buttons along the bottom."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.add_css_class("tile")
        self.caption = Gtk.Label(xalign=0)
        self.caption.add_css_class("tile-caption")
        self.append(self.caption)

        self.icon = Gtk.Image(pixel_size=24)
        self.icon_box = _icon_tile(self.icon, "tile-icon")
        self.title = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.title.add_css_class("tile-title")
        self.subtitle = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                  lines=2, ellipsize=Pango.EllipsizeMode.END)
        self.subtitle.add_css_class("tile-subtitle")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                        valign=Gtk.Align.CENTER, hexpand=True)
        texts.append(self.title)
        texts.append(self.subtitle)
        row = Gtk.Box(spacing=12)
        row.append(self.icon_box)
        row.append(texts)
        self.append(row)

        self.chip = Gtk.Box(spacing=8, visible=False)
        self.chip.add_css_class("chip")
        self.chip.append(Gtk.Image(icon_name="dialog-warning-symbolic", valign=Gtk.Align.START))
        self.chip_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.chip.append(self.chip_label)
        self.append(self.chip)

        # Pinned to the bottom, so both tiles' buttons line up.
        self.buttons = Gtk.Box(spacing=8, valign=Gtk.Align.END, vexpand=True)
        self.append(self.buttons)

    def show(self, caption, icon_name, title, subtitle, empty=False):
        self.caption.set_text(caption)
        self.icon.set_from_icon_name(icon_name)
        self.title.set_text(title)
        self.subtitle.set_text(subtitle)
        self.subtitle.set_tooltip_text(subtitle)
        _toggle_class(self.icon_box, "empty", empty)
        _toggle_class(self.title, "empty", empty)

    def set_chip(self, text=None, error=False):
        self.chip.set_visible(bool(text))
        self.chip_label.set_text(text or "")
        _toggle_class(self.chip, "error", error)
        _toggle_class(self.chip, "warning", not error)


class StepRow(Gtk.Box):
    """One backend step on the progress page: an icon for its state, its
    label, and (for the long steps) its own percentage."""

    STATES = {"pending": "", "current": "in progress", "done": "done",
              "failed": "failed", "stopped": "cancelled"}

    def __init__(self, label, show_percent):
        super().__init__(spacing=12)
        self.add_css_class("step-row")
        self.icons = Gtk.Stack(valign=Gtk.Align.CENTER, hexpand=False)
        pending = Gtk.Box()
        pending.add_css_class("step-icon")
        pending.add_css_class("pending")
        self.icons.add_named(pending, "pending")
        self.icons.add_named(_spinner(20), "current")
        for state, icon_name in (("done", "object-select-symbolic"),
                                 ("failed", "window-close-symbolic"),
                                 ("stopped", "list-remove-symbolic")):
            box = _icon_tile(Gtk.Image(icon_name=icon_name, pixel_size=12), "step-icon")
            box.add_css_class(state)
            self.icons.add_named(box, state)
        self.append(self.icons)

        self.label = Gtk.Label(label=label, xalign=0, hexpand=True, wrap=True)
        self.label.add_css_class("step-label")
        self.append(self.label)
        self.meta = Gtk.Label(xalign=1)
        self.meta.add_css_class("step-meta")
        self.append(self.meta)
        self.show_percent = show_percent
        self.state = None
        self.set_state("pending")

    def set_state(self, state):
        if state == self.state:
            return
        self.state = state
        self.icons.set_visible_child_name(state)
        for name in self.STATES:
            _toggle_class(self.label, name, name == state)
        self.label.update_property([Gtk.AccessibleProperty.DESCRIPTION], [self.STATES[state]])
        if state != "current":
            self.meta.set_text("")

    def set_fraction(self, fraction):
        if self.show_percent:
            self.meta.set_text(f"{int(fraction * 100)}%")


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Windows To Go")
        self.set_default_size(640, 820)
        # Breakpoints need a minimum size to work from.
        self.set_size_request(360, 480)
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
        self._weights = STEP_WEIGHTS
        self._step = None
        self._step_base = 0.0
        self._step_rows = {}
        self._finished_ok = False
        self._error = None
        # True while the progress page shows how the last job ended.
        self._showing_result = False
        self._primary_action = self._secondary_action = None
        self._refresh_labelled = None
        self._host_locale = customize.host_locale()

        self._add_actions()

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        # The hero below carries the title, so the header bar stays empty.
        view.add_top_bar(Adw.HeaderBar(title_widget=Gtk.Box()))
        self.banner = Adw.Banner()
        view.add_top_bar(self.banner)

        form, progress = self._build_form(), self._build_progress()
        bar, bar_content = self._build_action_bar()
        # The pages and the bottom bar line up, as wide as the widest needs.
        widths = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        for widget in (form, progress, bar_content):
            widths.add_widget(widget)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(_scrolling(form), "form")
        self.stack.add_named(_scrolling(progress), "progress")
        view.set_content(self.stack)
        view.add_bottom_bar(bar)

        self.toasts.set_child(view)
        self.set_content(self.toasts)

        if hasattr(Adw, "Breakpoint"):
            # Too narrow for two tiles side by side: stack them.
            breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 540sp"))
            breakpoint.add_setter(self.tiles, "orientation",
                                  GObject.Value(Gtk.Orientation, Gtk.Orientation.VERTICAL))
            self.add_breakpoint(breakpoint)

        self.connect("close-request", self._on_close_request)
        self._check_dependencies()
        self.refresh_disks()
        GLib.timeout_add_seconds(DISK_POLL_SECONDS, self._poll_disks)
        self.update_state()

    # ------------------------------------------------------------ building

    def _add_actions(self):
        # The edition and drive menus are radio items of these.
        self.edition_action = Gio.SimpleAction.new_stateful(
            "edition", GLib.VariantType.new("i"), GLib.Variant("i", -1))
        self.drive_action = Gio.SimpleAction.new_stateful(
            "drive", GLib.VariantType.new("s"), GLib.Variant("s", ""))
        for action in (self.edition_action, self.drive_action):
            action.connect("change-state", self._on_choice)
            self.add_action(action)

    def _on_choice(self, action, value):
        action.set_state(value)
        self.update_state()

    def _build_form(self):
        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28,
                       margin_top=8, margin_bottom=28, margin_start=18, margin_end=18)
        form.append(self._build_hero())

        choices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        # The tiles' buttons expand downwards; keep that inside the tiles.
        self.tiles = Gtk.Box(spacing=12, homogeneous=True, vexpand=False)
        self.tiles.append(self._build_source_tile())
        self.tiles.append(self._build_target_tile())
        choices.append(self.tiles)
        self.image_switch = Adw.SwitchRow(
            title="Create a disk image file instead",
            subtitle="For testing in a virtual machine, or writing to a drive later")
        self.image_switch.connect("notify::active", lambda *_: self.update_state())
        choices.append(_boxed_list(self.image_switch))
        form.append(choices)

        form.append(self._build_options())
        return form

    def _build_hero(self):
        tile = _icon_tile(Gtk.Image(icon_name="drive-removable-media-symbolic", pixel_size=30),
                          "hero-icon")

        title = Gtk.Label(label="Windows To Go", xalign=0)
        title.add_css_class("hero-title")
        subtitle = Gtk.Label(label="Install a portable Windows that boots from a USB drive",
                             xalign=0, wrap=True)
        subtitle.add_css_class("hero-subtitle")
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        text.append(title)
        text.append(subtitle)

        hero = Gtk.Box(spacing=16)
        hero.append(tile)
        hero.append(text)
        return hero

    def _build_source_tile(self):
        tile = self.source_tile = ChoiceTile()
        self.edition_button = Gtk.MenuButton(label="Edition", always_show_arrow=True,
                                             tooltip_text="Choose the Windows edition")
        tile.buttons.append(self.edition_button)
        self.iso_button = Gtk.Button()
        self.iso_button.connect("clicked", self._choose_iso)
        tile.buttons.append(self.iso_button)
        self.edition_spinner = _spinner(18)
        self.edition_spinner.set_visible(False)
        self.edition_spinner.set_valign(Gtk.Align.CENTER)
        tile.buttons.append(self.edition_spinner)
        return tile

    def _build_target_tile(self):
        tile = self.target_tile = ChoiceTile()
        self.drive_button = Gtk.MenuButton(label="Other Drive", always_show_arrow=True,
                                           tooltip_text="Choose another USB drive")
        tile.buttons.append(self.drive_button)
        self.refresh_button = Gtk.Button(tooltip_text="Look for drives again")
        self.refresh_button.connect("clicked", lambda *_: self.refresh_disks())
        tile.buttons.append(self.refresh_button)
        self.image_button = Gtk.Button()
        self.image_button.connect("clicked", self._choose_image)
        tile.buttons.append(self.image_button)
        return tile

    def _build_options(self):
        title = Gtk.Label(label="Options", xalign=0)
        title.add_css_class("section-title")
        note = Gtk.Label(label="The defaults suit most drives", xalign=0, wrap=True)
        note.add_css_class("section-note")
        heading = Gtk.Box(spacing=10)
        heading.append(title)
        heading.append(note)

        # Titles and summaries are plain text: an account name may contain "&".
        self.setup_row = Adw.ExpanderRow(title="First-time setup", use_markup=False)
        self.setup_row.add_prefix(Gtk.Image(icon_name="avatar-default-symbolic"))
        self.account_row = Adw.EntryRow(title="Local account name", text=_default_account())
        self.account_row.connect("changed", lambda *_: self.update_state())
        self.password_row = Adw.PasswordEntryRow(title="Password (optional)")
        self.online_switch = Adw.SwitchRow(
            title="Skip Microsoft account sign-in",
            subtitle="Also lets setup finish without an internet connection", active=True)
        self.privacy_switch = Adw.SwitchRow(
            title="Skip license and privacy questions",
            subtitle="Accepts the license and sends only required diagnostic data", active=True)
        locale = self._host_locale
        self.region_switch = Adw.SwitchRow(
            title="Use this computer's region settings",
            subtitle=f"Language format and keyboard: {locale}" if locale else "Not available",
            active=bool(locale), sensitive=bool(locale))
        self.name_row = Adw.EntryRow(title="Computer name (optional)")
        self.name_row.connect("changed", lambda *_: self.update_state())
        for row in (self.account_row, self.password_row, self.online_switch,
                    self.privacy_switch, self.region_switch, self.name_row):
            self.setup_row.add_row(row)

        self.wtg_row = Adw.ExpanderRow(title="Moving between PCs", use_markup=False)
        self.wtg_row.add_prefix(Gtk.Image(icon_name="security-high-symbolic"))
        self.disks_switch = Adw.SwitchRow(
            title="Hide the host computer's internal disks",
            subtitle="Windows won't mount or change the drives of the PC it runs on", active=True)
        self.bitlocker_switch = Adw.SwitchRow(
            title="Prevent automatic BitLocker encryption",
            subtitle="Otherwise Windows may encrypt the drive and tie it to one PC", active=True)
        self.hibernate_switch = Adw.SwitchRow(
            title="Turn off hibernation and Fast Startup",
            subtitle="Resuming on a different PC can corrupt the drive", active=True)
        for row in (self.disks_switch, self.bitlocker_switch, self.hibernate_switch):
            self.wtg_row.add_row(row)

        for switch in (self.online_switch, self.privacy_switch, self.region_switch,
                       self.disks_switch, self.bitlocker_switch, self.hibernate_switch):
            switch.connect("notify::active", lambda *_: self.update_state())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(heading)
        box.append(_boxed_list(self.setup_row, self.wtg_row))
        return box

    def _build_progress(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20,
                       margin_top=8, margin_bottom=28, margin_start=18, margin_end=18)

        # What is being installed where, with which options.
        self.run_icon = Gtk.Image(pixel_size=22)
        icon_box = _icon_tile(self.run_icon, "run-icon")
        self.run_title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.run_title.add_css_class("run-title")
        self.run_detail = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.run_detail.add_css_class("run-detail")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                        valign=Gtk.Align.CENTER, hexpand=True)
        texts.append(self.run_title)
        texts.append(self.run_detail)
        summary = Gtk.Box(spacing=14)
        summary.add_css_class("run-summary")
        summary.append(icon_box)
        summary.append(texts)
        page.append(summary)

        # Big percentage, current step, bar, and every step's state.
        self.percent_label = Gtk.Label(label="0%", xalign=0, valign=Gtk.Align.END)
        self.percent_label.add_css_class("percent")
        self.status_label = Gtk.Label(xalign=0, wrap=True)
        self.status_label.add_css_class("status")
        self.detail_label = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.detail_label.add_css_class("status-detail")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                        valign=Gtk.Align.END, hexpand=True)
        texts.append(self.status_label)
        texts.append(self.detail_label)
        top = Gtk.Box(spacing=18)
        top.append(self.percent_label)
        top.append(texts)
        self.progress = Gtk.ProgressBar()
        self.progress.add_css_class("big")
        self.steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        card.add_css_class("status-card")
        card.append(top)
        card.append(self.progress)
        card.append(self.steps_box)
        page.append(card)

        heading = Gtk.Label(label="Details", xalign=0, hexpand=True)
        heading.add_css_class("log-title")
        copy = Gtk.Button(label="Copy Log")
        copy.add_css_class("flat")
        copy.connect("clicked", self._copy_log)
        header = Gtk.Box(spacing=12)
        header.append(heading)
        header.append(copy)
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.add_css_class("log")
        # The log is light text on a dark panel in both colour schemes.
        buffer = self.log_view.get_buffer()
        for tag, colour in (("step", "#f2f0eb"), ("line", "#b9b5ad"), ("warning", "#f5d78a"),
                            ("error", "#ff9c94"), ("success", "#8fd6a8")):
            buffer.create_tag(tag, foreground=colour)
        scroller = Gtk.ScrolledWindow(child=self.log_view, height_request=168)
        scroller.add_css_class("log-frame")
        log = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        log.append(header)
        log.append(scroller)
        page.append(log)
        return page

    def _build_action_bar(self):
        self.footer_title = Gtk.Label(xalign=0, wrap=True)
        self.footer_title.add_css_class("footer-title")
        self.footer_detail = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.footer_detail.add_css_class("footer-detail")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                        valign=Gtk.Align.CENTER, hexpand=True)
        texts.append(self.footer_title)
        texts.append(self.footer_detail)

        self.secondary_button = Gtk.Button()
        self.secondary_button.connect("clicked", lambda *_: self._secondary_action())
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.add_css_class("cancel-button")
        self.cancel_button.connect("clicked", lambda *_: self._cancel())
        self.start_button = Gtk.Button(child=Adw.ButtonContent(
            label="Install", icon_name="document-save-symbolic"))
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", lambda *_: self._confirm_start())
        self.primary_button = Gtk.Button()
        self.primary_button.add_css_class("suggested-action")
        self.primary_button.connect("clicked", lambda *_: self._primary_action())
        buttons = Gtk.Box(spacing=10, valign=Gtk.Align.CENTER)
        for button in (self.secondary_button, self.cancel_button,
                       self.start_button, self.primary_button):
            button.add_css_class("footer-button")
            buttons.append(button)

        inner = Gtk.Box(spacing=16, margin_top=14, margin_bottom=14,
                        margin_start=18, margin_end=18)
        inner.append(texts)
        inner.append(buttons)
        bar = Gtk.Box()
        bar.add_css_class("action-bar")
        bar.append(_clamp(inner))
        return bar, inner

    # ------------------------------------------------------------ state

    @property
    def edition(self):
        i = self.edition_action.get_state().get_int32()
        return self.editions[i] if 0 <= i < len(self.editions) else None

    @property
    def disk(self):
        path = self.drive_action.get_state().get_string()
        return next((d for d in self.disks if d.path == path), None)

    def _problem(self):
        """What stops an install, as (what to do, more detail), or None."""
        image_mode = self.image_switch.get_active()
        if not self.iso_path:
            then = "" if image_mode or self.disk else "Then the USB drive to install onto"
            return "Choose a Windows ISO", then
        if not self.edition:
            return "Reading the Windows image…", ""
        if image_mode:
            if not self.image_path:
                return "Choose where to save the image", ""
        elif not self.disk:
            return "Plug in a USB drive", "It shows up here when you do"
        elif self.disk.size < pipeline.required_bytes(self.edition):
            need = disks.human_size(pipeline.required_bytes(self.edition))
            return "This drive is too small", f"{self.edition.name} needs at least {need}"
        try:
            self._options().validate()
        except WinToGoError as e:
            return _sentence(str(e)), "Change it under First-time setup"
        return None

    def _target_name(self):
        if self.image_switch.get_active():
            return Path(self.image_path).name
        return _disk_name(self.disk)

    def update_state(self):
        image_mode = self.image_switch.get_active()
        self._update_source_tile()
        self._update_target_tile(image_mode)

        self.setup_row.set_subtitle(self._setup_summary())
        self.wtg_row.set_subtitle(self._wtg_summary())
        account = self.account_row.get_text().strip()
        name = self.name_row.get_text().strip()
        _toggle_class(self.account_row, "error", not _valid(local_account=account))
        _toggle_class(self.name_row, "error", not _valid(computer_name=name))

        if self.job or self._showing_result:
            return
        problem = self._problem()
        if problem:
            title, detail = problem
        elif image_mode:
            title = f"{self.edition.name} → {self._target_name()}"
            detail = "Nothing is erased, and no password is needed"
        else:
            title = f"{self.edition.name} → {self._target_name()}"
            detail = f"Erases everything on {self.disk.path} ({disks.human_size(self.disk.size)})"
        self._set_footer(title, detail, install=True)
        self.start_button.set_sensitive(problem is None)

    def _update_source_tile(self):
        tile = self.source_tile
        loading = bool(self.iso_path) and not self.editions
        if not self.iso_path:
            tile.show("1 · INSTALL", "media-optical-symbolic", "No Windows image",
                      "A Windows 10 or 11 ISO", empty=True)
        else:
            edition = self.edition
            tile.show("1 · INSTALL", "media-optical-symbolic",
                      edition.name if edition else "Reading the image…",
                      Path(self.iso_path).name, empty=edition is None)
        self.edition_spinner.set_visible(loading)
        self.edition_button.set_visible(len(self.editions) > 1)
        self.iso_button.set_label("Change ISO…" if self.iso_path else "Choose ISO…")
        _toggle_class(self.iso_button, "suggested-action", not self.iso_path)
        _toggle_class(self.iso_button, "flat", bool(self.iso_path))

    def _update_target_tile(self, image_mode):
        tile = self.target_tile
        self.image_button.set_visible(image_mode)
        self.refresh_button.set_visible(not image_mode)
        self.drive_button.set_visible(not image_mode and len(self.disks) > 1)
        if image_mode:
            path = self.image_path
            tile.show("2 · INTO AN IMAGE FILE", "document-save-symbolic",
                      Path(path).name if path else "Disk image file",
                      _tilde(path) if path else "Not chosen yet", empty=not path)
            self.image_button.set_label("Change…" if path else "Choose…")
            tile.set_chip(None)
            return

        disk = self.disk
        if disk:
            tile.show("2 · ONTO THIS DRIVE", "drive-removable-media-symbolic", _disk_name(disk),
                      f"{disks.human_size(disk.size)} · {disk.path}")
        else:
            tile.show("2 · ONTO THIS DRIVE", "drive-removable-media-symbolic", "No USB drive",
                      "Plug one in; it shows up here", empty=True)
        # With no drive, refreshing is the only thing to do, so it gets a label.
        labelled = disk is None
        if labelled != self._refresh_labelled:
            self._refresh_labelled = labelled
            if labelled:
                self.refresh_button.set_child(Adw.ButtonContent(
                    icon_name="view-refresh-symbolic", label="Look Again"))
            else:
                self.refresh_button.set_icon_name("view-refresh-symbolic")
            _toggle_class(self.refresh_button, "flat", not labelled)
        tile.set_chip(*(self._drive_warning() or ()))

    def _drive_warning(self):
        """(text, is_error) about the chosen drive, or None."""
        disk, edition = self.disk, self.edition
        if disk is None:
            return None
        if edition and disk.size < pipeline.required_bytes(edition):
            need = disks.human_size(pipeline.required_bytes(edition))
            return f"Too small: {edition.name} needs at least {need}", True
        if disk.size < pipeline.RECOMMENDED_MIN:
            return "Little room left for apps and files; 64 GB or more is recommended", False
        if disk.mountpoints:
            return "Mounted; it will be unmounted first", False
        return None

    def _setup_summary(self):
        account = self.account_row.get_text().strip()
        parts = [f"Account {account}" if account else "No account",
                 "offline setup" if self.online_switch.get_active() else "Microsoft sign-in"]
        if self.region_switch.get_active() and self._host_locale:
            parts.append(self._host_locale)
        name = self.name_row.get_text().strip()
        if name:
            parts.append(f"named {name}")
        return " · ".join(parts)

    def _wtg_parts(self):
        return [text for switch, text in ((self.disks_switch, "host disks hidden"),
                                          (self.bitlocker_switch, "no BitLocker"),
                                          (self.hibernate_switch, "no hibernation"))
                if switch.get_active()]

    def _wtg_summary(self):
        parts = self._wtg_parts()
        return _sentence(" · ".join(parts)) if parts else "All off"

    def _set_footer(self, title, detail="", *, install=False, cancel=False,
                    secondary=None, primary=None):
        """Fill the bottom bar. secondary and primary are (label, callback)."""
        self.footer_title.set_text(title)
        self.footer_title.set_visible(bool(title))
        self.footer_detail.set_text(detail)
        self.footer_detail.set_visible(bool(detail))
        self.start_button.set_visible(install)
        self.cancel_button.set_visible(cancel)
        self.cancel_button.set_sensitive(True)
        for button, action, attr in ((self.secondary_button, secondary, "_secondary_action"),
                                     (self.primary_button, primary, "_primary_action")):
            button.set_visible(action is not None)
            if action:
                button.set_label(action[0])
                setattr(self, attr, action[1])

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
        self._set_editions([])
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
        if error:
            self.iso_path = None
            self.alert("Can't use this ISO", error)
        else:
            self._set_editions(editions)
        self.update_state()

    def _set_editions(self, editions):
        self.editions = editions
        menu = Gio.Menu()
        for i, e in enumerate(editions):
            item = Gio.MenuItem.new(f"{e.name} ({e.arch})", None)
            item.set_action_and_target_value("win.edition", GLib.Variant("i", i))
            menu.append_item(item)
        self.edition_button.set_menu_model(menu)
        # Default to Pro, the usual choice, when present.
        pro = next((i for i, e in enumerate(editions) if e.edition_id == "Professional"), 0)
        self.edition_action.set_state(GLib.Variant("i", pro if editions else -1))

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
        menu = Gio.Menu()
        for d in found:
            item = Gio.MenuItem.new(d.label, None)
            item.set_action_and_target_value("win.drive", GLib.Variant("s", d.path))
            menu.append_item(item)
        self.drive_button.set_menu_model(menu)
        if not any(d.path == selected for d in found):
            selected = found[0].path if found else ""
        self.drive_action.set_state(GLib.Variant("s", selected))
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
        dialog = Adw.AlertDialog(heading=f"Erase {_disk_name(disk)}?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("erase", "Erase and Install")
        dialog.set_response_appearance("erase", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "erase" and self._start())
        dialog.present(self)

    def _retry(self):
        # The drive may have gone (unplugged, or ejected by a failed write).
        if self._problem():
            self._back()
        else:
            self._confirm_start()

    def _start(self):
        image_mode = self.image_switch.get_active()
        target = self.image_path if image_mode else self.disk.path
        if not image_mode and self.disk.mountpoints:
            if not self._unmount(self.disk):
                return
        self._weights = STAGED_STEP_WEIGHTS if IS_MACOS and not image_mode else STEP_WEIGHTS
        self._show_progress_page(image_mode)
        self.job = Job(iso=self.iso_path, edition_index=self.edition.index, target=target,
                       as_root=not image_mode, options=self._options().__dict__,
                       on_event=self._on_event, on_exit=self._on_exit, log_path=log_path())

    def _show_progress_page(self, image_mode):
        name = self._target_name()
        self.run_icon.set_from_icon_name(
            "document-save-symbolic" if image_mode else "drive-removable-media-symbolic")
        self.run_title.set_text(f"{self.edition.name} → {name}")
        self.run_detail.set_text(" · ".join([self._setup_summary()] + self._wtg_parts()))

        while child := self.steps_box.get_first_child():
            self.steps_box.remove(child)
        self._step_rows = {}
        for step, weight in self._weights.items():
            if weight:
                label = f"Installing {self.edition.name}" if step == "apply" else STEP_LABELS[step]
                row = self._step_rows[step] = StepRow(label, weight >= STEP_PERCENT_WEIGHT)
                self.steps_box.append(row)

        self.log_view.get_buffer().set_text("")
        self._step, self._step_base = None, 0.0
        self._finished_ok, self._error = False, None
        self._showing_result = False
        self._set_progress(0)
        self._set_outcome(None)
        self.status_label.set_text("Starting…")
        self.detail_label.set_text("" if image_mode else "Waiting for authorization")
        self._set_footer("", f"Building {name}" if image_mode else
                         "Keep the drive plugged in until this finishes", cancel=True)
        self.stack.set_visible_child_name("progress")

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

    def _log(self, text, tag="line"):
        prefix = {"step": "▸ ", "line": "  ", "warning": "  ⚠ ",
                  "error": "✗ ", "success": "✓ "}[tag]
        buffer = self.log_view.get_buffer()
        buffer.insert_with_tags_by_name(buffer.get_end_iter(), prefix + text + "\n", tag)
        self.log_view.scroll_to_mark(buffer.get_insert(), 0, False, 0, 0)

    def _copy_log(self, *_):
        buffer = self.log_view.get_buffer()
        self.get_clipboard().set(buffer.get_text(*buffer.get_bounds(), False))
        self.toast("Log copied")

    def _mark_steps(self, current):
        """Steps before current are done, current is running."""
        order = list(self._weights)
        index = order.index(current) if current in order else len(order)
        for step, row in self._step_rows.items():
            i = order.index(step)
            row.set_state("done" if i < index else "current" if i == index else "pending")

    def _on_event(self, event):
        kind = event.get("type")
        if kind == "step":
            if self._step:
                self._step_base += self._weights.get(self._step, 0) / 100
            self._step = event["id"]
            self._mark_steps(self._step)
            if self._step in self._step_rows:
                self._step_rows[self._step].label.set_text(event["label"])
            self.status_label.set_text(event["label"])
            self.detail_label.set_text("")
            self._set_progress(self._step_base)
            self._log(event["label"], "step")
        elif kind == "progress":
            weight = self._weights.get(self._step, 0) / 100
            self._set_progress(min(1.0, self._step_base + weight * event["fraction"]))
            if self._step in self._step_rows:
                self._step_rows[self._step].set_fraction(event["fraction"])
            if event.get("detail"):
                self.detail_label.set_text(event["detail"])
        elif kind == "log":
            self.detail_label.set_text(event["message"])
            self._log(event["message"])
        elif kind == "warning":
            self._log(event["message"], "warning")
        elif kind == "done":
            self._finished_ok = True
        elif kind == "error":
            self._error = event["message"]
            self._log(event["message"], "error")

    def _set_progress(self, fraction):
        self.progress.set_fraction(fraction)
        self.percent_label.set_text(f"{int(fraction * 100)}%")

    def _set_outcome(self, outcome):
        """Colour the progress card for "success", "error" or "stopped", or reset it (None)."""
        for widget in (self.percent_label, self.progress, self.status_label):
            for name in ("success", "error", "stopped"):
                _toggle_class(widget, name, name == outcome)

    def _cancel(self):
        if self.job:
            self.cancel_button.set_sensitive(False)
            self.status_label.set_text("Cancelling…")
            self.job.cancel()

    def _on_exit(self, status):
        ok, error = self._finished_ok, self._error
        image_mode = self.image_switch.get_active()
        self.job = None
        if self.close_after_job:
            self.close()
            return
        self._showing_result = True
        current = self._step_rows.get(self._step)
        if ok:
            for row in self._step_rows.values():
                row.set_state("done")
            self._set_progress(1)
            self._set_outcome("success")
            self._log("Done", "success")
            if image_mode:
                self.status_label.set_text("The image is ready")
                self.detail_label.set_text(f"Saved to {_tilde(self.image_path)}")
                self._set_footer("", "Boot it with wintogo-vm, or write it to a drive at least "
                                 "as large", secondary=("Show in Folder", self._show_image),
                                 primary=("Done", self._back))
            else:
                self.status_label.set_text("Windows is ready")
                self.detail_label.set_text("The drive was ejected; you can unplug it" if IS_MACOS
                                           else "You can unplug the drive")
                self._set_footer("", "Plug the drive into a PC and choose it in the boot menu "
                                 "(usually F12, F11, F9 or Esc at power-on). The first start "
                                 "takes a few minutes.", primary=("Done", self._back))
        elif error == "cancelled":
            if current:
                current.set_state("stopped")
            self._set_outcome("stopped")
            self.status_label.set_text("Cancelled")
            self.detail_label.set_text(
                f"Stopped while {_lower_first(current.label.get_text())}" if current else "")
            self._set_footer("", "The unfinished image was deleted" if image_mode else
                             "The drive is unusable until you install again",
                             primary=("Back", self._back))
        else:
            if current:
                current.set_state("failed")
            self._set_outcome("error")
            self.status_label.set_text("Installation failed")
            self.detail_label.set_text(error or "Unknown error")
            self._set_footer("", f"The full log is in {_tilde(log_path())}",
                             secondary=("Back", self._back), primary=("Try Again", self._retry))

    def _back(self):
        self._showing_result = False
        self.stack.set_visible_child_name("form")
        self.refresh_disks()
        self.update_state()

    def _show_image(self):
        if IS_MACOS:
            subprocess.Popen(["open", "-R", self.image_path])
            return

        def opened(launcher, result):
            try:
                launcher.open_containing_folder_finish(result)
            except GLib.Error as e:
                self.toast(f"Can't open the folder: {e.message}")
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(self.image_path))
        launcher.open_containing_folder(self, None, opened)

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


def _clamp(child):
    clamp = Adw.Clamp(maximum_size=600, tightening_threshold=600, child=child, hexpand=True)
    if hasattr(Adw, "LengthUnit"):
        # Not scaled by the font DPI, which is 72 on macOS: that made it 450 pixels.
        clamp.set_unit(Adw.LengthUnit.PX)
    return clamp


def _scrolling(child):
    return Gtk.ScrolledWindow(child=_clamp(child), hscrollbar_policy=Gtk.PolicyType.NEVER)


def _icon_tile(image, css_class):
    """A rounded square, sized in CSS, with the icon centred in it."""
    image.set_hexpand(True)
    image.set_halign(Gtk.Align.CENTER)
    image.set_valign(Gtk.Align.CENTER)
    # An explicit hexpand=False stops the icon's expanding from spreading upwards.
    tile = Gtk.Box(valign=Gtk.Align.CENTER, hexpand=False)
    tile.add_css_class(css_class)
    tile.append(image)
    return tile


def _boxed_list(*rows):
    box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    box.add_css_class("boxed-list")
    for row in rows:
        box.append(row)
    return box


def _spinner(size):
    # Adw.Spinner is libadwaita 1.6+.
    if hasattr(Adw, "Spinner"):
        spinner = Adw.Spinner()
    else:
        spinner = Gtk.Spinner(spinning=True)
    spinner.set_size_request(size, size)
    return spinner


def _toggle_class(widget, name, on):
    if on:
        widget.add_css_class(name)
    else:
        widget.remove_css_class(name)


def _valid(**fields):
    try:
        customize.Options(**fields).validate()
    except WinToGoError:
        return False
    return True


def _disk_name(disk):
    return " ".join(x for x in (disk.vendor, disk.model) if x) or disk.path


def _tilde(path):
    path, home = str(path), str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def _sentence(text):
    return text[:1].upper() + text[1:]


def _lower_first(text):
    return text[:1].lower() + text[1:]


def _default_account():
    try:
        name = getpass.getuser()
    except Exception:
        return ""
    return name if name and name != "root" and len(name) <= 20 else ""
