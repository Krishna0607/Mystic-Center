"""GPU MUX switching (MSHybrid / Discrete / Integrated) — MSI Center parity.

MSI laptops with a MUX expose the graphics mode through three cooperating
pieces of firmware, all written together by MSI Center and by this module:

1. **UEFI variable** ``MsiDCVarData`` (GUID ``DD96BAAF-145E-4F56-B1CF-193256298E99``).
   A 20-byte value; byte 5 carries the mode:

   ======  ====================================================
   bits    meaning
   ======  ====================================================
   0..1    *selected target*: 0 MSHybrid · 1 Discrete · 2 Integrated
   2..3    *current mode* as reported by firmware (same encoding)
   4       "new switch" flow supported
   5       integrated mode supported
   6       discrete mode **not** supported
   7       reserved / OEM
   ======  ====================================================

   This machine reads ``byte[5] = 0x30`` → target MSHybrid, current
   MSHybrid, new-switch flow, discrete + integrated both supported.

2. **EC apply handshake** over the ``MSI_ACPI`` WMI device (debugfs
   ``get_ap`` / ``set_data``, the same interface used for fan curves):

   * ``Get_AP`` → data byte 1, bit 1 is *apply-ready*
   * trigger  = ``Set_Data(0xD1, (ap_byte1 & 0xFC) | 0x01)``
   * wait ≤ 5 s for apply-ready, then
   * ack      = ``Set_Data(0xBE, 0x02)``

3. **EC mirror registers** (read-only here, for status): ``0x2E`` bit 6 is
   the PXCT mux state, ``0xD1`` carries the switch bits.

Evidence: MSI Center 2.0.74.0's GPU switch flow (docs/MSI-CENTER-RE.md), the
in-tree ``msi-wmi-platform`` driver (``MSI_PLATFORM_GET_AP`` / ``SET_DATA``),
and live reads on this unit. The UEFI-variable + EC-trigger layout matches
the independently characterised msi-gpu-switcher and msi-gpu-mux-switch
projects.

Safety: the flow is only ever *staged* here — firmware applies it on the
next power-on, and the caller is told a full shutdown is required. Every
step is read back and verified, and a failed step restores the previous
target.
"""

from __future__ import annotations

import fcntl
import os
import re
import struct
import subprocess
import time

# ---- UEFI variable ---------------------------------------------------------

EFIVAR_DIR = "/sys/firmware/efi/efivars"
VAR_NAME = "MsiDCVarData"
VAR_GUID = "DD96BAAF-145E-4F56-B1CF-193256298E99"
VAR_PATH = os.path.join(EFIVAR_DIR, f"{VAR_NAME}-{VAR_GUID.lower()}")
VAR_ATTRS_EXPECTED = 0x00000007
VAR_LENGTH = 20
MODE_BYTE = 5

MODE_HYBRID = "hybrid"
MODE_DISCRETE = "discrete"
MODE_INTEGRATED = "integrated"

MODE_VALUES = {MODE_HYBRID: 0, MODE_DISCRETE: 1, MODE_INTEGRATED: 2}
MODE_NAMES = {v: k for k, v in MODE_VALUES.items()}
MODE_LABELS = {
    MODE_HYBRID: "MSHybrid",
    MODE_DISCRETE: "Discrete",
    MODE_INTEGRATED: "Integrated",
}
MODE_ALIASES = {
    "hybrid": MODE_HYBRID, "mshybrid": MODE_HYBRID, "ms-hybrid": MODE_HYBRID,
    "optimus": MODE_HYBRID, "dgpu": MODE_DISCRETE, "discrete": MODE_DISCRETE,
    "igpu": MODE_INTEGRATED, "integrated": MODE_INTEGRATED, "uma": MODE_INTEGRATED,
}

# ---- EC / WMI handshake ----------------------------------------------------

