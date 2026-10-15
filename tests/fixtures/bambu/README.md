# Bambu MQTT golden fixtures (sanitized)

Real `device/<SN>/report` traffic captured on 2026-10-10 from the "Кузница" printer farm,
used by the golden tests of the Bambu telemetry path (`tests/test_bambu_golden.py`): every
message is fed through the vendored pybambu (`app/vendor/pybambu`) and `normalize_bambu`.
All printers were **printing** (`gcode_state = RUNNING`) during capture.

## Format

JSON Lines, one MQTT message per line:

```json
{"t": 0.592, "msg": {"print": {...}}}
```

* `t`: seconds since the capture client connected.
* `msg`: the payload exactly as received (parsed JSON). Key order, numbers, packed ints
  and bit-field strings are unchanged. Only the identity fields listed below were replaced.
* The topic was not stored. It is `device/<fake SN>/report`, where the fake SN is the one listed per file below.

How the data was captured: one read-only MQTT client (TLS 8883, user `bblp`) subscribed to the
report topic and published a single `{"pushing":{"sequence_id":"1","command":"pushall"}}`. It then
recorded for 70 s. It sent no other commands.

No `get_version` was requested, so the captures carry no firmware version: replayed alone they give
`firmware_version "unknown"`, and the paths the library gates on the version (`filament_swap_ok`,
`door_open`) stay `None`. `test_replay_with_a_known_firmware` feeds a synthetic `get_version` reply
first. **Next capture:** also publish `{"info":{"sequence_id":"0","command":"get_version"}}` and keep
the reply (replace `module[].sn` like the serials below).

## Files

| File | Printer | Fake SN | Msgs | Cadence | First msg | Notes |
|---|---|---|---|---|---|---|
| `X1C.jsonl` | X1C | `00M00A000000000` | 64 | ~1.1 s, every msg is a **full** status (94 keys) | full | no `msg` key at all; no AMS (`ams.ams == []`), external spool in both `vt_tray` and `vir_slot[]`; `device.ctc` present, **no flat `chamber_temper`**; HMS 0500_0100_0003_0004 (SD card full) |
| `P2S.jsonl` | P2S | `22E00A000000000` | 60 | ~1.15 s, all full (98 keys), `msg: 0` | full | 1 AMS (2 trays loaded) with `humidity_raw`, `dry_time`, `dry_setting`; `device.airduct`, `device.ctc`, `device.extruder`, `device.toolhead`, `err2`, `fun2`; `vir_slot` only (no `vt_tray`); `rtsp_url` = `rtsps://0.0.0.0:322/...` (printer-sent, kept); HMS list **flaps** between 1 and 2 entries (0500_0600_0002_0070 always present; 0500_0100_0003_0007 appears and disappears at t=4.7/26.8/37.1/69.8). HMS entries carry `ts_boot`/`ts_unix` |
| `H2S.jsonl` | H2S | `09300A000000000` | 50 | ~1.4 s, all full (99 keys), `msg: 0` | full | 1 AMS (`version 899`) with `humidity_raw`/`dry_time`; `2D` block, `device.fire_ext`, `device.airduct` (4 parts), 4 `care` entries, `chamber_light2` in `lights_report`; at capture the print was in a prepare/heating stage (`layer_num 0`, `mc_action 526`, `stg_cur 14`, nozzle 164 to 140); 2 HMS (0300_2E00_0003_0001, 0700_2100_0002_0025) |
| `P1S.jsonl` | P1S | `01P00A000000000` | 24 | ~2–4 s | **full pushall** (63 keys, `msg: 0`), then 23 small deltas (`msg: 1`, 4–8 keys) | classic P1 protocol: flat `chamber_temper` (= 5, meaningless on P1), `k`, `flag3`, `lifecycle`, `mess_production_state`, `vt_tray` with `k`/`n`; **no** `device`, `stat`, `fun`, `hms` (empty list, absent in deltas); no AMS; `nozzle_type = "stainless_steel"`; job-name-style `gcode_file` (`*.gcode.3mf`) |

