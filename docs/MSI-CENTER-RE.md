# MSI Center 2.0.74.0 — reverse-engineering report

This document is the working record of tearing down MSI Center 2.0.74.0 in
order to port its notebook hardware features to Linux (`~/mystic-center`).
Every nontrivial claim cites the decompiled source tree under
`/tmp/opencode/decomp/` (reproducible — see §10) or the unpacked package under
`/tmp/opencode/msicenter/`. Companion docs: [`WMI2-MAP.md`](WMI2-MAP.md)
(the firmware interface fan curves ride on), [`EC-MAP.md`](EC-MAP.md) (EC
registers and why the EC byte path is a dead end for curves).

## 1. Distribution and install chain

- Downloaded artifact: `MSI Center_2.0.74.0.exe` — **Inno Setup 5.4.2**
  installer (~558 MB).
- It materializes an **`.appxbundle`** for MS Store-style deployment, identity
  `9426MICRO-STARINTERNATION.MSICenter`, x64 only
  (`app/x64/unpacked/AppxManifest.xml`).
- The unpacked appx root contains the full machine-side payload:

```
app/x64/unpacked/
├── AppxManifest.xml          # full-trust entry DCv2\DCv2.exe + startupTask
├── DCv2/                     # the WPF client shell + plugins (~730 MB)
│   ├── DCv2.exe              # main executable
│   ├── UC_*.dll              # per-feature plugin modules (see §5)
│   ├── CS_CommonAPI.dll      # client transport (TCP to central server)
│   ├── NamedPipeClientLib/   # client transport (named pipe to service)
│   ├── Package/MSI Center SDK.exe   # installs the services
│   ├── Mystic Light/… hid/…  # device-specific payloads (incl. our MS-1603)
│   └── Device/, sd/, …       # per-device descriptors & support data
├── Images/, resources.pri    # store tile resources
└── WinMetadata/              # appx metadata
```

The services MSI Center actually talks to are installed from
`DCv2/Package/MSI Center SDK.exe` → unpacks to `app/x64/unpacked/DCv2/Package/app`
(`MSI.CentralServer.exe`, `MSI_Central_Service.exe`, `API_Kernel.dll`,
`MsiHid.dll`, …). `Windows.FullTrustApplication` + `windows.startupTask`
(`MSI_Center_UWP`) means the shell runs elevated-ish at every boot, and a
custom `msi-mc:` URI protocol handler is registered.

## 2. Runtime architecture (what talks to what)

```
┌ WPF shell DCv2.exe (per-user) ────────────────────────────┐
│  plugin UC_*.dll UIs (User Scenario, Fans, Mystic Light…) │
│  … calls C_Client.SendData(DestID, [cmdId, payload+JSON]) │
└──────────┬────────────────────────────────────────────────┘
           │ TCP 127.0.0.1:32682   (registry override
           │  HK…\Component\SDK "Server Port")
           ▼
MSI.CentralServer.exe (msg dispatch hub between UI plugins and services)
           │ named pipe \\.\pipe\MSI_SERVICE_2   (NamedPipeClientLib)
           ▼
MSI_Central_Service.exe      (SYSTEM service, the privileged side;
                              peer of NativeNamedPipeClient)
           │                                │
           │ WMI2:<CMD>_REQ:<ascii>         │ WMI:…/REG:/memory ops
           ▼                                ▼
Windows WMI (ABBC0F6E-8EA1-11D1-…)     Registry / physical memory rw
  └── MSI_ACPI control method → EC firmware
```

### 2.1 Client→CentralServer (DCv2 UI side)

`CS_CommonAPI/CS_CommonAPI/C_Client.cs`:

- `DetectTransferSpeed()` probes `127.0.0.1:32682` (registry override
  `Component\SDK / Server Port`) with a fixed 32-byte pattern to negotiate the
  buffer size.
- `C_Client.SendData(DestID, bytes)` prefixes a **2-byte command id**:
  e.g. the Advanced-Fan window
  `UC_Base_Module/UC_AdvanceFan_Window.cs:19` uses
  `CMD_SetFanAdvanced = {0, 21}` and appends
  `Encoding.ASCII → JSONSerializer(new Scenario_FanAdvanced{ Advanced =
  "d1;…;d6;D1;…;D12 duties })` — i.e. **component = [0, 21], payload =
  JSON text**. Every plugin follows this shape; component-specific ids are
  what MSI.CentralServer dispatches.

### 2.2 CentralServer→service (pipe side)

`NamedPipeClientLib/NamedPipeClientLib/NativeNamedPipeClient.cs` — every
frame:

```
00 FF <BE32 length>          # 6-byte header, length = header + ascii payload
<ASCII command text>         # e.g. "WMI2:GFAN_REQ:<subfeature index>"
```

