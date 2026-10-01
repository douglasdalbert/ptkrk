import os
import sqlite3
import subprocess
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

MEDIA_ROOT = Path(os.environ.get("KARAOKE_MEDIA_PATH", "/media"))
MAX_DURATION_SECONDS = 12 * 60
MAX_FILE_BYTES = 500 * 1024 * 1024
NODE_ENV = os.getenv("NODE_ENV", "production").strip().lower()


def remove_unused_media(database: sqlite3.Connection, video_id: str, generation: str) -> bool:
    if NODE_ENV == "development":
        return False
    active = database.execute(
        """SELECT 1 FROM requests WHERE video_id = ?
              AND status IN ('pending', 'processing', 'ready', 'karaokezado') LIMIT 1""",
        (video_id,),
    ).fetchone()
    if active:
        return False
    database.execute(
        "UPDATE vocal_separation_jobs SET status = 'skipped', error = NULL "
        "WHERE generation = ? AND video_id = ? AND status IN ('pending', 'done')",
        (generation, video_id),
    )
    running_job = database.execute(
        "UPDATE vocal_separation_jobs SET status = 'cancelling' "
        "WHERE generation = ? AND video_id = ? AND status = 'processing'",
        (generation, video_id),
    )
    if running_job.rowcount or database.execute(
        "SELECT 1 FROM vocal_separation_jobs WHERE generation = ? AND video_id = ? AND status = 'cancelling'",
        (generation, video_id),
    ).fetchone():
        return False
    for category, suffix in (("videos", ".mp4"), ("karaoke_videos", ".mp4"), ("previews", ".jpg"),
                             ("analysis", ".json"), ("captions", ".json")):
        (MEDIA_ROOT / generation / category / f"{video_id}{suffix}").unlink(missing_ok=True)
    for alternate in (MEDIA_ROOT / generation / "captions").glob(f"{video_id}.*.json"):
        alternate.unlink(missing_ok=True)
    return True


def download_video(video_id: str, generation: str | None = None) -> str:
    directory = MEDIA_ROOT / generation / "videos" if generation else MEDIA_ROOT / "videos"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{video_id}.mp4"
    cached = target.is_file()
    url = f"https://www.youtube.com/watch?v={video_id}"
    options = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "format": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]",
        "merge_output_format": "mp4",
        "outtmpl": str(directory / f"{video_id}.%(ext)s"),
        "max_filesize": MAX_FILE_BYTES,
        "restrictfilenames": True,
        "progress_hooks": [check_size],
    }
    try:
        with YoutubeDL(options) as downloader:
            metadata = downloader.extract_info(url, download=False)
            if not metadata or metadata.get("id") != video_id:
                raise ValueError("Vídeo indisponível")
            duration = metadata.get("duration")
            if not duration or duration > MAX_DURATION_SECONDS or metadata.get("is_live"):
                raise ValueError("Vídeo ao vivo ou com mais de 12 minutos não é aceito")
            if not cached:
                downloader.download([url])
        if not target.is_file():
            raise ValueError("Não foi possível preparar o vídeo em MP4")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Vídeo excede o limite de 500 MB")
        return str(metadata.get("title") or video_id)[:200]
    except Exception as error:
        for path in directory.glob(f"{video_id}.*"):
            if path.is_file() and (not cached or path != target):
                path.unlink()
        if isinstance(error, DownloadError):
            if "video is unavailable" in str(error).lower():
                raise ValueError("Vídeo indisponível no YouTube") from error
            raise ValueError("YouTube não disponibilizou uma versão compatível deste vídeo") from error
        raise


def check_size(progress: dict) -> None:
    if progress.get("downloaded_bytes", 0) > MAX_FILE_BYTES:
        raise ValueError("Vídeo excede o limite de 500 MB")


def create_preview(video_id: str, generation: str | None = None) -> None:
    root = MEDIA_ROOT / generation if generation else MEDIA_ROOT
    video = root / "videos" / f"{video_id}.mp4"
    preview = root / "previews" / f"{video_id}.jpg"
    preview.parent.mkdir(parents=True, exist_ok=True)
    if preview.is_file():
        return
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "1", "-i", str(video),
         "-frames:v", "1", "-vf", "scale=640:-2", "-y", str(preview)],
        check=True,
        timeout=60,
    )
    if not preview.is_file():
        raise ValueError("Não foi possível gerar a prévia")