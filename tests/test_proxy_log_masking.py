"""Proxy URLs carry login:password — they must never reach the logs in clear."""
import asyncio
import logging
import re
import sys
import threading
import time
from pathlib import Path

import pytest

import proxy_checker
from proxy_checker import ProxyChecker, mask_proxy

SECRET = "s3cr3t-pa55"
URL_A = "http://bot_user:%s@192.0.2.10:8080" % SECRET
URL_B = "http://bot_user:%s@192.0.2.11:8080" % SECRET


def _clean(caplog, secret=SECRET):
    """No record carries the secret in its rendered message or traceback.

    Checks the records, not caplog.text: a formatter on a capture handler could
    otherwise hide an unmasked message.
    """
    for r in caplog.records:
        text = r.getMessage() + (r.exc_text or "")
        if r.exc_info:
            text += logging.Formatter().formatException(r.exc_info)
        if secret in text:
            return False
    return True


@pytest.fixture(autouse=True)
def _isolated_registry():
    """The proxy registry is process-global: give every test a clean copy."""
    with proxy_checker._known_lock:
        saved = (dict(proxy_checker._known_proxies), proxy_checker._known_compiled)
        proxy_checker._known_proxies.clear()
        proxy_checker._known_compiled = ((), None)
    yield
    with proxy_checker._known_lock:
        proxy_checker._known_proxies.clear()
        proxy_checker._known_proxies.update(saved[0])
        proxy_checker._known_compiled = saved[1]


def test_mask_proxy_hides_credentials_keeps_endpoint():
    assert mask_proxy(URL_A) == "http://***@192.0.2.10:8080"
    assert mask_proxy("http://192.0.2.10:8080") == "http://192.0.2.10:8080"
    assert mask_proxy(None) is None
    assert mask_proxy("") == ""


def test_best_proxy_change_and_all_failed_logs_are_masked(caplog):
    caplog.set_level(logging.DEBUG, logger="proxy_checker")
    pc = ProxyChecker([URL_A, URL_B])
    pc._update({URL_A: 0.5, URL_B: 0.9})   # best changes None -> A
    pc._update({URL_A: None, URL_B: 0.4})  # best changes A -> B
    pc._update({URL_A: None, URL_B: None})  # all failed, keeping current
    assert caplog.records, "expected log records"
    assert _clean(caplog)
    assert "192.0.2.11:8080" in caplog.text  # endpoint still identifiable


def test_check_one_failure_and_success_logs_are_masked(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="proxy_checker")

    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise RuntimeError("Cannot connect to proxy %s" % URL_A)

        async def __aexit__(self, *a):
            return False

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", _Boom)
    assert asyncio.run(ProxyChecker([URL_A])._check_one(URL_A)) is None
    assert "Proxy check failed" in caplog.text
    assert _clean(caplog)


def test_telegram_bot_logs_only_masked_proxy_urls():
    # The reporter's proxy logging lives inside a nested coroutine; guard it at
    # the source level: any logger call mentioning a proxy variable must mask it.
    src = Path(proxy_checker.__file__).with_name("telegram_bot_async.py").read_text(encoding="utf-8")
    calls = re.findall(r"logger\.\w+\([^\n]*(?:current_proxy|new_proxy)[^\n]*\)", src)
    assert calls, "expected proxy logging calls in telegram_bot_async.py"
    for call in calls:
        for var in re.findall(r"\b(current_proxy|new_proxy)\b", call):
            assert "mask_proxy(%s)" % var in call, call


# --- edge cases found by the pre-publication and adversarial reviews (12.10.2026) ---

def test_mask_proxy_never_raises_and_hides_odd_passwords():
    # Unbalanced IPv6 bracket: urlsplit raises ValueError on this one.
    assert SECRET not in mask_proxy("http://bot_user:%s@[2001:db8::1:8080" % SECRET)
    # Unencoded '/', '?', '#' or a raw '@' inside the password.
    for pw in ("pa/ss-%s" % SECRET, "pa?ss-%s" % SECRET, "pa#ss-%s" % SECRET, "p@ss-%s" % SECRET):
        masked = mask_proxy("http://bot_user:%s@192.0.2.10:8080" % pw)
        assert masked == "http://***@192.0.2.10:8080", masked
    # No scheme at all, and a '://' inside the password is not a scheme.
    assert mask_proxy("bot_user:%s@192.0.2.10:8080" % SECRET) == "***@192.0.2.10:8080"
    assert mask_proxy("bot_user:Zq9://%s@192.0.2.10:8080" % SECRET) == "***@192.0.2.10:8080"
    # Non-string garbage from a hand-edited settings row.
    assert mask_proxy(8080) == "<proxy>"


