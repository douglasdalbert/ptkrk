import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.main import request_preview, youtube_id
from app.media import create_preview
from app.storage import connection, initialize
from app.worker import process_next


class YoutubeUrlTests(unittest.TestCase):
    def test_valid_video_links(self):
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?t=3",
        ):
            with self.subTest(url=url):
                self.assertEqual(youtube_id(url), "dQw4w9WgXcQ")

    def test_rejects_other_hosts_and_malformed_ports(self):
        for url in (
            "http://youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com.evil.test/watch?v=dQw4w9WgXcQ",
            "https://youtube.com:invalid/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/playlist?list=dQw4w9WgXcQ",
            "https://127.0.0.1/watch?v=dQw4w9WgXcQ",
        ):
            with self.subTest(url=url), self.assertRaises(HTTPException):
                youtube_id(url)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "karaoke.sqlite3"
        self.path_patch = patch("app.storage.DATABASE_PATH", self.database_path)
        self.path_patch.start()
        initialize()
        with connection() as database:
            database.execute(
                "INSERT INTO clients (id, name, session_hash) VALUES (?, ?, ?)",
                ("client", "Cantor", "session"),
            )
            database.execute(
                "INSERT INTO requests (id, client_id, video_id) VALUES (?, ?, ?)",
                ("request", "client", "dQw4w9WgXcQ"),
            )

    def tearDown(self):
        self.path_patch.stop()
        self.directory.cleanup()

    def test_success_marks_request_ready(self):
        with patch("app.worker.download_video", return_value="Minha música") as download, patch(
            "app.worker.create_preview"
        ) as preview:
            self.assertTrue(process_next())
        download.assert_called_once_with("dQw4w9WgXcQ")
        preview.assert_called_once_with("dQw4w9WgXcQ")
        with connection() as database:
            row = database.execute("SELECT status, title FROM requests WHERE id = 'request'").fetchone()
        self.assertEqual((row["status"], row["title"]), ("ready", "Minha música"))
        self.assertFalse(process_next())

    def test_failure_marks_request_failed(self):
        with patch("app.worker.download_video", side_effect=ValueError("Falha")):
            self.assertTrue(process_next())
        with connection() as database:
            row = database.execute("SELECT status, error FROM requests WHERE id = 'request'").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["error"])

    def test_preview_requires_ready_request(self):
        with patch("app.main.MEDIA_ROOT", Path(self.directory.name)):
            with self.assertRaises(HTTPException) as missing:
                request_preview("request", "client")
            self.assertEqual(missing.exception.status_code, 404)
            preview = Path(self.directory.name) / "previews" / "dQw4w9WgXcQ.jpg"
            preview.parent.mkdir()
            preview.write_bytes(b"preview")
            with connection() as database:
                database.execute("UPDATE requests SET status = 'ready' WHERE id = 'request'")
            self.assertEqual(request_preview("request", "client").path, preview)


class PreviewTests(unittest.TestCase):
    def test_ffmpeg_creates_jpeg_from_video(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "videos" / "dQw4w9WgXcQ.mp4"
            video.parent.mkdir()
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "color=c=green:s=320x180:d=2", "-an", "-c:v", "mpeg4", str(video)],
                check=True,
                timeout=30,
            )
            with patch("app.media.MEDIA_ROOT", Path(directory)):
                create_preview("dQw4w9WgXcQ")
            preview = Path(directory) / "previews" / "dQw4w9WgXcQ.jpg"
            self.assertTrue(preview.read_bytes().startswith(b"\xff\xd8\xff"))


if __name__ == "__main__":
    unittest.main()