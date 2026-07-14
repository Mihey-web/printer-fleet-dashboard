"""Unit tests for MKS WiFi telemetry parsing (mks_wifi_client._parse_raw /
_result_from_raw). No sockets required -- we feed raw G-code reply text.

Regression: a reachable-but-garbled printer must be surfaced as offline
(ConnectionError) instead of silently appearing IDLE on the dashboard.
"""
import pytest

from mks_wifi_client import MksWifiClient


def _client():
    return MksWifiClient("10.0.0.9", "(D) MKS test", port=8080)


VALID_RAW = (
    "FIRMWARE_NAME:Marlin 2.0.9\n"
    "M997 PRINTING\n"
    "M27 42\n"
    "ok T:210.5 /210.0 B:60.0 /60.0\n"
    "M220 S100\n"
)


def test_parse_raw_extracts_core_fields():
    data = MksWifiClient._parse_raw(VALID_RAW)
    assert data["state"] == "printing"
    assert data["progress"] == 42.0
    assert data["nozzle_temp"] == 210.5
    assert data["bed_temp"] == 60.0
    assert data["firmware_version"] == "Marlin 2.0.9"
    assert data["feedrate_pct"] == 100


def test_result_from_raw_returns_filtered_dict():
    data = _client()._result_from_raw(VALID_RAW)
    assert data["state"] == "printing"
    # None values are stripped from the returned dict
    assert all(v is not None for v in data.values())


def test_result_from_raw_raises_on_empty():
    with pytest.raises(ConnectionError):
        _client()._result_from_raw("")


def test_result_from_raw_raises_on_garbage():
    # TCP connected but the device returned noise with no recognizable signal.
    with pytest.raises(ConnectionError):
        _client()._result_from_raw("zzz random bytes\nnot a printer\n")


# Real reply captured from a (D) Flying Bear Reborn 2 mid-print: temps carry
# targets (T:cur /target), M992 is elapsed time, M27 is progress percent.
REBORN_RAW = (
    "ok\r\nFIRMWARE_NAME:Robin\r\n"
    "T:255 /255 B:80 /80 T0:255 /255 T1:0 /0 @:0 B@:0\r\nok\r\n"
    "M997 PRINTING\r\nok\r\n"
    "M27 80\r\nok\r\n"
    "M992 01:00:00\r\nok\r\n"
)


def test_parse_raw_extracts_target_temps():
    data = MksWifiClient._parse_raw(REBORN_RAW)
    assert data["nozzle_temp"] == 255.0
    assert data["target_nozzle_temp"] == 255.0
    assert data["bed_temp"] == 80.0
    assert data["target_bed_temp"] == 80.0


def test_parse_raw_derives_remaining_time_from_elapsed_and_progress():
    # Elapsed 3600s at 80% -> remaining = 3600 * 20/80 = 900s.
    data = MksWifiClient._parse_raw(REBORN_RAW)
    assert data["print_time"] == 3600
    assert data["remaining_time"] == 900


def test_parse_raw_no_remaining_time_without_progress_or_elapsed():
    # Progress present but no M992 elapsed -> cannot derive ETA.
    data = MksWifiClient._parse_raw(VALID_RAW)
    assert data["remaining_time"] is None
    # Degenerate 0% and 100% must not divide-by-zero or extrapolate nonsense.
    assert MksWifiClient._parse_raw("M27 0\r\nM992 01:00:00\r\n")["remaining_time"] is None
    assert MksWifiClient._parse_raw("M27 100\r\nM992 01:00:00\r\n")["remaining_time"] is None


def test_result_from_raw_accepts_firmware_only_response():
    # A responsive printer that only echoed firmware is still online (no over-reject).
    data = _client()._result_from_raw("FIRMWARE_NAME:Marlin 2.0.9\n")
    assert data["firmware_version"] == "Marlin 2.0.9"
    assert "state" not in data
