"""Tests for docs/dev/HARDENING_2026-11.md: what people who install the panel
themselves run into (trusted proxies, .env, Secure cookies over http, Docker,
password guessing from many IPs, the test requirements).
"""
import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.responses import Response

from app.api import auth
from app.services import audit_service, env_file

ROOT = Path(__file__).resolve().parents[1]


def _req(host, headers=None):
    return types.SimpleNamespace(client=types.SimpleNamespace(host=host), headers=dict(headers or {}),
                                 state=types.SimpleNamespace())


# --- 1. FORGE_TRUSTED_PROXIES ---------------------------------------------------------------------

def _config_in_subprocess(env_extra):
    env = {k: v for k, v in os.environ.items() if k != "FORGE_TRUSTED_PROXIES"}
    env.update(env_extra)
    out = subprocess.run([sys.executable, "-c", "import config, json; print(json.dumps(config.TRUSTED_PROXIES))"],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_trusted_proxies_default_is_this_machine():
    assert _config_in_subprocess({}) == ["127.0.0.1", "::1"]


def test_trusted_proxies_from_the_environment():
    got = _config_in_subprocess({"FORGE_TRUSTED_PROXIES": " 127.0.0.1, 172.18.0.1 ,10.0.0.0/8,"})
    assert got == ["127.0.0.1", "172.18.0.1", "10.0.0.0/8"]


def test_parse_trusted_proxies_ips_and_networks_a_typo_is_skipped(caplog):
    with caplog.at_level(logging.WARNING):
        nets = auth.parse_trusted_proxies(["172.18.0.1", "10.0.0.0/8", "::1", "nginx", "300.1.1.1"])
    assert [str(n) for n in nets] == ["172.18.0.1/32", "10.0.0.0/8", "::1/128"]
    assert "nginx" in caplog.text and "300.1.1.1" in caplog.text


def test_init_auth_takes_the_configured_proxies(monkeypatch):
    # create_app passes config.TRUSTED_PROXIES here (app/api/main.py)
    for name in ("JWT_SECRET", "JWT_ACCESS_EXPIRES", "JWT_REFRESH_EXPIRES", "COOKIE_SECURE", "TRUSTED_PROXIES"):
        monkeypatch.setattr(auth, name, getattr(auth, name))
    auth.init_auth("s" * 64, trusted_proxies=["172.18.0.1"])
    assert auth.client_ip(_req("172.18.0.1", {"x-real-ip": "198.51.100.7"})) == "198.51.100.7"
    # 127.0.0.1 is no longer in the list
    assert auth.client_ip(_req("127.0.0.1", {"x-real-ip": "198.51.100.7"})) == "127.0.0.1"


def test_docker_bridge_gateway_trusted_only_when_listed(monkeypatch):
    # Docker behind nginx on the host: every request comes from the bridge gateway
    gw = _req("172.18.0.1", {"x-real-ip": "198.51.100.7"})
    assert auth.client_ip(gw) == "172.18.0.1"           # default: the header is ignored
    monkeypatch.setattr(auth, "TRUSTED_PROXIES", auth.parse_trusted_proxies(["127.0.0.1", "172.18.0.1"]))
    assert auth.client_ip(gw) == "198.51.100.7"
    # another container on the bridge is not the proxy
    assert auth.client_ip(_req("172.18.0.5", {"x-real-ip": "1.2.3.4"})) == "172.18.0.5"


def test_trusted_proxy_network_cidr(monkeypatch):
    monkeypatch.setattr(auth, "TRUSTED_PROXIES", auth.parse_trusted_proxies(["172.16.0.0/12"]))
    assert auth.client_ip(_req("172.20.3.4", {"x-forwarded-for": "1.1.1.1, 198.51.100.9"})) == "198.51.100.9"
    assert auth.client_ip(_req("192.168.1.2", {"x-real-ip": "1.2.3.4"})) == "192.168.1.2"


def test_ipv4_mapped_peer_and_non_ip_peers():
    # a dual-stack socket reports 127.0.0.1 as ::ffff:127.0.0.1
    assert auth.client_ip(_req("::ffff:127.0.0.1", {"x-real-ip": "198.51.100.7"})) == "198.51.100.7"
    assert auth.client_ip(_req("testclient", {"x-real-ip": "1.2.3.4"})) == "testclient"
    assert auth.client_ip(types.SimpleNamespace(client=None, headers={"x-real-ip": "1.2.3.4"})) == "unknown"


# --- 2. .env -------------------------------------------------------------------------------------

def test_env_file_parse_format():
    text = "\n".join([
        "\ufeff# a comment",
        "",
        "COOKIE_SECURE=0",
        "  WEB_PORT = 8080  ",
        "export WEB_HOST=0.0.0.0",
        "FORGE_ADMIN_PASSWORD='p#ss = word'  # comment after quotes",
        'FORGE_JWT_SECRET="abc def"',
        "FORGE_TRUSTED_PROXIES=127.0.0.1,172.18.0.1   # the bridge",
        "HISTORY_RETENTION_DAYS=",
        "FORGE_BAMBU_TLS_VERIFY=X1C#P2S",
        "not a line",
        "1BAD=x",
        "=x",
        "POLL_INTERVAL=30",
        "POLL_INTERVAL=60",
    ])
    assert env_file.parse(text.lstrip("\ufeff")) == {
        "COOKIE_SECURE": "0",
        "WEB_PORT": "8080",
        "WEB_HOST": "0.0.0.0",
        "FORGE_ADMIN_PASSWORD": "p#ss = word",
        "FORGE_JWT_SECRET": "abc def",
        "FORGE_TRUSTED_PROXIES": "127.0.0.1,172.18.0.1",
        "HISTORY_RETENTION_DAYS": "",
        "FORGE_BAMBU_TLS_VERIFY": "X1C#P2S",    # '#' without a space before it is part of the value
        "POLL_INTERVAL": "60",                  # a later duplicate wins
    }


def test_env_file_load_never_overrides_the_environment(tmp_path):
    p = tmp_path / ".env"
    p.write_text("\ufeffCOOKIE_SECURE=0\nWEB_PORT=8080\nEMPTY_ALREADY=x\n", encoding="utf-8")
    env = {"COOKIE_SECURE": "1", "EMPTY_ALREADY": ""}
    assert env_file.load(str(p), env) == ["WEB_PORT"]
    assert env == {"COOKIE_SECURE": "1", "EMPTY_ALREADY": "", "WEB_PORT": "8080"}


def test_env_file_missing_or_unreadable_sets_nothing(tmp_path):
    env = {}
    assert env_file.load(str(tmp_path / "nope.env"), env) == []
    bad = tmp_path / ".env"
    bad.write_bytes(b"KEY=\xff\xfe\xfa\n")
    assert env_file.load(str(bad), env) == [] and env == {}


_MAIN_PROBE = r"""
import builtins, os, runpy, sys
sys.path.insert(0, sys.argv[1])
class Stop(Exception):
    pass
real_import = builtins.__import__
def guarded(name, *a, **k):
    if name == "uvicorn":          # main.py imports it right after reading .env: stop there
        raise Stop()
    return real_import(name, *a, **k)
builtins.__import__ = guarded
try:
    runpy.run_path(sys.argv[2], run_name=sys.argv[3])
except Stop:
    pass
builtins.__import__ = real_import
print(os.environ.get("FORGE_TEST_FROM_DOTENV", "-"), os.environ.get("COOKIE_SECURE", "-"))
"""


@pytest.mark.parametrize("run_name, expected", [("__main__", "from-file 1"), ("main", "- 1")])
def test_main_reads_dotenv_before_anything_else_only_as_the_program(tmp_path, run_name, expected):
    # main.py next to a .env: started as the program it reads the file before its
    # first import of the app (modules read the environment at import time);
    # imported (tests) it reads nothing. COOKIE_SECURE from the environment wins.
    shutil.copy(ROOT / "main.py", tmp_path / "main.py")
    (tmp_path / ".env").write_text("FORGE_TEST_FROM_DOTENV=from-file\nCOOKIE_SECURE=0\n", encoding="utf-8")
    env = dict(os.environ, COOKIE_SECURE="1")
    env.pop("FORGE_TEST_FROM_DOTENV", None)
    out = subprocess.run([sys.executable, "-c", _MAIN_PROBE, str(ROOT), str(tmp_path / "main.py"), run_name],
                         env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split("\n")[-2] == expected


def test_main_loads_dotenv_before_importing_the_app():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    load_at = src.index("env_file.load(")
    for first_import in ("import uvicorn", "from app.api.main import", "import config"):
        assert load_at < src.index(first_import), first_import


def _code_env_names():
    names = set()
    files = [ROOT / "config.py", ROOT / "main.py"] + sorted((ROOT / "app").rglob("*.py"))
    pat = re.compile(r"""(?:os\.environ\.get|os\.getenv|_env_int|_env_days)\(\s*['"]([A-Z][A-Z0-9_]*)['"]""")
    for f in files:
        if "vendor" in f.parts:
            continue
        names |= set(pat.findall(f.read_text(encoding="utf-8")))
    return names


def test_env_example_lists_exactly_what_the_code_reads():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    listed = set(re.findall(r"^#?\s?([A-Z][A-Z0-9_]*)=", example, re.M))
    code = _code_env_names()
    assert "FORGE_TRUSTED_PROXIES" in code and "HISTORY_RETENTION_DAYS" in code
    assert listed == code, {"missing in .env.example": sorted(code - listed),
                            "not read by the code": sorted(listed - code)}


# --- 3. Secure cookie over plain http --------------------------------------------------------------

def test_cookie_secure_http_warning():
    import main
    assert main.cookie_secure_http_warning("0.0.0.0", 8000, True)
    assert "COOKIE_SECURE=0" in main.cookie_secure_http_warning("192.168.1.10", 8000, True)
    for host in ("127.0.0.1", "127.0.1.1", "localhost", "::1", "[::1]"):
        assert main.cookie_secure_http_warning(host, 8000, True) is None, host
    assert main.cookie_secure_http_warning("0.0.0.0", 8000, False) is None


def test_main_logs_the_cookie_warning_at_start():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert re.search(r"cookie_secure_http_warning\(WEB_HOST, WEB_PORT, getattr\(config, 'COOKIE_SECURE'", src)


LOGIN = (ROOT / "app" / "web" / "login.html").read_text(encoding="utf-8")
HINT = ("Вход принят, но браузер отклонил cookie: откройте панель по HTTPS или задайте "
        "COOKIE_SECURE=0 в локальной сети.")

# the login page's script with just enough of a browser around it
_LOGIN_HARNESS = r"""
var els = {};
function el(id) { return els[id] || (els[id] = { id: id, value: '', textContent: '', disabled: false,
  listeners: {}, addEventListener: function (t, f) { this.listeners[t] = f; } }); }
globalThis.document = { getElementById: el };
var replaced = [];
globalThis.location = { protocol: %(protocol)s, hash: '', replace: function (u) { replaced.push(u); } };
globalThis.window = { location: globalThis.location };
globalThis.sessionStorage = { getItem: function () { return String(Date.now()); }, setItem: function () {} };
var calls = [];
var answers = %(answers)s;
globalThis.fetch = function (url) {
  calls.push(url);
  var a = answers[url] || { ok: false, status: 404, body: {} };
  return Promise.resolve({ ok: a.ok, status: a.status, json: function () { return Promise.resolve(a.body); } });
};
%(script)s
el('u').value = 'admin'; el('p').value = 'pw';
el('f').listeners.submit({ preventDefault: function () {} });
setTimeout(function () {
  console.log(JSON.stringify({ calls: calls, replaced: replaced, err: el('e').textContent, disabled: el('b').disabled }));
}, 50);
"""


def _run_login_page(protocol, answers):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = re.findall(r"<script>(.*?)</script>", LOGIN, re.S)[-1]
    src = _LOGIN_HARNESS % {"protocol": json.dumps(protocol), "answers": json.dumps(answers), "script": script}
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


LOGIN_OK = {"ok": True, "status": 200, "body": {"user": {"username": "admin"}}}


def test_login_page_explains_a_rejected_cookie_over_http():
    r = _run_login_page("http:", {"/api/auth/login": LOGIN_OK,
                                  "/api/auth/me": {"ok": False, "status": 401, "body": {}}})
    assert r["calls"] == ["/api/auth/login", "/api/auth/me"]
    assert r["replaced"] == []
    assert r["err"] == HINT
    assert r["disabled"] is False


def test_login_page_goes_on_when_the_session_is_there():
    r = _run_login_page("http:", {"/api/auth/login": LOGIN_OK,
                                  "/api/auth/me": {"ok": True, "status": 200, "body": {}}})
    assert r["replaced"] == ["/"] and r["err"] == ""


def test_login_page_over_https_does_not_ask_twice():
    r = _run_login_page("https:", {"/api/auth/login": LOGIN_OK})
    assert r["calls"] == ["/api/auth/login"] and r["replaced"] == ["/"]


def test_login_page_wrong_password_unchanged():
    r = _run_login_page("http:", {"/api/auth/login": {"ok": False, "status": 401,
                                                      "body": {"detail": "Invalid username or password"}}})
    assert r["calls"] == ["/api/auth/login"] and r["err"] == "Invalid username or password"


def test_cookie_hint_is_in_the_english_dictionary():
    en = (ROOT / "app" / "web" / "i18n" / "en.js").read_text(encoding="utf-8")
    block = en[en.index("/*strings:begin*/"):en.index("/*strings:end*/")]
    assert json.dumps(HINT, ensure_ascii=False) + ":" in block


# --- 4. Docker -----------------------------------------------------------------------------------

def test_compose_mounts_every_database_the_app_opens():
    from app.services import ams_store, state_store
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for path in (state_store.DB_PATH, auth.USERS_DB, ams_store.DB_PATH):
        name = os.path.basename(path)
        assert re.search(r"^\s*- \./%s:/app/%s\s*$" % (re.escape(name), re.escape(name)), compose, re.M), name
        # and the first-run line creates it as a file
        assert re.search(r"touch [^\n]*\b%s\b" % re.escape(name), compose), name


def test_readme_warns_docker_bypasses_the_firewall():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "ufw" in readme and "127.0.0.1:8000:8000" in readme
    assert "touch printer_history.db users.db ams_history.db" in readme
    assert "FORGE_TRUSTED_PROXIES" in readme


def test_database_path_created_as_a_directory_is_reported(tmp_path, monkeypatch):
    import main
    from app.services import ams_store, state_store
    monkeypatch.setattr(state_store, "DB_PATH", str(tmp_path / "printer_history.db"))
    monkeypatch.setattr(auth, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(ams_store, "DB_PATH", str(tmp_path / "ams_history.db"))
    (tmp_path / "printer_history.db").touch()
    (tmp_path / "users.db").touch()
    assert main.db_paths_that_are_directories() == []
    (tmp_path / "ams_history.db").mkdir()            # what Docker does for a missing file
    assert main.db_paths_that_are_directories() == [str(tmp_path / "ams_history.db")]


# --- 5. password guessing from many IPs: a progressive delay per login name -------------------------

@pytest.fixture
def login_db(tmp_path, monkeypatch):
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(auth, "USERS_DB", db)
    monkeypatch.setattr(audit_service, "USERS_DB", db)
    monkeypatch.setattr(audit_service, "_audit", None)
    monkeypatch.setattr(auth, "JWT_SECRET", "s" * 64)
    monkeypatch.setattr(auth, "COOKIE_SECURE", False)
    # the password check itself is not under test: plain comparison, no bcrypt time
    monkeypatch.setattr(auth, "verify_password", lambda pw, h: pw == "right-" + h)
    auth.init_users_db()
    now = time.time()
    conn = sqlite3.connect(db)
    for name in ("admin", "bob"):
        conn.execute("INSERT INTO users (username, password_hash, role, created_at, updated_at)"
                     " VALUES (?, ?, 'admin', ?, ?)", (name, name, now, now))
    conn.commit()
    conn.close()
    return db


def _fail(username, n, ip_base="203.0.113."):
    svc = audit_service.get_audit_service()
    for i in range(n):
        svc.log(username, "login_fail", ip_base + str(i % 250), "ua")     # one try per address


@pytest.fixture
def waits(monkeypatch):
    got = []

    async def record(seconds):
        got.append(seconds)

    monkeypatch.setattr(auth, "_throttle_sleep", record)
    return got


def _login(username, password, ip="198.51.100.1"):
    return asyncio.run(auth.login(auth.LoginRequest(username=username, password=password), _req(ip), Response()))


def test_username_delay_schedule():
    assert [auth.username_delay_s(n) for n in range(0, 10)] == [0.0] * 10
    assert [auth.username_delay_s(n) for n in range(10, 17)] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert auth.username_delay_s(10 ** 6) == 30.0


def test_no_delay_below_ten_failures(login_db, waits):
    _fail("admin", 9)
    assert _login("admin", "right-admin")["user"]["username"] == "admin"
    assert waits == []


def test_right_password_waits_then_gets_in(login_db, waits):
    _fail("admin", 12)                                    # a pool of 12 addresses, one guess each
    assert _login("admin", "right-admin")["user"]["username"] == "admin"
    assert waits == [4.0]


def test_every_further_guess_waits_longer(login_db, waits):
    _fail("admin", 10)
    for i in range(6):                                    # each from a fresh address
        with pytest.raises(HTTPException) as e:
            _login("admin", "guess", ip="192.0.2.%d" % i)
        assert e.value.status_code == 429
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]


def test_delay_is_per_login_name(login_db, waits):
    _fail("admin", 15)
    assert _login("bob", "right-bob")["user"]["username"] == "bob"
    assert waits == []


def test_unknown_name_is_delayed_the_same_way(login_db, waits):
    # no oracle: a name that does not exist is throttled like a real one
    _fail("ghost", 11)
    with pytest.raises(HTTPException):
        _login("ghost", "x")
    assert waits == [2.0]


def test_per_ip_limit_unchanged(login_db, waits):
    svc = audit_service.get_audit_service()
    for _ in range(5):
        svc.log("someone", "login_fail", "198.51.100.1", "ua")
    with pytest.raises(HTTPException) as e:
        _login("bob", "right-bob")
    assert e.value.status_code == 429
    assert waits == []


def test_wait_blocks_neither_the_event_loop_nor_the_ip_lock(login_db):
    # A real 1 s wait for 'admin'. Meanwhile, from the SAME address, 'bob' signs
    # in at once: the waiting attempt holds no thread and not the per-IP lock.
    _fail("admin", 10)
    ip = "198.51.100.50"

    async def scenario():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        ticks = []

        async def ticker():
            while loop.time() - t0 < 0.9:
                ticks.append(loop.time())
                await asyncio.sleep(0.05)

        admin = asyncio.create_task(auth.login(auth.LoginRequest(username="admin", password="right-admin"),
                                               _req(ip), Response()))
        tick = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        assert not auth._login_lock(ip).locked()
        bob = await auth.login(auth.LoginRequest(username="bob", password="right-bob"), _req(ip), Response())
        bob_at = loop.time() - t0
        got = await admin
        admin_at = loop.time() - t0
        await tick
        return bob, bob_at, got, admin_at, len(ticks)

    bob, bob_at, got, admin_at, ticks = asyncio.run(scenario())
    assert bob["user"]["username"] == "bob" and bob_at < 0.8
    assert got["user"]["username"] == "admin" and admin_at >= 1.0
    assert ticks >= 10                                    # the loop kept running during the wait


def test_audit_comment_no_longer_promises_a_lockout():
    src = (ROOT / "app" / "services" / "audit_service.py").read_text(encoding="utf-8")
    assert "per-account lockout" not in src
    assert "progressive" in src


# --- 6. README: the test requirements --------------------------------------------------------------

def test_readme_test_section_installs_httpx():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index("## Running the tests"):]
    section = section[:section.index("\n## ", 5)]
    assert re.search(r"pip install [^\n]*\bpytest\b[^\n]*\bhttpx\b", section)
