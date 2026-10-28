"""Per-account Telegram proxy configuration with encrypted database storage."""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.settings import get_settings


class ProxyConfigurationError(ValueError):
    """Raised when proxy configuration cannot be encrypted or decoded."""


def _fernet() -> Fernet:
    secret = get_settings().proxy_encryption_key.strip()
    if not secret:
        raise ProxyConfigurationError(
            "Укажите PROXY_ENCRYPTION_KEY в .env, чтобы безопасно хранить настройки прокси."
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_proxy(config: dict[str, Any] | None) -> str | None:
    if not config:
        return None
    normalized = {
        "type": str(config.get("type", "socks5")).lower(),
        "host": str(config.get("host", "")).strip(),
        "port": int(config.get("port", 0)),
        "username": str(config.get("username", "")),
        "password": str(config.get("password", "")),
    }
    if normalized["type"] not in {"socks5", "http"}:
        raise ProxyConfigurationError("Поддерживаются только SOCKS5 и HTTP.")
    if not normalized["host"] or not (1 <= normalized["port"] <= 65535):
        raise ProxyConfigurationError("Проверьте адрес и порт прокси.")
    if any(ch.isspace() for ch in normalized["host"]):
        raise ProxyConfigurationError("Адрес прокси не должен содержать пробелы.")
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()
    return _fernet().encrypt(payload).decode("ascii")


def decrypt_proxy(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        value = json.loads(_fernet().decrypt(token.encode("ascii")))
    except (InvalidToken, ValueError, TypeError, UnicodeError) as exc:
        raise ProxyConfigurationError(
            "Не удалось расшифровать прокси. Проверьте PROXY_ENCRYPTION_KEY в .env."
        ) from exc
    if not isinstance(value, dict):
        raise ProxyConfigurationError("Повреждена сохранённая конфигурация прокси.")
    return value


def telethon_proxy(token: str | None = None, *, config: dict[str, Any] | None = None):
    """Return a Telethon-compatible proxy dictionary, or None for direct connection."""
    value = config if config is not None else decrypt_proxy(token)
    if not value:
        return None
    return {
        "proxy_type": "socks5" if value.get("type") == "socks5" else "http",
        "addr": value["host"],
        "port": int(value["port"]),
        "rdns": True,
        "username": value.get("username") or None,
        "password": value.get("password") or None,
    }
