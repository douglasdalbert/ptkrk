import logging
import time
from concurrent.futures import ThreadPoolExecutor

from app.media import create_preview, download_video
from app.storage import connection, initialize

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def process_next() -> bool:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        item = database.execute(
            """SELECT requests.id, requests.video_id FROM requests
               JOIN ready_queue ON ready_queue.request_id = requests.id
               WHERE requests.status = 'pending' AND ready_queue.position <= 10
                 AND NOT EXISTS (SELECT 1 FROM requests AS active
                                 WHERE active.video_id = requests.video_id AND active.status = 'processing')
               ORDER BY ready_queue.position LIMIT 1"""
        ).fetchone()
        if item is None:
            return False
        database.execute("UPDATE requests SET status = 'processing' WHERE id = ?", (item["id"],))

    try:
        title = download_video(item["video_id"])
        create_preview(item["video_id"])
    except Exception:
        logger.exception("Falha ao preparar vídeo %s", item["video_id"])
        with connection() as database:
            database.execute("BEGIN IMMEDIATE")
            updated = database.execute(
                "UPDATE requests SET status = 'failed', error = ? WHERE id = ? AND status = 'processing'",
                ("Não foi possível baixar/preparar este vídeo", item["id"]),
            )
            if updated.rowcount:
                database.execute("DELETE FROM ready_queue WHERE request_id = ?", (item["id"],))
                rows = database.execute("SELECT request_id FROM ready_queue ORDER BY position").fetchall()
                for position, row in enumerate(rows, start=1):
                    database.execute("UPDATE ready_queue SET position = ? WHERE request_id = ?", (position, row["request_id"]))
    else:
        with connection() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "UPDATE requests SET status = 'ready', title = ? WHERE id = ? AND status = 'processing'",
                (title, item["id"]),
            )
    return True


def main() -> None:
    initialize()
    with connection() as database:
        database.execute("UPDATE requests SET status = 'pending' WHERE status = 'processing'")
    with ThreadPoolExecutor(max_workers=2) as executor:
        active = set()
        while True:
            completed = {future for future in active if future.done()}
            for future in completed:
                try:
                    future.result()
                except Exception:
                    logger.exception("Falha inesperada no worker")
            active.difference_update(completed)
            if len(active) < 2:
                with connection() as database:
                    eligible = database.execute(
                        """SELECT 1 FROM requests JOIN ready_queue ON ready_queue.request_id = requests.id
                           WHERE requests.status = 'pending' AND ready_queue.position <= 10
                             AND NOT EXISTS (SELECT 1 FROM requests AS active
                                 WHERE active.video_id = requests.video_id AND active.status = 'processing')
                           LIMIT 1"""
                    ).fetchone()
                if eligible:
                    active.add(executor.submit(process_next))
            time.sleep(1)


if __name__ == "__main__":
    main()