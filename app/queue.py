import sqlite3
from collections import Counter


def enqueue_request(database: sqlite3.Connection, request_id: str) -> None:
    rows = database.execute(
        """SELECT ready_queue.request_id, requests.client_id,
                  COALESCE(accepted_counts.total, 0) AS accepted
           FROM ready_queue
           JOIN requests ON requests.id = ready_queue.request_id
           LEFT JOIN accepted_counts ON accepted_counts.client_id = requests.client_id
           WHERE requests.status IN ('pending', 'processing', 'ready')
           ORDER BY ready_queue.position"""
    ).fetchall()
    protected = rows[:4]
    newcomer = database.execute(
        """SELECT requests.id AS request_id, requests.client_id,
                  COALESCE(accepted_counts.total, 0) AS accepted
           FROM requests LEFT JOIN accepted_counts ON accepted_counts.client_id = requests.client_id
           WHERE requests.id = ? AND requests.status IN ('pending', 'processing', 'ready')""",
        (request_id,),
    ).fetchone()
    if newcomer is None:
        raise ValueError("Pedido não está na fila")
    if any(row["request_id"] == request_id for row in rows):
        return
    protected_counts = Counter(row["client_id"] for row in protected)
    rounds = Counter()
    candidates = []
    for original_order, row in enumerate([*rows[4:], newcomer]):
        client_id = row["client_id"]
        candidates.append((rounds[client_id], row["accepted"] + protected_counts[client_id], original_order, row))
        rounds[client_id] += 1
    candidates.sort(key=lambda candidate: candidate[:3])
    for position, row in enumerate([*protected, *(candidate[3] for candidate in candidates)], start=1):
        database.execute(
            "INSERT INTO ready_queue (request_id, position) VALUES (?, ?) "
            "ON CONFLICT(request_id) DO UPDATE SET position = excluded.position",
            (row["request_id"], position),
        )