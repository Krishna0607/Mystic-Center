# GPU MUX map — `MsiDCVarData` + EC apply handshake

Target: MSI Crosshair 16 HX D14VFKG (MS-15P2), EC `15P2EMS1.110`, BIOS
`E15P2IMS.110`. Implements `mystic_center/gpumux.py`.

MSI Center exposes this as **User Scenario → Graphics Mode** and
**General Settings → GPU Switch** with three choices (MSHybrid / Discrete /
Integrated) and a "restart required" prompt. On this unit the firmware
advertises all three; the value is staged in UEFI and applied by the EC on
the next power-on.

## 1. The UEFI variable

`MsiDCVarData`, GUID `DD96BAAF-145E-4F56-B1CF-193256298E99`, 20-byte
payload, attributes `0x00000007`. efivarfs prepends the 4 attribute bytes,
so the file is 24 bytes. It is `chattr +i` by default; the immutable flag
must be cleared around the write and restored afterwards.

Byte 5 carries the mode and the capability bits:

| bits | meaning |
|---|---|
| 0..1 | **selected target** — `0` MSHybrid · `1` Discrete · `2` Integrated |
| 2..3 | **current mode** as reported by firmware (same encoding) |
| 4 | "new switch" flow supported (handshake below is required) |
| 5 | integrated mode supported |
| 6 | discrete mode **not** supported (inverted) |
| 7 | reserved / OEM |

Live read on this unit:

```
$ od -An -tx1 /sys/firmware/efi/efivars/MsiDCVarData-dd96baaf-...
 07 00 00 00 01 01 00 06 00 30 01 00 ...
 └─ attrs ─┘ └──────── payload ────────┘
                                          byte5 = 0x30
```

`0x30` = target `0` (MSHybrid), current `0` (MSHybrid), bit4 set (new
switch), bit5 set (integrated supported), bit6 clear (discrete supported).
Supported set = **MSHybrid · Discrete · Integrated**.

A unit with only hybrid+discrete would read `0x10`; a legacy unit without
the handshake reads bit4 clear and only the hybrid/discrete pair is offered.

## 2. The apply handshake (EC / WMI2)

The mode is not live after the variable write — the EC must be told to
apply it. This rides the same `MSI_ACPI` WMI device as fan curves
(see [`WMI2-MAP.md`](WMI2-MAP.md)), through the `get_ap` / `set_data`
debugfs methods:

1. `Get_AP` → package byte 2 is a **two-bit state machine**:

   | value | meaning |
   |---|---|
   | `0x02` | **apply-ready** — latched after a completed apply |
   | `0x01` | **trigger in flight** — EC is applying, do not touch the target |
   | `0x00` | transient / idle between the two |

   The resting value before a trigger can also be `0x02` (latched from the
   previous apply), so apply-ready alone is not proof that a *new* trigger
   completed — see step 5.

2. **AC preflight.** MSI's firmware refuses to move the MUX without mains
   power, because an interrupted apply can leave the panel unrouted on the
   next boot. `set_mode` refuses on battery with a clear message before it
   writes anything (`/sys/class/power_supply/*/online`).
3. Preflight refuses **only on bit 0** (trigger in flight). Bit 1 is *not*
   an error: it stays latched at `0x02` after the first completed switch,
   which is the normal resting state. (Refusing on bit 1 was the original
   bug — it blocked every switch after the first.)
4. A target that is already staged is a **no-op**: the EC is not triggered
   again, because re-arming an unchanged target only risks a second apply.
6. Write the UEFI variable, read it back and verify the whole payload.
7. `Get_AP` again; **trigger** = `Set_Data(0xD1, (data_byte1 & 0xFC) | 0x01)`
   — the upper six bits are preserved from the live AP byte, bit 0 is set.
8. Poll `Get_AP` every 250 ms for up to 5 s until bit 1 is set **and bit 0
   is clear** (`0x02`). To reject a stale latched `0x02`, either the
   in-flight bit (`0x01`) must have been observed, or at least 0.5 s must
   have elapsed; the ready state is then confirmed with a second read.
9. **Acknowledge** = `Set_Data(0xBE, 0x02)`.
10. Failures are handled by phase:
   - **before** the trigger: the previous target is restored and verified;
   - **after** the trigger: the target is left staged (rewriting the
     variable would disagree with what the EC is already applying) and the
     user is told to power off fully.

Verified trace on this unit, captured with
`mystic-center gpu --self-test` (re-stages the *current* mode, so the boot
config is unchanged):

```
0.000  before        0x02  ready, not busy
0.011  trigger-sent  0x01  in flight
0.118  ready         0x02  apply complete (busy observed → accepted)
0.122  ack           Set_Data(0xBE, 0x02)
```

The package layout is confirmed by the in-tree kernel driver
(`drivers/platform/x86/msi-wmi-platform.c`: `MSI_PLATFORM_GET_AP = 0x19`,
`SET_DATA = 0x1c`, both 32-byte buffers).

## 3. EC mirror registers (status only)

