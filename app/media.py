import os
import subprocess
from pathlib import Path

from yt_dlp import YoutubeDL

MEDIA_ROOT = Path(os.environ.get("KARAOKE_MEDIA_PATH", "/media"))
MAX_DURATION_SECONDS = 12 * 60
MAX_FILE_BYTES = 300 * 1024 * 1024


def download_video(video_id: str) -> str:
    directory = MEDIA_ROOT / "videos"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{video_id}.mp4"
    cached = target.is_file()
    url = f"https://www.youtube.com/watch?v={video_id}"
    options = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
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
        if not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Vídeo não foi preparado em MP4 dentro do limite de 300 MB")
        return str(metadata.get("title") or video_id)[:200]
    except Exception:
        for path in directory.glob(f"{video_id}.*"):
            if path.is_file() and (not cached or path != target):
                path.unlink()
        raise


def check_size(progress: dict) -> None:
    if progress.get("downloaded_bytes", 0) > MAX_FILE_BYTES:
        raise ValueError("Vídeo ultrapassou 300 MB")


def create_preview(video_id: str) -> None:
    video = MEDIA_ROOT / "videos" / f"{video_id}.mp4"
    preview = MEDIA_ROOT / "previews" / f"{video_id}.jpg"
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