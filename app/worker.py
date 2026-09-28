import logging
import time

from app.media import create_preview, download_video
from app.queue import enqueue_ready
from app.storage import connection, initialize

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def process_next() -> bool:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        item = database.execute(
            "SELECT id, video_id FROM requests WHERE status = 'pending' ORDER BY created_at, rowid LIMIT 1"
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
            database.execute(
                "UPDATE requests SET status = 'failed', error = ? WHERE id = ?",
                ("Não foi possível baixar/preparar este vídeo", item["id"]),
            )
    else:
        with connection() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "UPDATE requests SET status = 'ready', title = ? WHERE id = ?",
                (title, item["id"]),
            )
            enqueue_ready(database, item["id"])
    return True


def main() -> None:
    initialize()
    with connection() as database:
        database.execute("UPDATE requests SET status = 'pending' WHERE status = 'processing'")
    while True:
        if not process_next():
            time.sleep(2)


if __name__ == "__main__":
    main()