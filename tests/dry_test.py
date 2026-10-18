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
check("msiec shift parse: name None, raw 192", out["name"] is None and out["raw"] == 192)
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

print(f"\nALL {CHECKS} CHECKS PASSED ✓")
