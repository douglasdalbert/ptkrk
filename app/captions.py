import html
import json
import re
import tempfile
from pathlib import Path

from yt_dlp import YoutubeDL

from app.media import MEDIA_ROOT
from app.scoring import block_duration_ms, onset_tolerance_ms as configured_onset_tolerance_ms

TIMESTAMP = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})"
)
VTT_TAG = re.compile(r"<[^>]*>")
BRACKETED_TEXT = re.compile(r"\[[^\]\r\n]*\]")
DECORATION = re.compile(r"[\s♪♫♬♩]+")
def to_milliseconds(timestamp: str) -> int:
    hours, minutes, seconds, milliseconds = map(int, re.split(r"[:.]", timestamp))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def select_native_caption_language(metadata: dict) -> str:
    available = metadata.get("automatic_captions") or {}
    if not available:
        raise ValueError("O vídeo não oferece legendas geradas automaticamente")

    native = (metadata.get("language") or "").lower().replace("_", "-")
    native_base = native.split("-", 1)[0]
    original_keys = [key for key in available if key.endswith("-orig")]
    if native_base:
        matching_original = [key for key in original_keys if key[:-5].lower() == native_base]
        if matching_original:
            return matching_original[0]
        matching_native = [key for key in available if key.lower() == native_base]
        if matching_native:
            return matching_native[0]
        matching_native = [
            key for key in available
            if key.lower().split("-", 1)[0] == native_base and not key.endswith("-orig")
        ]
        if matching_native:
            return matching_native[0]

    if len(original_keys) == 1:
        return original_keys[0]
    raise ValueError("Não foi possível identificar a legenda automática no idioma nativo")


def parse_vtt_caption_bars(
    text: str,
    duration_ms: int,
    onset_tolerance_ms: int | None = None,
    max_block_ms: int | None = None,
) -> dict:
    onset_tolerance_ms = configured_onset_tolerance_ms() if onset_tolerance_ms is None else onset_tolerance_ms
    max_block_ms = block_duration_ms() if max_block_ms is None else max_block_ms
    cues = []
    source_cues = 0
    bracketed_annotations = 0
    ignored_annotation_cues = 0
    for block in re.split(r"\r?\n\s*\r?\n", text):
        timestamp = TIMESTAMP.search(block)
        if not timestamp:
            continue
        source_cues += 1
        caption_source = block[timestamp.end():].partition("\n")[2]
        caption = html.unescape(VTT_TAG.sub("", caption_source)).strip()
        annotations = BRACKETED_TEXT.findall(caption)
        bracketed_annotations += len(annotations)
        caption = BRACKETED_TEXT.sub("", caption)
        caption = " ".join(DECORATION.split(caption)).strip()
        start_ms = min(to_milliseconds(timestamp.group("start")), duration_ms)
        end_ms = min(to_milliseconds(timestamp.group("end")), duration_ms)
        if not caption or end_ms <= start_ms:
            if annotations:
                ignored_annotation_cues += 1
            continue
        block_start = start_ms
        while block_start < end_ms:
            block_end = min(block_start + max_block_ms, end_ms)
            cues.append({
                "block_index": len(cues),
                "start_ms": block_start,
                "end_ms": block_end,
                "text": caption,
                "onset_tolerance_ms": onset_tolerance_ms,
                "score_window_start_ms": max(0, block_start - onset_tolerance_ms),
                "score_window_end_ms": min(duration_ms, block_start + onset_tolerance_ms),
                "score_duration_ms": block_end - block_start,
            })
            block_start = block_end
    return {
        "source_cue_count": source_cues,
        "cue_count": len(cues),
        "bracketed_annotation_count": bracketed_annotations,
        "ignored_annotation_cue_count": ignored_annotation_cues,
        "bar_count": len(cues),
        "bars": cues,
        "cues": cues,
        "scoring_rule": "hit_caption_onset_within_tolerance_awards_full_caption_duration",
        "max_block_ms": max_block_ms,
    }


def points_for_caption_onset(bar: dict, observed_onset_ms: int) -> float:
    if bar["score_window_start_ms"] <= observed_onset_ms <= bar["score_window_end_ms"]:
        return bar["score_duration_ms"] / 1000
    return 0.0


def create_caption_bars(
    video_id: str,
    generation: str,
    onset_tolerance_ms: int | None = None,
    max_block_ms: int | None = None,
) -> dict:
    onset_tolerance_ms = configured_onset_tolerance_ms() if onset_tolerance_ms is None else onset_tolerance_ms
    max_block_ms = block_duration_ms() if max_block_ms is None else max_block_ms
    root = MEDIA_ROOT / generation
    output = root / "captions" / f"{video_id}.json"
    if output.is_file():
        try:
            cached = json.loads(output.read_text(encoding="utf-8"))
            if (cached.get("version") == 5
                    and cached.get("onset_tolerance_ms") == onset_tolerance_ms
                    and cached.get("max_block_ms") == max_block_ms
                    and isinstance(cached.get("bars"), list)):
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    url = f"https://www.youtube.com/watch?v={video_id}"
    with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                    "noplaylist": True, "socket_timeout": 20}) as downloader:
        metadata = downloader.extract_info(url, download=False)
    language = select_native_caption_language(metadata)

    with tempfile.TemporaryDirectory(prefix="karaoke-captions-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "writeautomaticsub": True,
            "subtitleslangs": [language],
            "subtitlesformat": "vtt",
            "noplaylist": True,
            "socket_timeout": 20,
            "outtmpl": str(temporary_path / "%(id)s.%(ext)s"),
        }
        with YoutubeDL(options) as downloader:
            metadata = downloader.extract_info(url, download=True)
        caption_path = temporary_path / f"{metadata['id']}.{language}.vtt"
        if not caption_path.is_file():
            raise ValueError(f"Legenda automática {language} indisponível")
        duration_ms = round(float(metadata.get("duration") or 0) * 1000)
        parsed = parse_vtt_caption_bars(
            caption_path.read_text(encoding="utf-8"), duration_ms, onset_tolerance_ms, max_block_ms
        )

    result = {
        "version": 5,
        "video_id": video_id,
        "language": language,
        "duration_ms": duration_ms,
        "onset_tolerance_ms": onset_tolerance_ms,
        "max_block_ms": max_block_ms,
        "timing_method": "caption_cue_start_with_full_cue_bar",
        **parsed,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_suffix(".tmp")
    temporary_output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary_output.replace(output)
    return result


def remove_caption_bars(video_id: str, generation: str) -> None:
    (MEDIA_ROOT / generation / "captions" / f"{video_id}.json").unlink(missing_ok=True)