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
    """Spawn the animator in its own session; returns its pid.

    Raises RuntimeError if the child dies immediately (bad name, HID
    permission problem…), surfacing the captured stderr tail."""
    stop()
    os.makedirs(CONFIG_DIR, exist_ok=True)
    cmd = [
        sys.executable, "-m", "mystic_center.lighting.animate",
        effect,
        "--speed", str(speed),
        "--dir", str(direction),
    ]
    env = dict(os.environ)
    # make the package importable for the child regardless of CWD
    from .keyboard import __file__ as kb_file  # noqa: F401
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(kb_file)))
    env["PYTHONPATH"] = package_dir + ":" + env.get("PYTHONPATH", "")
    err_path = os.path.join(CONFIG_DIR, "animate.err")
    with open(err_path, "wb") as err_fh:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=err_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    with open(PID_PATH, "w") as fh:
        fh.write(str(proc.pid))
    # verify the child is actually alive (module found, device opened)
    time.sleep(0.5)
    if proc.poll() is not None:
        try:
            with open(err_path, "rb") as fh:
                tail = fh.read()[-400:].decode(errors="replace").strip()
        except OSError:
            tail = ""
        raise RuntimeError(
            f"animator exited immediately ({tail or 'no stderr'})")
    return proc.pid
