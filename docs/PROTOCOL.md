# MSI MysticLight 24-zones keyboard protocol (MS-1603)

Device: USB HID `1462:1603` — "Micro Star International MysticLight MS-1603"
(Laptop: MSI Crosshair 16 HX D14VFKG, MS-15P2; probably shared with other
2023+ MSI 24-zone laptops using the same MysticLight controller generation).

Transport
---------

The keyboard exposes a vendor-defined HID interface (`06 FF 00 …`) with
64-byte reports. The report descriptor declares:

| Report ID | Type                | Size  | Role                              |
|-----------|---------------------|-------|-----------------------------------|
| 1         | Feature             | 64    | response/status (read)            |
| 1         | Output              | 64    | (legacy per-zone color writes?)   |
| 2         | Feature             | 64    | COMMAND CHANNEL (used by MSI Center) |
| 3         | Output              | 64    |                                   |
| 3         | Input               | 64    |                                   |
| 4,5,6     | Feature/Out/In      | bits  | small flags (unmapped)            |
| 7         | Input               | 1 byte|                                   |
| 8         | Feature/Out/In      | 64    | (unmapped)                        |

Commands are 64-byte `SET_REPORT(Feature)` transactions: report id byte `0x02`
followed by a 63-byte payload. After every command the host reads the 64-byte
feature report 1 (ack).

Packet type 1 — zone select
---------------------------

Payload: `01 | u32 LE zone mask`

Bit N of the mask selects zone N (0…23). All following effect packets apply
only to the selected zones — this is how per-zone painting works.

Packet type 2 — effect config
-----------------------------

Payload layout (offset 0 = first byte after report id):

```
 [0]     0x02   packet id
 [1]     u8     animation type
                  0 off      1 steady    2 breath
                  3 color cycle          4 color wave
 [2..3]  u16LE  speed = cycle seconds * 100
                  (MSI Center: 600 == 6 s; UI range 3..12 s => 300..1200)
 [4..5]  0x00   (observed zeros)
 [6]     u8     level-ish byte — MSI Center sends 0x32 (50) for steady.
                 Candidates: transition or brightness. Exposed as "level".
 [7]     u8     constant 0x08 observed in MSI Center (was 0x01 on MS-1565)
 [8]     u8     direction: 0 = right→left, 1 = left→right
 [9..]   keyframes, 4 bytes each = (u8 time%, u8 r, u8 g, u8 b)
                 up to 10 keyframes; first must have t=0 and last t=100
```

MSI Center steady packet (captured, reproduced exactly by this app):

```
02 02 01 58 02 00 00 32 08 01 00 <R> <G> <B> 64 <R> <G> <B> 00…
```

i.e. animation `steady`, speed 600, level 50, direction L→R, and two
keyframes: t=0 and t=100 with the same color (per the MS-1565 research, MSI
Center always sends two identical single-color keyframes for steady).

Animation semantics (from the MS-1565 reverse engineering, same family):

* The MCU does NOT wrap keyframe colors — for a seamless loop make the last
  keyframe color equal the first.
* Color Wave (type 4) is a synced wave across the keyboard, direction from
  byte 8.
* Speed values outside 300..1200 work but MSI Center UI shows 3–12 s.

Zone↔physical mapping
---------------------

24 zones, 4 columns × 6 rows assumption. The physical order (row-major vs
column-major, block size) must be verified visually with `mystic-control
sweep` — each zone lights white, 0.9 s apart, printing its index.

Sources
-------

* Protocol of the closest sibling (Katana 15 B12V, `1462:1601`, 4 zones):
  https://gist.github.com/natanalt/06f1d5854230c788b9b9e7e33ab90b9f
* MS-1603 MSI Center packet capture constants (per-zone `1 << N` masks,
  speed 600, level 0x32, constant 0x08, double keyframe steady):
  https://github.com/N3uralCreativity/MSI-KeyLight (MSI Center captures)
* OpenRGB issue threads for the same device family:
  https://gitlab.com/CalcProgrammer1/OpenRGB/-/work_items/4233
* This device report descriptor, read live on Crosshair 16 HX D14VFKG.

Open questions / to verify with the physical device
---------------------------------------------------

* byte [6] ("level") semantics: brightness vs transition—or ignored.
* Whether wavy type-4 animates in sync per-zone on the 24-zone firmware.
* Hardware wave vs software wave fallback.
* Zone order mapping (sweep confirms).
* The 1-byte reports (id 4-7) may hide brightness control or numlock mode.
