"""Subprocess helpers, errors and progress reporting shared by the backend."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_MACOS = sys.platform == "darwin"

# Tools Homebrew lacks (see macos/build-deps.sh) are built into this checkout.
DEPS_PREFIX = Path(__file__).resolve().parents[2] / ".deps"


def setup_environment():
    """Make the tools findable however the program was started.

    On macOS, apps and sudo don't get Homebrew's directories on PATH, and
    ntfs-3g's tools, wimlib with NTFS-3G support and hivex's Python module
    come from .deps/, which must win over Homebrew's wimlib (built without
    NTFS-3G).
    """
    if not IS_MACOS:
        return
    dirs = [DEPS_PREFIX / "bin", DEPS_PREFIX / "sbin",
            "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin"]
    path = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    first = [str(d) for d in dirs if os.path.isdir(d)]
    os.environ["PATH"] = os.pathsep.join(first + [p for p in path if p not in first])
    site = str(DEPS_PREFIX / "python")
    if os.path.isdir(site) and site not in sys.path:
        sys.path.insert(0, site)


class WinToGoError(Exception):
    """A failure the user should see, with a message that explains it."""


def require(*commands):
    missing = [c for c in commands if shutil.which(c) is None]
    if missing:
        hint = " (see Requirements in the README)" if IS_MACOS else ""
        raise WinToGoError("missing required commands: " + ", ".join(missing) + hint)


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
