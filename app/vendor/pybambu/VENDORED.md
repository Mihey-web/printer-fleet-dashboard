# Vendored: pybambu (from ha-bambulab)

This folder is a vendored copy of the `pybambu` library that lives inside the
Home Assistant integration **ha-bambulab**. It replaces the PyPI package
`pybambu==1.0.1`, which has not been updated since 2024.

| | |
|---|---|
| Upstream repository | https://github.com/greghesp/ha-bambulab |
| Path in upstream | `custom_components/bambu_lab/pybambu` |
| Commit | `0e027ff135a6d9265cb756d3e246747954c76722` (2026-09-21) |
| License | MIT, "Copyright (c) 2023 ha-bambulab contributors". `LICENSE` in this folder is the upstream repository's root `LICENSE`, copied verbatim. |
| PyPI release | none (`__init__.py`: "TODO: Once complete, move pybambu to PyPi") |

The code is imported as `app.vendor.pybambu`. Upstream uses only relative
imports inside the package, and every data file is found relative to
`__file__`, so the package works from this location unchanged. Its logger is
`logging.getLogger(__package__)`, i.e. `app.vendor.pybambu`.

## Files

Copied from upstream:

| File | Why |
|---|---|
| `__init__.py`, `bambu_client.py`, `bambu_cloud.py`, `commands.py`, `const.py`, `media_sources.py`, `models.py`, `utils.py` | runtime code (the cloud and media modules are imported at module level) |
| `mock_mqtt_client.py` | imported at module level by `bambu_client.py`; only used for `MOCK-` demo serials |
| `filaments.json` | filament names, read by `const.py` at import |
| `certs/bambu.cert`, `certs/bambu_h2c_251122.cert`, `certs/bambu_p2s_250626.cert`, `certs/bambu_x2c_260425.cert` | Bambu Lab CA certificates (8 PEM blocks) for the printers' MQTT TLS |
| `hms_error_text/hms_ru.json.gz`, `hms_error_text/hms_en.json.gz`, `hms_error_text/wiki_links.json.gz` | HMS / print-error texts (Russian, English fallback) and wiki links |

Left out on purpose:

| Upstream file | Why it is not needed |
|---|---|
| `mock_data/` | canned payloads for upstream's `MOCK-` demo serials |
| `extended_filaments.json` | not read by any pybambu code (only by an upstream build script) |
| `hms_error_text/hms_<lang>.json.gz` for the other 22 languages | the app always asks for Russian; the lookup chain is `ru` → `en` → `"unknown"`, and a missing language file just yields `{}` |
| upstream `tests/` | they import a top-level `pybambu` package |

Integrity of the binary files (SHA-256, identical to upstream):

```
4bd46b3d181f5c7e0ffbec47af0a79029ff4bf66388153555564e11ca6ab7032  hms_error_text/hms_ru.json.gz
2688bd580dc6ea57c8546be0ec1aea72a1049630384177c1f26e2509cd4714e7  hms_error_text/hms_en.json.gz
c44438b3bfb1b7c9d86b62ef834990024ae7d6b2a01cfce8a8989a5bde819b57  hms_error_text/wiki_links.json.gz
```

Text files are compared with `git diff --no-index --ignore-cr-at-eol <upstream>/<file> <this folder>/<file>`
(the working tree may use CRLF on Windows).

## Local patches

Every edited line carries a `# VENDOR PATCH: <reason>` comment, and
`tests/test_vendor_guard.py` checks that this list matches the markers in the code.
All other files are byte-for-byte upstream, except that git's `* text=auto` stored
`certs/bambu_h2c_251122.cert`, the package's only CRLF file upstream, with LF line endings.

- `utils.py` (2 markers): `from bs4 import BeautifulSoup` moved from module level into
  `get_upgrade_url()`, its only user. That function scrapes Bambu's firmware page and is reached
  only through `Upgrade.install()`, which the app never calls, so bs4 is not a dependency.
