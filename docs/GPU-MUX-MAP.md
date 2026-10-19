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

1. `Get_AP` → package byte 2 is the data byte; **bit 1 = apply-ready**.
2. Refuse to start if apply-ready is already asserted (ambiguous state).
3. Write the UEFI variable, read it back and verify the whole payload.
4. `Get_AP` again; **trigger** = `Set_Data(0xD1, (data_byte1 & 0xFC) | 0x01)`
   — the upper six bits are preserved from the live AP byte, bit 0 is set.
5. Poll `Get_AP` every 250 ms for up to 5 s until apply-ready asserts.
6. **Acknowledge** = `Set_Data(0xBE, 0x02)`.
7. If any step fails after the variable was written, the previous target is
   restored and re-verified.

Live AP read on this unit: data byte `0x0A`, apply-ready clear — i.e. the
trigger is `0x09`, matching the characterised sequence.

The package layout is confirmed by the in-tree kernel driver
(`drivers/platform/x86/msi-wmi-platform.c`: `MSI_PLATFORM_GET_AP = 0x19`,
`SET_DATA = 0x1c`, both 32-byte buffers).

## 3. EC mirror registers (status only)

| Reg | Meaning | This unit (hybrid) |
|---|---|---|
| `0x2E` bit 6 | PXCT — panel path to the dGPU | `0x0B` → bit clear = hybrid |
| `0xD1` bits 0/1 | switch trigger state | `0x00` |

These are **read-only** in this app: the apply path is the WMI handshake,
not an EC byte poke. They are surfaced in `mystic-center gpu` for status.

## 4. Restart semantics

The firmware applies the staged target on **power-on**, not on a warm
reboot — MSI Center's own prompt says "restart", but the characterised
sequence requires a full shutdown (`poweroff`, then power on). Until then
`target != current` and the GUI shows a "reboot pending" row.

## 5. What the app exposes

| Surface | Behaviour |
|---|---|
| `mystic-center gpu` | current/staged mode, supported set, EC mux, GPU list |
| `mystic-center gpu discrete` | stage + handshake, then prints the shutdown requirement |
| `mystic-center gpu --dry-run` | shows the byte-5 transition, writes nothing |
| GUI → **GPU** tab | three-mode selector gated by the capability bits, Apply with a confirmation dialog, current/staged/supported/EC rows, detected GPUs |
| `snapshot()["gpu_mode"]` | cheap summary (UEFI read + EC mirror, no WMI calls) |
| `gpu_mode_state` op | full state including `Get_AP` and the GPU list |

## 6. Evidence and caveats

- MSI Center 2.0.74.0's GPU switch flow (see
  [`MSI-CENTER-RE.md`](MSI-CENTER-RE.md) §5, `UC_NB_Base`).
- In-tree `msi-wmi-platform` driver (method ids and 32-byte package).
- Live reads on this unit: variable `0x30`, AP data byte `0x0A`, EC
  `0x2E = 0x0B`.
- The variable/trigger layout independently matches the
  `ElXreno/msi-gpu-switcher` and `steelbrain/msi-gpu-mux-switch` projects,
  which characterise the same `MsiDCVarData` byte 5 and the same
  `0xD1` / `0xBE` handshake on other MSI boards.
- Not every MSI laptop has a MUX; when the variable is absent the GPU page
  reports "not available" and the CLI exits with a clear error.
- A failed or interrupted firmware operation can leave the machine needing
  recovery — the app verifies every write, restores on failure, and never
  reboots or powers off on its own.
