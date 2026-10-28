from types import SimpleNamespace

import pytest

from app.telegram_gateway import proxy


def test_proxy_round_trip(monkeypatch):
    monkeypatch.setattr(
        proxy, "get_settings", lambda: SimpleNamespace(proxy_encryption_key="test-secret-key")
    )
    config = {
        "type": "socks5",
        "host": "127.0.0.1",
        "port": 1080,
        "username": "user",
        "password": "pass",
    }
    token = proxy.encrypt_proxy(config)
    assert token
    assert proxy.decrypt_proxy(token) == config
    result = proxy.telethon_proxy(token)
    assert result == {
        "proxy_type": "socks5",
        "addr": "127.0.0.1",
        "port": 1080,
        "rdns": True,
        "username": "user",
        "password": "pass",
    }


def test_proxy_requires_encryption_key(monkeypatch):
    monkeypatch.setattr(proxy, "get_settings", lambda: SimpleNamespace(proxy_encryption_key=""))
    with pytest.raises(proxy.ProxyConfigurationError):
        proxy.encrypt_proxy({"type": "http", "host": "proxy.example", "port": 8080})


def test_direct_connection_has_no_proxy():
    assert proxy.telethon_proxy(None) is None
