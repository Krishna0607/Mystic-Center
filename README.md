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

Register map documentation: [`docs/EC-MAP.md`](docs/EC-MAP.md). The lighting HID
protocol documentation (reverse-engineered) is in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

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
mystic-center fans balanced          # auto | silent | balanced | cooler
mystic-center battery --end 80 --start 60
mystic-center webcam off             # WiFi-cam privacy toggle
mystic-center fn-swap right          # Fn on the right side
mystic-center calibrate              # raw EC mode-code exploration (safe)
mystic-center lighting steady FF7F20 # the full legacy lighting CLI
mystic-center                        # GUI
```

## Safety notes

- EC writes are limited to `0x71 0x89 0xD2 0xD4 0xD7 0x98 0xEB` — every one is
  from the msi-ec driver's own G2_10 register map.
- Fan modes (auto/silent) run in firmware; Cooler Boost gives max cooling.
- A one-time probe on this firmware confirmed the EC **ignores** user
  fan-duty/curve writes (the register blocks MControlCenter uses on older
  generations bounce back) — so Mystic Center never pretends to write them and
  the allow-list stays minimal. Custom fan curves are a *Windows-only* feature
  on this hardware generation (MSI Center drives them over its WMI platform
  interface). Details in [`docs/EC-MAP.md`](docs/EC-MAP.md).
- Charge limits are clamped (start ≤ end − 5, end ≤ 100).
- If anything odd happens, a full laptop power-off (cold boot) restores the EC
  to factory defaults.

## Troubleshooting

- **Daemon down** — `systemctl start mystic-center`, or if it fails:
  `journalctl -u mystic-center -n 50`
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
| Custom fan curves | ❌ not possible on this EC firmware — see EC-MAP.md |
| Battery charge-limit sliders | ✅ kernel interface; values verified |
| 24-zone RGB keyboard | ✅ fully verified (incl. software wave) |
| Webcam / Fn-side swap | ✅ via msi-ec |
| Autostart + packaging | ✅ systemd service + .desktop |
