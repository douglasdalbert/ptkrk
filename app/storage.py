import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATABASE_PATH = Path(os.environ.get("KARAOKE_DB_PATH", "/data/karaoke.sqlite3"))


@contextmanager
def connection():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(DATABASE_PATH, timeout=30)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("PRAGMA foreign_keys=ON")
    try:
        yield database
        database.commit()
    except BaseException:
        database.rollback()
        raise
    finally:
        database.close()


def initialize() -> None:
    with connection() as database:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                session_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL REFERENCES clients(id),
                video_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                title TEXT,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS requests_status_idx ON requests(status, created_at);
            """
        )