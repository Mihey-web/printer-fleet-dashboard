"""Proxy URLs carry login:password — they must never reach the logs in clear."""
import asyncio
import logging
import re
from pathlib import Path

import proxy_checker
from proxy_checker import ProxyChecker, mask_proxy

SECRET = "s3cr3t-pa55"
URL_A = "http://bot_user:%s@192.0.2.10:49152" % SECRET
URL_B = "http://bot_user:%s@192.0.2.11:49152" % SECRET


def test_mask_proxy_hides_credentials_keeps_endpoint():
    assert mask_proxy(URL_A) == "http://***@192.0.2.10:49152"
    assert mask_proxy("http://192.0.2.10:49152") == "http://192.0.2.10:49152"
    assert mask_proxy(None) is None
    assert mask_proxy("") == ""


def test_best_proxy_change_and_all_failed_logs_are_masked(caplog):
    caplog.set_level(logging.DEBUG, logger="proxy_checker")
    pc = ProxyChecker([URL_A, URL_B])
    pc._update({URL_A: 0.5, URL_B: 0.9})   # best changes None -> A
    pc._update({URL_A: None, URL_B: 0.4})  # best changes A -> B
    pc._update({URL_A: None, URL_B: None})  # all failed, keeping current
    assert caplog.records, "expected log records"
    assert SECRET not in caplog.text
    assert "192.0.2.11:49152" in caplog.text  # endpoint still identifiable


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
    assert SECRET not in caplog.text


def test_telegram_bot_logs_only_masked_proxy_urls():
    # The reporter's proxy logging lives inside a nested coroutine; guard it at
    # the source level: any logger call mentioning a proxy variable must mask it.
    src = Path(proxy_checker.__file__).with_name("telegram_bot_async.py").read_text(encoding="utf-8")
    calls = re.findall(r"logger\.\w+\([^\n]*(?:current_proxy|new_proxy)[^\n]*\)", src)
    assert calls, "expected proxy logging calls in telegram_bot_async.py"
    for call in calls:
        for var in re.findall(r"\b(current_proxy|new_proxy)\b", call):
            assert "mask_proxy(%s)" % var in call, call
