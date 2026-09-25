"""Provider credentials protect real store reads and writes, including SDK calls."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest
import requests

from aa_sdk import HttpStore


TEST_TOKEN = "provider-http-store-test-token"


@pytest.fixture()
def store_url(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    store_dir = Path(__file__).resolve().parents[2] / "store"
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=store_dir,
        env={**os.environ, "STORE_TOKEN": TEST_TOKEN, "STORE_DB": str(tmp_path / "store.db")},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("test store exited before becoming ready")
            try:
                if requests.get(url + "/health", timeout=0.2).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("test store did not become ready")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stderr.close()


@pytest.mark.parametrize("explicit", [False, True])
def test_http_store_authenticated_roundtrip(store_url, monkeypatch, explicit):
    monkeypatch.setenv("STORE_TOKEN", "wrong-environment-token" if explicit else TEST_TOKEN)
    store = HttpStore(store_url, token=TEST_TOKEN) if explicit else HttpStore(store_url)
    first = {"seq": 1, "session_id": "0xS", "tool": "send_email", "args": {"to": "alice@example.com"},
             "result": {"sent": True}, "ts": 1001}
    second = {**first, "seq": 2, "ts": 1002}
    store.append_record("0xS", second)
    store.append_record("0xS", first)
    store.append_record("0xS", {**first, "tool": "different"})
    assert store.get_records("0xS") == [first, second]
    promise = {"predicate": "egress_within_allowlist", "params": {"allowed": ["alice@example.com"]}}
    store.put_promise(7, promise)
    assert store.get_promise(7) == promise
    assert store.inventory() == {"promise_count": 1, "record_count": 2}


@pytest.mark.parametrize("token", [None, "", "   "])
def test_http_store_requires_provider_credential(monkeypatch, token):
    if token is None:
        monkeypatch.delenv("STORE_TOKEN", raising=False)
    else:
        monkeypatch.setenv("STORE_TOKEN", token)
    with pytest.raises(ValueError, match="STORE_TOKEN"):
        HttpStore("http://127.0.0.1:1")


def test_http_store_bad_credential_cannot_read_data(store_url, monkeypatch):
    monkeypatch.setenv("STORE_TOKEN", "wrong-token")
    store = HttpStore(store_url)
    with pytest.raises(requests.HTTPError) as failed:
        store.get_records("0xS")
    assert failed.value.response.status_code == 401


@pytest.mark.parametrize('url', ['http://store.example.com', 'http://10.0.0.8:8000',
    'https://user:password@store.example.com', 'ftp://store.example.com'])
def test_remote_plaintext_or_embedded_credentials_refused(url):
    with pytest.raises(ValueError, match='HTTPS'):
        HttpStore(url, token=TEST_TOKEN)


@pytest.mark.parametrize('url', ['http://127.0.0.1:8000', 'http://localhost:8000',
    'http://[::1]:8000', 'https://store.example.com'])
def test_secure_or_loopback_store_url_accepted(url):
    assert HttpStore(url, token=TEST_TOKEN).base_url == url


def test_store_rejects_redirect_without_forwarding_private_record(monkeypatch):
    from types import SimpleNamespace
    calls = []
    def redirect(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=307, raise_for_status=lambda: None)
    monkeypatch.setattr(requests, 'post', redirect)
    with pytest.raises(requests.HTTPError, match='redirect'):
        HttpStore('https://store.example.com', token=TEST_TOKEN).append_record('session', {'private': 'data'})
    assert calls[0]['allow_redirects'] is False
