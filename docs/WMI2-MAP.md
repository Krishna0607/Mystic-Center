# MSI WMI2 platform map — `MSI_ACPI` / `msi-wmi-platform` (fan curves & friends)

Target: MSI Crosshair 16 HX D14VFKG (MS-15P2), EC `15P2EMS1.110`, Linux kernel
6.12+ with the in-tree `msi_wmi_platform` driver (CachyOS: loaded by default).
Evidence sources: the extracted/decompiled MSI Center 2.0.74.0 binaries (see
[`MSI-CENTER-RE.md`](MSI-CENTER-RE.md)), the upstream kernel driver + the
draft fan-control extensions (OGC repo, `c549695`), and probes on the machine
itself (this document mirrors `mystic_center/wmi2.py`, which implements it).

## The interface

MSI exposes one WMI device per notebook with the class GUID
`ABBC0F6E-8EA1-11D1-00A0-C90629100000` (`MSI_ACPI`). The kernel driver creates
two things on top of it:

1. `hwmon` (`pwm1..4` rpm inputs) — throttled telemetry,
2. **debugfs** — one file per WMI method (method ids 1..24, named
   `get_/set_<thing>`): glob `/sys/kernel/debug/msi-wmi-platform-*/`.

The debugfs files are 32-byte packages (ACPI `Buffer`). Write = invoke the
method with the written buffer; the next `open()` after each write re-renders
the stored output buffer (seq_file). Files are `0600` → **root only**, which
is exactly why `mysticd` (the daemon) is the writer.

Input byte `[0]` selects the **subfeature**. Output byte `[0]` is a status
value that goes **non-zero on success** (0 = failure/unknown output). For the
ACPI control method behind it, it is *not* thread-safe — it must be
serialized externally; both the kernel driver (mutex) and `mysticd` (thread
lock) do that.

`mystic_center/wmi2.py` wraps the files normally used for live control.
Inventory (`get_wmi`, `get_ec`) is used only by `mystic-center wmi2`.

## FAN (method `GET_FAN`/`SET_FAN`, id 17/18)

| Subfeature (buf[0]) | Semantics |
|---|---|
| `0x00` | Fan speeds — 4 channels, big-endian 16-bit each at buf[1..8]. Sub-package math: RPM = 480000 / v, so the firmware stores the division counter. `v == 0` means a stopped channel. |
| `0x01` | CPU fan duty table — six u8 values (percent, firmware-clamps) at buf[2..7]. buf[1] stays 0/unknown; buf[8..9] carry firmware-internal fields the SET preserves. |
| `0x02` | GPU fan duty table — same layout at buf[2..7]. |

Note the offset asymmetry with TEMPERATURE below: fan duty points start at
package byte 2 (data offset 1), matching the upstream hwmon patch's
`auto_pointN_pwm` attributes (`index 0x1..0x6 -> buffer[index+1]`).

`fan_rpm()` decodes the counter to RPM. `fan_table(which)` returns `None`
when all six bytes are zero, which is also the "no custom curve stored yet"
marker (used to seed MSI defaults on first manual enable).

## TEMPERATURE (method `GET_TEMPERATURE`/`SET_TEMPERATURE`, id 13/14)

| Subfeature | Semantics |
|---|---|
| `0x01` | CPU curve temperature points (°C) — six u8 at buf[1] **and buf[4..8]**; buf[2..3] stay untouched by the firmware (kept — mirroring the kernel patch: temp attributes use index `0x0, 0x3..0x7`). |
| `0x02` | GPU curve temperature points — same layout. |

The set flow mirrors the upstream driver: GET into a 32-byte buffer → patch
the six temp bytes (preserving everything else, including the reserved
buf[2..3]) → SET that buffer.

## AP (method `GET_AP`/`SET_AP`, id 23/24) — the switch MSI Center uses

| Subfeature | Semantics |
|---|---|
| `0x01` | Fan control "auto points" mode: flags byte at buf[1]. `BIT(7)` (`0x80`) enables **table-driven fan control** (the six duty/temp tables above). Clear → firmware-embedded automatic control (what `msi-ec` exposes as `auto`). Other bits are kept for firmware state; the upstream write flow is GET → set/clear the bit → SET. |

This bit is what MSI Center 2.x toggles for its "User scenario custom fan
speed" UI; our module names the flag "manual" everywhere in the RPC.

