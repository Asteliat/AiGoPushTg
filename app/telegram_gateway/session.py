"""SQLite settings for Telethon session databases shared with snapshot readers."""

import logging

logger = logging.getLogger(__name__)

SESSION_BUSY_TIMEOUT_MS = 30_000


def configure_telethon_sqlite_session(client) -> bool:
    """Enable WAL and a bounded busy timeout on a Telethon SQLite session.

    The worker owns the live session while the control bot may create a consistent
    SQLite backup for read/search operations. WAL lets that backup read without
    blocking Telethon's entity-cache writes.
    """
    session = getattr(client, "session", None)
    connection = getattr(session, "_conn", None)
    if connection is None:
        logger.warning("Telethon session has no SQLite connection; WAL was not configured")
        return False

    try:
        connection.execute(f"PRAGMA busy_timeout = {SESSION_BUSY_TIMEOUT_MS}")
        row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        mode = str(row[0]).lower() if row else ""
        connection.execute("PRAGMA synchronous = NORMAL")
        if mode != "wal":
            logger.warning("Could not enable WAL for Telethon session (journal_mode=%s)", mode or "unknown")
            return False
        logger.info("Telethon session SQLite configured: journal_mode=WAL busy_timeout=%sms", SESSION_BUSY_TIMEOUT_MS)
        return True
    except Exception:
        logger.exception("Could not configure Telethon session SQLite")
        return False
