import sqlite3

SKIP_SECONDS = 5


def invitation_state(database: sqlite3.Connection) -> dict | None:
    row = database.execute(
        "SELECT request_id, deadline, accepted FROM invitation WHERE id = 1"
    ).fetchone()
    return {"request_id": row["request_id"], "deadline_ms": round(row["deadline"] * 1000),
            "accepted": bool(row["accepted"])} if row else None


def start_invitation(database: sqlite3.Connection, now: float, exclude_request_id: str | None = None) -> dict | None:
    current = invitation_state(database)
    if current:
        return current
    first = database.execute(
        """SELECT ready_queue.request_id FROM ready_queue
           JOIN requests ON requests.id = ready_queue.request_id
                     WHERE ready_queue.position <= 4 AND requests.status = 'ready'
                           AND (? IS NULL OR ready_queue.request_id != ?)
                     ORDER BY ready_queue.position LIMIT 1""",
                       (exclude_request_id, exclude_request_id),
    ).fetchone()
    if first is None:
        return None
    database.execute(
        "INSERT INTO invitation (id, request_id, deadline) VALUES (1, ?, ?)",
        (first["request_id"], 0),
    )
    return invitation_state(database)


def skip_state(database: sqlite3.Connection) -> dict | None:
    row = database.execute("SELECT request_id, deadline FROM skip_request WHERE id = 1").fetchone()
    return {"request_id": row["request_id"], "deadline_ms": round(row["deadline"] * 1000)} if row else None


def schedule_skip(database: sqlite3.Connection, request_id: str, now: float) -> dict | None:
    current = invitation_state(database)
    if current is None or current["request_id"] != request_id or skip_state(database):
        return None
    database.execute(
        "INSERT INTO skip_request(id, request_id, deadline) VALUES (1, ?, ?)",
        (request_id, now + SKIP_SECONDS),
    )
    return skip_state(database)


def apply_skip(database: sqlite3.Connection, now: float) -> dict | None:
    skipping = skip_state(database)
    if skipping is None or now < skipping["deadline_ms"] / 1000:
        return skipping
    request_id = skipping["request_id"]
    current = invitation_state(database)
    rows = database.execute(
        "SELECT request_id FROM ready_queue ORDER BY position"
    ).fetchall()
    remaining = [row["request_id"] for row in rows if row["request_id"] != request_id]
    if current and current["accepted"]:
        database.execute("UPDATE requests SET status = 'skipped' WHERE id = ?", (request_id,))
        database.execute("DELETE FROM ready_queue WHERE request_id = ?", (request_id,))
    else:
        previous_position = next(index for index, row in enumerate(rows) if row["request_id"] == request_id)
        remaining.insert(min(max(2, previous_position), len(remaining)), request_id)
    for position, queued_id in enumerate(remaining, start=1):
        database.execute("UPDATE ready_queue SET position = ? WHERE request_id = ?", (position, queued_id))
    database.execute("DELETE FROM skip_request WHERE id = 1")
    database.execute("DELETE FROM invitation WHERE id = 1")
    next_ready = database.execute(
        """SELECT ready_queue.request_id FROM ready_queue JOIN requests ON requests.id = ready_queue.request_id
           WHERE ready_queue.position <= 4 AND requests.status = 'ready'
             AND ready_queue.request_id != ? ORDER BY ready_queue.position LIMIT 1""",
        (request_id,),
    ).fetchone()
    if next_ready:
        start_invitation(database, now, exclude_request_id=request_id)
    return None


def accept_invitation(database: sqlite3.Connection, request_id: str, client_id: str, now: float) -> bool:
    current = invitation_state(database)
    if current is None or current["request_id"] != request_id or current["accepted"]:
        return False
    if skip_state(database):
        return False
    owner = database.execute("SELECT client_id FROM requests WHERE id = ?", (request_id,)).fetchone()
    if owner is None or owner["client_id"] != client_id:
        return False
    database.execute("UPDATE invitation SET accepted = 1 WHERE id = 1")
    database.execute(
        """INSERT INTO accepted_counts(client_id, total) VALUES (?, 1)
           ON CONFLICT(client_id) DO UPDATE SET total = total + 1""",
        (client_id,),
    )
    return True