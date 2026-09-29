import tempfile
import unittest
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import ANY, patch

from fastapi import HTTPException

from app.main import NewRequest, create_request, remove_request, request_preview, request_snapshot, youtube_id
from app.media import create_preview
from app.queue import enqueue_request
from app.storage import connection, initialize
from app.worker import process_next, recover_missing_media


class YoutubeCodeTests(unittest.TestCase):
    def test_valid_video_code(self):
        self.assertEqual(youtube_id("glvVYIhdWlU"), "glvVYIhdWlU")

    def test_rejects_urls_and_invalid_codes(self):
        for code in (
            "",
            "short",
            "invalid.code",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "http://youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com.evil.test/watch?v=dQw4w9WgXcQ",
            "https://youtube.com:invalid/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/playlist?list=dQw4w9WgXcQ",
            "https://127.0.0.1/watch?v=dQw4w9WgXcQ",
        ):
            with self.subTest(code=code), self.assertRaises(HTTPException):
                youtube_id(code)


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
            enqueue_request(database, "request")

    def tearDown(self):
        self.path_patch.stop()
        self.directory.cleanup()

    def test_success_marks_request_ready(self):
        with patch("app.worker.download_video", return_value="Minha música") as download, patch(
            "app.worker.create_preview"
        ) as preview:
            self.assertTrue(process_next())
        download.assert_called_once_with("dQw4w9WgXcQ", ANY)
        preview.assert_called_once_with("dQw4w9WgXcQ", download.call_args.args[1])
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
            with connection() as database:
                generation = database.execute("SELECT generation FROM party WHERE id=1").fetchone()[0]
            preview = Path(self.directory.name) / generation / "previews" / "dQw4w9WgXcQ.jpg"
            preview.parent.mkdir(parents=True)
            preview.write_bytes(b"preview")
            with connection() as database:
                database.execute("UPDATE requests SET status = 'ready' WHERE id = 'request'")
            self.assertEqual(request_preview("request", "client").path, preview)

    def test_snapshot_reflects_worker_transition(self):
        self.assertEqual(request_snapshot()[0]["status"], "pending")
        with patch("app.worker.download_video", return_value="Minha música"), patch("app.worker.create_preview"):
            process_next()
        self.assertEqual(request_snapshot()[0]["status"], "ready")

    def test_request_persists_youtube_code(self):
        created = create_request(NewRequest(youtubeCode="glvVYIhdWlU"), "client")
        with connection() as database:
            row = database.execute("SELECT video_id FROM requests WHERE id = ?", (created["id"],)).fetchone()
        self.assertEqual(row["video_id"], "glvVYIhdWlU")

    def test_missing_ready_video_is_requeued_without_changing_position(self):
        with connection() as database:
            generation = database.execute("SELECT generation FROM party WHERE id=1").fetchone()[0]
            database.execute("UPDATE requests SET status='ready' WHERE id='request'")
            database.execute("INSERT INTO invitation(id, request_id, deadline) VALUES (1, 'request', 0)")
            with patch("app.worker.MEDIA_ROOT", Path(self.directory.name)):
                recover_missing_media(database, generation)
            self.assertEqual(database.execute("SELECT status FROM requests WHERE id='request'").fetchone()[0], "pending")
            self.assertEqual(database.execute("SELECT position FROM ready_queue WHERE request_id='request'").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM invitation").fetchone()[0], 0)

    def test_removal_during_download_cannot_restore_request(self):
        def cancel_during_download(video_id, generation):
            remove_request("request", "client")
            return "Vídeo terminado"

        with patch("app.worker.download_video", side_effect=cancel_during_download), patch("app.worker.create_preview"):
            self.assertTrue(process_next())
        with connection() as database:
            status = database.execute("SELECT status FROM requests WHERE id = 'request'").fetchone()["status"]
            queued = database.execute("SELECT COUNT(*) FROM ready_queue").fetchone()[0]
        self.assertEqual(status, "removed")
        self.assertEqual(queued, 0)
        self.assertEqual(request_snapshot(), [])

    def test_eleventh_waits_until_it_enters_first_ten(self):
        with connection() as database:
            database.execute("UPDATE requests SET status = 'ready' WHERE id = 'request'")
            for index in range(2, 12):
                request_id = f"request-{index}"
                database.execute(
                    "INSERT INTO requests(id, client_id, video_id, status) VALUES (?, 'client', ?, 'ready')",
                    (request_id, "glvVYIhdWlU"),
                )
                enqueue_request(database, request_id)
            database.execute("UPDATE requests SET status = 'pending' WHERE id = 'request-11'")
        with patch("app.worker.download_video") as download:
            self.assertFalse(process_next())
            download.assert_not_called()
        with connection() as database:
            database.execute("UPDATE requests SET status = 'removed' WHERE id = 'request-2'")
            database.execute("DELETE FROM ready_queue WHERE request_id = 'request-2'")
            for position, row in enumerate(database.execute(
                "SELECT request_id FROM ready_queue ORDER BY position"
            ).fetchall(), start=1):
                database.execute("UPDATE ready_queue SET position = ? WHERE request_id = ?", (position, row[0]))
        with patch("app.worker.download_video", return_value="Nova música") as download, patch(
            "app.worker.create_preview"
        ):
            self.assertTrue(process_next())
            download.assert_called_once_with("glvVYIhdWlU", ANY)
        with connection() as database:
            status = database.execute("SELECT status FROM requests WHERE id = 'request-11'").fetchone()[0]
        self.assertEqual(status, "ready")

    def test_promoted_request_starts_without_canceling_in_progress_download(self):
        started = Event()
        finish = Event()
        with connection() as database:
            database.execute("UPDATE requests SET status = 'ready' WHERE id = 'request'")
            for index in range(2, 10):
                request_id = f"A{index}"
                database.execute(
                    "INSERT INTO requests(id, client_id, video_id, status) VALUES (?, 'client', ?, 'ready')",
                    (request_id, "glvVYIhdWlU"),
                )
                enqueue_request(database, request_id)
            database.execute("INSERT INTO requests(id, client_id, video_id) VALUES ('A10', 'client', 'abcdefghijk')")
            enqueue_request(database, "A10")

        def download(video_id, generation):
            if video_id == "abcdefghijk":
                started.set()
                if not finish.wait(5):
                    raise TimeoutError("Primeiro preparo não foi liberado")
            return video_id

        try:
            with patch("app.worker.download_video", side_effect=download), patch("app.worker.create_preview"):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(process_next)
                    self.assertTrue(started.wait(3))
                    with connection() as database:
                        database.execute("INSERT INTO clients(id,name,session_hash) VALUES ('C','C','C')")
                        database.execute(
                            "INSERT INTO requests(id, client_id, video_id) VALUES ('C1', 'C', 'lmnopqrstuv')"
                        )
                        enqueue_request(database, "C1")
                        displaced = database.execute(
                            "SELECT position FROM ready_queue WHERE request_id='A10'"
                        ).fetchone()[0]
                    self.assertEqual(displaced, 11)
                    self.assertTrue(executor.submit(process_next).result(timeout=3))
                    with connection() as database:
                        self.assertEqual(database.execute(
                            "SELECT status FROM requests WHERE id='C1'"
                        ).fetchone()[0], "ready")
                    finish.set()
                    self.assertTrue(first.result(timeout=3))
        finally:
            finish.set()


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