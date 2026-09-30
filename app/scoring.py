import json
import math
import os
import sqlite3
from pathlib import Path


def integer_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.getenv(name, default))))
    except (TypeError, ValueError):
        return default


def max_score() -> int:
    return integer_setting("KARAOKE_SCORE_MAX", 1000, 1, 100000)


def off_cue_penalty() -> int:
    return integer_setting("KARAOKE_SCORE_OFF_CUE_PENALTY", 1, 0, 100)


def block_duration_ms() -> int:
    return integer_setting("KARAOKE_SCORE_BLOCK_MS", 1000, 100, 5000)


def caption_history_ms() -> int:
    return integer_setting("KARAOKE_CAPTION_HISTORY_MS", 60000, 1000, 600000)


def caption_repeat_percent() -> int:
    return integer_setting("KARAOKE_CAPTION_REPEAT_PERCENT", 70, 0, 100)


def onset_tolerance_ms() -> int:
    return integer_setting("KARAOKE_SCORE_TOLERANCE_MS", 100, 0, 500)


def off_cue_rearm_ms() -> int:
    return integer_setting("KARAOKE_SCORE_OFF_CUE_REARM_MS", 500, 100, 3000)


def microphone_rms_threshold() -> float:
    try:
        value = float(os.getenv("KARAOKE_SCORE_RMS_THRESHOLD", "0.04"))
    except (TypeError, ValueError):
        return 0.04
    return max(0.005, min(0.5, value)) if math.isfinite(value) else 0.04


def matching_block(bars: list[dict], position_ms: int) -> int | None:
    matches = [
        bar for bar in bars
        if bar["score_window_start_ms"] <= position_ms <= bar["score_window_end_ms"]
    ]
    if not matches:
        return None
    nearest = min(matches, key=lambda bar: abs(position_ms - bar["start_ms"]))
    return int(nearest["block_index"])


def score_value(hits: int, penalties: int, total_blocks: int, ranking_max: int | None = None) -> float:
    if total_blocks <= 0:
        return 0.0
    ranking_max = max_score() if ranking_max is None else ranking_max
    adjusted = max(0, min(total_blocks, hits - penalties * off_cue_penalty()))
    return round(adjusted * ranking_max / total_blocks, 1)


def record_onset(
    database: sqlite3.Connection,
    request_id: str,
    singer_id: str,
    event_id: str,
    position_ms: int,
    caption_path: Path,
) -> dict | None:
    if not caption_path.is_file():
        return None
    try:
        captions = json.loads(caption_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    bars = captions.get("bars", [])
    total_blocks = len(bars)
    if total_blocks == 0 or not 0 <= position_ms <= captions.get("duration_ms", 0):
        return None

    block_index = matching_block(bars, position_ms)
    offcue_window = position_ms // off_cue_rearm_ms() if block_index is None else None
    event_type = "hit" if block_index is not None else "off_cue"
    inserted = database.execute(
        "INSERT OR IGNORE INTO score_events(event_id, request_id, singer_id, event_type, block_index, offcue_window) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, request_id, singer_id, event_type, block_index, offcue_window),
    )
    if not inserted.rowcount:
        return None

    database.execute(
        "INSERT OR IGNORE INTO song_scores(request_id, singer_id) VALUES (?, ?)",
        (request_id, singer_id),
    )
    score = database.execute(
        "SELECT hit_blocks, penalties FROM song_scores WHERE request_id = ? AND singer_id = ?",
        (request_id, singer_id),
    ).fetchone()
    hit_blocks = set(json.loads(score["hit_blocks"]))
    penalties = score["penalties"]
    if block_index is None:
        previous_penalty = database.execute(
            "SELECT 1 FROM score_events WHERE request_id = ? AND singer_id = ? "
            "AND event_type = 'off_cue' AND offcue_window = ? AND event_id != ? LIMIT 1",
            (request_id, singer_id, offcue_window, event_id),
        ).fetchone()
        if previous_penalty:
            event_type = "off_cue_repeat"
        else:
            penalties += 1
    else:
        hit_blocks.add(block_index)
    database.execute(
        "UPDATE song_scores SET hit_blocks = ?, penalties = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE request_id = ? AND singer_id = ?",
        (json.dumps(sorted(hit_blocks)), penalties, request_id, singer_id),
    )
    points = score_value(len(hit_blocks), penalties, total_blocks)
    singer_name = database.execute("SELECT name FROM singers WHERE id = ?", (singer_id,)).fetchone()[0]
    return {
        "type": "score_update",
        "request_id": request_id,
        "singer_id": singer_id,
        "name": singer_name,
        "block_index": block_index,
        "result": event_type,
        "hits": len(hit_blocks),
        "penalties": penalties,
        "total_blocks": total_blocks,
        "points": points,
        "percent": round(points * 100 / max_score(), 1),
        "ranking_max": max_score(),
    }


def score_snapshot(database: sqlite3.Connection, request_id: str, caption_path: Path | None) -> list[dict]:
    if caption_path is None or not caption_path.is_file():
        return []
    try:
        total_blocks = len(json.loads(caption_path.read_text(encoding="utf-8")).get("bars", []))
    except (OSError, json.JSONDecodeError):
        return []
    rows = database.execute(
        "SELECT song_scores.singer_id, singers.name, song_scores.hit_blocks, song_scores.penalties "
        "FROM song_scores JOIN singers ON singers.id = song_scores.singer_id "
        "WHERE song_scores.request_id = ? ORDER BY singers.name",
        (request_id,),
    ).fetchall()
    scores = []
    for row in rows:
        hits = len(json.loads(row["hit_blocks"]))
        points = score_value(hits, row["penalties"], total_blocks)
        scores.append({
            "singer_id": row["singer_id"],
            "name": row["name"],
            "hits": hits,
            "penalties": row["penalties"],
            "total_blocks": total_blocks,
            "points": points,
            "percent": round(points * 100 / max_score(), 1),
            "ranking_max": max_score(),
        })
    return scores