- `models.py` (3 markers):
  - `from PIL import Image` moved from module level to the one place that decodes the 3MF pick
    image. That path is the FTP model download, which the app keeps disabled, and it sits in a
    `try/except` that degrades to a debug log. The `image: Image` annotation is never evaluated
    (`from __future__ import annotations`).
  - The example RTSP URL in a comment (`Camera.print_update`) uses the documentation address
    `192.0.2.64` instead of upstream's private LAN address: this repository is mirrored publicly,
    and its privacy checks reject LAN addresses.

Without bs4 and Pillow at module level the package imports about 0.9 s faster on the Raspberry Pi
and holds ~12 MB less RSS: bs4 ~0.76 s and Pillow ~0.10 s, measured with `requests`, `paho` and
`dateutil` already loaded, as the package loads them anyway (bs4 alone takes ~1.3 s, part of it
charset detection it shares with `requests`).

## Public mirror and privacy checks

`tests/test_vendor_guard.py` fails on any private (RFC 1918), shared/CGNAT (RFC 6598) or
link-local IPv4 address in this folder or in `tests/fixtures/bambu/`.

The public-sync grep (`192.168.|access_code|token|passw|\.gcode`) is expected to hit, and these
hits are not secrets:

- `app/vendor/pybambu/*.py`: the library's own handling of access codes, cloud tokens and
  passwords (names and code, no values), plus `.gcode` / `.3mf` file-name logic;
- `tests/fixtures/bambu/*.jsonl` and `README.md`: the generic `/data/Metadata/plate_1.gcode` and the
  sanitized `fixture_job.gcode.3mf`.

Do not "sanitize" vendored files beyond the patches listed above.

## What the app does on top (outside this folder)

`app/collectors/bambu_collector.py` wraps the client in `_ForgeBambuClient`, a
subclass (nothing is monkeypatched at class level):

- client config: LAN MQTT only (no cloud credentials), camera off, `force_ip` (keep the
  configured host), Russian HMS texts, no timelapse cache, TLS like pybambu 1.0.1 (encrypted,
  certificate not verified), and a per-collector cache path that is never created;
- FTP / TCP-6000 media off (`_enable_ftp = False`: upstream has no config key for it),
  `MOCK-` serials carry no special meaning, the media-cache pruning in `connect()` is a no-op;
- keepalive back to 5 s in `on_connect` (paho's private `_keepalive`; a test guards the paho
  internals this relies on);
- paho's reconnect back-off is capped at 5 s (upstream: 30 s, so a printer back from an outage
  could wait up to 30 s more); only a refused login (CONNACK 4 / 5: bad access code, LAN /
  Developer mode off) keeps 1 → 30 s. CONNACK 1-3 (3 = server unavailable: a printer still
  booting) and a failed reconnect (paho's `on_connect_fail`: TCP refused or timed out) count as
  an outage; two of them in a row end the refused-login state (one is a flaky link and keeps
  the 30 s cap: `reconnect_delay_set()` restarts paho's wait at 1 s, so the cap is set only
  when it changes);
- a refused CONNACK does not run upstream's `on_connect` (which would mark the client connected
  and subscribe / request a push on a connection that is being closed);
- a refused CONNACK (paho reports every one as rc=5) keeps paho's own reconnect loop running
  instead of ending the listener for good;
- every raw report is teed to `printer_commands.note_local_reply` (command replies);
- every `print` report also goes through `app/collectors/bambu_raw.py` (`RawReport`, as
  `device.forge_raw`) before the lib parses it: the lib drops `fun2`, the AMS units' `info` bit
  field and `dry_sf_reason`, `ams_status`, `ams.tray_tar` and most of `device.extruder` (and keeps
  `fun` only as a private string). The hook is an instance attribute that wraps
  `device.print_update`; a failure in it never stops the lib's own parse;
- `publish()` is upstream's body with the paho client read once (`close()` and a rebuild swap
  it on another thread), and returns False when there is no paho client;
