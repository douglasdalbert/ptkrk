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


if __name__ == "__main__":
    unittest.main()