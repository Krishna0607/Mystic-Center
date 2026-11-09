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

   * ``Get_AP`` → package byte 2 is a two-bit state machine: bit 1
     (``0x02``) = *apply-ready*, bit 0 (``0x01``) = *trigger in flight*
   * trigger  = ``Set_Data(0xD1, (ap_byte1 & 0xFC) | 0x01)``
   * wait ≤ 5 s for apply-ready, then
   * ack      = ``Set_Data(0xBE, 0x02)``

   Apply-ready stays latched after the acknowledgement, so the preflight
   checks for an in-flight trigger (bit 0) instead of refusing whenever
   apply-ready is set — the latter blocks every switch after the first.
   Verified live: ``0x02`` (ready) → ``0x01`` (trigger sent) → ``0x02``
   (ready) → ack.

3. **EC mirror registers** (read-only here, for status): ``0x2E`` bit 6 is
   the PXCT mux state, ``0xD1`` carries the switch bits.

Evidence: MSI Center 2.0.74.0's GPU switch flow (docs/MSI-CENTER-RE.md), the
in-tree ``msi-wmi-platform`` driver (``MSI_PLATFORM_GET_AP`` / ``SET_DATA``),
and live reads on this unit. The UEFI-variable + EC-trigger layout matches
the independently characterised msi-gpu-switcher and msi-gpu-mux-switch
projects.

