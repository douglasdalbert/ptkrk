import os
import sqlite3
import json
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


def save_playback_sync(sync: dict | None) -> None:
    with connection() as database:
        if sync is None:
            database.execute("DELETE FROM runtime_state WHERE key = 'playback_sync'")
            return
        database.execute(
            "INSERT INTO runtime_state(key, value) VALUES ('playback_sync', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(sync),),
        )


def load_playback_sync() -> dict | None:
    with connection() as database:
        row = database.execute("SELECT value FROM runtime_state WHERE key = 'playback_sync'").fetchone()
    return json.loads(row["value"]) if row else None


def add_vocal_activity(
    event_id: str, request_id: str, singer_id: str, speaking: bool,
    position_ms: int | None, state_since_ms: int | None, created_at: float,
) -> None:
    with connection() as database:
        database.execute(
            "INSERT OR IGNORE INTO vocal_activity(event_id, request_id, singer_id, speaking, "
            "position_ms, state_since_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, request_id, singer_id, int(speaking), position_ms, state_since_ms, created_at),
        )
        database.execute(
            "DELETE FROM vocal_activity WHERE id <= "
            "(SELECT COALESCE(MAX(id), 0) - 200 FROM vocal_activity)"
        )


def vocal_activity_since(event_id: int) -> list[dict]:
    with connection() as database:
        return [dict(row) for row in database.execute(
            "SELECT id, event_id, request_id, singer_id, speaking, position_ms, state_since_ms "
            "FROM vocal_activity WHERE id > ? ORDER BY id", (event_id,),
        )]


def latest_vocal_activity_id() -> int:
    with connection() as database:
        return database.execute("SELECT COALESCE(MAX(id), 0) FROM vocal_activity").fetchone()[0]


def add_tv_notification(event_id: str, request_id: str, payload: dict) -> None:
    with connection() as database:
        database.execute(
            "INSERT OR IGNORE INTO tv_notifications(event_id, request_id, payload) VALUES (?, ?, ?)",
            (event_id, request_id, json.dumps(payload)),
        )
        database.execute(
            "DELETE FROM tv_notifications WHERE id <= "
            "(SELECT COALESCE(MAX(id), 0) - 200 FROM tv_notifications)"
        )


def tv_notifications_since(notification_id: int) -> list[dict]:
    with connection() as database:
        return [dict(row) for row in database.execute(
            "SELECT id, payload FROM tv_notifications WHERE id > ? ORDER BY id", (notification_id,),
        )]


def latest_tv_notification_id() -> int:
    with connection() as database:
        return database.execute("SELECT COALESCE(MAX(id), 0) FROM tv_notifications").fetchone()[0]


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
            CREATE TABLE IF NOT EXISTS backvocals (
                request_id TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                singer_id TEXT NOT NULL REFERENCES singers(id) ON DELETE CASCADE,
                accepted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (request_id, singer_id)
            );
            CREATE TABLE IF NOT EXISTS group_rooms (
                request_id TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
                video_id TEXT NOT NULL,
                opened_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ready_queue (
                request_id TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
                position INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accepted_counts (
                singer_id TEXT PRIMARY KEY REFERENCES singers(id) ON DELETE CASCADE,
                total INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS song_scores (
                request_id TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                singer_id TEXT NOT NULL REFERENCES singers(id) ON DELETE CASCADE,
                hit_blocks TEXT NOT NULL DEFAULT '[]',
                boosted_blocks TEXT NOT NULL DEFAULT '[]',
                penalties INTEGER NOT NULL DEFAULT 0,
                score_units INTEGER NOT NULL DEFAULT 0 CHECK (score_units >= 0),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (request_id, singer_id)
            );
            CREATE TABLE IF NOT EXISTS score_events (
                event_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                singer_id TEXT NOT NULL REFERENCES singers(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                block_index INTEGER,
                offcue_window INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS score_history (
                request_id TEXT NOT NULL,
                singer_id TEXT NOT NULL REFERENCES singers(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                video_id TEXT NOT NULL,
                points REAL NOT NULL DEFAULT 0,
                preview BLOB,
                participants TEXT NOT NULL DEFAULT '[]',
                finished_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (request_id, singer_id)
            );
            CREATE INDEX IF NOT EXISTS score_history_singer_points_idx
                ON score_history(singer_id, points DESC, finished_at DESC);
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
            CREATE TABLE IF NOT EXISTS runtime_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vocal_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                request_id TEXT NOT NULL,
                singer_id TEXT NOT NULL,
                speaking INTEGER NOT NULL,
                position_ms INTEGER,
                state_since_ms INTEGER,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tv_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                request_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            """
        )
        database.execute("BEGIN IMMEDIATE")
        if "position_ms" not in {row[1] for row in database.execute("PRAGMA table_info(vocal_activity)")}:
            database.execute("ALTER TABLE vocal_activity ADD COLUMN position_ms INTEGER")
        if "state_since_ms" not in {row[1] for row in database.execute("PRAGMA table_info(vocal_activity)")}:
            database.execute("ALTER TABLE vocal_activity ADD COLUMN state_since_ms INTEGER")
        if "lead_accepted" not in {row[1] for row in database.execute("PRAGMA table_info(invitation)")}:
            database.execute("ALTER TABLE invitation ADD COLUMN lead_accepted INTEGER NOT NULL DEFAULT 0")
            database.execute("UPDATE invitation SET lead_accepted = accepted")
        if "joined" not in {row[1] for row in database.execute("PRAGMA table_info(backvocals)")}:
            database.execute("ALTER TABLE backvocals ADD COLUMN joined INTEGER NOT NULL DEFAULT 1")
        if "score_eligible" not in {row[1] for row in database.execute("PRAGMA table_info(backvocals)")}:
            database.execute("ALTER TABLE backvocals ADD COLUMN score_eligible INTEGER NOT NULL DEFAULT 1")
        if "offcue_window" not in {row[1] for row in database.execute("PRAGMA table_info(score_events)")}:
            database.execute("ALTER TABLE score_events ADD COLUMN offcue_window INTEGER")
        if "score_units" not in {row[1] for row in database.execute("PRAGMA table_info(song_scores)")}:
            database.execute("ALTER TABLE song_scores ADD COLUMN score_units INTEGER")
        if "boosted_blocks" not in {row[1] for row in database.execute("PRAGMA table_info(song_scores)")}:
            database.execute("ALTER TABLE song_scores ADD COLUMN boosted_blocks TEXT NOT NULL DEFAULT '[]'")
        if (database.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'performances'").fetchone()
                and database.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'performance_participants'").fetchone()):
            for performance in database.execute(
                "SELECT request_id, title, video_id, points, preview, finished_at FROM performances"
            ).fetchall():
                participants = [dict(row) for row in database.execute(
                    "SELECT singers.id AS singer_id, singers.name FROM performance_participants "
                    "JOIN singers ON singers.id = performance_participants.singer_id "
                    "WHERE performance_participants.request_id = ? ORDER BY performance_participants.rowid",
                    (performance["request_id"],),
                )]
                database.executemany(
                    "INSERT OR IGNORE INTO score_history(request_id, singer_id, title, video_id, points, preview, "
                    "participants, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(performance["request_id"], participant["singer_id"], performance["title"],
                      performance["video_id"], performance["points"], performance["preview"],
                      json.dumps(participants), performance["finished_at"]) for participant in participants],
                )
            database.execute("DROP TABLE performance_participants")
            database.execute("DROP TABLE performances")
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