"""Daemon orchestration for software-driven animations.

The animator runs as a detached background process (`python -m
mystic_center.lighting.animate <engine> --speed N --dir D`) that continuously
paints frames via the HID feature path. The GUI/CLI talk to it implicitly:
starting an animation kills any previous one; switching to a hardware effect
stops it.

An exclusive flock serializes stop/spawn so two animators can never fight
over the device, and PID targets are verified against /proc/<pid>/cmdline so
a recycled PID is never signaled by mistake.
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
ERR_PATH = os.path.join(CONFIG_DIR, "animate.err")
_LOCK_PATH = os.path.join(CONFIG_DIR, "animate.lock")

# stop latency the GUI actually feels: SIGTERM (clean frame end + off), then
# a quick forceful kill — never the old 3-second hang
_TERM_GRACE = 0.60
_KILL_GRACE = 0.40
_POLL = 0.03


def load_pid() -> int | None:
    try:
        with open(PID_PATH) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _is_animator(pid: int) -> bool:
    """True if /proc/<pid> is really our animator (guards recycled PIDs)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return b"lighting.animate" in fh.read()
    except OSError:
        return False


def _is_dead(pid: int) -> bool:
    """True once the process is gone OR is an unreaped zombie (stat field 3
    == 'Z'): zombies keep os.kill(pid, 0) alive forever until reaped."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
        fields = data.rsplit(b")", 1)[1].split()
        return fields[0] == b"Z"
    except OSError:
        return True  # fully gone


def _reap(pid: int) -> None:
    """Give the stored Popen a chance to reap its zombie child."""
    proc = _PROCS.pop(pid, None)
    if proc is not None:
        try:
            proc.poll()
        except Exception:
            pass


_PROCS: dict[int, "subprocess.Popen"] = {}


def running() -> bool:
    pid = load_pid()
    return pid is not None and _is_animator(pid)


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, OSError):
        pass


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_dead(pid):
            _reap(pid)  # collect the zombie so the slot is truly free
            return True
        time.sleep(_POLL)
    _reap(pid)
    return False


def _clear_pid() -> None:
    try:
        os.unlink(PID_PATH)
    except OSError:
        pass


def _do_stop() -> bool:
    """Termination primitives — caller must hold the flock."""
    pid = load_pid()
    if pid is None:
        return False
    if not _is_animator(pid):
        _clear_pid()  # stale PID slot
        return True
    _signal(pid, signal.SIGTERM)
    if not _wait_gone(pid, _TERM_GRACE):
        _signal(pid, signal.SIGKILL)
        _wait_gone(pid, _KILL_GRACE)
    _clear_pid()
    return True


def stop() -> bool:
    """Terminate the background animator (if any). Returns True if stopped."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    import fcntl

    with open(_LOCK_PATH, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _do_stop()


def start_detached(effect: str, speed: float = 6.0, direction: int = 1) -> int:
    """Spawn the animator in its own session; returns its pid.

    Serialized with stop() under one flock. Raises RuntimeError if the child
    dies immediately (bad name, HID permission problem…), surfacing the
    captured stderr tail."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    import fcntl

    with open(_LOCK_PATH, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _do_stop()
        cmd = [
            sys.executable, "-m", "mystic_center.lighting.animate",
            effect,
            "--speed", str(speed),
            "--dir", str(direction),
        ]
        env = dict(os.environ)
        # make the package importable for the child regardless of CWD
        from .keyboard import __file__ as kb_file  # noqa: F401
        package_dir = os.path.dirname(
            os.path.dirname(os.path.abspath(kb_file)))
        env["PYTHONPATH"] = package_dir + ":" + env.get("PYTHONPATH", "")
        with open(ERR_PATH, "wb") as err_fh:
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
        _PROCS[proc.pid] = proc
        # verify the child is actually alive (module found, device opened)
        # while still holding the lock
        time.sleep(0.35)
        proc.poll()  # reap if it crashed on startup
        if proc.poll() is not None:
            try:
                with open(ERR_PATH, "rb") as fh:
                    tail = fh.read()[-400:].decode(errors="replace").strip()
            except OSError:
                tail = ""
            _clear_pid()
            raise RuntimeError(
                f"animator exited immediately ({tail or 'no stderr'})")
        return proc.pid
