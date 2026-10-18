# Mystic Center

One GTK4 app to replace MSI Center on your MSI laptop (Crosshair 16 HX D14VFKG,
EC firmware `15P2EMS1.110`): system overview, performance modes, fan control,
battery charge limits, and the full 24-zone RGB keyboard controller — all in one
window.

```
Overview · Performance · Fans · Battery · Lighting · Settings
```

## How it works

- **GUI** (GTK4/libadwaita, Python) — unprivileged, talks to your keyboard via
  the udev `uaccess` rule, and to the privileged backend via a Unix socket.
- **`mysticd`** — root systemd service that owns all privileged work:
  - reads temperatures/fans/battery from kernel interfaces (msi-ec, hwmon,
    nvidia-smi, power_supply sysfs)
  - performs platform-mode + charge-threshold services through the `msi-ec`
    kernel driver
  - in *advanced fan mode*, drives CPU/GPU fan duty registers (EC `0x71`/`0x89`)
    through `/sys/kernel/debug/ec/ec0/io` — with a strict register
    **allow-list**, read-back verification, and a **fail-safe** that reverts fan
    control to firmware (`auto`) when the daemon stops or crashes.
  - drives **custom fan curves** exactly like MSI Center does on Windows: via
    the `msi_wmi_platform` interface (`get/set_fan`, `get/set_temperature`,
    `get/set_ap` debugfs files) — six (temp °C, duty %) points per fan,
    table-driven in the firmware itself, with a **thermal runaway watchdog**
    (CPU ≥ 97 °C sustained → revert to auto) plus the same daemon-shutdown
    fail-safe.

Register map documentation: [`docs/EC-MAP.md`](docs/EC-MAP.md). The lighting HID
protocol documentation (reverse-engineered) is in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).
The MSI WMI2 platform interface (custom fan curves) is in
[`docs/WMI2-MAP.md`](docs/WMI2-MAP.md); the full MSI Center teardown that
motivated it is in [`docs/MSI-CENTER-RE.md`](docs/MSI-CENTER-RE.md).

## Install

```bash
cd ~/mystic-center
./install.sh              # user parts (GUI + CLI + autostart-ready .desktop)
sudo ./install.sh --system  # systemd service + ec_sys write_support + udev rule
```

The service starts automatically. Check with:

```bash
mystic-center status
systemctl status mystic-center
```

## CLI

```bash
mystic-center status                 # live snapshot
mystic-center perf turbo             # eco | comfort | turbo
mystic-center perf --cooler on       # cooler boost toggle
mystic-center fans balanced          # auto | silent
mystic-center fan-curve show         # WMI2 table control + stored curves
mystic-center fan-curve manual --on  # enable table-driven (custom curve) mode
mystic-center fan-curve set \
  --cpu "50:35,60:45,70:55,80:70,90:80,100:90" \
  --gpu "50:35,60:45,70:55,80:70,90:80,100:90"
mystic-center fan-curve reset        # MSI-Center default curve (both fans)
mystic-center fan-curve profiles --apply daily   # saved profiles
mystic-center wmi2                   # WMI platform interface inventory
mystic-center battery --end 80 --start 60
mystic-center webcam off             # WiFi-cam privacy toggle
mystic-center fn-swap right          # Fn on the right side
mystic-center calibrate              # raw EC mode-code exploration (safe)
mystic-center lighting steady FF7F20 # the full legacy lighting CLI
mystic-center                        # GUI
```

## Safety notes

- EC writes are limited to `0x71 0x89 0xD2 0xD4 0xD7 0x98 0xEB` — every one is
  from the msi-ec driver's own G2_10 register map. (The EC fan curve
  registers are read-only on this firmware — write probing is not attempted.)
- Fan modes (auto/silent) run in firmware; Cooler Boost gives max cooling.
- **Custom fan curves** use the firmware's table-driven mode through the same
  WMI platform interface MSI Center drives on Windows (NOT EC writes) —
  every write is read-back verified, and:
  - a watchdog reverts to firmware auto if the CPU sits ≥ 97 °C under
    manual control (4 samples × 2.5 s),
  - leaving manual mode (or stopping/restarting the daemon) re-enables
    firmware automatic control, so the machine never stays in a stale state,
  - curves are sanitized (monotonic ramp, 0–100 %) before they reach the
    firmware.
- Charge limits are clamped (start ≤ end − 5, end ≤ 100).
- If anything odd happens, a full laptop power-off (cold boot) restores the EC
  to factory defaults.

## Troubleshooting

- **Daemon down** — `systemctl start mystic-center`, or if it fails:
  `journalctl -u mystic-center -n 50`
- **Custom fan curve unavailable** — check
  `ls /sys/kernel/debug/msi-wmi-platform-*`. If missing:
  - `lsmod | grep msi_wmi_platform` — driver must be loaded (standard on
    CachyOS for this generation);
  - `mount | grep debugfs` — mount it with
    `sudo mount -t debugfs debugfs /sys/kernel/debug`.
  The GUI/CLI surface the exact error from the daemon otherwise.
- **EC writes fail** — check `cat /sys/module/ec_sys/parameters/write_support`
  → should print `Y`. If `N`, reboot once (mcontrolcenter's and our modprobe
  conf both request it).
- **Lighting not accessible** — the udev rule needs a re-login or
  `sudo ./install.sh --system` again after per-session ACLs change.

## Status of features

| Feature | State |
|---|---|
| Overview telemetry | ✅ verified on hardware |
| Shift modes (eco/comfort/turbo) | ✅ written, raw codes calibrated (eco 0xC2 · comfort 0xC1 · turbo 0xC4) |
| Cooler Boost / Super Battery | ✅ via msi-ec |
| Fan modes (auto/silent) | ✅ firmware-managed, via msi-ec |
| Custom fan curves | 🟦 implemented — MSI-Center parity via `msi_wmi_platform` (WMI2 tables); semantics verified against kernel patch + decompiled MSI Center; **live hardware probe pending**, run `sudo tools/wmi2_probe.py --write-test` after installing |
| Battery charge-limit sliders | ✅ kernel interface; values verified |
| 24-zone RGB keyboard | ✅ fully verified (incl. software wave) |
| Webcam / Fn-side swap | ✅ via msi-ec |
| Autostart + packaging | ✅ systemd service + .desktop |