EC_IO = "/sys/kernel/debug/ec/ec0/io"
EC_MUX_REG = 0x2E
EC_MUX_MASK = 0x40          # PXCT: 1 = discrete panel path
EC_SWITCH_REG = 0xD1
AP_DATA_ADDR = 0xD1
AP_ACK_ADDR = 0xBE
AP_ACK_VALUE = 0x02
AP_READY_MASK = 0x02
TRIGGER_MASK = 0x01
AP_POLL_S = 0.25
AP_READY_TIMEOUT_S = 5.0

FS_IOC_GETFLAGS = 0x80086601
FS_IOC_SETFLAGS = 0x40086602
FS_IMMUTABLE_FL = 0x00000010


class GpuModeError(RuntimeError):
    pass


# ---- UEFI variable helpers -------------------------------------------------


def available() -> bool:
    """True when this machine exposes the MSI MUX variable."""
    return os.path.exists(VAR_PATH)


def _read_var() -> tuple[int, bytes]:
    with open(VAR_PATH, "rb") as fh:
        raw = fh.read()
    if len(raw) < 4 + MODE_BYTE + 1:
        raise GpuModeError(f"{VAR_NAME} is only {len(raw)} bytes")
    attrs = struct.unpack_from("<I", raw, 0)[0]
    return attrs, raw[4:]


def _clear_immutable(path: str) -> int | None:
    """efivarfs vars are chattr +i by default; clear it and return old flags."""
    fd = os.open(path, os.O_RDONLY)
    try:
        flags = struct.unpack("i", fcntl.ioctl(fd, FS_IOC_GETFLAGS,
                                               struct.pack("i", 0)))[0]
        if flags & FS_IMMUTABLE_FL:
            new = flags & ~FS_IMMUTABLE_FL
            fcntl.ioctl(fd, FS_IOC_SETFLAGS, struct.pack("i", new))
            return flags
    finally:
        os.close(fd)
    return None


def _restore_immutable(path: str, flags: int | None) -> None:
    if flags is None:
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.ioctl(fd, FS_IOC_SETFLAGS, struct.pack("i", flags))
    finally:
        os.close(fd)


def _write_var(attrs: int, payload: bytes) -> None:
    if len(payload) != VAR_LENGTH:
        raise GpuModeError(
            f"refusing write: {VAR_NAME} must be {VAR_LENGTH} bytes, "
            f"got {len(payload)}")
    if attrs != VAR_ATTRS_EXPECTED:
        raise GpuModeError(
            f"refusing write: unexpected UEFI attributes 0x{attrs:08X}")
    old = _clear_immutable(VAR_PATH)
    try:
        with open(VAR_PATH, "wb") as fh:
            fh.write(struct.pack("<I", attrs) + payload)
    finally:
        _restore_immutable(VAR_PATH, old)


def decode(payload: bytes) -> dict:
    if len(payload) <= MODE_BYTE:
        raise GpuModeError(f"{VAR_NAME} shorter than {MODE_BYTE + 1} bytes")
    b = payload[MODE_BYTE]
    target = b & 0x03
    current = (b >> 2) & 0x03
    return {
        "byte5": b,
        "target": MODE_NAMES.get(target),
        "current": MODE_NAMES.get(current),
        "new_switch": bool(b & 0x10),
        "integrated_supported": bool(b & 0x20),
        "discrete_supported": not (b & 0x40),
        "reserved": bool(b & 0x80),
    }


def supported_modes(info: dict) -> list[str]:
    """Modes the firmware advertises, in MSI Center's display order."""
    if not info.get("new_switch"):
        # Legacy flow: only the hybrid/discrete pair is wired.
        modes = [MODE_HYBRID]
        if info.get("discrete_supported"):
            modes.append(MODE_DISCRETE)
        return modes
    modes = [MODE_HYBRID]
    if info.get("discrete_supported"):
        modes.append(MODE_DISCRETE)
    if info.get("integrated_supported"):
        modes.append(MODE_INTEGRATED)
    return modes


