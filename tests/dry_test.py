#!/usr/bin/env python3
"""Hardware-free tests for the Mystic Center privileged backend and GUI data
logic: fan curves, battery clamps, EC allow-list, JSON protocol, msi-ec text
parsing.

Run:  tests/dry_test.py
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import tempfile

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mystic_center import battery, msiec                    # noqa: E402
from mystic_center.ecio import ALLOWED, EC, IO_FILE         # noqa: E402

CHECKS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        raise SystemExit(1)


# ---------------------------------------------------------------- fans

check("fan modes exposed to users are firmware-managed",
      msiec.FAN_MODES == ("auto", "silent", "advanced"))

# ------------------------------------------------------------- battery clamps

check("battery validate: start > end-5 → pulled under end",
      battery.validate(80, 90) == (75, 80))
check("battery validate: end 100 → start 0 (no limit)",
      battery.validate(100, 80) == (0, 100))
check("battery validate: end below floor clamps to 20",
      battery.validate(5, 0) == (0, 20))
check("battery validate: sane input unchanged", battery.validate(80, 60) == (60, 80))

# ------------------------------------------------------------- EC allow-list

check("EC file path is debugfs io", IO_FILE.endswith("ec0/io"))

ec = EC()
check("EC unavailable exposed as unavailable",
      ec.available() is False)  # only true on the real lazy open path

# simulate availability by monkey-patching since debugfs needs root
import mystic_center.ecio as ecio_mod  # noqa: E402

ec._available = True
ec._read_space = lambda: bytearray(256)  # type: ignore[method-assign]
ec._handle = lambda: -1                  # type: ignore[method-assign]

_pwrite_calls: list[tuple[int, bytes]] = []
_orig_pwrite = ecio_mod.os.pwrite
ecio_mod.os.pwrite = lambda fd, data, off: _pwrite_calls.append((fd, bytes(data)))


try:
    ec.write_reg(0x99, 0x10)
    check("EC write to unallow-listed reg raises", False)
except Exception:
    check("EC write to unallow-listed reg raises", True)

ok = ec.write_reg(0xD4, 0x0D)   # fake space verifies fail → returns False
ok = ec.write_reg(0xD4, 0x0D)   # fake space verifies fail → returns False
err_verify = ec.last_error
check("EC write to allow-listed reg attempts pwrite pair",
      ok is False and
      any(ch[1] == bytes((0xD4, 0x0D)) for ch in _pwrite_calls))
check("EC write failure surfaces last_error",
      err_verify is not None and "verify" in err_verify)

try:
    ec.write_reg(0x71, 0x30)
    check("EC write to duty-readback reg rejected", False)
except Exception as exc:
    check("EC write to duty-readback reg rejected", "allow-list" in str(exc))

ecio_mod.os.pwrite = _orig_pwrite  # restore symmetry

check("EC allow-list is exactly the 5 writable registers",
      {hex(a) for a in ALLOWED} ==
      {"0xd2", "0xd4", "0xd7", "0x98", "0xeb"})

# ------------------------------------------------------- msi-ec text parsing

parsed = msiec.shift_mode.__wrapped__ if hasattr(msiec.shift_mode, "__wrapped__") else None
# direct parse of the documented 'unknown (192)' line via a fake attr read
msiec.read_file = lambda path: "unknown (192)" if path.endswith("shift_mode") else None  # type: ignore
out = msiec.shift_mode()
check("msiec shift parse: raw 192 (0xC0) resolves to turbo",
      out["name"] == "turbo" and out["raw"] == 192)
msiec.read_file = lambda path: "unknown (77)" if path.endswith("shift_mode") else None  # type: ignore
out = msiec.shift_mode()
check("msiec shift parse: truly unknown raw stays None",
      out["name"] is None and out["raw"] == 77)
msiec.read_file = lambda path: "turbo" if path.endswith("shift_mode") else None  # type: ignore
out = msiec.shift_mode()
check("msiec shift parse: clean name turbo", out["name"] == "turbo" and out["raw"] is None)

# ------------------------------------------------------------ JSON protocol
from mystic_center import client as client_mod  # noqa: E402
from mystic_center.server import MysticServer   # noqa: E402

TMP_SOCKET = os.path.join(tempfile.mkdtemp(), "test.sock")
client_mod.SOCKET_PATH = TMP_SOCKET

OPS = {
    "ping": lambda args: {"pong": True, "echo": args},
    "boom": lambda args: (_ for _ in ()).throw(RuntimeError("intentional")),
}


def dispatcher(op, args):
    if op in OPS:
        return OPS[op](args)
    raise KeyError(f"unknown op {op!r}")


server = MysticServer(ops_dispatch=dispatcher, socket_path=TMP_SOCKET)
threading.Thread(target=server.serve_forever, daemon=True).start()

with client_mod.Client() as c:
    res = c.request("ping", hello="world")
    check("socket roundtrip", res["pong"] is True and res["echo"].get("hello") == "world")

    try:
        c.request("boom")
        check("daemon errors propagate", False)
    except RuntimeError as e:
        check("daemon errors propagate", "intentional" in str(e))

    try:
        c.request("no-such-op")
        check("unknown op rejected", False)
    except RuntimeError as e:
        check("unknown op rejected", "unknown op" in str(e))

server.shutdown()
server.server_close()

# ---------------------------------------------------------------- GPU MUX

from mystic_center import gpumux  # noqa: E402

# byte5 layout, verified against this machine's live MsiDCVarData
info = gpumux.decode(bytes([0x01, 0x01, 0x00, 0x06, 0x00, 0x30, 0x01] + [0] * 13))
check("gpu decode: hybrid target and current", 
      info["target"] == "hybrid" and info["current"] == "hybrid")
check("gpu decode: capability bits",
      info["new_switch"] and info["integrated_supported"]
      and info["discrete_supported"] and not info["reserved"])
check("gpu supported modes: all three advertised",
      gpumux.supported_modes(info) == ["hybrid", "discrete", "integrated"])

# a board that only advertises hybrid+discrete (integrated bit clear)
legacy = gpumux.decode(bytes([0] * 5 + [0x10, 0] + [0] * 13))
check("gpu supported modes: no integrated bit → hybrid+discrete",
      gpumux.supported_modes(legacy) == ["hybrid", "discrete"])

# no new-switch flow → legacy pair only
old = gpumux.decode(bytes([0] * 5 + [0x00, 0] + [0] * 13))
check("gpu supported modes: legacy flow → hybrid+discrete",
      gpumux.supported_modes(old) == ["hybrid", "discrete"])

check("gpu mode aliases map to canonical modes",
      gpumux.normalize_mode("dgpu") == "discrete"
      and gpumux.normalize_mode("igpu") == "integrated"
      and gpumux.normalize_mode("MSHybrid") == "hybrid")

try:
    gpumux.normalize_mode("vulkan")
    check("gpu unknown mode rejected", False)
except gpumux.GpuModeError:
    check("gpu unknown mode rejected", True)

# staging must only touch bits 0..1 of byte 5 and nothing else
_payload = bytes([0x01, 0x01, 0x00, 0x06, 0x00, 0x30, 0x01] + [0xAB] * 13)
for mode, want in (("hybrid", 0x30), ("discrete", 0x31), ("integrated", 0x32)):
    staged = bytearray(_payload)
    staged[gpumux.MODE_BYTE] = (staged[gpumux.MODE_BYTE] & ~0x03) | gpumux.MODE_VALUES[mode]
    check(f"gpu staging {mode}: byte5 0x30 → 0x{want:02X}",
          staged[gpumux.MODE_BYTE] == want
          and staged[:5] == _payload[:5] and staged[6:] == _payload[6:])

# dry run must never write: point the paths at a temp copy
import tempfile as _tf  # noqa: E402
_tmpdir = _tf.mkdtemp()
_tmpvar = os.path.join(_tmpdir, "MsiDCVarData")
with open(_tmpvar, "wb") as fh:
    fh.write((0x00000007).to_bytes(4, "little")
             + bytes([0x01, 0x01, 0x00, 0x06, 0x00, 0x30, 0x01] + [0] * 13))

_orig_var_path = gpumux.VAR_PATH
gpumux.VAR_PATH = _tmpvar
try:
    before_bytes = open(_tmpvar, "rb").read()
    res = gpumux.set_mode("discrete", dry_run=True)
    after_bytes = open(_tmpvar, "rb").read()
    check("gpu dry run reports staged byte", res["byte5_staged"] == 0x31)
    check("gpu dry run writes nothing", before_bytes == after_bytes)

    # unsupported-mode guard: advertise no integrated bit
    with open(_tmpvar, "wb") as fh:
        fh.write((0x00000007).to_bytes(4, "little")
                 + bytes([0x01, 0x01, 0x00, 0x06, 0x00, 0x10, 0x01] + [0] * 13))
    try:
        gpumux.set_mode("integrated", dry_run=True)
        check("gpu unsupported mode refused", False)
    except gpumux.GpuModeError as exc:
        check("gpu unsupported mode refused", "not advertised" in str(exc))
finally:
    gpumux.VAR_PATH = _orig_var_path

# The AP data byte is a two-bit state machine, verified live on this unit:
#   0x02 = apply-ready (latched after a completed apply)
#   0x01 = trigger in flight
#   0x00 = transient
check("gpu AP state: 0x02 is ready and not busy",
      bool(0x02 & gpumux.AP_READY_MASK) and not (0x02 & gpumux.AP_BUSY_MASK))
check("gpu AP state: 0x01 is busy and not ready",
      not (0x01 & gpumux.AP_READY_MASK) and bool(0x01 & gpumux.AP_BUSY_MASK))
check("gpu AP state: ready and busy are disjoint",
      gpumux.AP_READY_MASK & gpumux.AP_BUSY_MASK == 0)

# A failed post-trigger step must keep the target staged, not rewrite the
# variable underneath an EC that is already applying it.
import mystic_center.gpumux as _gm  # noqa: E402
_tmpdir2 = _tf.mkdtemp()
_tmpvar2 = os.path.join(_tmpdir2, "MsiDCVarData")
with open(_tmpvar2, "wb") as fh:
    fh.write((0x00000007).to_bytes(4, "little")
             + bytes([0x01, 0x01, 0x00, 0x06, 0x00, 0x30, 0x01] + [0] * 13))

_orig = (_gm.VAR_PATH, _gm.ap_state, _gm._set_data, _gm._wait_apply_ready,
         _gm._ac_online, _gm.STATE_DIR, _gm.PENDING_PATH, _gm.FAILED_PATH)
_state_dir = _tf.mkdtemp()
_gm.STATE_DIR = _state_dir
_gm.PENDING_PATH = os.path.join(_state_dir, 'gpu-pending.json')
_gm.FAILED_PATH = os.path.join(_state_dir, 'gpu-apply-failed.json')
_gm._ac_online = lambda: True
_gm.VAR_PATH = _tmpvar2
_gm.ap_state = lambda: {"flag": 1, "data_byte1": 0x02,
                        "apply_ready": True, "busy": False,
                        "raw": "0100020000000000"}


def _boom(*_a, **_k):
    raise _gm.GpuModeError("simulated readiness timeout")


_gm._wait_apply_ready = _boom
try:
    _gm._set_data = lambda addr, value: None
    try:
        _gm.set_mode("discrete")
        check("gpu post-trigger failure raises", False)
    except _gm.GpuModeError as exc:
        check("gpu post-trigger failure raises", "trigger" in str(exc))
    staged = open(_tmpvar2, "rb").read()[4:]
    check("gpu post-trigger failure keeps target staged (no rollback)",
          gpumux.decode(staged)["target"] == "discrete")

    # Preflight: a latched apply-ready (0x02) is the normal resting state and
    # must NOT block the next switch. Only an in-flight trigger (0x01) may.
    calls = []
    _gm._wait_apply_ready = lambda: {"data_byte1": 0x02}
    _gm._set_data = lambda addr, value: calls.append((addr, value))
    with open(_tmpvar2, "wb") as fh:
        fh.write((0x00000007).to_bytes(4, "little")
                 + bytes([0x01, 0x01, 0x00, 0x06, 0x00, 0x30, 0x01] + [0] * 13))
    res = _gm.set_mode("integrated")
    check("gpu latched apply-ready does not block a switch",
          gpumux.decode(open(_tmpvar2, "rb").read()[4:])["target"] == "integrated")
    check("gpu trigger preserves upper bits of the AP data byte",
          (0xD1, 0x01) in calls)
    check("gpu successful switch acknowledges apply-ready",
          (0xBE, 0x02) in calls and res["shutdown_required"] is True)

    # An in-flight trigger is the genuinely ambiguous state: refuse before
    # touching the variable.
    _gm.ap_state = lambda: {"flag": 1, "data_byte1": 0x01,
                            "apply_ready": False, "busy": True, "raw": ""}
    with open(_tmpvar2, "wb") as fh:
        fh.write((0x00000007).to_bytes(4, "little")
                 + bytes([0x01, 0x01, 0x00, 0x06, 0x00, 0x30, 0x01] + [0] * 13))
    before = open(_tmpvar2, "rb").read()
    try:
        _gm.set_mode("discrete")
        check("gpu in-flight trigger refused", False)
    except _gm.GpuModeError as exc:
        check("gpu in-flight trigger refused", "in flight" in str(exc))
    check("gpu in-flight trigger leaves the variable untouched",
          open(_tmpvar2, "rb").read() == before)
finally:
    (_gm.VAR_PATH, _gm.ap_state, _gm._set_data, _gm._wait_apply_ready,
     _gm._ac_online, _gm.STATE_DIR, _gm.PENDING_PATH, _gm.FAILED_PATH) = _orig

# ------------------------------------------------------- runtime topology
_both = [{"vendor": "0x8086", "pci": "0000:00:02.0", "boot_vga": "1"},
         {"vendor": "0x10de", "pci": "0000:01:00.0", "boot_vga": "0"}]
check("gpu runtime: both controllers → hybrid",
      gpumux.runtime_mode(_both)["mode"] == "hybrid")
check("gpu runtime: dGPU only → discrete",
      gpumux.runtime_mode([_both[1]])["mode"] == "discrete")
check("gpu runtime: iGPU only → integrated",
      gpumux.runtime_mode([_both[0]])["mode"] == "integrated")
check("gpu runtime: no controllers → unknown",
      gpumux.runtime_mode([])["mode"] is None)

# ------------------------------------------------- post-boot journal logic
_jdir = _tf.mkdtemp()
_orig_j = (gpumux.STATE_DIR, gpumux.PENDING_PATH, gpumux.FAILED_PATH)
gpumux.STATE_DIR = _jdir
gpumux.PENDING_PATH = os.path.join(_jdir, "gpu-pending.json")
gpumux.FAILED_PATH = os.path.join(_jdir, "gpu-apply-failed.json")
try:
    gpumux.record_pending("integrated", "hybrid", 0x30)
    check("gpu journal: pending target recorded",
          gpumux.read_pending().get("target") == "integrated")
    same = {"current": "hybrid"}
    gpumux._settle_journal(same)
    check("gpu journal: same boot → pending_reboot",
          same.get("pending_reboot") is True)

    # simulate a reboot that did NOT apply the change
    pend = gpumux.read_pending()
    pend["boot_id"] = "0000-simulated-previous-boot"
    gpumux._write_json(gpumux.PENDING_PATH, pend)
    failed = {"current": "hybrid"}
    gpumux._settle_journal(failed)
    check("gpu journal: reboot without apply → apply_failed",
          failed.get("apply_failed") is True
          and failed.get("requested") == "integrated")
    check("gpu journal: failed apply is persisted for the next snapshot",
          gpumux.read_pending() == {}
          and os.path.exists(gpumux.FAILED_PATH))

    # simulate a boot that DID apply the change
    gpumux.clear_failed()
    gpumux.record_pending("discrete", "hybrid", 0x30)
    ok = {"current": "discrete"}
    gpumux._settle_journal(ok)
    check("gpu journal: boot in the requested mode clears the journal",
          ok.get("switch_applied") is True
          and gpumux.read_pending() == {})
finally:
    (gpumux.STATE_DIR, gpumux.PENDING_PATH, gpumux.FAILED_PATH) = _orig_j

# power actions must never fire without a staged switch
_orig_pa = (gpumux.available, gpumux.read_state)
gpumux.available = lambda: True
gpumux.read_state = lambda: {"available": True, "restart_required": False}
try:
    gpumux.power_action("reboot")
    check("gpu restart refused when nothing is staged", False)
except gpumux.GpuModeError as exc:
    check("gpu restart refused when nothing is staged",
          "nothing to restart" in str(exc))
finally:
    gpumux.available, gpumux.read_state = _orig_pa

# handshake: busy → ready is accepted; a steady busy times out
_ap_seq = iter([0x01, 0x02, 0x02])
_orig_ap = gpumux.ap_state
def _fake_ap():
    b = next(_ap_seq, 0x02)
    return {"data_byte1": b, "apply_ready": bool(b & 2),
            "busy": bool(b & 1), "raw": ""}
gpumux.ap_state = _fake_ap
try:
    got = gpumux._wait_apply_ready(timeout=1.0, poll=0.01)
    check("gpu handshake waits through the in-flight bit",
          got["data_byte1"] == 0x02)
    gpumux.ap_state = lambda: {"data_byte1": 0x01, "apply_ready": False,
                               "busy": True, "raw": ""}
    try:
        gpumux._wait_apply_ready(timeout=0.25, poll=0.05)
        check("gpu handshake times out on a steady in-flight trigger", False)
    except gpumux.GpuModeError:
        check("gpu handshake times out on a steady in-flight trigger", True)
finally:
    gpumux.ap_state = _orig_ap

# --------------------------------------------------- EC shift byte decoding
from mystic_center import msiec  # noqa: E402
check("shift raw 0xC0 decodes to turbo",
      msiec.SHIFT_RAW_NAMES[0xC0] == "turbo")
check("shift raw 0xC2 decodes to eco",
      msiec.SHIFT_RAW_NAMES[0xC2] == "eco")


print(f"\nALL {CHECKS} CHECKS PASSED \u2713")
