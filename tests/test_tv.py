import tempfile
import time
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.invitations import start_invitation
import app.main as main_module
from app.main import app
from app.queue import enqueue_request
from app.storage import connection, save_playback_sync
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

    def test_tv_adjusts_microphone_noise_for_singers_and_reset_restores_default(self):
        with patch.dict("os.environ", {"KARAOKE_SCORE_RMS_THRESHOLD": "0.15"}):
            self.assertEqual(self.client.get("/api/tv/state").json()["microphone_rms_threshold"], 0.15)
            self.assertEqual(self.client.post("/api/tv/noise", json={"percent": 14}).status_code, 403)
            response = self.client.post("/api/tv/noise", headers=self.tv_headers, json={"percent": 14})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["microphone_rms_threshold"], 0.14)
            self.assertEqual(self.client.get("/api/tv/state").json()["microphone_rms_threshold"], 0.14)
            self.assertEqual(main_module.party_snapshot()["microphone_rms_threshold"], 0.14)
            for percent in (0, 51, 14.5):
                self.assertEqual(self.client.post("/api/tv/noise", headers=self.tv_headers,
                                                  json={"percent": percent}).status_code, 422)
            self.assertEqual(self.client.get("/api/tv/state").json()["microphone_rms_threshold"], 0.14)
            self.client.post("/api/tv/reset", headers=self.tv_headers)
            self.assertEqual(self.client.get("/api/tv/state").json()["microphone_rms_threshold"], 0.15)

    def test_tv_state_exposes_configured_nice_star_threshold(self):
        with patch.dict("os.environ", {"KARAOKE_STAR_NICE_PERCENT": "90.1"}):
            self.assertEqual(self.client.get("/api/tv/state").json()["star_nice_percent"], 90.1)

    def test_only_local_tv_can_start_and_serve_video(self):
        self.add_ready()
        with connection() as database:
            generation = database.execute("SELECT generation FROM party WHERE id=1").fetchone()[0]
        video = Path(self.directory.name) / generation / "videos" / "glvVYIhdWlU.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"sample mp4")
        captions = Path(self.directory.name) / generation / "captions" / "glvVYIhdWlU.json"
        captions.parent.mkdir(parents=True)
        captions.write_text('{"bars":[{"text":"Trecho","start_ms":0,"end_ms":1000}]}', encoding="utf-8")
        with patch.dict("os.environ", {"KARAOKE_TV_LOCAL": "false"}):
            self.assertEqual(self.client.post("/api/tv/start").status_code, 404)
            self.assertEqual(self.client.get("/tv").status_code, 404)
            self.assertEqual(self.client.get("/api/tv/song/video").status_code, 404)
            self.assertEqual(self.client.get("/api/tv/song/captions").status_code, 404)
        self.assertEqual(self.client.post("/api/tv/start").status_code, 403)
        self.assertEqual(self.client.post("/api/tv/start", headers=self.tv_headers).json()["invitation"]["request_id"], "song")
        self.assertEqual(self.client.get("/api/tv/song/video").content, b"sample mp4")
        self.assertEqual(self.client.get("/api/tv/song/captions").json()["bars"][0]["text"], "Trecho")
        partial = self.client.get("/api/tv/song/video", headers={"Range": "bytes=0-3"})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, b"samp")
        self.assertEqual(self.client.post("/api/tv/song/finish", headers=self.tv_headers).status_code, 409)
        with connection() as database:
            database.execute("UPDATE invitation SET accepted = 1 WHERE id = 1")
        self.assertEqual(self.client.post("/api/tv/song/finish", headers=self.tv_headers).json(), {"status": "played"})
        self.assertIsNone(self.client.get("/api/tv/state").json()["invitation"])
        self.assertEqual(self.client.get("/api/tv/song/video").status_code, 404)

    def test_websocket_scores_caption_onset_and_penalizes_sound_outside_cues(self):
        identity = self.client.post("/api/singers", json={"name": "Cantor do teste"}).json()
        with connection() as database:
            generation = database.execute("SELECT generation FROM party WHERE id=1").fetchone()[0]
            database.execute(
                "INSERT INTO requests(id,singer_id,video_id,status) VALUES "
                "('score-song',?,'1pLrF_rGLyg','ready')", (identity["singer_id"],)
            )
            enqueue_request(database, "score-song")
            database.execute(
                "INSERT INTO invitation(id,request_id,deadline,accepted,lead_accepted) "
                "VALUES (1,'score-song',0,1,1)"
            )
        captions = Path(self.directory.name) / generation / "captions" / "1pLrF_rGLyg.json"
        captions.parent.mkdir(parents=True)
        captions.write_text(json.dumps({"duration_ms": 10000, "bars": [
            {"block_index": 0, "start_ms": 1000, "end_ms": 2000,
             "score_window_start_ms": 900, "score_window_end_ms": 1100},
            {"block_index": 1, "start_ms": 5000, "end_ms": 6000,
             "score_window_start_ms": 4900, "score_window_end_ms": 5100},
        ]}), encoding="utf-8")

        with patch.dict("os.environ", {"KARAOKE_TV_LOCAL": "true"}):
            with self.client.websocket_connect("/ws/requests", headers={
                "Origin": "http://localhost:8001", "Host": "localhost:8001"
            }) as tv_socket:
                tv_socket.send_json({"token": ""})
                tv_socket.receive_json()
                with patch.dict("os.environ", {"KARAOKE_TV_LOCAL": "false"}):
                    with self.client.websocket_connect("/ws/requests") as singer_socket:
                        singer_socket.send_json({"token": identity["token"]})
                        singer_snapshot = singer_socket.receive_json()
                        self.assertNotIn("active_bars", singer_snapshot)
                        main_module.playback_sync = {
                            "request_id": "score-song", "position_ms": 1000,
                            "playing": False, "server_time": time.time(),
                        }
                        save_playback_sync(main_module.playback_sync)
                        main_module.playback_sync = None
                        singer_socket.send_json({
                            "type": "microphone_activity", "request_id": "score-song",
                            "event_id": "test-speaking", "speaking": True,
                            "position_ms": 950, "state_since_ms": 950,
                        })
                        speaking = tv_socket.receive_json()
                        self.assertEqual((speaking["type"], speaking["speaking"]), ("vocal_activity", True))
                        self.assertEqual((speaking["position_ms"], speaking["state_since_ms"]), (950, 950))
                        singer_socket.send_json({
                            "type": "microphone_activity", "request_id": "score-song",
                            "event_id": "test-silence", "speaking": False,
                            "position_ms": 1400, "state_since_ms": 1100,
                        })
                        silence = tv_socket.receive_json()
                        self.assertEqual((silence["type"], silence["speaking"]), ("vocal_activity", False))
                        self.assertEqual(silence["state_since_ms"], 1100)
                        tv_socket.send_json({
                            "type": "block_result", "request_id": "score-song",
                            "event_id": "test-hit", "singer_id": identity["singer_id"],
                            "block_index": 0, "hit": True,
                        })
                        hit = tv_socket.receive_json()
                        self.assertEqual((hit["type"], hit["result"], hit["block_index"]),
                                         ("score_update", "hit", 0))
                        self.assertEqual((hit["result"], hit["hits"]), ("hit", 1))
                        self.assertEqual(hit["points"], 500.0)
                        tv_socket.send_json({
                            "type": "block_result", "request_id": "score-song",
                            "event_id": "test-miss", "singer_id": identity["singer_id"],
                            "block_index": 1, "hit": False,
                        })
                        miss = tv_socket.receive_json()
                        self.assertEqual((miss["type"], miss["result"], miss["block_index"]),
                                         ("score_update", "miss", 1))
                        tv_socket.send_json({
                            "type": "offcue_penalty", "request_id": "score-song",
                            "event_id": "test-offcue-penalty", "singer_id": identity["singer_id"],
                            "position_ms": 3000,
                        })
                        penalty = tv_socket.receive_json()
                        self.assertEqual((penalty["type"], penalty["result"]),
                                         ("score_update", "off_cue"))
                        self.assertEqual(penalty["penalty_value"], 1)

        with connection() as database:
            row = database.execute(
                "SELECT hit_blocks,penalties FROM song_scores WHERE request_id='score-song'"
            ).fetchone()
        self.assertEqual(json.loads(row["hit_blocks"]), [0])
        self.assertEqual(row["penalties"], 1)

    def test_reset_keeps_join_url_but_invalidates_sessions_and_media(self):
        self.add_ready()
        old = self.client.get("/api/tv/state").json()["party"]
        self.assertEqual(self.client.get("/api/tv/join").json()["url"],
                         "https://192.168.68.108:8000/cantor")
        old_qr = self.client.get("/api/tv/qr").content
        self.assertTrue(old_qr.startswith(b"\x89PNG"))
        previous_singer = self.client.post("/api/singers", json={"name": "Old"})
        self.assertEqual(previous_singer.status_code, 201)
        self.assertEqual(previous_singer.json()["party"], old)
        old_token = previous_singer.json()["token"]
        media = Path(self.directory.name) / old / "videos" / "glvVYIhdWlU.mp4"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"old")
        self.assertEqual(self.client.post("/api/tv/reset").status_code, 403)
        new = self.client.post("/api/tv/reset", headers=self.tv_headers).json()["party"]
        self.assertNotEqual(new, old)
        self.assertFalse(media.exists())
        self.assertEqual(self.client.get("/api/tv/state").json()["items"], [])
        expired = self.client.get("/api/requests", headers={"Authorization": f"Bearer {old_token}"})
        self.assertEqual(expired.status_code, 401)
        self.assertEqual(expired.headers["X-Karaoke-Party"], new)
        response = self.client.post("/api/singers", json={"name": "New"})
        self.assertEqual(response.status_code, 201)
        self.assertIn("singer_id", response.json())
        self.assertEqual(response.json()["party"], new)
        self.assertEqual(response.headers["X-Karaoke-Party"], new)
        self.assertEqual(self.client.get("/api/requests", headers={
            "Authorization": f"Bearer {response.json()['token']}"
        }).headers["X-Karaoke-Party"], new)
        self.assertEqual(self.client.get("/api/tv/join").json()["url"], "https://192.168.68.108:8000/cantor")
        self.assertEqual(self.client.get("/api/tv/qr").content, old_qr)

    def test_reentering_same_name_restores_existing_request_list(self):
        first = self.client.post("/api/singers", json={"name": "  Cantora  Unica "}).json()
        first_headers = {"Authorization": f"Bearer {first['token']}"}
        created = self.client.post("/api/requests", json={"youtubeCode": "glvVYIhdWlU"}, headers=first_headers)
        self.assertEqual(created.status_code, 201)

        resumed = self.client.post("/api/singers", json={"name": "cantora unica"}).json()
        self.assertEqual(resumed["singer_id"], first["singer_id"])
        self.assertEqual(resumed["name"], first["name"])
        self.assertNotEqual(resumed["token"], first["token"])
        self.assertEqual(self.client.get("/api/requests", headers={
            "Authorization": f"Bearer {resumed['token']}"
        }).json()[0]["id"], created.json()["id"])
        self.assertEqual(self.client.get("/api/requests", headers=first_headers).status_code, 401)

    def test_same_name_recovery_consolidates_legacy_profiles(self):
        primary = self.client.post("/api/singers", json={"name": "Cantor"}).json()
        with connection() as database:
            database.execute("INSERT INTO singers(id, name, session_hash) VALUES ('duplicate', ' cantor ', 'old-hash')")
            database.execute("INSERT INTO requests(id, singer_id, video_id) VALUES ('legacy-song', 'duplicate', 'glvVYIhdWlU')")
            enqueue_request(database, "legacy-song")
            database.execute("INSERT INTO accepted_counts(singer_id, total) VALUES (?, 2)", (primary["singer_id"],))
            database.execute("INSERT INTO accepted_counts(singer_id, total) VALUES ('duplicate', 3)")
        response = self.client.post("/api/singers", json={"name": "CANTOR"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["singer_id"], primary["singer_id"])
        requests = self.client.get("/api/requests", headers={
            "Authorization": f"Bearer {response.json()['token']}"
        }).json()
        self.assertIn("legacy-song", {request["id"] for request in requests})
        with connection() as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM singers WHERE lower(trim(name)) = 'cantor'").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT singer_id FROM requests WHERE id = 'legacy-song'").fetchone()[0],
                             primary["singer_id"])
            self.assertEqual(database.execute("SELECT total FROM accepted_counts WHERE singer_id = ?",
                                              (primary["singer_id"],)).fetchone()[0], 5)

    def test_tv_websocket_opens_invitation_only_while_connected(self):
        self.add_ready()
        with self.client.websocket_connect("/ws/requests", headers={
            "Origin": "http://localhost:8001", "Host": "localhost:8001"
        }) as websocket:
            websocket.send_json({"token": ""})
            snapshot = websocket.receive_json()
            self.assertEqual(snapshot["invitation"]["request_id"], "song")

    def test_group_responses_are_published_to_the_lead_singer_websocket(self):
        lead = self.client.post("/api/singers", json={"name": "Cantor"}).json()
        guests = [self.client.post("/api/singers", json={"name": name}).json()
                  for name in ("Convidada", "Convidado")]
        with connection() as database:
            database.execute("INSERT INTO requests(id, singer_id, video_id, status) "
                             "VALUES ('song', ?, 'glvVYIhdWlU', 'ready')", (lead["singer_id"],))
            enqueue_request(database, "song")
            start_invitation(database, time.time())
        lead_headers = {"Authorization": f"Bearer {lead['token']}"}
        with patch.dict("os.environ", {"KARAOKE_TV_LOCAL": "false"}):
            with self.client.websocket_connect("/ws/requests") as lead_socket:
                lead_socket.send_json({"token": lead["token"]})
                lead_socket.receive_json()
                opened = self.client.post("/api/requests/song/group/open", headers=lead_headers)
                self.assertEqual(opened.status_code, 200)
                lead_socket.send_json({"type": "join_group", "request_id": "song"})
                initial_group = lead_socket.receive_json()
                self.assertEqual(initial_group["type"], "group_state")
                self.assertEqual(initial_group["video_id"], "glvVYIhdWlU")

                def receive_response(expected_singer_id, expected_joined):
                    for _ in range(4):
                        message = lead_socket.receive_json()
                        if message.get("type") != "group_state":
                            continue
                        member = next((entry for entry in message["members"]
                                       if entry["singer_id"] == expected_singer_id), None)
                        if member and member["joined"] == expected_joined:
                            return message
                    self.fail("The group room did not publish the expected response")

                for guest, accepted in zip(guests, (False, True)):
                    with self.client.websocket_connect("/ws/requests") as guest_socket:
                        guest_socket.send_json({"token": guest["token"]})
                        guest_socket.receive_json()
                        guest_headers = {"Authorization": f"Bearer {guest['token']}"}
                        invited = self.client.post("/api/requests/song/invite/" + guest["singer_id"],
                                                   headers=lead_headers)
                        self.assertEqual(invited.status_code, 202)
                        receive_response(guest["singer_id"], 0)
                        response = self.client.post(
                            f"/api/requests/song/invite/respond?accepted={str(accepted).lower()}",
                            headers=guest_headers,
                        )
                        self.assertEqual(response.status_code, 200, response.text)
                        updated = receive_response(guest["singer_id"], 1 if accepted else -1)
                        vocal = next(member for member in updated["members"]
                                     if member["singer_id"] == guest["singer_id"])
                        self.assertEqual(vocal["joined"], 1 if accepted else -1)
                        persisted = self.client.get("/api/requests/song/group/state", headers=lead_headers)
                        self.assertEqual(persisted.json()["members"], updated["members"])
                        self.assertEqual(self.client.get("/api/requests/song/group/state", headers=guest_headers).status_code,
                                         404)

    def test_reset_disconnects_singer_websocket(self):
        response = self.client.post("/api/singers", json={"name": "Cantor"})
        token = response.json()["token"]
        with patch.dict("os.environ", {"KARAOKE_TV_LOCAL": "false"}):
            with self.client.websocket_connect("/ws/requests") as websocket:
                websocket.send_json({"token": token})
                self.assertEqual(websocket.receive_json()["party"], response.json()["party"])
                with patch.dict("os.environ", {"KARAOKE_TV_LOCAL": "true"}):
                    self.assertEqual(self.client.post("/api/tv/reset", headers=self.tv_headers).status_code, 200)
                with self.assertRaises(WebSocketDisconnect) as closed:
                    websocket.receive_json()
                self.assertEqual(closed.exception.code, 1008)

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
        ), patch("app.worker.create_preview"), patch("app.worker.create_caption_bars"):
            self.assertTrue(process_next())
        self.assertFalse((Path(self.directory.name) / generation).exists())
        self.assertEqual(self.client.get("/api/tv/state").json()["items"], [])


if __name__ == "__main__":
    unittest.main()