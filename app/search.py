import os
import re
from concurrent.futures import ThreadPoolExecutor

from yt_dlp import YoutubeDL

from app.captions import select_caption_track
from app.media import MAX_DURATION_SECONDS

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
BASE_OPTIONS = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 15}


def search_limit() -> int:
    try:
        return max(1, min(int(os.getenv("KARAOKE_SEARCH_LIMIT", "10")), 30))
    except ValueError:
        return 10


def video_summary(entry: dict) -> dict | None:
    video_id = entry.get("id")
    if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
        return None
    duration = entry.get("duration")
    if entry.get("live_status") in ("is_live", "is_upcoming") or (duration and duration > MAX_DURATION_SECONDS):
        return None
    return {
        "video_id": video_id,
        "title": str(entry.get("title") or video_id)[:200],
        "channel": str(entry.get("channel") or entry.get("uploader") or "")[:100],
        "duration": int(duration) if duration else None,
    }


def search_entries(query: str, count: int) -> list[dict]:
    with YoutubeDL({**BASE_OPTIONS, "extract_flat": "in_playlist"}) as downloader:
        result = downloader.extract_info(f"ytsearch{count}:{query}", download=False)
    videos, seen = [], set()
    for entry in (result or {}).get("entries") or []:
        video = video_summary(entry or {})
        if video and video["video_id"] not in seen:
            seen.add(video["video_id"])
            videos.append(video)
    return videos


def has_captions(video_id: str) -> bool:
    try:
        with YoutubeDL({**BASE_OPTIONS, "ignore_no_formats_error": True}) as downloader:
            metadata = downloader.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        select_caption_track(metadata or {})
        return True
    except Exception:
        return False


def search_with_captions(query: str, limit: int) -> list[dict]:
    # Captions are only visible in full metadata, so over-fetch and check candidates in parallel.
    candidates = search_entries(query, limit * 2)
    with ThreadPoolExecutor(max_workers=8) as pool:
        flags = list(pool.map(lambda video: has_captions(video["video_id"]), candidates))
    return [video for video, captioned in zip(candidates, flags) if captioned][:limit]


def search_karaoke(query: str, limit: int) -> list[dict]:
    return search_entries(f"Karaoke {query}", limit)
