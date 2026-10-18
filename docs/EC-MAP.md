# EC register map — MSI-15P2 / CONF_G2_10 (mmsi-ec source, r734)

Active firmware: `15P2EMS1.110` → block `CONF_G2_10` in
`/usr/src/msi-ec-r734…/msi-ec.c`.

| Register | Meaning | Driver route |
|---|---|---|
| `0x2C`/`0x2D` | micmute / mute LED | driver led attrs |
| `0x2E` + bit 1 | webcam enable | sysfs `webcam`, `webcam_block` |
| `0x68` | CPU realtime temperature (°C) | sysfs `cpu/realtime_temperature` |
| `0x6A`–`0x6F` | CPU fan curve — 6 temp points · **read-only here** | probe: writes ignored |
| `0x71` | CPU fan realtime duty (readback) | read-only |
| `0x72`–`0x78` | CPU fan curve — 7 duty points · **read-only here** | probe: writes ignored |
| `0x80` | GPU realtime temperature (reads 0 on this unit → prefer nvidia-smi) | sysfs `gpu/realtime_temperature` |
| `0x82`–`0x87` | GPU fan curve — 6 temp points · **read-only here** | probe: writes ignored |
| `0x89` | GPU fan realtime duty (readback) | read-only |
| `0x8A`–`0x90` | GPU fan curve — 7 duty points · **read-only here** | probe: writes ignored |
| `0x98` + bit 7 | Cooler Boost | sysfs `cooler_boost` |
| `0xD2` | shift-mode byte: turbo `0xC4` (notes say "sometimes 0xC0"), eco `0xC2`, comfort `0xC1` | sysfs `shift_mode` (our calibration op reads raw) |
| `0xD4` | fan mode byte: auto `0x0D`, silent `0x1D`, basic `0x4D`, advanced `0x8D` | sysfs `fan_mode` |
| `0xD7` | charge control (kernel power_supply handles charge thresholds) | sysfs `charge_control_*_threshold` |
| `0xE8` bit 4 (invert) | Fn↔Win swap | sysfs `fn_key` / `win_key` |
| `0xEB` mask 0x0F | Super Battery | sysfs `super_battery` |

## Why the daemon still needs raw EC access

msi-ec sysfs covers the mode/battery/perk registers, but there is one class of
raw reads worth having: fan duty readbacks (`0x71`/`0x89`) and the raw
shift-mode byte used by the calibration op. All writes stay inside the seven
documented registers above; fan curve tables are NOT writable on this
firmware (see findings below). EC access goes through the `ec_sys` debugfs
`io` file (binary `[address, value]` writes, 256-byte space read).

## Fan tuning on this firmware generation — findings

This unit runs a **2024-generation EC** (it ships the `msi_wmi_platform`
kernel driver alongside msi-ec). Controlled probe, 2026-10-18 (see repository
history), with fan mode latched to advanced (`0xD4 = 0x8D`):

- The default curve *is* visible in the register space as (temp, duty) pairs —
  CPU `0x6A..↔0x72..` (50 °C/35 % … 100 °C/90 %), GPU `0x82..↔0x8B..`.
- **Writes to those addresses do not stick**: single-byte writes (values 45 and
  77) to `0x6A`, `0x6B`, `0x71`, `0x72` were re-read unchanged, and the
  distinctive values appeared nowhere in `0x60..0x94`.
- The legacy MControlCenter table layout (`fan1SpeedSettingStartAddress=0x72`
  etc., verified in their `operate.cpp`) evidently applies to older EC
  generations only.

Conclusion: MSI Center on Windows drives the fans over its **WMI platform
interface** (`MSI_ACPI` GUID `ABBC0F6E-…`), not raw EC bytes. The legacy
MControlCenter EC-table layout (`0x72/0x8A …`) applies to older EC
generations only, and this firmware **silently ignores** those writes.

> **Post-RE update (2026-10):** the WMI avenue is the real path: the
> in-kernel `msi_wmi_platform` driver exposes the same methods MSI Center
> uses (`get/set_fan`, `get/set_temperature`, `get/set_ap`) via debugfs, and
> BIT(7) of the AP fan-mode flag switches the firmware to the six-point
> duty/temperature tables. Custom fan curves are therefore **not**
> a Windows-only feature anymore — implemented in `mystic_center/wmi2.py`
> (semantics validated against the upstream kernel patch; live probe:
> `sudo tools/wmi2_probe.py --write-test`, see
> [`WMI2-MAP.md`](WMI2-MAP.md)). The EC byte path above stays the dead end on
> this firmware generation; the `0x71`/`0x89` duty registers remain
> readback-only telemetry.

## Access summary

- sysfs msi-ec attributes — root writable
- `/sys/kernel/debug/ec/ec0/io` — root, requires `ec_sys write_support=1`
  (mcontrolcenter's modprobe conf already covers it; ours duplicates idempotently)
- everything else (hwmon telemetry, nvidia-smi, power_supply) — user-readable