`sequence_id`: X1C, P2S and H2S send `"2021"` on every push. P1S counts up (3520..3543). None
of them echoed the request's `"1"`. On the new-protocol printers (X1C fw with `device.*`, P2S, H2S),
every report is a full snapshot, so "pushall arrived" means the first message is already full.

### Notable fields matrix

| field | X1C | P2S | H2S | P1S |
|---|---|---|---|---|
| `device.ctc` (chamber, packed `target<<16 \| current`) | yes (35) | yes (41) | yes (35) | – |
| `device.extruder.info[]` (packed temps) | yes | yes | yes | – |
| `device.bed.info.temp` packed (4587590 = 70<<16\|70) | yes | yes | yes | – |
| `device.airduct` | – | yes | yes | – |
| `ams.ams[].humidity_raw` / `dry_time` | – (no AMS) | yes / yes | yes / yes | – |
| `hms` non-empty | 1 | 1–2 | 2 | empty |
| `print_error` | 0 | 0 | 0 | 0 |
| `stat` / `fun` / `fun2` | yes / yes / – | yes / yes / yes | yes / yes / yes | – |
| flat `chamber_temper` | **–** | – | – | yes |
| `vt_tray` / `vir_slot` | both | vir_slot | vir_slot | vt_tray |
| `2D`, `device.fire_ext` | – | – | yes | – |

## What was replaced (everything else is byte-for-byte the printer's data)

| Field | Replacement |
|---|---|
| device serial (`upgrade_state.sn`, any string equal to a real serial, any 15-char `[0-9A-Z]` string containing a non-hex letter) | per-model fake: real 3-char model prefix + `00A` + zeros (see table) |
| `net.info[].ip` (little-endian packed IPv4) | packed `192.0.2.10` (= 167903424). `0` entries kept. `mask` kept (255.255.255.0 = 16777215, not identifying) |
| IPv4 inside strings (`ipcam.rtsp_url`, any URL) | `192.0.2.10`. `0.0.0.0` / `127.0.0.1` kept. Dotted version strings with leading zeros (`upgrade_state.lower_limit = "00.00.00.00"`) are **not** IPs and are kept |
| `subtask_name` | `fixture_job` (empty kept empty) |
| `gcode_file`, `file` | kept if generic `/data/Metadata/plate_N.gcode`; otherwise `fixture_job` + original extension (P1S: `fixture_job.gcode.3mf`) |
| `task_id`, `subtask_id`, `project_id`, `profile_id`, `job_id`, `design_id`, `model_id`, `lan_task_id` | non-empty becomes `"0"`; `""` kept as `""` (the code distinguishes empty vs set) |
| `tag_uid`, `tray_uuid` | zeros of the same length (were already all-zero) |
| `ssid`/`bssid`/`mac`/user-id keys, non-empty `oss_url` | placeholders (none present in these captures) |

Kept on purpose (numeric, not identifying): `batch_id`, `canvas_id`, `home_flag`,
`upgrade_state.idx2`, `xcam.cfg`, `hms[].ts_unix`, `upload.sequence_id`.

## Provenance

The files were sanitized and privacy-audited before they were committed: no real serial, access
code, host or job name of any printer appears in them, no IPv4 outside `192.0.2.0/24` (TEST-NET-1)
appears, and the files are 100 % ASCII. Each file keeps the line count, timing, key order, container
shapes and value types of its capture; values differ only at the identity paths listed above.
Parsing the captures and the sanitized files with pybambu 1.0.1 gave the same state apart from
`subtask_name`, `gcode_file` and `ipcam.rtsp_url`.

The capture scripts and the unsanitized captures are not part of this repository.

## `contract_golden_pybambu_1.0.1.json`

Synthetic data only (no capture involved): the `PrinterStatus` fields that the app produced on
PyPI pybambu 1.0.1 (plus the app's former monkeypatches) for hand-written payload scenarios (the
`*_hms_*` ones were added later, produced by the same probe on 1.0.1).
`tests/test_bambu_contract_parity.py` replays the same scenarios through the vendored library and
allows only the documented differences.
