import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.storage import connection, initialize


class SingerMigrationTests(unittest.TestCase):
    def test_existing_party_keeps_singers_requests_and_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "karaoke.sqlite3"
            legacy = sqlite3.connect(database_path)
            legacy.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE clients (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, session_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE requests (
                    id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id),
                    video_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    title TEXT, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE accepted_counts (
                    client_id TEXT PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE,
                    total INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO clients(id, name, session_hash) VALUES ('singer-1', 'Cantor', 'saved-token-hash');
                INSERT INTO requests(id, client_id, video_id, status)
                    VALUES ('request-1', 'singer-1', 'glvVYIhdWlU', 'ready');
                INSERT INTO accepted_counts(client_id, total) VALUES ('singer-1', 3);
                """
            )
            legacy.close()
            with patch("app.storage.DATABASE_PATH", database_path):
                initialize()
                initialize()
                with connection() as database:
                    singer = database.execute("SELECT id, session_hash FROM singers").fetchone()
                    request = database.execute("SELECT singer_id FROM requests").fetchone()
                    count = database.execute("SELECT singer_id, total FROM accepted_counts").fetchone()
                    self.assertEqual(tuple(singer), ("singer-1", "saved-token-hash"))
                    self.assertEqual(request[0], singer[0])
                    self.assertEqual(tuple(count), (singer[0], 3))
                    self.assertEqual(database.execute("SELECT COUNT(*) FROM backvocals").fetchone()[0], 0)
                    self.assertEqual(database.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertIsNone(database.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='clients'"
                    ).fetchone())

    def test_performance_records_migrate_to_one_score_history_row_per_singer(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "karaoke.sqlite3"
            with patch("app.storage.DATABASE_PATH", database_path):
                initialize()
                with connection() as database:
                    database.execute(
                        "INSERT INTO singers(id,name,session_hash) VALUES "
                        "('A','A','hash-a'), ('B','B','hash-b')"
                    )
                    database.execute(
                        "INSERT INTO requests(id,singer_id,video_id,status,title) "
                        "VALUES ('AB','A','abcdefghijk','played','Dueto')"
                    )
                    database.executescript(
                        """
                        CREATE TABLE performances (
                            request_id TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
                            title TEXT NOT NULL, video_id TEXT NOT NULL, points REAL NOT NULL,
                            preview BLOB, finished_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                        );
                        CREATE TABLE performance_participants (
                            request_id TEXT NOT NULL REFERENCES performances(request_id) ON DELETE CASCADE,
                            singer_id TEXT NOT NULL REFERENCES singers(id) ON DELETE CASCADE,
                            PRIMARY KEY (request_id, singer_id)
                        );
                        INSERT INTO performances(request_id,title,video_id,points,preview)
                            VALUES ('AB','Dueto','abcdefghijk',800,X'6A706567');
                        INSERT INTO performance_participants(request_id,singer_id)
                            VALUES ('AB','A'), ('AB','B');
                        """
                    )

                initialize()
                with connection() as database:
                    records = database.execute(
                        "SELECT singer_id, title, points, preview, participants FROM score_history ORDER BY rowid"
                    ).fetchall()
                    self.assertEqual(
                        [(row["singer_id"], row["title"], row["points"], row["preview"]) for row in records],
                        [("A", "Dueto", 800, b"jpeg"), ("B", "Dueto", 800, b"jpeg")],
                    )
                    self.assertEqual(
                        database.execute(
                            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                            "AND name IN ('performances', 'performance_participants')"
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(database.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()