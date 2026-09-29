import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.invitations import accept_invitation, apply_skip, invitation_state, schedule_skip, skip_state, start_invitation
from app.main import accept_request, party_snapshot, remove_request, skip_request
from app.queue import enqueue_request
from app.storage import connection, initialize


class InvitationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path_patch = patch("app.storage.DATABASE_PATH", Path(self.directory.name) / "karaoke.sqlite3")
        self.path_patch.start()
        initialize()
        with connection() as database:
            for client_id in ("A", "B", "C"):
                database.execute("INSERT INTO clients(id, name, session_hash) VALUES (?, ?, ?)",
                                 (client_id, client_id, client_id))
            for request_id in ("A1", "B1", "C1", "A2"):
                database.execute("INSERT INTO requests(id, client_id, video_id, status) "
                                 "VALUES (?, ?, 'glvVYIhdWlU', 'ready')", (request_id, request_id[0]))
                enqueue_request(database, request_id)

    def tearDown(self):
        self.path_patch.stop()
        self.directory.cleanup()

    def order(self, database):
        return [row[0] for row in database.execute("SELECT request_id FROM ready_queue ORDER BY position")]

    def test_manual_skip_moves_to_third_only_after_five_seconds(self):
        with connection() as database:
            invite = start_invitation(database, 100)
            self.assertEqual(invite["deadline_ms"], 0)
            self.assertIsNone(apply_skip(database, 120))
            self.assertEqual(self.order(database)[0], "A1")
            self.assertEqual(schedule_skip(database, "A1", 120)["deadline_ms"], 125000)
            self.assertIsNotNone(apply_skip(database, 124.9))
            self.assertEqual(self.order(database)[0], "A1")
            self.assertIsNone(apply_skip(database, 125))
            self.assertEqual(self.order(database), ["B1", "C1", "A1", "A2"])
            self.assertEqual(invitation_state(database)["request_id"], "B1")

    def test_repeated_manual_skips_do_not_remove_a_song(self):
        with connection() as database:
            start_invitation(database, 100)
            for now in (100, 110, 120, 130, 140, 150, 160, 170, 180):
                current = invitation_state(database)
                self.assertIsNotNone(schedule_skip(database, current["request_id"], now))
                apply_skip(database, now + 5)
            self.assertEqual(database.execute("SELECT status FROM requests WHERE id = 'A1'").fetchone()[0], "ready")

    def test_skipping_accepted_song_ends_it_instead_of_reinviting(self):
        with connection() as database:
            start_invitation(database, 100)
            self.assertTrue(accept_invitation(database, "A1", "A", 101))
            schedule_skip(database, "A1", 102)
            apply_skip(database, 107)
            self.assertEqual(self.order(database), ["B1", "C1", "A2"])
            self.assertEqual(invitation_state(database)["request_id"], "B1")
            self.assertEqual(database.execute("SELECT status FROM requests WHERE id='A1'").fetchone()[0], "skipped")
            self.assertEqual(database.execute("SELECT total FROM accepted_counts WHERE client_id='A'").fetchone()[0], 1)
        self.assertNotIn("A1", [item["id"] for item in party_snapshot()["items"]])

    def test_only_owner_can_accept_once_without_auto_deadline(self):
        with connection() as database:
            start_invitation(database, 100)
            self.assertFalse(accept_invitation(database, "A1", "B", 110))
            self.assertTrue(accept_invitation(database, "A1", "A", 200))
            self.assertFalse(accept_invitation(database, "A1", "A", 200))
            self.assertTrue(invitation_state(database)["accepted"])
            self.assertEqual(database.execute("SELECT total FROM accepted_counts WHERE client_id = 'A'").fetchone()[0], 1)

    def test_no_invitation_is_started_without_explicit_activation(self):
        with connection() as database:
            self.assertIsNone(invitation_state(database))
            self.assertEqual(self.order(database), ["A1", "B1", "C1", "A2"])

    def test_first_ready_in_podium_is_invited_without_reordering(self):
        with connection() as database:
            database.execute("UPDATE requests SET status = 'processing' WHERE id = 'A1'")
            self.assertEqual(start_invitation(database, 100)["request_id"], "B1")
            self.assertEqual(self.order(database), ["A1", "B1", "C1", "A2"])

    def test_fifth_ready_cannot_jump_unprepared_podium(self):
        with connection() as database:
            database.execute("UPDATE requests SET status = 'processing' WHERE id IN ('A1', 'B1', 'C1', 'A2')")
            database.execute(
                "INSERT INTO requests(id, client_id, video_id, status) VALUES ('B2', 'B', 'glvVYIhdWlU', 'ready')"
            )
            from app.queue import enqueue_request
            enqueue_request(database, "B2")
            self.assertEqual(self.order(database)[:4], ["A1", "B1", "C1", "A2"])
            self.assertIsNone(start_invitation(database, 100))
            database.execute("UPDATE requests SET status = 'ready' WHERE id = 'C1'")
            self.assertEqual(start_invitation(database, 101)["request_id"], "C1")

    def test_deadline_survives_restart(self):
        with connection() as database:
            start_invitation(database, 100)
            schedule_skip(database, "A1", 101)
        initialize()
        with connection() as database:
            self.assertEqual(skip_state(database)["deadline_ms"], 106000)
            apply_skip(database, 106)
            self.assertEqual(invitation_state(database)["request_id"], "B1")

    def test_api_only_accepts_owner_once_and_publishes_state(self):
        with connection() as database:
            start_invitation(database, 100)
        self.assertEqual(party_snapshot()["invitation"]["request_id"], "A1")
        self.assertEqual(party_snapshot()["items"][0]["client_id"], "A")
        with patch("app.main.time.time", return_value=110):
            with self.assertRaises(HTTPException) as not_owner:
                accept_request("A1", "B")
            self.assertEqual(not_owner.exception.status_code, 409)
            self.assertEqual(accept_request("A1", "A"), {"status": "accepted"})
            with self.assertRaises(HTTPException):
                accept_request("A1", "A")
        self.assertTrue(party_snapshot()["invitation"]["accepted"])
        with self.assertRaises(HTTPException) as already_accepted:
            remove_request("A1", "A")
        self.assertEqual(already_accepted.exception.status_code, 409)

    def test_removing_active_unaccepted_request_invites_next(self):
        with connection() as database:
            start_invitation(database, 100)
        with patch("app.main.time.time", return_value=105):
            remove_request("A1", "A")
        self.assertEqual(party_snapshot()["invitation"]["request_id"], "B1")
        self.assertEqual(party_snapshot()["invitation"]["deadline_ms"], 0)
        self.assertEqual([row["position"] for row in party_snapshot()["items"]], [1, 2, 3])

    def test_any_client_can_request_skip_unless_disabled(self):
        with connection() as database:
            start_invitation(database, 100)
        with patch("app.main.time.time", return_value=100):
            self.assertEqual(skip_request("A1", "C")["deadline_ms"], 105000)
            with self.assertRaises(HTTPException) as duplicate:
                skip_request("A1", "B")
            self.assertEqual(duplicate.exception.status_code, 409)
        with patch.dict("os.environ", {"KARAOKE_ALLOW_SKIP": "false"}):
            with self.assertRaises(HTTPException) as disabled:
                skip_request("A1", "C")
            self.assertEqual(disabled.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()