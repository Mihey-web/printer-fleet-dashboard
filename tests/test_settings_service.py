import pytest

from app.services import settings_service as svc


class FakeCfg:
    TELEGRAM_ENABLED = True
    TELEGRAM_TOKEN = "123:abc"
    TELEGRAM_ALLOWED_CHAT_ID = 42
    TELEGRAM_UPDATE_INTERVAL = 7
    TELEGRAM_NOTIFY_ON_FINISH = True
    TELEGRAM_FINISH_MESSAGE_TEMPLATE = "done: {label}"
    PROXY_LIST = ["http://u:p@10.0.0.1:8080", "http://u:p@10.0.0.2:8080"]
    PROXY_CHECK_INTERVAL = 300


class LegacyProxyCfg:
    PROXY_URL = "http://u:p@10.0.0.9:8080"


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "settings.db")
    svc.init_db(path)
    yield path
    svc.reset_cache()


def test_migration_maps_config_values(db):
    imported = svc.migrate_from_config(FakeCfg, db)
    assert imported == 8
    values = svc.get_all(db)
    assert values["telegram_enabled"] is True
    assert values["telegram_token"] == "123:abc"
    assert values["telegram_chat_id"] == 42
    assert values["telegram_update_interval"] == 7
    assert values["telegram_notify_on_finish"] is True
    assert values["telegram_finish_template"] == "done: {label}"
    assert values["proxy_list"] == list(FakeCfg.PROXY_LIST)
    assert values["proxy_check_interval"] == 300


def test_migration_defaults_for_missing_attrs(db):
    svc.migrate_from_config(FakeCfg, db)
    values = svc.get_all(db)
    # FakeCfg has no error/paused settings — defaults apply.
    assert values["telegram_notify_on_error"] is False
    assert values["telegram_notify_on_paused"] is False
    assert "{label}" in values["telegram_error_template"]


def test_migration_runs_once(db):
    assert svc.migrate_from_config(FakeCfg, db) == 8
    assert svc.migrate_from_config(FakeCfg, db) == 0


def test_migration_folds_legacy_proxy_url(db):
    imported = svc.migrate_from_config(LegacyProxyCfg, db)
    assert imported == 1
    assert svc.get_all(db)["proxy_list"] == [LegacyProxyCfg.PROXY_URL]


def test_set_many_roundtrip_types(db):
    svc.migrate_from_config(FakeCfg, db)
    svc.set_many({
        "telegram_enabled": False,
        "telegram_chat_id": None,
        "telegram_update_interval": 3,
        "proxy_list": ["http://u:p@10.1.1.1:9000"],
    }, db)
    values = svc.get_all(db)
    assert values["telegram_enabled"] is False
    assert values["telegram_chat_id"] is None
    assert values["telegram_update_interval"] == 3
    assert values["proxy_list"] == ["http://u:p@10.1.1.1:9000"]


def test_finish_repeat_defaults(db):
    values = svc.get_all(db)
    assert values["telegram_notify_on_finish_repeat"] is True
    assert values["telegram_finish_repeat_interval_min"] == 30


def test_finish_repeat_roundtrip(db):
    svc.set_many({
        "telegram_notify_on_finish_repeat": True,
        "telegram_finish_repeat_interval_min": 15,
    }, db)
    values = svc.get_all(db)
    assert values["telegram_notify_on_finish_repeat"] is True
    assert values["telegram_finish_repeat_interval_min"] == 15


def test_set_many_rejects_unknown_key(db):
    with pytest.raises(KeyError):
        svc.set_many({"nope": 1}, db)


def test_get_rejects_unknown_key(db):
    with pytest.raises(KeyError):
        svc.get("nope", db)


def test_proxy_checker_set_proxies_drops_removed_state():
    from proxy_checker import ProxyChecker
    pc = ProxyChecker(["http://a:1@h1:1", "http://a:1@h2:2"], 600)
    pc._update({"http://a:1@h1:1": 0.1, "http://a:1@h2:2": 0.2})
    assert pc.best_proxy == "http://a:1@h1:1"
    assert pc.last_check is not None
    pc.set_proxies(["http://a:1@h2:2", "http://a:1@h3:3"])
    assert pc.best_proxy is None  # removed best is forgotten
    assert pc.latencies == {"http://a:1@h2:2": 0.2, "http://a:1@h3:3": None}
    assert pc.proxies == ["http://a:1@h2:2", "http://a:1@h3:3"]
