import html
import json
import re
import tempfile
from pathlib import Path

from yt_dlp import YoutubeDL

from app.media import MEDIA_ROOT
from app.scoring import block_duration_ms, caption_history_ms, caption_repeat_percent, onset_tolerance_ms as configured_onset_tolerance_ms

TIMESTAMP = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})"
)
CUE_LINE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s+-->[^\r\n]*(?:\r?\n|$)", re.MULTILINE)
VTT_TAG = re.compile(r"<[^>]*>")
INLINE_TIMESTAMP = re.compile(r"<(?P<time>\d{2}:\d{2}:\d{2}\.\d{3})>")
BRACKETED_TEXT = re.compile(r"\[[^\]\r\n]*\]")
DECORATION = re.compile(r"[\s♪♫♬♩]+")
SPEAKER_MARKER = re.compile(r"(^|\n)[^\S\r\n]*>>[^\S\r\n]*")
CAPTION_VERSION = 8


def normalized_words(text: str) -> list[str]:
    return [word.casefold().strip(".,!?;:") for word in text.split()]


def contains_phrase(haystack: list[str], phrase: list[str]) -> bool:
    return any(haystack[index:index + len(phrase)] == phrase
               for index in range(len(haystack) - len(phrase) + 1))


def mostly_repeated(haystack: list[str], candidate: list[str], repeat_percent: int) -> bool:
    previous_lengths = [0] * (len(haystack) + 1)
    for word in candidate:
        lengths = [0] * (len(haystack) + 1)
        for index, old_word in enumerate(haystack, start=1):
            if word == old_word:
                lengths[index] = previous_lengths[index - 1] + 1
                if lengths[index] * 100 > repeat_percent * len(candidate):
                    return True
        previous_lengths = lengths
    return False


def new_caption_text(previous: str, current: str) -> str:
    old_words = previous.split()
    new_words = current.split()
    for count in range(min(len(old_words), len(new_words)), 0, -1):
        if [word.casefold().strip(".,!?;:") for word in old_words[-count:]] == [
            word.casefold().strip(".,!?;:") for word in new_words[:count]
        ]:
            return " ".join(new_words[count:])
    return current


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
    history_ms: int | None = None,
    repeat_percent: int | None = None,
) -> dict:
    onset_tolerance_ms = configured_onset_tolerance_ms() if onset_tolerance_ms is None else onset_tolerance_ms
    max_block_ms = block_duration_ms() if max_block_ms is None else max_block_ms
    history_ms = caption_history_ms() if history_ms is None else history_ms
    repeat_percent = caption_repeat_percent() if repeat_percent is None else repeat_percent
    cues = []
    source_cues = 0
    bracketed_annotations = 0
    ignored_annotation_cues = 0
    previous_text = ""
    previous_start = -max_block_ms - 1
    events = []
    history = []
    cue_lines = list(CUE_LINE.finditer(text))
    for cue_index, cue_line in enumerate(cue_lines):
        timestamp = TIMESTAMP.search(cue_line.group())
        source_cues += 1
        caption_source = text[cue_line.end():cue_lines[cue_index + 1].start() if cue_index + 1 < len(cue_lines) else len(text)].strip()
        annotations = BRACKETED_TEXT.findall(caption_source)
        bracketed_annotations += len(annotations)
        start_ms = min(to_milliseconds(timestamp.group("start")), duration_ms)
        end_ms = min(to_milliseconds(timestamp.group("end")), duration_ms)
        if end_ms <= start_ms:
            continue
        fragments = INLINE_TIMESTAMP.split(caption_source)
        timed_text = [(start_ms, fragments[0])]
        timed_text.extend(
            (min(to_milliseconds(fragments[index]), duration_ms), fragments[index + 1])
            for index in range(1, len(fragments), 2)
        )
        has_words = False
        for fragment_start, fragment in timed_text:
            caption = html.unescape(VTT_TAG.sub("", fragment))
            caption = BRACKETED_TEXT.sub("", caption)
            caption = SPEAKER_MARKER.sub(r"\1", caption)
            caption = " ".join(DECORATION.split(caption)).strip()
            if not caption or not start_ms <= fragment_start < end_ms:
                continue
            has_words = True
            history = [entry for entry in history if fragment_start - entry[0] <= history_ms]
            candidate = normalized_words(caption)
            fresh = new_caption_text(previous_text, caption) if fragment_start - previous_start <= max_block_ms else caption
            if len(candidate) >= 3 and (
                contains_phrase(normalized_words(" ".join(entry[1] for entry in history)), candidate)
                or contains_phrase(normalized_words(" ".join(entry[2] for entry in history)), candidate)
                or (fresh == caption and (
                    mostly_repeated(normalized_words(" ".join(entry[1] for entry in history)), candidate, repeat_percent)
                    or mostly_repeated(normalized_words(" ".join(entry[2] for entry in history)), candidate, repeat_percent)
                ))
            ):
                continue
            if fresh:
                if events and events[-1][0] == fragment_start:
                    previous_start_ms, previous_end_ms, previous_fragment = events[-1]
                    events[-1] = (previous_start_ms, max(previous_end_ms, end_ms), f"{previous_fragment} {fresh}")
                else:
                    events.append((fragment_start, end_ms, fresh))
                previous_text = f"{previous_text} {fresh}" if fragment_start - previous_start <= max_block_ms else fresh
                previous_start = fragment_start
                history.append((fragment_start, caption, fresh))
        if not has_words:
            if annotations:
                ignored_annotation_cues += 1
    for index, (start_ms, end_ms, caption) in enumerate(events):
        next_start = events[index + 1][0] if index + 1 < len(events) else end_ms
        block_end = min(start_ms + max_block_ms, end_ms, max(start_ms + 1, next_start))
        cues.append({
            "block_index": len(cues),
            "start_ms": start_ms,
            "end_ms": block_end,
            "text": caption,
            "onset_tolerance_ms": onset_tolerance_ms,
            "score_window_start_ms": max(0, start_ms - onset_tolerance_ms),
            "score_window_end_ms": min(duration_ms, start_ms + onset_tolerance_ms),
            "score_duration_ms": block_end - start_ms,
        })
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
        "history_ms": history_ms,
        "repeat_percent": repeat_percent,
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
    history_ms = caption_history_ms()
    repeat_percent = caption_repeat_percent()
    root = MEDIA_ROOT / generation
    output = root / "captions" / f"{video_id}.json"
    if output.is_file():
        try:
            cached = json.loads(output.read_text(encoding="utf-8"))
            if (cached.get("version") == CAPTION_VERSION
                    and cached.get("onset_tolerance_ms") == onset_tolerance_ms
                    and cached.get("max_block_ms") == max_block_ms
                    and cached.get("history_ms") == history_ms
                    and cached.get("repeat_percent") == repeat_percent
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
            caption_path.read_text(encoding="utf-8"), duration_ms, onset_tolerance_ms, max_block_ms,
            history_ms, repeat_percent
        )

    result = {
        "version": CAPTION_VERSION,
        "video_id": video_id,
        "language": language,
        "duration_ms": duration_ms,
        "onset_tolerance_ms": onset_tolerance_ms,
        "max_block_ms": max_block_ms,
        "history_ms": history_ms,
        "repeat_percent": repeat_percent,
        "timing_method": "native_vtt_segments_with_rolling_overlap_removed",
        **parsed,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_suffix(".tmp")
    temporary_output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary_output.replace(output)
    return result


def remove_caption_bars(video_id: str, generation: str) -> None:
    (MEDIA_ROOT / generation / "captions" / f"{video_id}.json").unlink(missing_ok=True)