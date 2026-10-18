# MSI MysticLight 24-zones keyboard protocol (MS-1603)

Device: USB HID `1462:1603` — "Micro Star International MysticLight MS-1603"
(Laptop: MSI Crosshair 16 HX D14VFKG, MS-15P2; probably shared with other
2023+ MSI 24-zone laptops using the same MysticLight controller generation).

Everything below is verified against **MSI Center 2.0.74's own code** for this
exact device — `UC_Mystic Light.dll` → class `Class_24Zone`. Where this app
deliberately sends slightly different bytes for legacy effects, it is marked
as "legacy-validated".

Transport
---------

The keyboard exposes a vendor-defined HID interface (`06 FF 00 …`) with
64-byte reports. The report descriptor declares:

| Report ID | Type                | Size  | Role                              |
|-----------|---------------------|-------|-----------------------------------|
| 1         | Feature             | 64    | response/status (read) + query payloads |
| 1         | Output              | 64    | legacy per-zone colour writes     |
| 2         | Feature             | 64    | COMMAND CHANNEL (used by MSI Center) |
| 3         | Output              | 64    |                                   |
| 3         | Input               | 64    |                                   |
| 4,5,6     | Feature/Out/In      | bits  | small flags (unmapped)            |
| 7         | Input               | 1 byte|                                   |
| 8         | Feature/Out/In      | 64    | (unmapped)                        |

Commands are 64-byte `SET_REPORT(Feature)` transactions: report id byte `0x02`
followed by a 63-byte payload. After every command the host reads the 64-byte
feature report 1 (ack, best effort).

