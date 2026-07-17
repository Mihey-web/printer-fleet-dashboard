"""Smoke tests for the AMS API endpoints (/api/ams/current, /api/ams/history).

Auth is bypassed by stubbing the token-validation helpers the JWT middleware
imports, so the tests exercise the endpoint logic, not authentication.
"""
import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.services import ams_store, auth_service
from app.api import auth as api_auth
from app.domain.models import PrinterStatus, PrinterKind, PrinterState


class FakeStore:
    def __init__(self, statuses):
        self._s = statuses

    def get_all(self):
        return self._s


def _bambu_with_ams():
    return PrinterStatus(
        id="b1", label="P2S-1", kind=PrinterKind.BAMBU, online=True,
        state=PrinterState.PRINTING, device_type="P2S",
        ams={"tray_now": 1, "units": [{
            "humidity": 2, "humidity_pct": 34, "temp": 28.0, "dry_time": 0,
            "slots": [{"empty": True},
                      {"type": "PLA", "color": "2dd4bf", "name": "x", "remain_pct": 80, "empty": False}],
        }]},
    )


def _creality():
    return PrinterStatus(id="c1", label="K1", kind=PrinterKind.CREALITY,
                         online=True, state=PrinterState.IDLE)


@pytest.fixture
def client(monkeypatch, tmp_path):
    import config as cfg
    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    # Middleware imports these locally from their modules — patch at the source.
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("admin", "admin"))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)

    # Separate AMS DB under tmp, shared by the writer store and the read endpoint.
    db = str(tmp_path / "ams.db")
    monkeypatch.setattr(ams_store, "DB_PATH", db)
    monkeypatch.setattr(api_main, "_ams_db_path", lambda: db)
    writer = ams_store.AmsStore()
    writer.record_ams(_bambu_with_ams())

    app = api_main.create_app(FakeStore([_bambu_with_ams(), _creality()]))
    c = TestClient(app)
    c.cookies.set("access_token", "stub")
    yield c
    writer._db.close()


def test_current_returns_only_bambu_with_ams(client):
    r = client.get("/api/ams/current")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1  # creality excluded
    u = data[0]
    assert u["printer_id"] == "b1"
    assert u["unit_index"] == 0
    assert u["humidity_pct"] == 34
    assert u["drying"] is False
    assert len(u["slots"]) == 2
    # active slot: tray_now=1 → local index 1
    assert u["tray_now_local"] == 1


def test_history_returns_series(client):
    r = client.get("/api/ams/history?printer_id=b1&unit=0&fr=0&to=9999999999")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["humidity_pct"] == 34
    assert rows[0]["temp"] == 28.0


def test_history_rejects_bad_window(client):
    r = client.get("/api/ams/history?printer_id=b1&unit=0&fr=100&to=1")
    assert r.status_code == 400


def test_history_empty_when_no_data(client):
    r = client.get("/api/ams/history?printer_id=nope&unit=3&fr=0&to=9999999999")
    assert r.status_code == 200
    assert r.json()["rows"] == []
