import sqlite3

INVITATION_SECONDS = 20


def invitation_state(database: sqlite3.Connection) -> dict | None:
    row = database.execute(
        "SELECT request_id, deadline, accepted FROM invitation WHERE id = 1"
    ).fetchone()
    return {"request_id": row["request_id"], "deadline_ms": round(row["deadline"] * 1000),
            "accepted": bool(row["accepted"])} if row else None


def start_invitation(database: sqlite3.Connection, now: float) -> dict | None:
    current = invitation_state(database)
    if current:
        return current
    first = database.execute(
        """SELECT ready_queue.request_id, requests.status FROM ready_queue
           JOIN requests ON requests.id = ready_queue.request_id
           ORDER BY ready_queue.position LIMIT 1"""
    ).fetchone()
    if first is None or first["status"] != "ready":
        return None
    database.execute(
        "INSERT INTO invitation (id, request_id, deadline) VALUES (1, ?, ?)",
        (first["request_id"], now + INVITATION_SECONDS),
    )
    return invitation_state(database)


def expire_invitation(database: sqlite3.Connection, now: float) -> dict | None:
    current = invitation_state(database)
    if current is None or current["accepted"] or now < current["deadline_ms"] / 1000:
        return current
    request_id = current["request_id"]
    database.execute(
        """INSERT INTO missed_invitations (request_id, total) VALUES (?, 1)
           ON CONFLICT(request_id) DO UPDATE SET total = total + 1""",
        (request_id,),
    )
    misses = database.execute(
        "SELECT total FROM missed_invitations WHERE request_id = ?", (request_id,)
    ).fetchone()["total"]
    rows = database.execute(
        "SELECT request_id FROM ready_queue ORDER BY position"
    ).fetchall()
    remaining = [row["request_id"] for row in rows if row["request_id"] != request_id]
    if misses >= 3:
        database.execute("UPDATE requests SET status = 'removed' WHERE id = ?", (request_id,))
        database.execute("DELETE FROM ready_queue WHERE request_id = ?", (request_id,))
    else:
        remaining.insert(min(2, len(remaining)), request_id)
    for position, queued_id in enumerate(remaining, start=1):
        database.execute("UPDATE ready_queue SET position = ? WHERE request_id = ?", (position, queued_id))
    database.execute("DELETE FROM invitation WHERE id = 1")
    return start_invitation(database, now)


def accept_invitation(database: sqlite3.Connection, request_id: str, client_id: str, now: float) -> bool:
    current = invitation_state(database)
    if current is None or current["request_id"] != request_id or current["accepted"]:
        return False
    if now >= current["deadline_ms"] / 1000:
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