(`GetHeaderByteArray`, line 1162 — plain big-endian length prefix, *not*
encrypted; the earlier "obfuscated header" suspicion is resolved by the
decompile.) Responses come back effectively as UTF-16 text that contains
e.g. `WMI2:GFAN_RST:<0|1>;b0,b1,…,b31` — first field is a success flag,
then comma-separated decimal bytes of the 32-byte package payload.

### 2.3 The `WMI2:` command set (service ⇄ WMI)

`NativeNamedPipeClient.WMIACPI2Class` (line 199) matches 1:1 the in-kernel
`msi-wmi-platform` driver's method list:

| Class enum | Value | GET cmd | SET cmd | Linux debugfs file |
|---|---|---|---|---|
| BIOS | 1 | `WMI2:GBIOS_REQ:` | `WMI2:SBIOS_REQ:` | get/set_bios |
| SMBus | 2 | `WMI2:GSMBUS_REQ:` | `WMI2:SSMBUS_REQ:` | get/set_smbus |
| MasterBattery | 3 | `WMI2:GMBAT_REQ:` | `WMI2:SMBAT_REQ:` | get/set_master_battery |
| SlaveBattery | 4 | `WMI2:GSBAT_REQ:` | `WMI2:SSBAT_REQ:` | get/set_slave_battery |
| **Temperature** | 5 | `WMI2:GTEMP_REQ:` | `WMI2:STEMP_REQ:` | get/set_temperature |
| Thermal | 6 | `WMI2:GTHER_REQ:` | `WMI2:STHER_REQ:` | get/set_thermal |
| **Fan** | 7 | `WMI2:GFAN_REQ:` | `WMI2:SFAN_REQ:` | get/set_fan |
| Device | 8 | `WMI2:GDEV_REQ:` | `WMI2:SDEV_REQ:` | get/set_device |
| Power | 9 | `WMI2:GPWR_REQ:` | `WMI2:SPWR_REQ:` | get/set_power |
| Debug | 10 | `WMI2:GDBG_REQ:` | `WMI2:SDBG_REQ:` | get/set_debug |
| **AP** | 11 | `WMI2:GAP_REQ:` | `WMI2:SAP_REQ:` | get/set_ap |
| Data | 12 | `WMI2:GDATA_REQ:` | `WMI2:SDATA_REQ:` | get/set_data |

- GET frame payload = the **subfeature index in ASCII decimal**
  (`Get_Temperature`: `text = iDataBlockIndex.ToString()`).
- SET frame payload = `"<subfeature>;<b0>,…,<b31>"` (32 decimal bytes) —
  `Set_WMIACPI2` (~line 1095).
- The service then evaluates the WMI ACPI method with these 32-byte buffers.
  Firmware/interface semantics (subfeatures, offsets, BIT(7)) are fully
  specified in [`WMI2-MAP.md`](WMI2-MAP.md); this is the *same* interface the
  kernel driver exposes in debugfs, so `mysticd` reproduces MSI Center's
  service behavior by writing those files directly — no pipe, no service.

There is also a legacy `WMI:WREQ:/RREQ:` family (`GetWMI`, read/modify of
"SMBus-ish" key/value pairs — used by older laptops) and `REG:` registry
read/write commands the service exposes (e.g. `DeleteRegistry`, line ~2166).
MSI Center even does **physical-memory read/write** through the service
(`ReadMemory`, used by `nb_base/MemoryIO/MemRwService.cs` — EC RAM reads
formed as address `0xFC0008xx` where `xx` is the EC register; that's how it
dumps EC bytes without a driver).

## 3. Fan curves — exactly what MSI Center sends

`UC_Base_Module/UC_AdvanceFan_Window.cs` (the MSI Center custom-fan editor):