Safety: the flow is only ever *staged* here — firmware applies it on the
next power-on, and the caller is told a full shutdown is required. Every
step is read back and verified. A failure before the trigger restores the
previous target; a failure after it leaves the target staged, because the
EC has already taken it.
"""

from __future__ import annotations

import fcntl
import json
import os
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
# The Get_AP data byte is a two-bit state machine, verified live on this
# unit (see handshake_probe() / docs/GPU-MUX-MAP.md §2):
#
#   0x02  bit1 set, bit0 clear  → apply-ready (latched after a completed apply)
#   0x01  bit0 set, bit1 clear  → trigger in flight
#   0x00  neither               → transient / idle between the two
#
# The reference implementation refuses to start when apply-ready is set,
# which works only on a machine that has never switched: bit 1 stays
# latched after the acknowledgement, so that preflight blocks every
# subsequent transition. The genuinely ambiguous state is an in-flight
# trigger (bit 0), so that is what the preflight checks.
AP_READY_MASK = 0x02
AP_BUSY_MASK = 0x01
TRIGGER_MASK = 0x01
AP_POLL_S = 0.25
AP_READY_TIMEOUT_S = 5.0

FS_IOC_GETFLAGS = 0x80086601
FS_IOC_SETFLAGS = 0x40086602
FS_IMMUTABLE_FL = 0x00000010

# Persistent state (survives a reboot, unlike /run). The daemon runs as root
# and is the only writer; every helper tolerates a read-only filesystem so the
# read paths stay usable from an unprivileged process.
STATE_DIR = "/var/lib/mystic-center"
PENDING_PATH = os.path.join(STATE_DIR, "gpu-pending.json")
FAILED_PATH = os.path.join(STATE_DIR, "gpu-apply-failed.json")


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
    """Read-only status: UEFI variable + EC mirror + live GPUs.

    ``current`` is the *firmware-reported* mode (byte 5 bits 2..3), which is
    authoritative once POST has run. ``runtime`` adds independent evidence
    from live display-controller visibility, used to confirm a staged switch
    immediately after the next boot. ``target`` is what is staged for the next
    power-on; ``reboot_pending`` is true while the two disagree.
    """
    attrs, payload = _read_var()
    info = decode(payload)
    info["available"] = True
    info["attributes"] = f"0x{attrs:08X}"
    info["length"] = len(payload)
    info["supported"] = supported_modes(info)
    info["mux_ec"] = _read_ec_byte(EC_MUX_REG)
    info["switch_ec"] = _read_ec_byte(EC_SWITCH_REG)
    gpus = list_gpus()
    info["gpus"] = gpus
    info["ac_online"] = _ac_online()
    info["current_uefi"] = info["current"]

    rt = runtime_mode(gpus)
    info["runtime"] = rt
    info["current_source"] = "uefi"
    if info["target"] != info["current"] and rt["mode"] == info["target"]:
        # The firmware byte has not caught up yet but the live GPU topology
        # already matches the staged target — the switch applied.
        info["current"] = rt["mode"]
        info["current_source"] = "runtime"

    info["reboot_pending"] = info["target"] != info["current"]
    _settle_journal(info)
    info["restart_required"] = bool(
        info.get("reboot_pending") or info.get("apply_failed"))
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
            "boot_vga": _read("boot_vga"),
        })
    return out


# ---- live (runtime) mode detection ----------------------------------------
#
# The UEFI variable's "current" bits are written by firmware during POST. That
# is authoritative but only refreshes after a power cycle, so a machine that
# has just switched needs independent evidence of what is *actually* running.
# The panel/PCIe MUX hides the unused GPU at POST:
#
#   MSHybrid   → both display controllers present
#   Discrete   → dGPU only (iGPU disabled)
#   Integrated → iGPU only (dGPU powered down / hidden)
#
# This gives the GUI an immediate, truthful "you are now running X" after a
# restart instead of a stale "reboot pending" banner.

_INTEL, _NVIDIA, _AMD = "0x8086", "0x10de", "0x1002"


def _classify_gpu(gpu: dict) -> str:
    """'i' integrated · 'd' discrete · '?' unknown, from vendor/boot_vga."""
    vendor = (gpu.get("vendor") or "").lower()
    if vendor == _NVIDIA:
        return "d"
    if vendor == _INTEL:
        return "i"
    if vendor == _AMD:
        # AMD ships both APUs and dGPUs; the POST-selected controller is the
        # integrated one on an APU, and a discrete card is never boot_vga when
        # an APU exists. Bus 00 devices are the integrated display engine on
        # the vast majority of boards.
        if gpu.get("boot_vga") == "1":
            return "i"
        return "d" if not (gpu.get("pci") or "").startswith("0000:00") else "i"
    return "?"


def runtime_mode(gpus: list[dict] | None = None) -> dict:
    """Infer the mode the machine is *currently running* from GPU visibility.

    Returns ``{"mode": hybrid|discrete|integrated|None, "confidence": ...,
    "evidence": str}``. ``None`` means the heuristic is inconclusive and the
    firmware byte should be trusted instead.
    """
    if gpus is None:
        gpus = list_gpus()
    kinds = [_classify_gpu(g) for g in gpus]
    has_i, has_d = "i" in kinds, "d" in kinds
    if has_i and has_d:
        mode = MODE_HYBRID
    elif has_d:
        mode = MODE_DISCRETE
    elif has_i:
        mode = MODE_INTEGRATED
    else:
        mode = None
    named = ", ".join(
        f"{g.get('pci')}:{g.get('vendor')}" for g in gpus) or "no display controllers"
    return {
        "mode": mode,
        "confidence": "high" if mode else "low",
        "evidence": named,
        "gpu_count": len(gpus),
    }


# ---- staged-switch journal (post-boot verification) -----------------------


def boot_id() -> str | None:
    try:
        with open("/proc/sys/kernel/random/boot_id") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _write_json(path: str, data: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _read_json(path: str) -> dict:
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def record_pending(target: str, previous: str | None,
                   byte5_before: int | None = None) -> dict:
    """Remember that ``target`` was staged in this boot.

    On the next boot the journal is compared with the firmware's current mode:
    a match clears it, a mismatch means the switch did not take (the user
    probably did a warm reboot instead of a full power-off) and is surfaced as
    ``apply_failed`` so the app can tell them what to do.
    """
    data = {
        "target": target,
        "previous": previous,
        "byte5_before": byte5_before,
        "staged_at": time.time(),
        "boot_id": boot_id(),
    }
    try:
        _write_json(PENDING_PATH, data)
    except OSError:
        pass
    try:
        os.unlink(FAILED_PATH)
    except OSError:
        pass
    return data


def read_pending() -> dict:
    return _read_json(PENDING_PATH)


def clear_pending() -> None:
    try:
        os.unlink(PENDING_PATH)
    except OSError:
        pass


def clear_failed() -> None:
    try:
        os.unlink(FAILED_PATH)
    except OSError:
        pass


def _settle_journal(info: dict) -> None:
    """Reconcile a staged switch with the mode the machine boots into."""
    pending = read_pending()
    if not pending:
        failed = _read_json(FAILED_PATH)
        if failed:
            info["apply_failed"] = True
            info["requested"] = failed.get("target")
            info["apply_failed_at"] = failed.get("at")
        return

    target = pending.get("target")
    info["staged_target"] = target
    info["staged_at"] = pending.get("staged_at")

    if target and target == info.get("current"):
        # Booted into the requested mode: the switch applied.
        info["switch_applied"] = True
        clear_pending()
        clear_failed()
        return

    same_boot = pending.get("boot_id") == boot_id()
    if not same_boot:
        # A new boot happened and the mode still differs → the change did not
        # reach POST (usually a warm reboot rather than a full power-off).
        info["apply_failed"] = True
        info["requested"] = target
        try:
            _write_json(FAILED_PATH, {
                "target": target,
                "running": info.get("current"),
                "at": time.time(),
            })
        except OSError:
            pass
        clear_pending()
        return

    info["pending_reboot"] = True



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
        "busy": bool(out[2] & AP_BUSY_MASK),
        "raw": out[:8].hex(),
    }


def probe_ap() -> dict:
    """Read-only: dump Get_AP for every subfeature (root diagnostic).

    Used to pin down the package layout on a new board. Sends only GET
    methods — no Set_Data, no variable writes.
    """
    out: dict = {}
    for sub in range(4):
        payload = bytearray(32)
        payload[0] = sub
        try:
            raw = _invoke("get_ap", bytes(payload))
            out[f"get_ap[{sub:#04x}]"] = raw.hex()
        except (GpuModeError, OSError) as exc:
            out[f"get_ap[{sub:#04x}]"] = f"error: {exc}"
    return out


def switch_state() -> dict:
    """Read-only: EC mirror registers used by the handshake."""
    return {
        "mux_2e": _read_ec_byte(EC_MUX_REG),
        "switch_d1": _read_ec_byte(EC_SWITCH_REG),
        "ack_be": _read_ec_byte(AP_ACK_ADDR),
    }


def _set_data(addr: int, value: int) -> None:
    payload = bytearray(32)
    payload[0] = addr
    payload[1] = value
    out = _invoke("set_data", bytes(payload))
    if out[0] == 0:
        raise GpuModeError(f"Set_Data(0x{addr:02X}) reported failure")


def _wait_apply_ready(timeout: float = AP_READY_TIMEOUT_S,
                      poll: float = AP_POLL_S) -> dict:
    """Poll until the EC reports apply-ready after a trigger.

    The state machine walks ``0x01`` (trigger in flight) → ``0x02``
    (ready). Apply-ready is latched at ``0x02`` after a completed apply, so
    the resting value *before* a trigger is also ``0x02``. Accepting the
    first ``0x02`` could therefore acknowledge an apply that the EC has not
    started yet.

    Guard against that: after the trigger, require either the in-flight bit
    (``0x01``) to have been observed, or a minimum settle window, and then
    confirm apply-ready with a second, independent read.
    """
    started = time.monotonic()
    deadline = started + timeout
    seen_busy = False
    while True:
        time.sleep(poll)
        state = ap_state()
        if state["busy"]:
            seen_busy = True
        if state["apply_ready"] and not state["busy"]:
            settled = (time.monotonic() - started) >= 0.5
            if seen_busy or settled:
                time.sleep(0.05)
                confirm = ap_state()
                if confirm["apply_ready"] and not confirm["busy"]:
                    return confirm
        if time.monotonic() >= deadline:
            raise GpuModeError(
                f"firmware did not assert apply-ready within {timeout:.0f} s "
                f"(Get_AP data byte 0x{state['data_byte1']:02X})")


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

    # MSI's firmware refuses to move the MUX without mains power: an
    # interrupted apply on battery can leave the panel unrouted. Match that
    # requirement rather than risking a black screen on the next boot.
    if not _ac_online():
        raise GpuModeError(
            "AC power is not connected — connect the charger before changing "
            "the graphics mode (MSI firmware requires mains power to apply a "
            "MUX switch safely)")

    # Already staged for the next boot: never re-trigger the EC for nothing.
    if before["target"] == target:
        info = read_state()
        result.update({
            "noop": True,
            "already_staged": True,
            "state": info,
            "restart_required": bool(info.get("reboot_pending")),
            "note": f"{MODE_LABELS[target]} is already the staged target",
        })
        return result

    # Preflight: refuse only when a trigger is genuinely in flight.
    #
    # apply-ready (bit 1) stays latched after a completed apply — it is the
    # normal resting state once the machine has switched at least once.
    # Refusing on it would block every transition after the first, which is
    # exactly what happened on this unit. An in-flight trigger (bit 0) is
    # the ambiguous case: the EC is mid-apply and the target must not move.
    pre = ap_state()
    if pre["busy"]:
        raise GpuModeError(
            "an EC apply is already in flight (trigger bit set) — power off "
            "fully before switching again")
    result["ap_before"] = f"0x{pre['data_byte1']:02X}"

    _write_var(before_attrs, bytes(staged))
    after_attrs, after_payload = _read_var()
    if after_payload != bytes(staged) or after_attrs != before_attrs:
        raise GpuModeError("UEFI target write verification failed")
    result["byte5_after"] = decode(after_payload)["byte5"]

    triggered = False
    try:
        ap = ap_state()
        if ap["busy"]:
            raise GpuModeError(
                "an EC apply started while staging — refusing an ambiguous "
                "transition")
        trigger = (ap["data_byte1"] & 0xFC) | TRIGGER_MASK
        _set_data(AP_DATA_ADDR, trigger)
        triggered = True
        result["trigger"] = f"Set_Data(0x{AP_DATA_ADDR:02X}, 0x{trigger:02X})"

        ready = _wait_apply_ready()
        result["apply_ready_byte1"] = f"0x{ready['data_byte1']:02X}"
        _set_data(AP_ACK_ADDR, AP_ACK_VALUE)
        result["ack"] = f"Set_Data(0x{AP_ACK_ADDR:02X}, 0x{AP_ACK_VALUE:02X})"
    except Exception as exc:
        if triggered:
            # The EC has the trigger. Rewriting the variable now would make
            # the staged target disagree with what the EC is applying, so
            # leave it staged and tell the caller the truth.
            result["triggered"] = True
            result["error"] = str(exc)
            result["staged"] = True
            result["shutdown_required"] = True
            result["warning"] = (
                "the apply trigger was already sent, so the target stays "
                "staged; power off fully to let the firmware settle")
            raise GpuModeError(
                f"{exc} — the EC apply trigger was already sent, so "
                f"{MODE_LABELS[target]} remains staged; perform a full "
                "shutdown to let the firmware settle") from exc
        # Nothing reached the EC: put the previous target back.
        try:
            restore = bytearray(before_payload)
            _write_var(before_attrs, bytes(restore))
            result["restored"] = True
        except Exception:
            result["restored"] = False
        raise

    result["state"] = read_state()
    result["shutdown_required"] = True
    result["restart_required"] = True
    result["journal"] = record_pending(
        target, before["target"], before["byte5"])
    result["note"] = (
        f"{MODE_LABELS[target]} is staged. Restart to apply it; if a warm "
        "restart does not change the mode, shut down fully and power on.")
    return result


def power_action(kind: str = "reboot") -> dict:
    """Restart (or power off) to apply a staged MUX switch.

    Deliberately refuses unless a switch is actually staged: this must never
    become a generic remote reboot button on the daemon.
    """
    if kind not in ("reboot", "poweroff"):
        raise GpuModeError(f"unknown power action {kind!r}")
    if not available():
        raise GpuModeError("no GPU MUX on this machine")
    state = read_state()
    if not state.get("restart_required"):
        raise GpuModeError(
            "no graphics-mode change is staged — nothing to restart for")

    cmd = ["systemctl", "reboot" if kind == "reboot" else "poweroff"]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise GpuModeError(f"could not run {' '.join(cmd)}: {exc}") from exc
    return {
        "scheduled": kind,
        "staged_target": state.get("target"),
        "current": state.get("current"),
        "note": ("Reboot scheduled. If the mode is unchanged after boot, "
                 "shut down completely and power on again."),
    }


def handshake_probe(timeout: float = AP_READY_TIMEOUT_S,
                    poll: float = 0.1) -> dict:
    """Trace the EC apply handshake without changing the boot mode.

    Re-stages the *current* target, so the UEFI variable keeps the mode the
    firmware is already running — the trace exercises the trigger/ack path
    while leaving the next boot unchanged. Refuses when a different mode is
    already staged (that would silently apply it).
    """
    attrs, payload = _read_var()
    info = decode(payload)
    if info["target"] != info["current"]:
        raise GpuModeError(
            "a different mode is already staged — power off to apply or "
            "re-stage the current mode before running a self-test")
    pre = ap_state()
    if pre["busy"]:
        raise GpuModeError(
            "an EC apply is already in flight — wait for it to finish "
            "before running a self-test")

    trace: list[dict] = []
    t0 = time.monotonic()

    def snap(tag: str) -> dict:
        rec: dict = {"t": round(time.monotonic() - t0, 3), "step": tag}
        try:
            rec["ap"] = ap_state()
        except (GpuModeError, OSError) as exc:
            rec["ap"] = {"error": str(exc)}
        rec["ec"] = switch_state()
        trace.append(rec)
        return rec

    snap("before")
    _write_var(attrs, payload)          # same bytes: verifies the write path
    snap("var-written")

    ap = ap_state()
    trigger = (ap["data_byte1"] & 0xFC) | TRIGGER_MASK
    _set_data(AP_DATA_ADDR, trigger)
    snap("trigger-sent")

    deadline = time.monotonic() + timeout
    ready_after = None
    while time.monotonic() < deadline:
        time.sleep(poll)
        st = ap_state()
        if st["apply_ready"] and not st["busy"]:
            ready_after = st
            snap("ready")
            break
    if ready_after is None:
        snap("timeout")

    _set_data(AP_ACK_ADDR, AP_ACK_VALUE)
    snap("ack-sent")

    return {
        "mode": info["target"],
        "trigger": f"0x{trigger:02X}",
        "ready_after": ready_after,
        "trace": trace,
        "note": "boot mode unchanged — the current target was re-staged",
    }


def restore_target(mode: str) -> dict:
    """Re-stage a previous target without running the handshake."""
    target = normalize_mode(mode)
    attrs, payload = _read_var()
    staged = bytearray(payload)
    staged[MODE_BYTE] = (staged[MODE_BYTE] & ~0x03) | MODE_VALUES[target]
    _write_var(attrs, bytes(staged))
    return {"restored_target": target}
