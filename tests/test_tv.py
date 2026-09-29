import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.queue import enqueue_request
from app.storage import connection
from app.worker import process_next


class TvTests(unittest.TestCase):
    tv_headers = {"X-Karaoke-TV": "local", "Origin": "http://localhost:8001"}
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.database_patch = patch("app.storage.DATABASE_PATH", root / "karaoke.sqlite3")
        self.media_patch = patch("app.main.MEDIA_ROOT", root)
        self.environment = patch.dict("os.environ", {"KARAOKE_TV_LOCAL": "true", "KARAOKE_LAN_IP": "192.168.68.108"})
        self.database_patch.start()
        self.media_patch.start()
        self.environment.start()
        self.client = TestClient(app, base_url="http://localhost:8001")
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()
        self.media_patch.stop()
        self.database_patch.stop()
        self.directory.cleanup()

    def add_ready(self):
        with connection() as database:
            database.execute("INSERT INTO singers(id, name, session_hash) VALUES ('singer', 'Cantor', 'hash')")
            database.execute("INSERT INTO requests(id, singer_id, video_id, status) "
                             "VALUES ('song', 'singer', 'glvVYIhdWlU', 'ready')")
            enqueue_request(database, "song")

    def test_only_local_tv_can_start_and_serve_video(self):
        self.add_ready()
        with connection() as database:
            generation = database.execute("SELECT generation FROM party WHERE id=1").fetchone()[0]
        video = Path(self.directory.name) / generation / "videos" / "glvVYIhdWlU.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"sample mp4")
        with patch.dict("os.environ", {"KARAOKE_TV_LOCAL": "false"}):
            self.assertEqual(self.client.post("/api/tv/start").status_code, 404)
            self.assertEqual(self.client.get("/tv").status_code, 404)
            self.assertEqual(self.client.get("/api/tv/song/video").status_code, 404)
        self.assertEqual(self.client.post("/api/tv/start").status_code, 403)
        self.assertEqual(self.client.post("/api/tv/start", headers=self.tv_headers).json()["invitation"]["request_id"], "song")
        self.assertEqual(self.client.get("/api/tv/song/video").content, b"sample mp4")
        partial = self.client.get("/api/tv/song/video", headers={"Range": "bytes=0-3"})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, b"samp")
        self.assertEqual(self.client.post("/api/tv/song/finish", headers=self.tv_headers).status_code, 409)
        with connection() as database:
            database.execute("UPDATE invitation SET accepted = 1 WHERE id = 1")
        self.assertEqual(self.client.post("/api/tv/song/finish", headers=self.tv_headers).json(), {"status": "played"})
        self.assertIsNone(self.client.get("/api/tv/state").json()["invitation"])
        self.assertEqual(self.client.get("/api/tv/song/video").status_code, 404)

    def test_reset_rotates_qr_and_invalidates_sessions_and_media(self):
        self.add_ready()
        old = self.client.get("/api/tv/state").json()["party"]
        self.assertEqual(self.client.get("/api/tv/join").json()["url"],
                         f"http://192.168.68.108:8000/cantor?party={old}")
        self.assertTrue(self.client.get("/api/tv/qr").content.startswith(b"\x89PNG"))
        media = Path(self.directory.name) / old / "videos" / "glvVYIhdWlU.mp4"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"old")
        self.assertEqual(self.client.post("/api/tv/reset").status_code, 403)
        new = self.client.post("/api/tv/reset", headers=self.tv_headers).json()["party"]
        self.assertNotEqual(new, old)
        self.assertFalse(media.exists())
        self.assertEqual(self.client.get("/api/tv/state").json()["items"], [])
        self.assertEqual(self.client.post("/api/singers", json={"name": "Old", "party": old}).status_code, 410)
        response = self.client.post("/api/singers", json={"name": "New", "party": new})
        self.assertEqual(response.status_code, 201)
        self.assertIn("singer_id", response.json())
        self.assertIn(new, self.client.get("/api/tv/join").json()["url"])

    def test_tv_websocket_opens_invitation_only_while_connected(self):
        self.add_ready()
        with self.client.websocket_connect("/ws/requests", headers={
            "Origin": "http://localhost:8001", "Host": "localhost:8001"
        }) as websocket:
            websocket.send_json({"token": ""})
            snapshot = websocket.receive_json()
            self.assertEqual(snapshot["invitation"]["request_id"], "song")

    def test_legacy_media_moves_into_current_party(self):
        legacy = Path(self.directory.name) / "videos" / "glvVYIhdWlU.mp4"
        legacy.parent.mkdir()
        legacy.write_bytes(b"existing video")
        from app.storage import initialize
        with patch("app.storage.MEDIA_ROOT", Path(self.directory.name)):
            initialize()
        generation = self.client.get("/api/tv/state").json()["party"]
        self.assertFalse(legacy.exists())
        self.assertEqual((Path(self.directory.name) / generation / "videos" / legacy.name).read_bytes(), b"existing video")

    def test_reset_during_download_does_not_restore_old_party(self):
        with connection() as database:
            generation = database.execute("SELECT generation FROM party WHERE id=1").fetchone()[0]
            database.execute("INSERT INTO singers(id,name,session_hash) VALUES ('A','A','hash')")
            database.execute("INSERT INTO requests(id,singer_id,video_id) VALUES ('A1','A','glvVYIhdWlU')")
            enqueue_request(database, "A1")

        def download(video_id, old_generation):
            self.assertEqual(old_generation, generation)
            self.client.post("/api/tv/reset", headers=self.tv_headers).raise_for_status()
            old_path = Path(self.directory.name) / old_generation / "videos" / f"{video_id}.mp4"
            old_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.write_bytes(b"late download")
            return "Old song"

        with patch("app.worker.MEDIA_ROOT", Path(self.directory.name)), patch(
            "app.worker.download_video", side_effect=download
        ), patch("app.worker.create_preview"):
            self.assertTrue(process_next())
        self.assertFalse((Path(self.directory.name) / generation).exists())
        self.assertEqual(self.client.get("/api/tv/state").json()["items"], [])


if __name__ == "__main__":
    unittest.main()