def read_state() -> dict:
    """Read-only status: UEFI variable + EC mirror + live GPUs."""
    attrs, payload = _read_var()
    info = decode(payload)
    info["attributes"] = f"0x{attrs:08X}"
    info["length"] = len(payload)
    info["supported"] = supported_modes(info)
    info["mux_ec"] = _read_ec_byte(EC_MUX_REG)
    info["switch_ec"] = _read_ec_byte(EC_SWITCH_REG)
    info["gpus"] = list_gpus()
    info["ac_online"] = _ac_online()
    info["reboot_pending"] = info["target"] != info["current"]
    return info


def _read_ec_byte(addr: int) -> int | None:
    try:
        fd = os.open(EC_IO, os.O_RDONLY)
    except OSError:
        return None
    try:
        data = os.pread(fd, 1, addr)
        return data[0] if data else None
    finally:
        os.close(fd)


def _ac_online() -> bool:
    try:
        for name in sorted(os.listdir("/sys/class/power_supply")):
            base = f"/sys/class/power_supply/{name}"
            try:
                with open(f"{base}/type") as fh:
                    if fh.read().strip() != "Mains":
                        continue
                with open(f"{base}/online") as fh:
                    return fh.read().strip() == "1"
            except OSError:
                continue
    except OSError:
        pass
    return True  # unknown: don't block on a guess


def list_gpus() -> list[dict]:
    """Display controllers with vendor/driver, for status display."""
    out: list[dict] = []
    base = "/sys/bus/pci/devices"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return out
    for entry in entries:
        dev = os.path.join(base, entry)
        try:
            with open(f"{dev}/class") as fh:
                klass = fh.read().strip()
        except OSError:
            continue
        if not (klass.startswith("0x0300") or klass.startswith("0x0302")):
            continue
        def _read(name: str) -> str:
            try:
                with open(f"{dev}/{name}") as fh:
                    return fh.read().strip()
            except OSError:
                return ""
        try:
            driver = os.path.basename(os.readlink(f"{dev}/driver"))
        except OSError:
            driver = "none"
        out.append({
            "pci": entry,
            "vendor": _read("vendor"),
            "device": _read("device"),
            "driver": driver,
        })
    return out


# ---- WMI apply handshake ---------------------------------------------------


def _wmi_dir() -> str | None:
    import glob
    for path in sorted(glob.glob("/sys/kernel/debug/msi-wmi-platform-*")):
        if (os.path.exists(os.path.join(path, "get_ap"))
                and os.path.exists(os.path.join(path, "set_data"))):
            return path
    return None


def _invoke(method: str, payload: bytes) -> bytes:
    path = _wmi_dir()
    if path is None:
        raise GpuModeError(
            "msi-wmi-platform debugfs not present — cannot drive the EC "
            "apply handshake")
    if len(payload) != 32:
        raise GpuModeError("WMI payload must be 32 bytes")
    with open(os.path.join(path, method), "r+b") as fh:
        fh.write(payload)
        fh.seek(0)
        out = fh.read(32)
    if len(out) != 32:
        raise GpuModeError(f"{method}: short output ({len(out)} bytes)")
    return out


def ap_state() -> dict:
    out = _invoke("get_ap", bytes(32))
    if out[0] == 0:
        raise GpuModeError("Get_AP reported failure")
    return {
        "flag": out[0],
        "data_byte1": out[2],
        "apply_ready": bool(out[2] & AP_READY_MASK),
        "raw": out[:8].hex(),
    }


def _set_data(addr: int, value: int) -> None:
    payload = bytearray(32)
    payload[0] = addr
    payload[1] = value
    out = _invoke("set_data", bytes(payload))
    if out[0] == 0:
        raise GpuModeError(f"Set_Data(0x{addr:02X}) reported failure")