def test_scrub_catches_bare_password_fragments_and_never_raises():
    bad = "http://bot_user:%s@[2001:db8::1:8080" % SECRET
    assert SECRET not in proxy_checker._scrub("Invalid URL %s (password %s)" % (bad, SECRET), bad)
    # Percent-encoded password quoted decoded by a library.
    enc = "http://bot_user:s3cr3t%2Fpa55@192.0.2.10:8080"
    assert "s3cr3t/pa55" not in proxy_checker._scrub("auth failed for s3cr3t/pa55", enc)
    # urlsplit/yarl quote the piece before a raw '/': "... int() with base 10: 'Zq9xK'".
    raw = "http://bot_user:Zq9xK/fakePW7@192.0.2.10:8080"
    out = proxy_checker._scrub("invalid literal for int() with base 10: 'Zq9xK'", raw)
    assert "Zq9xK" not in out
    # A short login without password must not mangle unrelated text.
    assert proxy_checker._scrub("user connected", "http://u@192.0.2.10:8080") == "user connected"
    assert proxy_checker._scrub("text", 5) == "text"


def test_scrub_secrets_prefix_password_does_not_leave_a_tail():
    a = "http://bot_user:Zq9-fake-PW@192.0.2.10:8080"
    b = "http://bot_user:Zq9-fake-PW-Tail77@192.0.2.11:8080"
    out = proxy_checker.scrub_secrets("auth rejected for Zq9-fake-PW-Tail77", [a, b])
    assert "Tail77" not in out and "Zq9" not in out


def test_redact_credentials_quotes_and_plain_urls():
    r = proxy_checker.redact_credentials
    # yarl decodes %27 into a raw quote; repr then switches to double quotes.
    assert "fake-PW" not in r('Error: url="http://bot_user:Zq9\'fake-PW@127.0.0.1:8080"')
    assert r("proxy %s failed" % URL_A) == "proxy http://***@192.0.2.10:8080 failed"
    # URLs without credentials stay intact.
    assert r("GET https://api.telegram.org/bot") == "GET https://api.telegram.org/bot"
    assert r("git@host:repo and ws://192.0.2.5:9999") == "git@host:repo and ws://192.0.2.5:9999"


def test_redact_credentials_is_linear_on_pathological_lines():
    for line in ("A" * 20000 + " http://x @", "a://" * 5000 + " @",
                 "a" * 16000 + "://" + "x" * 16000 + " @"):
        t = time.perf_counter()
        proxy_checker.redact_credentials(line)
        assert time.perf_counter() - t < 0.05


def _record(msg, args=(), exc=None):
    return logging.LogRecord("t", logging.ERROR, __file__, 1, msg, args, exc)


def test_redacting_filter_masks_message_traceback_and_known_secrets():
    proxy_checker.register_proxies([URL_A, "http://bot_user:Zq9 fake-PW@192.0.2.12:8080"])
    try:
        try:
            raise ValueError("Invalid URL %s" % URL_A)
        except ValueError as inner:
            raise RuntimeError("cycle failed") from inner
    except RuntimeError:
        rec = _record("proxy %s; bare %s; spaced %s", (URL_B, SECRET, "Zq9 fake-PW"), sys.exc_info())
    assert proxy_checker.RedactingFilter().filter(rec) is True
    out = logging.Formatter("%(message)s").format(rec)
    assert SECRET not in out and "fake-PW" not in out
    assert "http://***@192.0.2.11:8080" in out and "Traceback" in out


def test_redacting_filter_never_raises_or_drops():
    class BadStr(Exception):
        def __str__(self):
            raise RuntimeError("str() exploded")

    rec = _record("boom %s", (BadStr(),))
    assert proxy_checker.RedactingFilter().filter(rec) is True


