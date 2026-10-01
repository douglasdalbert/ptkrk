import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.main import remove_request, request_snapshot, retry_request
from app.queue import enqueue_request
from app.storage import connection, initialize


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path_patch = patch("app.storage.DATABASE_PATH", Path(self.directory.name) / "karaoke.sqlite3")
        self.path_patch.start()
        initialize()
        with connection() as database:
            for singer in ("A", "B", "C"):
                database.execute(
                    "INSERT INTO singers(id, name, session_hash) VALUES (?, ?, ?)",
                    (singer, singer, singer),
                )

    def tearDown(self):
        self.path_patch.stop()
        self.directory.cleanup()

    def add_ready(self, request_id):
        with connection() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "INSERT INTO requests(id, singer_id, video_id, status) VALUES (?, ?, ?, 'ready')",
                (request_id, request_id[0], "glvVYIhdWlU"),
            )
            enqueue_request(database, request_id)

    def order(self):
        return [row["id"] for row in request_snapshot() if row["status"] == "ready"]

    def seed_ab(self):
        for request_id in ("A1", "B1", "A2", "B2", "A3", "B3", "A4", "B4", "A5", "B5", "A6", "A7"):
            self.add_ready(request_id)

    def test_two_protected_and_new_b_between_a6_and_a7(self):
        self.seed_ab()
        self.add_ready("B6")
        order = self.order()
        self.assertEqual(order[:2], ["A1", "B1"])
        self.assertEqual(order[order.index("A6"):order.index("A7") + 1], ["A6", "B6", "A7"])
        self.assertEqual([row["position"] for row in request_snapshot()], list(range(1, 14)))

    def test_first_and_second_c_join_separate_rounds(self):
        for request_id in ("A1", "B1", "A2", "B2", "A3", "B3", "A4"):
            self.add_ready(request_id)
        self.add_ready("C1")
        self.assertEqual(self.order()[:7], ["A1", "B1", "C1", "A2", "B2", "A3", "B3"])
        self.add_ready("C2")
        self.assertEqual(self.order(), ["A1", "B1", "C1", "A2", "B2", "C2", "A3", "B3", "A4"])

    def test_fewer_accepted_wins_same_waiting_round(self):
        for request_id in ("A1", "B1", "A2", "B2", "A3"):
            self.add_ready(request_id)
        with connection() as database:
            database.execute("INSERT INTO accepted_counts(singer_id, total) VALUES ('A', 5)")
        self.add_ready("B3")
        self.assertEqual(self.order()[4:], ["B3", "A3"])

    def test_pending_not_given_queue_position(self):
        self.add_ready("A1")
        with connection() as database:
            database.execute(
                "INSERT INTO requests(id, singer_id, video_id) VALUES ('B1', 'B', 'glvVYIhdWlU')"
            )
        snapshot = request_snapshot()
        self.assertEqual([(row["id"], row["position"]) for row in snapshot], [("A1", 1), ("B1", None)])

    def test_initialize_positions_existing_ready_requests(self):
        with connection() as database:
            database.execute(
                "INSERT INTO requests(id, singer_id, video_id, status) "
                "VALUES ('A1', 'A', 'glvVYIhdWlU', 'ready')"
            )
        initialize()
        initialize()
        self.assertEqual([(row["id"], row["position"]) for row in request_snapshot()], [("A1", 1)])

    def test_owner_removes_ready_and_positions_compact(self):
        for request_id in ("A1", "B1", "A2"):
            self.add_ready(request_id)
        with self.assertRaises(HTTPException) as wrong_owner:
            remove_request("A1", "B")
        self.assertEqual(wrong_owner.exception.status_code, 404)
        remove_request("B1", "B")
        self.assertEqual([(row["id"], row["position"]) for row in request_snapshot()],
                         [("A1", 1), ("A2", 2)])
        with self.assertRaises(HTTPException) as repeated:
            remove_request("B1", "B")
        self.assertEqual(repeated.exception.status_code, 404)

    def test_owner_can_remove_failed_request(self):
        with connection() as database:
            database.execute(
                "INSERT INTO requests(id, singer_id, video_id, status) "
                "VALUES ('A1', 'A', 'glvVYIhdWlU', 'failed')"
            )
        remove_request("A1", "A")
        self.assertEqual(request_snapshot(), [])

    def test_owner_can_retry_failed_request_without_changing_id(self):
        with connection() as database:
            database.execute(
                "INSERT INTO requests(id, singer_id, video_id, status, error) "
                "VALUES ('A1', 'A', 'glvVYIhdWlU', 'failed', 'Falha')"
            )
        self.assertEqual(retry_request("A1", "A"), {"status": "pending"})
        item = next(item for item in request_snapshot() if item["id"] == "A1")
        self.assertEqual(item["status"], "pending")
        self.assertIsNone(item["error"])
        with self.assertRaises(HTTPException) as wrong_owner:
            retry_request("A1", "B")
        self.assertEqual(wrong_owner.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()