- Editor state lives in per-user registry under
  `Component\Base Module\Scenario\`: `Default_Temp`
  (12 °C values: CPU[0..5] + GPU[6..11]) and `User_Fan` /
  `Default_Fan` (12 duty values, same order). Six rows per fan, one duty
  slider per row → temps are the row X positions from `Default_Temp`.
- Save button: builds `Scenario_FanAdvanced.Advanced = "d1;…;d6;D1;…;D6"`
  (**12 duty values — CPU first then GPU, temps are not part of this
  message**) and sends `SendData(ComponentID, [0,21] + ASCII-JSON)`.

Downstream (`MSI_Central_Service.exe` peer, not decompiled in full — §9)
this lands as `WMI2:SAP_REQ:`/`SFAN/STEMP_REQ` traffic: **AP BIT(7) on +
six-point duty tables + firmware temp table**, i.e. precisely the byte
sequence `mystic_center/wmi2.py` writes through debugfs. The registers
MControlCenter writes directly (`0x72/0x8A …`, see
[`EC-MAP.md`](EC-MAP.md)) are *not* part of the modern flow.

Parity notes for the port:

| MSI Center | Mystic Center (Linux) |
|---|---|
| 6 duty sliders + 6 temp positions per fan, registry-stored | 6 (temp°, duty%) spin pairs per fan in the GUI + JSON profiles on disk |
| `{"Advanced": "d…;d…;D…;D…"}` (duties only) | `fan_curve_set` (temps *and* duties) |
| `Set_AP` BIT(7) manual mode | `fan_curve_manual(±)` / Fans-page switch |
| silent/auto scenario modes | `msi-ec fan_mode` presets (`0xD4`) |

## 4. Other hardware features MSI Center uses

- **EC ram**: direct EC byte ops only for telemetry/scenarios through the
  memory-op channel (`MemRwService`, base `0xFC0008`); EC-register *map* in
  `docs/EC-MAP.md`. The privileged UIs mostly live in the old
  `SystemProcess`-style components now routed via WMI2.
- **Mystic Light / RGB keyboard**: `Mystic Light/` payload + `HIDCtrl`/` HidSharp`
  talks USB HID report ids to the keyboard MCU — the protocol is documented
  and replicated in `docs/PROTOCOL.md` (MS-1603, 24-zone).
- **Battery**: charge limit is WMI/kernel territory; on Linux the kernel's
  `charge_control_*_threshold` covers the same registers (`0xD7`).
- **Shift/performance modes & fan modes & cooler boost & super battery &
  webcam & fn-swap**: WMI2/EC-backed via msi-ec sysfs on Linux — feature parity
  verified on hardware.

## 5. Plugin inventory (`UC_*.dll`)

Notebook-relevant modules and their Linux status:

| Module | Purpose | Linux port status |
|---|---|---|
| UC_NB_Base Module | user scenario (perf/fan/cooler/super-battery), fan curves | ✅ (mysticd + msi-ec + WMI2) |
| UC_NB_Monitor | temps/fans/battery/meters | ✅ (kernel interfaces) |
| UC_Mystic Light | RGB keyboard | ✅ (docs/PROTOCOL.md, HIDraw) |
| UC_NB_Gaming Mode | general toggle page | ➖ cosmetic-only pages not worth porting |
| UC_NB_AI Engine / UC_AI Engine | adaptive tuning | ➖ not ported (no Windows AI stack) |
| UC_NB_System Diagnosis, UC_HW Diagnosis | diagnostics | ➖ not ported |
| UC_NB_Live Update, UC_NB_System Restore, UC_Burn Recovery | updater/security/BIOS flash | ➖ out of scope |
| UC_Cooling Wizard, UC_Cube Fan (desktop) | guided tuning | ➖ superseded by custom curves |
| UC_Graphics Fan Tool | dGPU fan overrides | ➖ nvidia-smi covers telemetry; curves shared with CPU via msi-ec |
| UC_Power Meter, UC_Super Battery | power steering | ✅ super saver maps to eco+silent cap |
| UC_General Settings | settings | partial |
| others (Coreliquid, Duet, EZ Display…) | other hardware lines | ➖ n/a |

## 6. What the port deliberately skips

- Physical memory access & registry-based scenario persistence (root-only and
  Windows-specific concepts).
- The CentralServer hub (we have a single Unix-socket daemon, simpler and
  auditable).
- Live Update / support / telemetry / AI — all cloud/Windows-bound.

## 7. Toolchain used for the teardown

- `innoextract`/7z for the Inno installer package (installer version check
  via `strings`/Inno signatures → 5.4.2).
- Appx deployment: `7z x appxbundle` → `.appx` → unpacked package tree above.
- .NET decompile: `ilspycmd` 8.2 (in `~/.dotnet/tools`, run with
  `DOTNET_ROOT=/tmp/opencode/dotnet DOTNET_ROLL_FORWARD=Major`) for CS_CommonAPI,
  NamedPipeClientLib, nb_* plugin trees, central/terminal servers.
- Kernel side: in-tree `msi-wmi-platform.c` + OGC fan-control draft
  (`/tmp/opencode/ogc-linux`, commit `c549695`) to confirm the byte layout of
  fan/temp/AP packages (details in `docs/WMI2-MAP.md`).

## 8. Open questions / limitations of the RE

- `MSI_Central_Service.exe` (the pipe peer) was not decompiled exhaustively;
  the client-side protocol + kernel-patch semantics are enough to drive the
  hardware, but any exotic subfeature (e.g. `THER`, `SMBUS`) may have
  service-side quirks we can't see.
- The two reserved bytes inside fan table packages (`buf[1]`, `buf[8..9]`)
  and temp packages (`buf[2..3]`) are preserved rather than interpreted.
- Fan curves on this firmware were not yet probed on the live Linux machine
  (needs root) — the probe is `sudo tools/wmi2_probe.py` (+`--write-test`);
  until then, treat the duty/temp table mapping as *high confidence from two
  independent sources (patch + service protocol), still awaiting a hardware
  round-trip*.
- `DetectTransferSpeed` (TCP) and a couple of CentralServer plugin-id tables
  were mapped only far enough to explain dispatch; a full command-id catalogue
  is out of scope for the port.