## How Mystic Center's port maps to MSI Center (Windows)

MSI Center service message `WMI2:GFAN_REQ:` → this interface
(`Get_Fan`/`Set_Fan` with the ASCII subfeature appended); the custom-fan UI
writes `Set_AP` (bit7 on), then rebuilds Set_Fan/Set_Temperature by subfeature
0x01/0x02 in the same order as MSI Center's FanComponent JSON =
`{SpeedUp, SpeedDown, ...}` — the numbers we push through the six-point table
editor are the same six points (MSI Center displays seven UI rows; the first
row is fixed 0 °C → firmware-managed and doesn't reach the tables).

`mystic-center` GUI mapping:

| MSI Center | mystic-center |
|---|---|
| Advanced/Custom user scenario | `mystic-center fan-curve manual --on` / Fans page "Custom fan curve" |
| Auto | `fan-curve manual --off` (or leaving `set_fan_preset auto`, watchdog revert) |
| Curve editor (7 rows) | six editable points via GUI spin row / CLI `--cpu/--gpu T:D` |
| Presets (Fast/Quiet/Balanced...) | saved profiles: `fan-curve set --save-as name` |

## Fail-safes (all in `privops.py` + `wmi2.FanWatchdog`)

1. Write path is serialized (`WMI2._lock`) — the ACPI method is not
   thread-safe.
2. Every write is verified by a subsequent read-back; mismatch → `WMI2Error`.
3. Latencies are bounded; nothing busy-waits on the WMI bus.
4. While manual control is on, a watchdog thread samples every 2.5 s and
   reverts to firmware automatic control after a CPU temp ≥ 97 °C sustained
   for 4 consecutive samples (~10 s), parking `msi-ec fan_mode` to `auto`.
5. Daemon exit (`close()`) clears the AP bit and re-parks fan mode even on
   exception/crash — systemd restarts do not leave the firmware wedged.
6. Any `set_fan_preset(auto)` first disable table-driven control.

The physical curve lives in the EC firmware — if the daemon dies without
reverting (SIGKILL), the firmware keeps the last table; the watchdog runs in
the daemon, so treat `systemctl kill --signal=SIGKILL mystic-center` as the
only escape hatch that skips a revert (the curve used stays valid and firmware
applies it). Beware: a bad curve with a low duty saturating at high temp
would not recover without disabling manual mode — the 97 °C watchdog covers
that window as long as the daemon lives.

## Verification workflow (hardware in the loop)

```bash
sudo ./install.sh --system          # after install
sudo tools/wmi2_probe.py            # read-only inventory + versions
sudo tools/wmi2_probe.py --watch 10 # sanity RPM
sudo tools/wmi2_probe.py --write-test   # temporary curve + restore
```

Then the daemonized path (`mystic-center wmi2`, `mystic-center fan-curve ...`)
and the GUI Fans page.

## Live validation (2026-10-18, MS-15P2, EC `15P2EMS1.110`)

Scripted through the running daemon (root-side, no sudo needed): baseline
(auto) → manual ON with unchanged tables → test duties `65..90 %` → restore
→ manual OFF. Fans audibly ramped; everything was restored.

- **AP bit**: flags `0x0D` → `0x8D` (BIT(7) set) → `0x0D` — exact
  round-trip, no other bits touched.
- **Duty writes reach the EC**: the test curve drove CPU tach
  2962 → 4137 RPM and the second (GPU) tach 0 → 2981 RPM from its zero-fan
  idle; every write was readback-verified by `set_fan_table` /
  `set_temp_table`.
- **Restore**: captured duty tables written back byte-exact, AP bit
  cleared, RPMs returned to the auto regime (CPU ~2900 @ 48 %).
- **One sanitizer normalisation**: the firmware's leading temp point is a
  placeholder `0`; `set_temp_table` floors point temps at 30 °C, so after
  any curve write that byte reads 30 — identical first-segment behaviour,
  and irrelevant while the AP bit is clear.
- **Watchdog**: armed at daemon start (`fan watchdog armed` in the
  journal), never fired (temps stayed < 60 °C).

Reference: kernel driver `msi-wmi-platform.c` (in-tree) + fan-control draft
(`ogc-linux/drivers/platform/x86/msi-wmi-platform.c`, commit `c549695`) —
root of the offset conventions documented above.
