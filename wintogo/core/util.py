"""Subprocess helpers, errors and progress reporting shared by the backend."""

import json
import shutil
import subprocess
import sys


class WinToGoError(Exception):
    """A failure the user should see, with a message that explains it."""


def require(*commands):
    missing = [c for c in commands if shutil.which(c) is None]
    if missing:
        raise WinToGoError("missing required commands: " + ", ".join(missing))


def run(cmd, *, input=None, check=True):
    """Run a command and return its stdout; raise WinToGoError on failure."""
    proc = subprocess.run(
        [str(c) for c in cmd],
        input=input,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise WinToGoError(f"{cmd[0]} failed (exit {proc.returncode}): {detail}")
    return proc.stdout


class Reporter:
    """Receives progress events from the pipeline.

    The base class prints human-readable lines; JsonReporter emits one JSON
    object per line for the GUI, which runs the backend through pkexec.
    """

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self._last_pct = None

    def step(self, step_id, label):
        self._last_pct = None
        print(f"==> {label}", file=self.stream, flush=True)

    def progress(self, fraction, detail=None):
        pct = int(fraction * 100)
        if pct != self._last_pct:
            self._last_pct = pct
            text = f"    {pct:3d}%" + (f"  {detail}" if detail else "")
            print(text, file=self.stream, flush=True)

    def log(self, message):
        print(f"    {message}", file=self.stream, flush=True)

    def warning(self, message):
        print(f"WARNING: {message}", file=self.stream, flush=True)


class JsonReporter(Reporter):
    def __init__(self, stream=None):
        super().__init__(stream or sys.stdout)

    def _emit(self, **event):
        print(json.dumps(event), file=self.stream, flush=True)

    def step(self, step_id, label):
        self._last_pct = None
        self._emit(type="step", id=step_id, label=label)

    def progress(self, fraction, detail=None):
        # Rate-limit to whole percents; the GUI doesn't need more.
        pct = int(fraction * 100)
        if pct != self._last_pct:
            self._last_pct = pct
            self._emit(type="progress", fraction=round(fraction, 4), detail=detail)

    def log(self, message):
        self._emit(type="log", message=message)

    def warning(self, message):
        self._emit(type="warning", message=message)

    def done(self):
        self._emit(type="done")

    def error(self, message):
        self._emit(type="error", message=message)
