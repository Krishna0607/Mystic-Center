"""Low-level hidraw access, zero external dependencies.

Uses raw /dev/hidraw ioctls (HIDIOCSFEATURE / HIDIOCGFEATURE) so we don't need
python-hidapi. The device node must be user-accessible — install the udev rule
(via install.sh) or run as root.
"""

from __future__ import annotations

import glob
import logging
import os

log = logging.getLogger(__name__)

# --- hidraw ioctl helpers (mirrors linux/hidraw.h) ----------------------------------

_HID_TYPE = 0x48  # 'H'
_IOC_NRSH = 0
_IOC_TYPESHIFT = 8
_IOC_SIZESHIFT = 16
_IOC_DIRSHIFT = 30
_IOC_READ_WRITE = 3


def _ioc(nr: int, size: int) -> int:
    return (
        (_IOC_READ_WRITE << _IOC_DIRSHIFT)
        | (size << _IOC_SIZESHIFT)
        | (_HID_TYPE << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSH)
    )


HIDIOCSFEATURE = _ioc(0x06, 64)  # set feature report, 64-byte buffer
HIDIOCGFEATURE = _ioc(0x07, 64)  # get feature report, 64-byte buffer

REPORT_ID_ACK = 1  # device responds/statuses here
REPORT_ID_CMD = 2  # command channel used by MSI Center
REPORT_LEN = 64  # 1 byte report id + 63 bytes payload


class DeviceNotFound(Exception):
    pass


class HidAccessError(Exception):
    pass


def find_hidraw(vid: int = 0x1462, pid: int = 0x1603) -> str | None:
    """Return a /dev/hidraw node for the given VID/PID, or None."""
    for sysdev in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(sysdev, "device/uevent"), "r") as f:
                uevent = f.read()
        except OSError:
            continue
        hid_id = ""
        hid_name = ""
        for line in uevent.splitlines():
            if line.startswith("HID_ID="):
                hid_id = line.split("=", 1)[1]
            elif line.startswith("HID_NAME="):
                hid_name = line.split("=", 1)[1]
        parts = hid_id.strip().split(":")
        if len(parts) != 3:
            continue
        try:
            actual_vid = int(parts[1], 16)
            actual_pid = int(parts[2], 16)
        except ValueError:
            continue
        if actual_vid == vid and actual_pid == pid:
            log.debug("found %s (%s)", sysdev, hid_name)
            return os.path.join("/dev", os.path.basename(sysdev))
    return None


class MysticHid:
    """Open /dev/hidraw node; send 64-byte feature reports (the MSI cmd channel)."""

    def __init__(self, path: str):
        self.path = path
        self._fd: int | None = None

    @classmethod
    def open_first(cls, vid: int = 0x1462, pid: int = 0x1603) -> "MysticHid":
        path = find_hidraw(vid, pid)
        if path is None:
            raise DeviceNotFound(
                f"No HID device with VID={vid:#06x} PID={pid:#06x} found."
            )
        dev = cls(path)
        dev.open()
        return dev

    def open(self) -> None:
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_CLOEXEC)
        except PermissionError as e:
            raise HidAccessError(
                f"Permission denied on {self.path}. Install the udev rule "
                "(sudo ./install.sh --udev) and log out/in, or run with sudo."
            ) from e
        except OSError as e:
            raise HidAccessError(f"Cannot open {self.path}: {e}") from e
        log.debug("opened %s", self.path)

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def ensure_open(self) -> None:
        if self._fd is None:
            self.open()

    def __enter__(self) -> "MysticHid":
        self.ensure_open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def send_feature(self, payload: bytes) -> bool:
        """Send a 64-byte SET_FEATURE with report id REPORT_ID_CMD.

        `payload` holds the 63 bytes AFTER the report id.
        Returns True on success.
        """
        self.ensure_open()
        buf = bytes([REPORT_ID_CMD]) + bytes(payload).ljust(REPORT_LEN - 1, b"\x00")
        result = fcntl_result = None
        import fcntl  # local import keeps module importable on other OSes

        try:
            result = fcntl.ioctl(self._fd, HIDIOCSFEATURE, buf, True)
        except OSError as e:
            raise HidAccessError(f"SET_FEATURE failed on {self.path}: {e}") from e
        return len(result) == len(buf)

    def read_ack(self) -> bytes | None:
        """Read the 64-byte GET_FEATURE report id 1 response (best effort)."""
        self.ensure_open()
        import fcntl

        buf = bytearray(REPORT_LEN)
        buf[0] = REPORT_ID_ACK
        try:
            result = fcntl.ioctl(self._fd, HIDIOCGFEATURE, bytes(buf), True)
            return bytes(result)
        except OSError:
            return None

    def write_output(self, payload: bytes, report_id: int = 1) -> int:
        """Write to the OUT endpoint (not used by MSI Center protocol, helper)."""
        self.ensure_open()
        buf = bytes([report_id]) + bytes(payload).ljust(REPORT_LEN - 1, b"\x00")
        return os.write(self._fd, buf)