def _wait_apply_ready(timeout: float = AP_READY_TIMEOUT_S) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        time.sleep(AP_POLL_S)
        state = ap_state()
        if state["apply_ready"]:
            return state
        if time.monotonic() >= deadline:
            raise GpuModeError(
                f"firmware did not assert apply-ready within {timeout:.0f} s")


# ---- the switch ------------------------------------------------------------


def normalize_mode(name: str) -> str:
    key = (name or "").strip().lower()
    if key not in MODE_ALIASES:
        raise GpuModeError(
            f"unknown GPU mode {name!r} — use hybrid, discrete or integrated")
    return MODE_ALIASES[key]


def set_mode(mode: str, dry_run: bool = False) -> dict:
    """Stage ``mode`` and run MSI Center's apply handshake.

    Raises on any failure and restores the previous target when the write
    already landed. Returns the resulting state; the caller must tell the
    user to power off completely.
    """
    target = normalize_mode(mode)
    if not available():
        raise GpuModeError(
            f"{VAR_NAME} not present — this machine has no switchable MUX")

    before_attrs, before_payload = _read_var()
    before = decode(before_payload)
    if target not in supported_modes(before):
        raise GpuModeError(
            f"{MODE_LABELS[target]} mode is not advertised by this firmware "
            f"(supported: {', '.join(supported_modes(before))})")

    result: dict = {
        "mode": target,
        "label": MODE_LABELS[target],
        "previous": before["target"],
        "byte5_before": before["byte5"],
        "dry_run": dry_run,
    }

    staged = bytearray(before_payload)
    staged[MODE_BYTE] = (staged[MODE_BYTE] & ~0x03) | MODE_VALUES[target]
    result["byte5_staged"] = staged[MODE_BYTE]

    if dry_run:
        result["note"] = "dry run — firmware untouched"
        return result

    # Preflight: never start from an ambiguous firmware state.
    pre = ap_state()
    if pre["apply_ready"]:
        raise GpuModeError(
            "apply-ready is already asserted before any write — refusing an "
            "ambiguous transition")
    result["ap_before"] = f"0x{pre['data_byte1']:02X}"

    _write_var(before_attrs, bytes(staged))
    after_attrs, after_payload = _read_var()
    if after_payload != bytes(staged) or after_attrs != before_attrs:
        raise GpuModeError("UEFI target write verification failed")
    result["byte5_after"] = decode(after_payload)["byte5"]

    try:
        ap = ap_state()
        if ap["apply_ready"]:
            raise GpuModeError(
                "apply-ready became set before the trigger — refusing an "
                "ambiguous transition")
        trigger = (ap["data_byte1"] & 0xFC) | TRIGGER_MASK
        _set_data(AP_DATA_ADDR, trigger)
        result["trigger"] = f"Set_Data(0x{AP_DATA_ADDR:02X}, 0x{trigger:02X})"

        ready = _wait_apply_ready()
        result["apply_ready_byte1"] = f"0x{ready['data_byte1']:02X}"
        _set_data(AP_ACK_ADDR, AP_ACK_VALUE)
        result["ack"] = f"Set_Data(0x{AP_ACK_ADDR:02X}, 0x{AP_ACK_VALUE:02X})"
    except Exception:
        # Put the previous target back so the next boot is unchanged.
        try:
            restore = bytearray(before_payload)
            _write_var(before_attrs, bytes(restore))
            result["restored"] = True
        except Exception:
            result["restored"] = False
        raise

    result["state"] = read_state()
    result["shutdown_required"] = True
    return result


def restore_target(mode: str) -> dict:
    """Re-stage a previous target without running the handshake."""
    target = normalize_mode(mode)
    attrs, payload = _read_var()
    staged = bytearray(payload)
    staged[MODE_BYTE] = (staged[MODE_BYTE] & ~0x03) | MODE_VALUES[target]
    _write_var(attrs, bytes(staged))
    return {"restored_target": target}