def test_periodic_check_failure_logs_no_password_no_traceback(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="proxy_checker")
    pc = ProxyChecker([URL_A])

    async def boom():
        pc._stop_event.set()
        raise ValueError("Invalid IPv6 URL: %s" % URL_A)

    monkeypatch.setattr(pc, "_check_all_async", boom)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(proxy_checker.asyncio, "sleep", no_sleep)
    asyncio.run(pc._periodic_check())
    assert "Proxy check cycle failed: ValueError" in caplog.text
    assert _clean(caplog)
    assert not any(r.exc_info for r in caplog.records)


def test_checker_thread_survives_unprintable_errors_and_non_str_proxies(monkeypatch):
    class BadStr(Exception):
        def __str__(self):
            raise RuntimeError("str() exploded")

    pc = ProxyChecker([URL_A, 5], check_interval=0)
    calls = []

    async def boom():
        calls.append(1)
        if len(calls) >= 3:
            pc._stop_event.set()
        raise (BadStr() if len(calls) % 2 else ValueError("Invalid URL %s" % URL_A))

    monkeypatch.setattr(pc, "_check_all_async", boom)
    t = threading.Thread(target=pc._run_loop)
    t.start()
    t.join(10)
    assert not t.is_alive() and len(calls) >= 3


def test_main_installs_the_filter_on_its_own_root_handler():
    # Behavioural, in a fresh interpreter (empty root logger, as in production).
    import subprocess
    code = ("import logging, main, proxy_checker; h = logging.getLogger().handlers; "
            "print(len(h), any(isinstance(f, proxy_checker.RedactingFilter) for x in h for f in x.filters))")
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(proxy_checker.__file__).parent,
                         capture_output=True, text=True, timeout=120)
    assert out.stdout.split()[-2:] == ["1", "True"], out.stdout + out.stderr


def test_main_redacts_uvicorn_handlers_after_config():
    import uvicorn
    from fastapi import FastAPI
    import main

    uvicorn.Config(FastAPI(), log_level="info")
    main._redact_uvicorn_logs()
    main._redact_uvicorn_logs()  # idempotent
    handlers = logging.getLogger("uvicorn").handlers
    assert handlers
    for h in handlers:
        assert sum(isinstance(f, proxy_checker.RedactingFilter) for f in h.filters) == 1
    src = Path(main.__file__).read_text(encoding="utf-8")
    assert src.index("uvicorn.Config(app") < src.index("_redact_uvicorn_logs()  #")


def test_uvicorn_error_handler_with_filter_masks_asgi_traceback(capsys):
    import uvicorn
    from fastapi import FastAPI

    uvicorn.Config(FastAPI(), log_level="info")  # installs uvicorn's own handlers
    for h in logging.getLogger("uvicorn").handlers:
        h.addFilter(proxy_checker.RedactingFilter())
    try:
        raise ValueError("Port could not be cast to integer value as 'Zq9xK' in %s" % URL_A)
    except ValueError as exc:
        logging.getLogger("uvicorn.error").error("Exception in ASGI application\n", exc_info=exc)
    err = capsys.readouterr().err
    assert "Exception in ASGI application" in err
    assert SECRET not in err


def test_admin_rejects_odd_proxy_passwords_without_500_or_echo():
    from fastapi import HTTPException
    from app.api import admin

    for url in ("http://bot_user:Zq9xK/fakePW7@192.0.2.10:8080",
                "http://bot_user:Zq9xK#fakePW7@192.0.2.10:8080",
                "http://bot_user:Zq9xK\uff0ffakePW7@[2001:db8::1:8080",
                "http://bot_user:%s@192.0.2.10" % SECRET):
        with pytest.raises(HTTPException) as ei:
            admin._validate_settings({"proxy_list": [url]})
        assert ei.value.status_code == 400
        assert "fakePW7" not in ei.value.detail and "Zq9xK" not in ei.value.detail
        assert SECRET not in ei.value.detail
    admin._validate_settings({"proxy_list": [URL_A, "http://192.0.2.20:3128"]})  # valid ones pass


# --- round 2 of the adversarial review -------------------------------------------

