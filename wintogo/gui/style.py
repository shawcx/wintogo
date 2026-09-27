"""Loading the app stylesheet, with dark overrides that follow the system."""

from pathlib import Path

from gi.repository import Adw, Gdk, Gtk

HERE = Path(__file__).resolve().parent
# Above the theme, below user overrides (~/.config/gtk-4.0/gtk.css).
PRIORITY = Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION


def install():
    display = Gdk.Display.get_default()
    base = Gtk.CssProvider()
    base.load_from_path(str(HERE / "style.css"))
    Gtk.StyleContext.add_provider_for_display(display, base, PRIORITY)

    # GTK 4.14 has no prefers-color-scheme media query, so add or remove the
    # dark overrides whenever libadwaita switches scheme.
    dark = Gtk.CssProvider()
    dark.load_from_path(str(HERE / "style-dark.css"))
    manager = Adw.StyleManager.get_default()

    def update(*_):
        Gtk.StyleContext.remove_provider_for_display(display, dark)
        if manager.get_dark():
            Gtk.StyleContext.add_provider_for_display(display, dark, PRIORITY + 1)

    manager.connect("notify::dark", update)
    update()
