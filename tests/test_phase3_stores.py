"""Phase-3 store retention: printer_history / ams_history must prune old rows."""
import time

from app.domain.models import PrinterStatus, PrinterKind, PrinterState


def _status():
    return PrinterStatus(id="p1", label="P1", kind=PrinterKind.BAMBU,
                         online=True, state=PrinterState.IDLE,
                         ams={"units": [{"humidity": 2, "humidity_pct": 20,
                                         "temp": 30.0, "dry_time": 0}]})


def test_state_store_prunes_rows_past_retention(tmp_path, monkeypatch):
    import app.services.state_store as ss
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "ph.db"))
    monkeypatch.setattr(ss, "RETENTION_SECONDS", 100)
    monkeypatch.setattr(ss, "_PRUNE_INTERVAL", 0)  # prune on every snapshot
    store = ss.StateStore()
    store._db.execute(
        "INSERT INTO printer_history (printer_id,label,kind,online,state,recorded_at)"
        " VALUES (?,?,?,?,?,?)",
        ("p1", "P1", "bambu", 1, "idle", time.time() - 1000),  # well past retention
    )
    store._db.commit()
    store.record_snapshot(_status())  # fresh row + triggers prune
    total = store._db.execute("SELECT COUNT(*) FROM printer_history").fetchone()[0]
    assert total == 1  # old row pruned, only the fresh snapshot remains


def test_ams_store_prunes_rows_past_retention(tmp_path, monkeypatch):
    import app.services.ams_store as a
    monkeypatch.setattr(a, "DB_PATH", str(tmp_path / "ams.db"))
    monkeypatch.setattr(a, "RETENTION_SECONDS", 100)
    monkeypatch.setattr(a, "_PRUNE_INTERVAL", 0)
    store = a.AmsStore()
    store._db.execute(
        "INSERT INTO ams_history (printer_id,label,unit_index,recorded_at)"
        " VALUES (?,?,?,?)",
        ("p1", "P1", 0, time.time() - 1000),
    )
    store._db.commit()
    store.record_ams(_status())
    total = store._db.execute("SELECT COUNT(*) FROM ams_history").fetchone()[0]
    assert total == 1


def test_ams_store_keys_history_by_physical_unit_index(tmp_path, monkeypatch):
    import app.services.ams_store as a
    monkeypatch.setattr(a, "DB_PATH", str(tmp_path / "ams.db"))
    store = a.AmsStore()
    status = PrinterStatus(
        id="p1", label="P1", kind=PrinterKind.BAMBU, online=True,
        state=PrinterState.PRINTING,
        ams={"units": [{"index": 2, "humidity": 1, "humidity_pct": 10,
                        "temp": 25.0, "dry_time": 0}]})
    store.record_ams(status)
    row = store._db.execute("SELECT unit_index FROM ams_history").fetchone()
    assert row[0] == 2  # physical index from the unit dict, not enumerate 0


def test_state_store_prunes_old_closed_print_jobs_keeps_open_ones(tmp_path, monkeypatch):
    import app.services.state_store as ss
    from app.services import job_journal as jj
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "ph.db"))
    monkeypatch.setattr(ss, "_PRUNE_INTERVAL", 0)
    monkeypatch.setattr(jj, "JOBS_RETENTION_S", 1000)
    store = ss.StateStore()
    now = time.time()
    old = {"printer_id": "p1", "kind": "bambu", "started_at": now - 5000, "last_seen": now - 4000,
           "source": "live"}
    jj.insert_job(store._db, dict(old, result="finished"), now)                          # pruned
    jj.insert_job(store._db, dict(old), now)                                             # open: kept
    jj.insert_job(store._db, dict(old, printer_id="p2", started_at=now - 10, result="failed"), now)
    store._db.commit()
    store.record_snapshot(_status())  # triggers the hourly prune
    rows = store._db.execute("SELECT printer_id, result FROM print_jobs ORDER BY id").fetchall()
    assert rows == [("p1", None), ("p2", "failed")]
    assert store._db.execute("SELECT COUNT(*) FROM printer_history").fetchone()[0] == 1
