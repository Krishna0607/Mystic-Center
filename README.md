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

## Requirements

### Hardware

- **MSI Crosshair 16 HX D14V** (`MS-15P2`) with the 24-zone per-key RGB
  keyboard (HID `1462:1603`) and EC firmware `15P2EMS1.110` — everything here
  is developed and hardware-verified against this exact configuration.
- Other MSI laptops with per-key RGB keyboards may work, but EC behaviour is
  model-specific — treat other models as untested.

### Software

A systemd Linux distribution (developed on CachyOS, kernel 7.2.x):

| Need | How to check | What it enables |
|---|---|---|
| Python 3.10+ | `python3 --version` | everything |
| PyGObject + GTK 4 + libadwaita | `python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")'` | the GUI (`install.sh` refuses to run without it) |
| `msi-ec` kernel driver | `ls /sys/devices/platform/msi-ec` | platform modes, battery limits, fan modes, webcam/Fn switches |
| `msi_wmi_platform` + debugfs | `ls /sys/kernel/debug/msi-wmi-platform-*` | custom fan curves (WMI2 tables). If missing, everything except custom fan curves still works, and the app tells you why. |
| `ec_sys` write support | `cat /sys/module/ec_sys/parameters/write_support` → `Y` | advanced fan mode; installed by `install.sh --system` (a reboot may be needed once for the module to come up with `write_support=1`) |
| `wheel` group | `id -nG \| grep -w wheel` | the daemon accepts RPC from root and `wheel` members (default on Arch/Fedora; otherwise `sudo usermod -aG wheel $USER` + re-login) |
| hidraw access to the keyboard | `lsusb -d 1462:1603`, `ls /dev/hidraw*` | keyboard lighting; granted by the udev rule from `install.sh --system` |
| `nvidia-smi` (optional) | `nvidia-smi -L` | GPU temps/usage in the overview |
| `git`, `sudo` | — | clone + install |

GUI packages per distribution (the CLI and daemon need only Python itself):

```bash
# Arch / CachyOS
sudo pacman -S python-gobject gtk4 libadwaita
# Fedora
sudo dnf install python3-gobject gtk4 libadwaita
# Ubuntu / Debian (24.04+ has the libadwaita bindings)
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```

Not affiliated with MSI — everything was reverse-engineered for interoperability.
Read the [safety notes](#safety-notes) before enabling EC writes.

## Installation

```bash
git clone https://github.com/Krishna0607/mystic-center.git
cd mystic-center

./install.sh                # user parts: app, CLI, desktop entry, icon
sudo ./install.sh --system  # root parts: systemd service, udev rule, EC access
```

What the two steps do:

- `./install.sh` — checks the Python/GTK4 dependencies, removes the legacy
  `mystic-control` app if present (your saved keyboard state in
  `~/.config/mystic-control/` is kept), installs the app to
  `~/.local/share/mystic-center`, the `mystic-center` command to
  `~/.local/bin`, and the desktop entry + icon.
- `sudo ./install.sh --system` — writes `mystic-center.service` (the
  privileged backend), the `ec_sys` modprobe option with write support, the
  udev rule that gives the active seat user access to the RGB keyboard's
  hidraw node, then enables and starts the service.

After the first install, log out and back in once (or run
`sudo udevadm control --reload && sudo udevadm trigger -c add -s hidraw`) so
your session picks up the keyboard ACL.

Check everything:

```bash
mystic-center status                # live snapshot through the daemon
systemctl status mystic-center
journalctl -u mystic-center -n 30   # if it is not active
```

Start the GUI with `mystic-center` (or the **Mystic Center** icon).

### Optional: re-apply lighting at login

```bash
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/mystic-center-light.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Mystic Center (lighting)
Comment=Re-apply keyboard lighting profile at login
Exec=sh -c "$HOME/.local/bin/mystic-center lighting apply"
Terminal=false
X-GNOME-Autostart-enabled=true
DESKTOP
```

### Uninstall

```bash
rm -rf ~/.local/share/mystic-center ~/.local/bin/mystic-center \
       ~/.local/share/applications/mystic-center.desktop \
       ~/.config/autostart/mystic-center-light.desktop
sudo systemctl disable --now mystic-center
sudo rm -f /etc/systemd/system/mystic-center.service \
           /etc/udev/rules.d/61-mystic-control.rules \
           /usr/lib/modprobe.d/mystic-center.conf \
           /usr/lib/modules-load.d/mystic-center.conf
sudo systemctl daemon-reload && sudo udevadm control --reload
```

Your saved keyboard state (`~/.config/mystic-control/`) and the EC's own
settings are left untouched.

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
mystic-center lighting steady FF7F20 # the full lighting CLI
mystic-center lighting wave --rainbow --dir right --speed medium
mystic-center lighting custom-set 0-7 steady --colors FF0000
mystic-center lighting save        # persist current lighting on the keyboard
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
| Custom fan curves | ✅ **live-validated** (2026-10-18) — MSI-Center parity via `msi_wmi_platform` (WMI2 tables); AP-bit round-trip exact (`0x0D`↔`0x8D`), every write readback-verified, duty tables drive real RPM (CPU 2962→4137, GPU 0→2981), clean restore to firmware auto; `sudo tools/wmi2_probe.py --write-test` remains for first-contact checks on fresh machines |
| Battery charge-limit sliders | ✅ kernel interface; values verified |
| 24-zone RGB keyboard | ✅ full MSI-Center parity set (wave, dance, wheel, bouncy ball, Customize, all-sync, Save/Revert to MCU) — live-verified on hardware incl. wave parameter sweep and both directions (2026-10-18); packet-level parity tests passing |
| Webcam / Fn-side swap | ✅ via msi-ec |
| Autostart + packaging | ✅ systemd service + .desktop |

## Author

**Krishna Dave** — [@Krishna0607](https://github.com/Krishna0607) · krishna.dave0607@gmail.com