| Reg | Meaning | This unit (hybrid) |
|---|---|---|
| `0x2E` bit 6 | PXCT — panel path to the dGPU | `0x0B` → bit clear = hybrid |
| `0xD1` bits 0/1 | switch trigger state | `0x02` (ready) |
| `0xBE` bits 0/1 | apply acknowledgement | `0x02` (ack latched) |

These are **read-only** in this app: the apply path is the WMI handshake,
not an EC byte poke. They are surfaced in `mystic-center gpu` for status.

## 4. Restart semantics

The firmware applies the staged target on **power-on**, not on a warm
reboot — MSI Center's own prompt says "restart", but the characterised
sequence requires a full shutdown (`poweroff`, then power on). Until then
`target != current` and the GUI shows a "restart required" banner.

The app therefore offers **two** explicit actions once a switch is staged:

| Action | What it does | When to use it |
|---|---|---|
| **Restart now** | `systemctl reboot` | Fast path; usually works, but a warm reboot may skip the MUX POST step on some firmware |
| **Shut down fully** | `systemctl poweroff` | Guaranteed path — the MUX is re-routed during the next POST |

Both are refused by the daemon unless `restart_required` is true, so they
can never act as a generic remote reboot button.

**Post-boot verification journal.** Staging writes
`/var/lib/mystic-center/gpu-pending.json` (target, previous mode, boot id,
timestamp). On the next boot the daemon compares the journal with the
firmware's current mode:

- match → the switch applied: journal cleared, status reports success;
- mismatch on a different boot id → the switch **did not** apply (almost
  always a warm reboot): the failure is persisted to
  `gpu-apply-failed.json`, the GUI/CLI report the requested vs. running
  mode, and `gpu_clear_failed` dismisses it.

**Runtime topology detection.** Because the firmware "current" bits only
refresh at POST, a just-booted machine is confirmed independently by the
display controllers the OS can see:

| Visible controllers | Inferred mode |
|---|---|
| both iGPU and dGPU | MSHybrid |
| dGPU only | Discrete |
| iGPU only | Integrated |

When the live topology already matches the staged target but the firmware
byte has not caught up, `current` is reported from runtime evidence
(`current_source = "runtime"`). This prevents a stuck "reboot pending"
banner when the switch did in fact apply.

**Can the restart be skipped?** No. The MUX is a hardware switch on the
panel/PCIe path, and this firmware latches the routing during POST, before
any OS driver loads. The EC apply handshake only arms the change; it does
not re-route a running system. Live reads confirm it: after a successful
trigger + ack, EC `0x2E` bit 6 (PXCT panel path) is unchanged. MSI Center
has the same limitation — its own dialog asks for a restart. Unbinding the
GPU drivers and re-scanning PCIe would not move the panel path and risks a
black screen, so this app deliberately does not attempt it.

**No-reboot alternative:** while in MSHybrid mode the dGPU is still
available through PRIME render offload:

```sh
__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia <command>
# or, for a single application:
DRI_PRIME=1 <command>
```

## 5. What the app exposes

| Surface | Behaviour |
|---|---|
| `mystic-center gpu` | current (UEFI or runtime), staged target, live topology, supported set, AC state, EC mux, GPU list |
| `mystic-center gpu discrete` | AC gate + stage + handshake, then prints the restart options |
| `mystic-center gpu --dry-run` | shows the byte-5 transition, writes nothing |
| `mystic-center gpu --restart` | restarts, but only when a switch is staged |
| `mystic-center gpu --poweroff` | full shutdown (most reliable apply), only when staged |
| `mystic-center gpu --self-test` | traces the EC handshake against the current target |
| GUI → **Graphics** | three mode cards with RUNNING / STAGED / SELECTED badges, status banner, Apply confirmation, Restart now / Shut down fully, live state and detected GPUs |
| `snapshot()["gpu_mode"]` | cheap summary: UEFI read, runtime classification and journal (no WMI calls) |
| `gpu_mode_state` op | full state including the GPU list |
| `gpu_restart` / `gpu_shutdown` | privileged power actions (staged switches only) |
| `gpu_clear_failed` | dismisses a persisted "did not apply" record |

## 6. Evidence and caveats

- MSI Center 2.0.74.0's GPU switch flow (see
  [`MSI-CENTER-RE.md`](MSI-CENTER-RE.md) §5, `UC_NB_Base`).
- In-tree `msi-wmi-platform` driver (method ids and 32-byte package).
- Live reads on this unit: variable `0x30`, AP data byte `0x02`
  (ready, not busy), EC `0x2E = 0x0B`.
- The variable/trigger layout independently matches the
  `ElXreno/msi-gpu-switcher` and `steelbrain/msi-gpu-mux-switch` projects,
  which characterise the same `MsiDCVarData` byte 5 and the same
  `0xD1` / `0xBE` handshake on other MSI boards.
- Not every MSI laptop has a MUX; when the variable is absent the GPU page
  reports "not available" and the CLI exits with a clear error.
- A failed or interrupted firmware operation can leave the machine needing
  recovery — the app verifies every write, restores on failure, and never
  reboots or powers off on its own.
