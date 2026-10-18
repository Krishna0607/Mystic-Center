"""Daemon orchestration for software-driven animations.

The animator runs as a detached background process (`python -m
mystic_control.animate <engine> --speed N --dir D`) that continuously paints
frames via the HID feature path. The GUI/CLI talk to it implicitly:
starting an animation kills any previous one; switching to a hardware effect
stops it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "mystic-control",
)
PID_PATH = os.path.join(CONFIG_DIR, "animate.pid")


def load_pid() -> int | None:
    try:
        with open(PID_PATH) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def running() -> bool:
    pid = load_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours


def stop() -> bool:
    """Termiṇate the background animator (if any). Returns True if stopped."""
    pid = load_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            try:
                os.unlink(PID_PATH)
            except OSError:
                pass
            return True
        except OSError:
            break
    try:
        os.kill(pid, signal.SIGKILL)
        os.unlink(PID_PATH)
    except OSError:
        pass
    return True


def start_detached(effect: str, speed: float = 6.0, direction: int = 1) -> int:
    """Spawn the animator in its own session; returns its pid."""
    stop()
    os.makedirs(CONFIG_DIR, exist_ok=True)
    cmd = [
        sys.executable, "-m", "mystic_control.animate",
        effect,
        "--speed", str(speed),
        "--dir", str(direction),
    ]
    env = dict(os.environ)
    # make the package importable for the child regardless of CWD
    from .keyboard import __file__ as kb_file  # noqa: F401
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(kb_file)))
    env["PYTHONPATH"] = package_dir + ":" + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    with open(PID_PATH, "w") as fh:
        fh.write(str(proc.pid))
    return proc.pid
