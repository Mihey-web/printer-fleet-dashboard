import logging
import re
import socket
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)


class MksWifiClient:
    def __init__(self, host: str, label: str, port: int = 8080, timeout: float = 2.0):
        self.host = host
        self.label = label
        self.port = port
        self.timeout = timeout

    def _read_available(self, sock: socket.socket, wait_seconds: float = 0.35) -> str:
        chunks = []
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
            deadline = time.time() + 0.15
        return b"".join(chunks).decode("utf-8", "ignore")

    def send_gcode(self, line: str) -> bool:
        """Open a short-lived TCP connection and send a single G-code line."""
        if not line.endswith("\n"):
            line += "\n"
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.sendall(line.encode("utf-8"))
            time.sleep(0.2)
        return True

    def get_data(self) -> Dict[str, Any]:
        primary_sequence = ["M115\n", "M997\n", "M27\n", "M105\n", "M114\n", "M220\n"]
        detail_sequence = ["M991\n", "M992\n", "M994\n"]
        responses = []

        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(0.2)
            for command in primary_sequence:
                sock.sendall(command.encode("utf-8"))
                time.sleep(0.55)
                responses.append(self._read_available(sock))

        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.settimeout(0.2)
                for command in detail_sequence:
                    try:
                        sock.sendall(command.encode("utf-8"))
                    except OSError:
                        break
                    time.sleep(0.55)
                    chunk = self._read_available(sock)
                    responses.append(chunk)
                    if not chunk:
                        break
        except OSError as e:
            # The detail sequence (print time / filename) is optional — its
            # failure must not abort the read — but log it so intermittent
            # connectivity to the MKS board is at least visible.
            logger.debug("[%s] MKS detail sequence failed (non-fatal): %s", self.label, e)

        return self._result_from_raw("".join(responses))

    def _result_from_raw(self, raw: str) -> Dict[str, Any]:
        """Parse the gathered raw output and reject empty/garbled responses.

        A reachable-but-unresponsive printer (TCP connects, but M997/M115/M105
        all return garbage or nothing) used to yield a dict containing only the
        default device_type="mks", which the collector's `if not data` check
        could never reject -- so the dashboard showed it as IDLE. We now require
        at least one real telemetry signal, otherwise raise so the printer is
        correctly surfaced as offline.
        """
        data = self._parse_raw(raw)
        signal_keys = ("state", "nozzle_temp", "bed_temp", "progress", "firmware_version")
        if not any(data.get(k) is not None for k in signal_keys):
            raise ConnectionError(
                f"[{self.label}] MKS printer reachable but returned no parseable telemetry"
            )
        return {k: v for k, v in data.items() if v is not None}

    @staticmethod
    def _parse_raw(raw: str) -> Dict[str, Any]:
        state_match = re.search(r"M997\s+(IDLE|PRINTING|PAUSE)", raw, re.IGNORECASE)
        progress_match = re.search(r"M27\s+(\d+)", raw, re.IGNORECASE)
        temp_match = re.search(
            r"T:(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)\s+B:(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)",
            raw,
            re.IGNORECASE,
        )
        firmware_match = re.search(r"FIRMWARE_NAME:([^\r\n]+)", raw, re.IGNORECASE)
        print_time_match = re.search(r"M992\s+(\d{2}):(\d{2}):(\d{2})", raw, re.IGNORECASE)
        file_match = re.search(r"M994\s+([^;\r\n]+)(?:;(\d+))?", raw, re.IGNORECASE)
        m991_match = re.search(r"M991\s+(.+)", raw, re.IGNORECASE)
        m991_raw = m991_match.group(1).strip() if m991_match else None
        m114_match = re.search(r"X:([\d.]+)\s+Y:([\d.]+)\s+Z:([\d.]+)", raw, re.IGNORECASE)
        m220_match = re.search(r"M220\s+S?(\d+)", raw, re.IGNORECASE)

        print_time_seconds = None
        if print_time_match:
            hours = int(print_time_match.group(1))
            minutes = int(print_time_match.group(2))
            seconds = int(print_time_match.group(3))
            print_time_seconds = hours * 3600 + minutes * 60 + seconds

        progress = float(progress_match.group(1)) if progress_match else None

        # The MKS WiFi protocol has no native "time remaining" command (only the
        # elapsed time from M992). Derive an estimate from elapsed time + progress
        # so the card can show an ETA. It's linear-extrapolation only -- M27
        # progress tracks the SD file read position, not wall-clock -- so it's
        # approximate, but it's the only signal available. Guard the divide and
        # skip the degenerate 0%/100% ends.
        remaining_time = None
        if print_time_seconds is not None and progress is not None and 0 < progress < 100:
            remaining_time = int(print_time_seconds * (100 - progress) / progress)

        data: Dict[str, Any] = {
            "state": state_match.group(1).lower() if state_match else None,
            "progress": progress,
            "nozzle_temp": float(temp_match.group(1)) if temp_match else None,
            "target_nozzle_temp": float(temp_match.group(2)) if temp_match else None,
            "bed_temp": float(temp_match.group(3)) if temp_match else None,
            "target_bed_temp": float(temp_match.group(4)) if temp_match else None,
            "print_time": print_time_seconds,
            "remaining_time": remaining_time,
            "filename": file_match.group(1).strip() if file_match else None,
            "file_size": int(file_match.group(2)) if file_match and file_match.group(2) else None,
            "device_type": firmware_match.group(1).strip() if firmware_match else "mks",
            "firmware_version": firmware_match.group(1).strip() if firmware_match else None,
            "feedrate_pct": int(m220_match.group(1)) if m220_match else None,
            "debug": {
                "raw": raw,
                "m991_raw": m991_raw,
                "position": f"X:{m114_match.group(1)} Y:{m114_match.group(2)} Z:{m114_match.group(3)}" if m114_match else None,
            },
        }

        return data