- `BambuClient.connect()` (a coroutine) runs in a short-lived `bambu-connect-<label>` thread; a
  connect that raised is retried with a 30 / 60 / 120 s backoff;
- the `app.vendor.pybambu` and `paho.mqtt.client` loggers are set to ERROR and rate-limited per
  call site and thread (`quiet_vendor_logging()`, called by `main.py`);
- TLS: by default the connection is encrypted but the certificate is not verified, as with
  pybambu 1.0.1. Models listed in `FORGE_BAMBU_TLS_VERIFY` (see `.env.example`) verify it
  against the CAs in `certs/` only, through the subclass's `local_tls_context`: upstream's own
  context also trusts the system store, so it would accept any publicly trusted certificate.
  Limits: the certificate name is not checked (printers are addressed by IP and their
  certificate names the serial), so any Bambu-signed device certificate is accepted for any
  printer; the bundled device CAs cover P2S (N7-V2), H2C (O1C2-V2) and X2C (N6-V2), and other
  models are unproven until tried; the "BBL CA" root expires on 2032-04-01; verification needs
  a correct system clock. A printer that fails verification is retried every second, without
  end (upstream's listener treats the failed handshake as a generic error with a 1 s pause, so
  paho's back-off never applies): take its model off the list promptly.

`app/services/normalizer.py` maps the library's shapes (AMS units in a dict keyed by unit id,
the per-extruder `active_*` properties, HMS `<i>-Code` / `<i>-Error`, ...) to `PrinterStatus`.
For HMS it also reads the private raw list `HMSList._hms_list` (with `utils.get_HMS_severity`):
upstream drops codes whose table text is empty, and the fatal / serious ones are put back.

Things worth knowing about the library itself:

- `BambuClient` is a `@dataclass` with its own `__init__`: instances are unhashable, and two
  clients compare equal when their four dataclass fields match. Never put clients in sets or
  dict keys, or compare them with `==` / `in`.
- The no-data watchdog (60 s; it marks the printer offline and publishes `START_PUSH`) starts
  with the first payload of each connection, not on CONNACK, so a connected printer that never
  sends anything is not nudged.
- One exception inside `Device.print_update` drops the rest of that message's sub-updaters (it
  is logged at ERROR); the next message parses normally.

## Runtime dependencies

- `paho-mqtt` 2.x (the library uses paho's VERSION1 callback API);
- `requests` and `python-dateutil` (imported at module level; only the cloud code uses them).

## Re-sync procedure

1. Check out upstream at the new commit.
2. Copy the same subset of files (tables above) over this folder.
3. Re-apply the local patches (`grep -rn "VENDOR PATCH"` in the old copy lists them).
4. Update the commit, the date and the SHA-256 lines in this file.
5. Re-run the privacy checks on the new subset (`pytest tests/test_vendor_guard.py`) and look
   at the public-sync grep hits in the diff.
6. Read the upstream diff of the code the app builds on: `bambu_client.py` (`BambuClient.__init__`,
   `connect`, `MqttThread`, `on_connect`, `on_disconnect`, `on_message`, `publish`, `disconnect`,
   `setup_tls`, `local_tls_context`) and `models.py` (`Temperature`, `Fans`, `AMSList`,
   `AMSInstance`, `AMSTray`, `HMSList` incl. `_hms_list`, `PrintJob` incl. its cache pruning,
   `Info`, `StageAction`, `Extruder`, `PrintFun`, `HomeFlag`, `PrintError`, `Device.print_update`
   and `Device.supports_feature`), against the adapter and normalizer assumptions above. When
   upstream starts parsing a field that `RawReport` reads itself, drop it from `RawReport`.
7. Run the test suite. The golden replay (`tests/test_bambu_golden.py`), the 1.0.1 parity
   (`tests/test_bambu_contract_parity.py`), the adapter tests and the guards are the gate; new
   stage ids fail the stage-label test until `app/web/app.js` gets Russian labels for them.