def test_proxy_checker_registers_its_proxies_for_the_log_filter():
    url_c = "http://bot_user:Kx7-only-in-checker@192.0.2.13:8080"
    url_d = "http://bot_user:Kx8-set-later-pw@192.0.2.14:8080"
    pc = ProxyChecker([url_c])
    pc.set_proxies([url_d])
    assert url_c in proxy_checker.known_proxies() and url_d in proxy_checker.known_proxies()
    rec = _record("auth failed for Kx7-only-in-checker and Kx8-set-later-pw")
    proxy_checker.RedactingFilter().filter(rec)
    assert "only-in-checker" not in rec.getMessage() and "set-later" not in rec.getMessage()


def test_registry_is_bounded():
    for i in range(proxy_checker._KNOWN_MAX + 40):
        proxy_checker.register_proxies(["http://bot_user:Bound%03dpw@192.0.2.30:8080" % i])
    assert len(proxy_checker.known_proxies()) <= proxy_checker._KNOWN_MAX


def test_mismatched_log_args_never_reach_handle_error(capsys):
    proxy_checker.register_proxies([URL_A])
    log = logging.getLogger("t.mismatch")
    log.propagate = False
    h = logging.StreamHandler(sys.stderr)
    h.addFilter(proxy_checker.RedactingFilter())
    log.addHandler(h)
    try:
        log.warning("bad call %s %s", URL_A)            # too few args
        log.warning("proxy %s latency %d", URL_A, "x")  # wrong type
    finally:
        log.removeHandler(h)
    err = capsys.readouterr().err
    assert "Logging error" not in err
    assert SECRET not in err and "bad call" in err


def test_uvicorn_colour_message_keeps_its_values():
    from uvicorn.logging import DefaultFormatter

    rec = _record("Uvicorn running on %s://%s:%d", ("http", "127.0.0.1", 8000))
    rec.levelno, rec.levelname = logging.INFO, "INFO"
    rec.color_message = "Uvicorn running on \x1b[1m%s://%s:%d\x1b[0m"
    proxy_checker.RedactingFilter().filter(rec)
    out = DefaultFormatter("%(levelprefix)s %(message)s", use_colors=True).format(rec)
    assert "127.0.0.1" in out and "8000" in out and "%d" not in out


def test_short_password_pieces_do_not_blank_ordinary_words():
    proxy_checker.register_proxies(["http://bot_user:proxy%2Fcheck@192.0.2.40:8080",
                                    "http://bot_user:admin%402024@192.0.2.41:8080"])
    for line in ("Best proxy latency: 0.412s", "Proxy check passed in 0.4s",
                 "user admin logged in 2024-10-12", "Web dashboard: http://127.0.0.1:8000"):
        assert proxy_checker.redact_text(line) == line
    # ...but the whole passwords are still hidden.
    assert "proxy/check" not in proxy_checker.redact_text("auth failed: proxy/check")
    assert "admin@2024" not in proxy_checker.redact_text("auth failed: admin@2024")


def test_basic_auth_header_is_masked():
    import base64
    proxy_checker.register_proxies([URL_A])
    b64 = base64.b64encode(("bot_user:" + SECRET).encode()).decode()
    line = "502 CONNECT api.telegram.org:443 | Proxy-Authorization: Basic %s | Host: x" % b64
    assert b64 not in proxy_checker.redact_text(line)
    # Generic header masking works for unregistered credentials too.
    other = base64.b64encode(b"someone:Qw3-other-pw").decode()
    assert other not in proxy_checker.redact_credentials("Authorization: Basic " + other)


# --- round 3 of the adversarial review -------------------------------------------

def test_basic_header_in_dict_repr_registered_and_generic():
    import base64
    b64 = base64.b64encode(("bot_user:" + SECRET).encode()).decode()
    line = "headers={'Proxy-Authorization': 'Basic %s', 'Host': 'x'}" % b64
    assert b64 not in proxy_checker.redact_credentials(line)  # generic header rule
    proxy_checker.register_proxies([URL_A])
    plain_only = proxy_checker._apply("token %s here" % b64, proxy_checker._known_compiled)
    assert b64 not in plain_only  # registry base64 pair, no header around it


def test_weak_password_words_stay_readable_but_url_forms_are_masked():
    proxy_checker.register_proxies(["http://proxy:proxy@192.0.2.50:3128",
                                    "http://bot_user:check@192.0.2.51:3128"])
    for line in ("Best proxy latency: 0.412s", "Telegram bot using proxy", "rechecking 3 printers",
                 "check passed"):
        assert proxy_checker.redact_text(line) == line
    assert proxy_checker.redact_text("using http://proxy:proxy@192.0.2.50:3128") == "using http://***@192.0.2.50:3128"
    assert "proxy:proxy" not in proxy_checker.redact_text("auth proxy:proxy@ rejected")


