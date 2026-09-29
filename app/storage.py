import os
import sqlite3
from uuid import uuid4

from app.media import MEDIA_ROOT
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
        database.execute("BEGIN IMMEDIATE")
        legacy = database.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'clients'").fetchone()
        if legacy:
            database.execute("ALTER TABLE clients RENAME TO singers")
            database.execute("ALTER TABLE requests RENAME COLUMN client_id TO singer_id")
            database.execute("ALTER TABLE accepted_counts RENAME COLUMN client_id TO singer_id")
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS singers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                session_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY,
                singer_id TEXT NOT NULL REFERENCES singers(id),
                video_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                title TEXT,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS requests_status_idx ON requests(status, created_at);
            CREATE TABLE IF NOT EXISTS ready_queue (
                request_id TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
                position INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accepted_counts (
                singer_id TEXT PRIMARY KEY REFERENCES singers(id) ON DELETE CASCADE,
                total INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS invitation (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                request_id TEXT NOT NULL REFERENCES requests(id),
                deadline REAL NOT NULL,
                accepted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS missed_invitations (
                request_id TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
                total INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS skip_request (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                request_id TEXT NOT NULL REFERENCES requests(id),
                deadline REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS party (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                generation TEXT NOT NULL
            );
            """
        )
        database.execute("BEGIN IMMEDIATE")
        database.execute("INSERT OR IGNORE INTO party(id, generation) VALUES (1, ?)", (str(uuid4()),))
        generation = database.execute("SELECT generation FROM party WHERE id=1").fetchone()[0]
        for category in ("videos", "previews"):
            legacy = MEDIA_ROOT / category
            if legacy.is_dir():
                destination = MEDIA_ROOT / generation / category
                destination.mkdir(parents=True, exist_ok=True)
                for file in legacy.iterdir():
                    if file.is_file() and not (destination / file.name).exists():
                        file.rename(destination / file.name)
                if not any(legacy.iterdir()):
                    legacy.rmdir()
        queued = {row[0] for row in database.execute("SELECT request_id FROM ready_queue")}
        position = database.execute("SELECT COALESCE(MAX(position), 0) FROM ready_queue").fetchone()[0]
        for row in database.execute(
            "SELECT id FROM requests WHERE status IN ('pending', 'processing', 'ready') ORDER BY created_at, rowid"
        ):
            if row["id"] not in queued:
                position += 1
                database.execute("INSERT INTO ready_queue (request_id, position) VALUES (?, ?)", (row["id"], position))