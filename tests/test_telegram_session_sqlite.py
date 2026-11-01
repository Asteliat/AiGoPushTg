import sqlite3
from types import SimpleNamespace

from telethon import TelegramClient

from app.telegram_gateway.session import (
    SESSION_BUSY_TIMEOUT_MS,
    configure_telethon_sqlite_session,
)


def test_telethon_session_uses_wal_and_busy_timeout(tmp_path):
    database = tmp_path / "account.session"
    connection = sqlite3.connect(database)
    client = SimpleNamespace(session=SimpleNamespace(_conn=connection))

    try:
        assert configure_telethon_sqlite_session(client) is True
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == SESSION_BUSY_TIMEOUT_MS
    finally:
        connection.close()


def test_wal_reader_does_not_block_worker_session_writes(tmp_path):
    database = tmp_path / "account.session"
    writer = sqlite3.connect(database, timeout=1)
    writer.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, value TEXT)")
    writer.commit()
    client = SimpleNamespace(session=SimpleNamespace(_conn=writer))

    reader = None
    try:
        assert configure_telethon_sqlite_session(client) is True
        reader = sqlite3.connect(database, timeout=1)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM entities").fetchall()

        # A long-lived snapshot reader must not prevent the live Telethon
        # session from recording newly discovered Telegram entities.
        writer.execute("INSERT INTO entities(value) VALUES ('new entity')")
        writer.commit()
        assert writer.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
    finally:
        if reader is not None:
            reader.close()
        writer.close()


def test_non_sqlite_session_is_left_unchanged():
    client = SimpleNamespace(session=SimpleNamespace())
    assert configure_telethon_sqlite_session(client) is False


def test_real_telethon_client_session_is_configured(tmp_path):
    client = TelegramClient(str(tmp_path / "telethon-account"), 12345, "0123456789abcdef0123456789abcdef")
    try:
        assert configure_telethon_sqlite_session(client) is True
        assert client.session._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        client.session.close()



def test_copy_sqlite_session_creates_consistent_snapshot(tmp_path):
    from app.telegram_gateway.management import _copy_sqlite_session

    source = tmp_path / "live-account.session"
    snapshot = tmp_path / "isolated-account.session"
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, username TEXT)")
        writer.execute("INSERT INTO entities(username) VALUES ('original-user')")
        writer.commit()

        _copy_sqlite_session(str(source), str(snapshot))

        copied = sqlite3.connect(snapshot)
        try:
            assert copied.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert copied.execute("SELECT username FROM entities").fetchall() == [("original-user",)]
        finally:
            copied.close()

        # Snapshot operations must never mutate the worker-owned source database.
        assert writer.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
    finally:
        writer.close()
