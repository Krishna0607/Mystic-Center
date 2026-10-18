"""Paths, constants and small helpers for Mystic Center."""

from __future__ import annotations

import os
import subprocess

APP_NAME = "Mystic Center"
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RUN_DIR = "/run/mystic-center"
SOCKET_PATH = os.path.join(RUN_DIR, "control.sock")

MSIEC_BASE = "/sys/devices/platform/msi-ec"
EC_DEBUGFS_BASE = "/sys/kernel/debug/ec/ec0"

BOARD_FW = "15P2EMS1.110"


def read_file(path: str, strip: bool = True) -> str | None:
    try:
        with open(path) as fh:
            text = fh.read()
        return text.strip() if strip else text
    except (OSError, ValueError):
        return None


def read_int(path: str) -> int | None:
    text = read_file(path)
    if text is None:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def write_file(path: str, value: str) -> bool:
    try:
        with open(path, "w") as fh:
            fh.write(value)
        return True
    except OSError:
        return False


def run_cmd(cmd: list[str], timeout_s: float = 3.0) -> str | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        return proc.stdout if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def clamp(low: int, high: int, value: int) -> int:
    return max(low, min(high, int(value)))
