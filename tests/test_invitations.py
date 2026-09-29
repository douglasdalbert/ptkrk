import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.invitations import accept_invitation, expire_invitation, invitation_state, start_invitation
from app.main import accept_request, party_snapshot, remove_request
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

    def test_missed_first_moves_to_third_and_next_gets_full_deadline(self):
        with connection() as database:
            invite = start_invitation(database, 100)
            self.assertEqual(invite["deadline_ms"], 120000)
            self.assertEqual(expire_invitation(database, 119.9), invite)
            next_invite = expire_invitation(database, 120)
            self.assertEqual(self.order(database), ["B1", "C1", "A1", "A2"])
            self.assertEqual(next_invite["request_id"], "B1")
            self.assertEqual(next_invite["deadline_ms"], 140000)

    def test_third_miss_removes_request(self):
        with connection() as database:
            start_invitation(database, 100)
            for now in (120, 140, 160, 180, 200, 220, 240, 260, 280):
                expire_invitation(database, now)
                if database.execute("SELECT status FROM requests WHERE id = 'A1'").fetchone()[0] == "removed":
                    break
            self.assertEqual(database.execute("SELECT status FROM requests WHERE id = 'A1'").fetchone()[0], "removed")
            self.assertNotIn("A1", self.order(database))
            self.assertEqual(database.execute("SELECT total FROM missed_invitations WHERE request_id='A1'").fetchone()[0], 3)

    def test_only_owner_can_accept_once_before_deadline(self):
        with connection() as database:
            start_invitation(database, 100)
            self.assertFalse(accept_invitation(database, "A1", "B", 110))
            self.assertFalse(accept_invitation(database, "A1", "A", 120))
            self.assertTrue(accept_invitation(database, "A1", "A", 119.9))
            self.assertFalse(accept_invitation(database, "A1", "A", 119.9))
            self.assertEqual(expire_invitation(database, 200)["request_id"], "A1")
            self.assertTrue(invitation_state(database)["accepted"])
            self.assertEqual(database.execute("SELECT total FROM accepted_counts WHERE client_id = 'A'").fetchone()[0], 1)

    def test_no_invitation_is_started_without_explicit_activation(self):
        with connection() as database:
            self.assertIsNone(invitation_state(database))
            self.assertEqual(self.order(database), ["A1", "B1", "C1", "A2"])

    def test_first_position_must_finish_processing_before_invite(self):
        with connection() as database:
            database.execute("UPDATE requests SET status = 'processing' WHERE id = 'A1'")
            self.assertIsNone(start_invitation(database, 100))
            database.execute("UPDATE requests SET status = 'ready' WHERE id = 'A1'")
            self.assertEqual(start_invitation(database, 100)["request_id"], "A1")

    def test_deadline_survives_restart(self):
        with connection() as database:
            start_invitation(database, 100)
        initialize()
        with connection() as database:
            self.assertEqual(invitation_state(database)["deadline_ms"], 120000)
            self.assertEqual(expire_invitation(database, 120)["request_id"], "B1")

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
        self.assertEqual(party_snapshot()["invitation"]["deadline_ms"], 125000)
        self.assertEqual([row["position"] for row in party_snapshot()["items"]], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()