"""Direct EC access via /sys/kernel/debug/ec/ec0/io (root only).

Kernel ec_sys layout (verified live on this machine):
  read  : whole 256-byte EC register space, file offset = register address
  write : two bytes  [address, value]  written at offset 0

All writes go through a hard allow-list (see docs/EC-MAP.md), are serialized,
space-limited, and verified by read-back.
"""

from __future__ import annotations

import os
import struct
import threading
import time

from .util import EC_DEBUGFS_BASE

IO_FILE = os.path.join(EC_DEBUGFS_BASE, "io")

# Register allow-list — the only addresses the daemon will ever WRITE.
# 0x71/0x89 (fan duty readbacks) and the curve tables are deliberately NOT
# here: a controlled probe proved this firmware ignores writes to them
# (docs/EC-MAP.md). Reading is unrestricted; writing is not.
ALLOWED: dict[int, str] = {
    0xD2: "shift_mode",     # turbo/comfort/eco value byte
    0xD4: "fan_mode",       # 0x0d auto / 0x1d silent / 0x4d basic / 0x8d advanced
    0xD7: "battery_ctrl",   # charge threshold (normally via sysfs instead)
    0x98: "cooler_boost",   # bit 7 (normally via sysfs instead)
    0xEB: "super_battery",  # mask 0x0f (normally via sysfs instead)
}

MIN_WRITE_INTERVAL_S = 0.02   # 20 ms spacing between EC writes
READ_RETRIES = 2
WRITE_RETRIES = 2


class ECError(RuntimeError):
    pass


class EC:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_write = 0.0
        self._fd: int | None = None
        self._available: bool = os.path.exists(IO_FILE)
        self.last_error: str | None = None

    # ---- internal ------------------------------------------------------------

    def _handle(self) -> int:
        if not self._available:
            raise ECError(f"{IO_FILE} not available (ec_sys loaded without debugfs?)")
        if self._fd is None:
            self._fd = os.open(IO_FILE, os.O_RDWR)
        return self._fd

    # ---- public ----------------------------------------------------------------

    def available(self) -> bool:
        return self._available

    def read_space(self) -> bytes:
        with self._lock:
            return self._read_space()

    def _read_space(self) -> bytes:
        try:
            fd = self._handle()
            for attempt in range(READ_RETRIES + 1):
                data = os.pread(fd, 256, 0)
                if len(data) == 256:
                    return data
            raise ECError("short read from EC space")
        except OSError as e:
            self.last_error = f"EC read failed: {e}"
            raise

    def read_reg(self, addr: int) -> int:
        return self._read_space()[addr & 0xFF]

    def write_reg(self, addr: int, value: int, verify: bool = True) -> bool:
        addr &= 0xFF
        value &= 0xFF
        if addr not in ALLOWED:
            self.last_error = f"address 0x{addr:02x} not in allow-list"
            raise ECError(self.last_error)
        with self._lock:
            gap = time.monotonic() - self._last_write
            if gap < MIN_WRITE_INTERVAL_S:
                time.sleep(MIN_WRITE_INTERVAL_S - gap)
            try:
                fd = self._handle()
                rc = False
                for _attempt in range(WRITE_RETRIES + 1):
                    os.pwrite(fd, struct.pack("BB", addr, value), 0)
                    self._last_write = time.monotonic()
                    time.sleep(0.01)
                    back = self._read_space()[addr]
                    if back == value:
                        rc = True
                        break
                    time.sleep(0.05)
                self.last_error = None if rc else (
                    f"EC write verify failed: 0x{addr:02x} wrote 0x{value:02x}, "
                    f"read back 0x{back:02x}")
                return rc
            except OSError as e:
                self.last_error = f"EC write failed: {e}"
                return False

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
