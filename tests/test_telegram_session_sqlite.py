import sqlite3
from types import SimpleNamespace

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
