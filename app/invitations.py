import sqlite3

from app.media import remove_unused_media
from app.queue import PROTECTED_QUEUE_SIZE

SKIP_SECONDS = 5


def invitation_state(database: sqlite3.Connection) -> dict | None:
    row = database.execute(
        "SELECT request_id, deadline, accepted, lead_accepted FROM invitation WHERE id = 1"
    ).fetchone()
    return {"request_id": row["request_id"], "deadline_ms": round(row["deadline"] * 1000),
            "accepted": bool(row["accepted"]), "lead_accepted": bool(row["lead_accepted"]),
            "backvocals": [dict(vocal) for vocal in database.execute(
                "SELECT singer_id, accepted, joined, score_eligible FROM backvocals WHERE request_id = ? ORDER BY rowid",
                (row["request_id"],),
            )]} if row else None


def start_invitation(database: sqlite3.Connection, now: float, exclude_request_id: str | None = None) -> dict | None:
    current = invitation_state(database)
    if current:
        return current
    first = database.execute(
        """SELECT ready_queue.request_id FROM ready_queue
           JOIN requests ON requests.id = ready_queue.request_id
                                         WHERE ready_queue.position <= ? AND requests.status = 'ready'
                           AND (? IS NULL OR ready_queue.request_id != ?)
                     ORDER BY ready_queue.position LIMIT 1""",
                                             (PROTECTED_QUEUE_SIZE, exclude_request_id, exclude_request_id),
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
        item = database.execute("SELECT video_id FROM requests WHERE id = ?", (request_id,)).fetchone()
        database.execute("UPDATE requests SET status = 'skipped' WHERE id = ?", (request_id,))
        database.execute("DELETE FROM ready_queue WHERE request_id = ?", (request_id,))
        database.execute("DELETE FROM group_rooms WHERE request_id = ?", (request_id,))
        database.execute("DELETE FROM backvocals WHERE request_id = ?", (request_id,))
        generation = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
        remove_unused_media(database, item["video_id"], generation)
    else:
        previous_position = next(index for index, row in enumerate(rows) if row["request_id"] == request_id)
        remaining.insert(min(max(2, previous_position), len(remaining)), request_id)
    for position, queued_id in enumerate(remaining, start=1):
        database.execute("UPDATE ready_queue SET position = ? WHERE request_id = ?", (position, queued_id))
    database.execute("DELETE FROM skip_request WHERE id = 1")
    database.execute("DELETE FROM invitation WHERE id = 1")
    next_ready = database.execute(
        """SELECT ready_queue.request_id FROM ready_queue JOIN requests ON requests.id = ready_queue.request_id
              WHERE ready_queue.position <= ? AND requests.status = 'ready'
             AND ready_queue.request_id != ? ORDER BY ready_queue.position LIMIT 1""",
          (PROTECTED_QUEUE_SIZE, request_id),
    ).fetchone()
    if next_ready:
        start_invitation(database, now, exclude_request_id=request_id)
    return None


def accept_invitation(database: sqlite3.Connection, request_id: str, singer_id: str, now: float) -> bool:
    current = invitation_state(database)
    if current is None or current["request_id"] != request_id or current["accepted"]:
        return False
    if skip_state(database):
        return False
    owner = database.execute("SELECT singer_id, status FROM requests WHERE id = ?", (request_id,)).fetchone()
    if owner is None or owner["status"] != "ready":
        return False
    if owner["singer_id"] == singer_id:
        if current["lead_accepted"]:
            return False
        database.execute("UPDATE invitation SET lead_accepted = 1 WHERE id = 1")
    else:
        accepted = database.execute(
            "UPDATE backvocals SET accepted = 1 WHERE request_id = ? AND singer_id = ? AND accepted = 0 AND joined = 1",
            (request_id, singer_id),
        )
        if not accepted.rowcount:
            return False
    ready = database.execute("SELECT lead_accepted FROM invitation WHERE id = 1").fetchone()[0]
    if ready and not database.execute(
        "SELECT 1 FROM backvocals WHERE request_id = ? AND joined = 1 AND accepted = 0 LIMIT 1", (request_id,),
    ).fetchone():
        database.execute("UPDATE invitation SET accepted = 1 WHERE id = 1")
    database.execute(
          """INSERT INTO accepted_counts(singer_id, total) VALUES (?, 1)
              ON CONFLICT(singer_id) DO UPDATE SET total = total + 1""",
          (singer_id,),
    )
    return True


def finish_song(database: sqlite3.Connection, request_id: str, now: float) -> bool:
    current = invitation_state(database)
    if current is None or current["request_id"] != request_id or not current["accepted"] or skip_state(database):
        return False
    item = database.execute("SELECT video_id FROM requests WHERE id = ?", (request_id,)).fetchone()
    database.execute("UPDATE requests SET status = 'played' WHERE id = ?", (request_id,))
    database.execute("DELETE FROM ready_queue WHERE request_id = ?", (request_id,))
    generation = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
    remove_unused_media(database, item["video_id"], generation)
    database.execute("DELETE FROM group_rooms WHERE request_id = ?", (request_id,))
    database.execute("DELETE FROM backvocals WHERE request_id = ?", (request_id,))
    for position, row in enumerate(database.execute(
        "SELECT request_id FROM ready_queue ORDER BY position"
    ).fetchall(), start=1):
        database.execute("UPDATE ready_queue SET position = ? WHERE request_id = ?", (position, row["request_id"]))
    database.execute("DELETE FROM invitation WHERE id = 1")
    start_invitation(database, now)
    return True