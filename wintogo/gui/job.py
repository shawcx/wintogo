"""Running the backend from the GUI and relaying its JSON progress events.

Writing to a drive needs root, so the backend runs through pkexec (on macOS,
sudo with a password dialog); building an image file doesn't, so it runs
directly. Either way the GUI talks to it the same way: the options as one
JSON line on stdin, then "cancel" to stop; progress comes back as JSON lines
on stdout.
"""

import json
import os
import sys
from pathlib import Path

from gi.repository import Gio, GLib

from ..core.util import IS_MACOS

BIN = Path(__file__).resolve().parents[2] / "bin"
BACKEND = BIN / "wintogo"
ASKPASS = BIN / "wintogo-askpass"


class Job:
    def __init__(self, *, iso, edition_index, target, as_root, options,
                 on_event, on_exit, vhdx=False, log_path=None):
        argv = [sys.executable, str(BACKEND), "create", "--json", "--stdin-control", "--yes",
                "--iso", str(iso), "--edition", str(edition_index), "--target", str(target)]
        if vhdx:
            argv.append("--vhdx")
        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE
            | Gio.SubprocessFlags.STDERR_MERGE)
        if as_root and IS_MACOS:
            argv = ["sudo", "-A", "--"] + argv
            launcher.setenv("SUDO_ASKPASS", str(ASKPASS), True)
        elif as_root:
            argv = ["pkexec"] + argv
        self._on_event = on_event
        self._on_exit = on_exit
        self._saw_result = False
        self._sudo_error = None
        self._log = open(log_path, "w") if log_path else None
        self._log_line("$ " + " ".join(argv))

        self._proc = launcher.spawnv(argv)
        self._stdin = self._proc.get_stdin_pipe()
        self._write(json.dumps(options) + "\n")
        self._stdout = Gio.DataInputStream.new(self._proc.get_stdout_pipe())
        self._read_next()
        self._proc.wait_async(None, self._finished)

    def cancel(self):
        self._write("cancel\n")

    def _write(self, text):
        try:
            self._stdin.write_all(text.encode(), None)
            self._stdin.flush(None)
        except GLib.Error:
            pass  # the backend has already exited

    def _log_line(self, line):
        if self._log:
            self._log.write(line + "\n")
            self._log.flush()

    def _read_next(self):
        self._stdout.read_line_async(GLib.PRIORITY_DEFAULT, None, self._got_line)

    def _got_line(self, stream, result):
        try:
            line, _ = stream.read_line_finish_utf8(result)
        except GLib.Error as e:
            line = None
            self._on_event({"type": "log", "message": f"read error: {e.message}"})
        if line is None:
            return
        self._log_line(line)
        try:
            event = json.loads(line)
        except ValueError:
            # Not a progress event: pkexec's or sudo's own messages, a Python traceback.
            event = {"type": "log", "message": line}
            if line.startswith("sudo:"):
                self._sudo_error = line
        if event.get("type") in ("done", "error"):
            self._saw_result = True
        self._on_event(event)
        self._read_next()

    def _finished(self, proc, result):
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        status = proc.get_exit_status() if proc.get_if_exited() else -1
        self._log_line(f"exit status {status}")
        if self._log:
            self._log.close()
        # pkexec exits 126 when the password dialog is dismissed, 127 when
        # authorization fails; sudo says why on stderr. None sends any event.
        if not self._saw_result:
            if self._sudo_error and "incorrect password" in self._sudo_error:
                self._on_event({"type": "error", "message": "Not authorized to write to the drive."})
            elif self._sudo_error:
                self._on_event({"type": "error", "message": "Authentication was cancelled."})
            elif status == 126:
                self._on_event({"type": "error", "message": "Authentication was cancelled."})
            elif status == 127:
                self._on_event({"type": "error", "message": "Not authorized to write to the drive."})
            else:
                self._on_event({"type": "error",
                                "message": f"The installer stopped unexpectedly (exit status {status})."})
        self._on_exit(status)


def log_path():
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "wintogo"
    base.mkdir(parents=True, exist_ok=True)
    return base / "last-run.log"
