import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from app.media import MEDIA_ROOT
from app.storage import connection

logger = logging.getLogger(__name__)
MODEL_FILENAME = "UVR_MDXNET_KARA_2.onnx"


def recover_interrupted_jobs() -> None:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        generation = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
        database.execute(
            "UPDATE vocal_separation_jobs SET status = 'pending' "
            "WHERE generation = ? AND status = 'processing'",
            (generation,),
        )


def skip_pending_job(database, generation: str, video_id: str) -> None:
    database.execute(
        "UPDATE vocal_separation_jobs SET status = 'skipped' "
        "WHERE generation = ? AND video_id = ? AND status = 'pending'",
        (generation, video_id),
    )


def claim_next_job() -> dict | None:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        generation = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
        database.execute(
            "UPDATE vocal_separation_jobs SET status = 'skipped' WHERE generation = ? AND status = 'pending' "
            "AND NOT EXISTS (SELECT 1 FROM requests WHERE requests.video_id = vocal_separation_jobs.video_id "
            "AND requests.status IN ('ready', 'karaokezado'))",
            (generation,),
        )
        if database.execute(
            "SELECT 1 FROM vocal_separation_jobs WHERE status = 'processing' LIMIT 1"
        ).fetchone():
            return None
        item = database.execute(
            """SELECT vocal_separation_jobs.generation, vocal_separation_jobs.video_id
               FROM vocal_separation_jobs
               JOIN requests ON requests.video_id = vocal_separation_jobs.video_id
               JOIN ready_queue ON ready_queue.request_id = requests.id
               WHERE vocal_separation_jobs.generation = ?
                 AND vocal_separation_jobs.status = 'pending'
                 AND requests.status IN ('ready', 'karaokezado')
               ORDER BY ready_queue.position LIMIT 1""",
            (generation,),
        ).fetchone()
        if item is None:
            return None
        database.execute(
            "UPDATE vocal_separation_jobs SET status = 'processing' "
            "WHERE generation = ? AND video_id = ? AND status = 'pending'",
            (item["generation"], item["video_id"]),
        )
        return dict(item)


def separate_video(job: dict) -> None:
    video_id = job["video_id"]
    generation = job["generation"]
    source = MEDIA_ROOT / generation / "videos" / f"{video_id}.mp4"
    target_dir = MEDIA_ROOT / generation / "karaoke_videos"
    target = target_dir / f"{video_id}.mp4"
    if not source.is_file():
        raise FileNotFoundError(f"Vídeo original não encontrado: {video_id}")

    environment = os.environ.copy()
    for variable in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        environment[variable] = "1"

    with tempfile.TemporaryDirectory(prefix="karaoke-separation-") as temporary:
        work = Path(temporary)
        audio = work / "audio.wav"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source), "-vn",
             "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", "-y", str(audio)],
            check=True, timeout=180,
        )
        subprocess.run(
            ["audio-separator", str(audio), "--model_filename", MODEL_FILENAME,
             "--model_file_dir", os.getenv("KARAOKE_MODEL_PATH", "/models"),
             "--output_dir", str(work), "--output_format", "WAV", "--single_stem", "Instrumental"],
            check=True, timeout=1800, env=environment,
        )
        instrumentals = list(work.glob("*_Instrumental*.wav"))
        if len(instrumentals) != 1 or instrumentals[0].stat().st_size == 0:
            raise RuntimeError("O separador não produziu uma faixa instrumental válida")

        target_dir.mkdir(parents=True, exist_ok=True)
        temporary_video = target_dir / f".{video_id}.{uuid4().hex}.tmp.mp4"
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
                 "-i", str(instrumentals[0]), "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "0",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-y",
                 str(temporary_video)],
                check=True, timeout=180,
            )
            if not temporary_video.is_file() or temporary_video.stat().st_size == 0:
                raise RuntimeError("O vídeo karaokê ficou vazio")
            with connection() as database:
                database.execute("BEGIN IMMEDIATE")
                current_generation = database.execute(
                    "SELECT generation FROM party WHERE id = 1"
                ).fetchone()[0]
                job_exists = database.execute(
                    "SELECT 1 FROM vocal_separation_jobs WHERE generation = ? AND video_id = ? "
                    "AND status = 'processing'",
                    (generation, video_id),
                ).fetchone()
                active_request = database.execute(
                    "SELECT 1 FROM requests WHERE video_id = ? AND status IN ('ready', 'karaokezado') LIMIT 1",
                    (video_id,),
                ).fetchone()
                if current_generation != generation or not job_exists or not active_request:
                    if current_generation != generation:
                        shutil.rmtree(MEDIA_ROOT / generation, ignore_errors=True)
                    elif not active_request:
                        target.unlink(missing_ok=True)
                    return
                os.replace(temporary_video, target)
                database.execute(
                    "UPDATE vocal_separation_jobs SET status = 'done' "
                    "WHERE generation = ? AND video_id = ?",
                    (generation, video_id),
                )
                database.execute(
                    "UPDATE requests SET status = 'karaokezado' WHERE video_id = ? AND status = 'ready'",
                    (video_id,),
                )
        finally:
            temporary_video.unlink(missing_ok=True)


def process_next_job() -> bool:
    job = claim_next_job()
    if job is None:
        return False
    try:
        separate_video(job)
    except Exception:
        logger.exception("Falha ao separar áudio de %s; mantendo vídeo original", job["video_id"])
        with connection() as database:
            database.execute(
                "UPDATE vocal_separation_jobs SET status = 'failed' "
                "WHERE generation = ? AND video_id = ? AND status = 'processing'",
                (job["generation"], job["video_id"]),
            )
    return True


def main() -> None:
    while True:
        try:
            recover_interrupted_jobs()
            while process_next_job():
                pass
        except Exception:
            logger.exception("Falha no serviço singleton de separação vocal")
        time.sleep(2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()