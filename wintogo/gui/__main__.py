import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

from ..core.util import setup_environment  # noqa: E402
from .window import MainWindow  # noqa: E402

APP_ID = "io.github.wintogo"


def main(argv=None):
    setup_environment()
    app = Adw.Application(application_id=APP_ID)

    def activate(app):
        window = app.get_active_window() or MainWindow(app)
        window.present()
        # An ISO passed on the command line is loaded straight away.
        args = (argv or sys.argv)[1:]
        if args and args[0].lower().endswith(".iso"):
            window.load_iso(args[0])

    app.connect("activate", activate)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