Command set (mirrors MSI Center's `Class_24Zone` methods)
--------------------------------------------------------

| Payload (after report id 2)  | Meaning                                        |
|------------------------------|------------------------------------------------|
| `01 <u32LE mask>`            | zone select — bit N = zone N (0…23)            |
| `02 <effect packet>`         | effect config (see below)                      |
| `03`                         | all-sync initialise                            |
| `04 <r> <g> <b>`             | all-sync solid colour                          |
| `A0` (=160)                  | Save — persist the current lighting into MCU memory |
| `B0` (=176)                  | Cancel — revert to the MCU's saved lighting    |
| `FF` (=255)                  | reset the MCU (recovery from a wedged state)   |
| report 1: `91 01` (=145,1)   | firmware-version query                         |
| report 1: `A0` (=160)        | language query — reply `InData[2] == 2` → JP, else US |

Packet type 1 — zone select
---------------------------

Payload: `01 | u32 LE zone mask`

Bit N of the mask selects zone N (0…23). All following effect packets apply
only to the selected zones — this is how per-zone painting works. MSI Center
sends the mask immediately before every effect packet.

Packet type 2 — effect config
-----------------------------

Payload layout (offset 0 = first byte after report id):

```
 [0]     0x02   packet id
 [1]     u8     effect:  0 off   1 steady   2 breath   3 color cycle
                         4 wave  5 dance    6 wheel    7 bouncy ball
 [2..3]  u16LE  speed = cycle seconds * 100  (0..65535; MSI uses 100..800)
 [4]     u8     key-centre X   (wave: 0;  wheel/bouncy: 0)
 [5]     u8     key-centre Y   (wave: 50; wheel/bouncy: 0)
 [6]     u8     wavelength * 10  (wave 8, wheel 5 / custom 3,
                                  bouncy 2; steady/breath/
                                  cycle: MSI leaves a stale 50)
 [7]     u8     wave mode: 1 = full-screen/rainbow, 0 = part-screen/custom
                 (steady/breath/cycle: MSI leaves a stale 0x08)
 [8]     u8     direction: 0 / 1  (meaning depends on the effect)
 [9..]   colour balls, 4 bytes each: u8 position (0..100), r, g, b
                 up to 13 balls fit the 63-byte payload
```

### Effect defaults (exactly MSI Center's tables)

| Effect  | Speed slow/medium/fast (s) | Direction byte | Notes |
|---------|----------------------------|----------------|-------|
| Off     | –                          | –              | empty packet, no balls |
| Steady  | 6 (not exposed to the UI)  | –              | |
| Breath  | 8 / 6 / 4                  | –              | |
| Cycle   | 8 / 6 / 4                  | –              | |
| Wave    | 5 / 3.5 / 2                | 0 = left, 1 = right; centre (0,50), wavelength 8 | |
| Dance   | 3 / 2 / 1                  | –              | MSI clears all zones first, then sends Dance with no colour balls |
| Wheel   | 5 / 3.5 / 2                | 0 = clockwise, 1 = counterclockwise; wavelength 5 (rainbow) / 3 (custom) | |
| Bouncy  | fixed 1.0                  | 1              | wavelength 2 (both modes) |

### Colour balls (MSI's `Set_ColorBall_Normal` / `Set_ColorBall_Rainbow`)

* `Steady` / `Breath` / `Cycle` (customized): colours spread at
  `100 // n * i`, wrapping to colour 0 at position 100 — one seamless loop.
  *(legacy-validated: steady/breath send two identical balls at 0 and 100)*
* `Wheel` (customized): black @ 0, up to 3 colours @ 15 / 50 / 85, black @ 100.
* `Bouncy ball` (customized): black @ 0, colour 0 @ 50, black @ 100.
* `Wave` / `Cycle` / `Wheel` (rainbow radio): 7-ball stream
  `(0,R) (17,Y) (33,G) (50,C) (67,B) (83,M) (100,R)`.
* `Bouncy ball` (rainbow radio): black @ 0 + black @ 100 (firmware colours it).
* `Steady` / `Breath` (rainbow radio): MSI lights **9 fixed hue groups**, one
  packet per zone, each with its own two-ball solid list:

  | Group | Zones                     | Colour  |
  |-------|---------------------------|---------|
  | 0     | 0, 11, 12                 | #FF0000 |
  | 1     | 1, 13, 14                 | #FF8000 |
  | 2     | 2, 15                     | #80FF00 |
  | 3     | 3, 16, 17                 | #00FF00 |
  | 4     | 4                         | #00FF80 |
  | 5     | 5, 18, 19                 | #0080FF |
  | 6     | 6                         | #0000FF |
  | 7     | 7, 8, 20, 21              | #8000FF |
  | 8     | 9, 10, 22, 23             | #FF0080 |

### All-sync

`Set_AllSyncBegin` (`02 03`) followed by either `02 04 <r> <g> <b>` (solid)
or the rainbow preset: a Wave packet (`AllSync_Rainbow`: speed 5.0 s,
wavelength 10, full-screen mode, direction right) using a 7-ball stream at
0/16/32/48/64/80/100.

Legacy packets kept by this app
-------------------------------

The hardware-validated packets from earlier revisions of this app are byte
compatible with MSI Center's parameter set, except that steady/breath/cycle
keep MSI's own stale header bytes (`wavelength = 50`, `wave mode = 0x08`) and
send explicit keyframes instead of an "empty" ball list. Example — steady
`#FF8000` on zone 3:

```
 02 02 | 01 | 58 02 | 00 00 | 32 | 08 | 01 | 00 <R G B> 64 <R G B> 00…
                  ↑ stale MSI bytes  ↑ direction
```

Animation semantics (MS-1565 research, same controller family)
--------------------------------------------------------------

* The MCU does NOT wrap keyframe colours — for a seamless loop make the last
  keyframe colour equal the first.
* Speed values outside 300..1200 work but MSI Center's UI shows 3–12 s.
* A distinct per-zone colour with a single-zone select mask is the reliable
  way to paint zones; repeating the same large mask with different effect
  packets can be cached/dropped by the MCU.

Zone↔physical mapping
---------------------

24 zones, 4 columns × 6 rows (row-major: `zone = row * 4 + column`). Verified
visually with `mystic-control sweep` — each zone lights white in order,
printing its index.

Sources
-------

* **MSI Center 2.0.74** → `UC_Mystic Light.dll` → `Class_24Zone` /
  `UC_Panel_24Zone` (decompiled with ilspycmd) — the authoritative source for
  the packet layout, effect table, direction codes and colour-ball builders.
  See `docs/MSI-CENTER-RE.md`.
* Protocol of the closest sibling (Katana 15 B12V, `1462:1601`, 4 zones):
  https://gist.github.com/natanalt/06f1d5854230c788b9b9e7e33ab90b9f
* MS-1603 MSI Center packet capture constants (per-zone `1 << N` masks,
  speed 600, level 0x32, constant 0x08, double keyframe steady):
  https://github.com/N3uralCreativity/MSI-KeyLight (MSI Center captures)
* OpenRGB issue threads for the same device family:
  https://gitlab.com/CalcProgrammer1/OpenRGB/-/work_items/4233
* This device report descriptor, read live on Crosshair 16 HX D14VFKG.

Open questions / to verify with the physical device
--------------------------------------------------

* Wave plays on this firmware revision with MSI's exact parameters — centre
  (0,50), wavelength byte 8, speeds 5/3.5/2 s, direction 0/1 — validated live
  in rainbow mode at all three speeds (2026-10-18).
* If firmware effects stop responding after heavy per-zone write bursts, the
  MCU can sit in a stale state (observed once; wave was ignored until the
  keyboard was re-initialised). `mystic-control reset` (`[2,255]`), the MCU
  `save`+`revert` pair, or a replug clear it.
* Dance behaviour when colour balls are omitted (this app sends none; MSI
  itself sends stale bytes).
* The 1-byte reports (id 4–7) may hide brightness control or numlock mode.
