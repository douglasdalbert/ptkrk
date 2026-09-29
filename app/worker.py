import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor

from app.captions import create_caption_bars
from app.media import MEDIA_ROOT, create_preview, download_video, remove_unused_media
from app.storage import connection, initialize

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def recover_missing_media(database, generation: str) -> None:
    for item in database.execute("SELECT id, video_id FROM requests WHERE status = 'ready'").fetchall():
        video = MEDIA_ROOT / generation / "videos" / f"{item['video_id']}.mp4"
        if not video.is_file():
            database.execute("UPDATE requests SET status = 'pending' WHERE id = ?", (item["id"],))
            database.execute("DELETE FROM skip_request WHERE request_id = ?", (item["id"],))
            database.execute("DELETE FROM invitation WHERE request_id = ?", (item["id"],))


def process_next() -> bool:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        generation = database.execute("SELECT generation FROM party WHERE id=1").fetchone()[0]
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
        title = download_video(item["video_id"], generation)
        create_preview(item["video_id"], generation)
        try:
            create_caption_bars(item["video_id"], generation)
        except Exception:
            logger.exception("Não foi possível preparar a legenda de %s", item["video_id"])
    except Exception as error:
        logger.exception("Falha ao preparar vídeo %s", item["video_id"])
        reason = str(error) if isinstance(error, ValueError) else "Não foi possível baixar/preparar este vídeo"
        with connection() as database:
            database.execute("BEGIN IMMEDIATE")
            updated = database.execute(
                "UPDATE requests SET status = 'failed', error = ? WHERE id = ? AND status = 'processing' "
                "AND ? = (SELECT generation FROM party WHERE id=1)",
                (reason[:200], item["id"], generation),
            )
            if updated.rowcount:
                database.execute("DELETE FROM ready_queue WHERE request_id = ?", (item["id"],))
                rows = database.execute("SELECT request_id FROM ready_queue ORDER BY position").fetchall()
                for position, row in enumerate(rows, start=1):
                    database.execute("UPDATE ready_queue SET position = ? WHERE request_id = ?", (position, row["request_id"]))
                remove_unused_media(database, item["video_id"], generation)
    else:
        with connection() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "UPDATE requests SET status = 'ready', title = ? WHERE id = ? AND status = 'processing' "
                "AND ? = (SELECT generation FROM party WHERE id=1)",
                (title, item["id"], generation),
            )
    with connection() as database:
        current = database.execute("SELECT generation FROM party WHERE id=1").fetchone()[0]
        if current == generation:
            remove_unused_media(database, item["video_id"], generation)
    if current != generation:
        shutil.rmtree(MEDIA_ROOT / generation, ignore_errors=True)
    return True


def main() -> None:
    initialize()
    with connection() as database:
        database.execute("UPDATE requests SET status = 'pending' WHERE status = 'processing'")
        generation = database.execute("SELECT generation FROM party WHERE id=1").fetchone()[0]
        recover_missing_media(database, generation)
    for directory in MEDIA_ROOT.iterdir():
        if directory.is_dir() and directory.name not in {generation, "videos", "previews"}:
            shutil.rmtree(directory, ignore_errors=True)
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