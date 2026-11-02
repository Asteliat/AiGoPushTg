import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

from app.api import miniapp


def _signed_init_data(token: str, user_id: int) -> str:
    values = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "first_name": "Admin"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_telegram_init_data_accepts_signed_admin(monkeypatch):
    monkeypatch.setattr(
        miniapp, "get_settings",
        lambda: SimpleNamespace(bot_token="test-token", admin_id_set={12345}),
    )
    assert miniapp._telegram_user_id(_signed_init_data("test-token", 12345)) == 12345


def test_telegram_init_data_rejects_invalid_signature(monkeypatch):
    monkeypatch.setattr(
        miniapp, "get_settings",
        lambda: SimpleNamespace(bot_token="test-token", admin_id_set={12345}),
    )
    with pytest.raises(HTTPException) as error:
        miniapp._telegram_user_id("auth_date=1&user=%7B%22id%22%3A12345%7D&hash=bad")
    assert error.value.status_code == 401


def test_telegram_init_data_rejects_non_admin(monkeypatch):
    monkeypatch.setattr(
        miniapp, "get_settings",
        lambda: SimpleNamespace(bot_token="test-token", admin_id_set={12345}),
    )
    with pytest.raises(HTTPException) as error:
        miniapp._telegram_user_id(_signed_init_data("test-token", 999))
    assert error.value.status_code == 403