def test_long_word_password_is_masked_only_as_a_whole_word():
    proxy_checker.register_proxies(["http://bot_user:checkpoint@192.0.2.52:3128"])
    assert proxy_checker.redact_text("saved checkpoints: 3") == "saved checkpoints: 3"
    assert proxy_checker.redact_text("pw checkpoint used") == "pw *** used"


def test_percent_escaped_forms_are_masked():
    proxy_checker.register_proxies(["http://bot_user:Zq9xK%2ffakePW7@192.0.2.60:8080",
                                    "http://bot_user:Zq9xK-fakePW7@192.0.2.61:8080"])
    out = proxy_checker.redact_text("auth bot_user:Zq9xK%2FfakePW7 failed")
    assert "fakePW7" not in out and "Zq9xK" not in out
    from urllib.parse import quote
    q = quote("http://bot_user:Zq9xK-fakePW7@192.0.2.61:8080", safe="")
    assert "fakePW7" not in proxy_checker.redact_text("GET /x?proxy=%s 200" % q)


def test_unregistered_credential_url_is_masked_by_the_filter():
    rec = _record("Cannot connect to proxy %s", ("http://other:Unreg-PW-5z@192.0.2.70:9",))
    proxy_checker.RedactingFilter().filter(rec)
    assert "Unreg-PW-5z" not in rec.getMessage()


def test_stack_info_is_redacted():
    proxy_checker.register_proxies([URL_A])
    rec = _record("x")
    rec.stack_info = 'Stack (most recent call last):\n  File "x.py", line 1, in f\n    connect("%s")' % URL_A
    proxy_checker.RedactingFilter().filter(rec)
    assert SECRET not in rec.stack_info


def test_bad_args_fallback_keeps_the_call_site():
    rec = _record("bad %d", ("x",))
    proxy_checker.RedactingFilter().filter(rec)
    assert "bad log args" in rec.getMessage() and ":1]" in rec.getMessage()


def test_registry_keeps_a_refreshed_proxy_and_a_large_current_batch():
    keep = "http://bot_user:Keep-me-9pw@192.0.2.80:8080"
    proxy_checker.register_proxies([keep])
    for i in range(10):
        proxy_checker.register_proxies(["http://bot_user:Old%02d-pw9x@192.0.2.81:8080" % i])
    proxy_checker.register_proxies([keep])  # set_proxies() again: moves to the newest end
    for i in range(proxy_checker._KNOWN_MAX - 1):
        proxy_checker.register_proxies(["http://bot_user:New%02d-pw9x@192.0.2.82:8080" % i])
    assert keep in proxy_checker.known_proxies()
    # A single list larger than the bound is kept whole.
    big = ["http://bot_user:Big%03d-pw9x@192.0.2.83:8080" % i for i in range(proxy_checker._KNOWN_MAX + 6)]
    ProxyChecker(big)
    known = proxy_checker.known_proxies()
    assert all(u in known for u in big)
    rec = _record("auth Big000-pw9x rejected")
    proxy_checker.RedactingFilter().filter(rec)
    assert "Big000-pw9x" not in rec.getMessage()


def test_periodic_check_masks_fragments_and_unlisted_urls(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="proxy_checker")
    pc = ProxyChecker(["http://bot_user:Zq9xK/fakePW7@192.0.2.90:8080"])
    msgs = iter(["invalid literal for int() with base 10: 'Zq9xK'",
                 "Cannot connect to http://other:Unlisted-9pw@192.0.2.91:1"])

    async def boom():
        m = next(msgs, None)
        if m is None:
            pc._stop_event.set()
            raise ValueError("done")
        raise ValueError(m)

    monkeypatch.setattr(pc, "_check_all_async", boom)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(proxy_checker.asyncio, "sleep", no_sleep)
    asyncio.run(pc._periodic_check())
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "Zq9xK" not in text and "Unlisted-9pw" not in text
    assert text.count("Proxy check cycle failed") == 3
