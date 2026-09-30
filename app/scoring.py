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


def score_lane_count() -> int:
    return integer_setting("KARAOKE_SCORE_LANES", 3, 1, 10)


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


def microphone_silence_ms() -> int:
    return integer_setting("KARAOKE_SCORE_SILENCE_MS", 300, 0, 5000)


def boost_fill_percent() -> int:
    return integer_setting("KARAOKE_BOOST_FILL_PERCENT", 25, 1, 100)


def boost_duration_ms() -> int:
    return integer_setting("KARAOKE_BOOST_DURATION_MS", 15000, 1000, 300000)


def boost_loudness_percent() -> int:
    return integer_setting("KARAOKE_BOOST_LOUDNESS_PERCENT", 40, 5, 500)


def star_low_percent() -> int:
    return integer_setting("KARAOKE_STAR_LOW_PERCENT", 25, 0, 100)


def star_high_percent() -> int:
    return integer_setting("KARAOKE_STAR_HIGH_PERCENT", 75, 0, 100)


def star_nice_percent() -> float:
    try:
        value = float(os.getenv("KARAOKE_STAR_NICE_PERCENT", "91"))
    except (TypeError, ValueError):
        return 91.0
    return max(0.0, min(100.0, value)) if math.isfinite(value) else 91.0


def star_close_ms() -> int:
    return integer_setting("KARAOKE_STAR_CLOSE_MS", 30000, 0, 600000)


def boost_multiplier() -> float:
    try:
        value = float(os.getenv("KARAOKE_BOOST_MULTIPLIER", "1.5"))
    except (TypeError, ValueError):
        return 1.5
    return max(1.0, min(5.0, value)) if math.isfinite(value) else 1.5


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
    earned_points = min(total_blocks, hits) * ranking_max / total_blocks
    return round(max(0, earned_points - penalties * off_cue_penalty()), 1)


def _score_units(score: sqlite3.Row, total_blocks: int) -> float:
    units = score["score_units"]
    if units is None:
        units = len(json.loads(score["hit_blocks"])) - score["penalties"] * off_cue_penalty()
    return max(0.0, min(float(total_blocks), float(units)))


def _apply_off_cue_penalty(score_units: float, total_blocks: int) -> float:
    current_points = _points_for_units(score_units, total_blocks)
    remaining_points = max(0.0, current_points - off_cue_penalty())
    return remaining_points * total_blocks / max_score()


def _points_for_units(units: float, total_blocks: int, boosted_hits: int = 0) -> float:
    if total_blocks <= 0:
        return 0.0
    # Boost bonus is extra credit on top of the regular ranking, so it may exceed max_score().
    bonus = boosted_hits * (boost_multiplier() - 1)
    return round((max(0, min(total_blocks, units)) + bonus) * max_score() / total_blocks, 1)


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
        "SELECT hit_blocks, penalties, score_units FROM song_scores WHERE request_id = ? AND singer_id = ?",
        (request_id, singer_id),
    ).fetchone()
    hit_blocks = set(json.loads(score["hit_blocks"]))
    penalties = score["penalties"]
    score_units = _score_units(score, total_blocks)
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
            score_units = _apply_off_cue_penalty(score_units, total_blocks)
    else:
        if block_index not in hit_blocks:
            hit_blocks.add(block_index)
            score_units = min(total_blocks, score_units + 1)
    database.execute(
        "UPDATE song_scores SET hit_blocks = ?, penalties = ?, score_units = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE request_id = ? AND singer_id = ?",
        (json.dumps(sorted(hit_blocks)), penalties, score_units, request_id, singer_id),
    )
    points = _points_for_units(score_units, total_blocks)
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


def record_block_result(
    database: sqlite3.Connection,
    request_id: str,
    singer_id: str,
    event_id: str,
    block_index: int,
    hit: bool,
    caption_path: Path,
    boosted: bool = False,
) -> dict | None:
    if not caption_path.is_file():
        return None
    try:
        bars = json.loads(caption_path.read_text(encoding="utf-8")).get("bars", [])
    except (OSError, json.JSONDecodeError):
        return None
    if not any(int(bar.get("block_index", index)) == block_index for index, bar in enumerate(bars)):
        return None
    already_scored = database.execute(
        "SELECT 1 FROM score_events WHERE request_id = ? AND singer_id = ? AND block_index = ? "
        "AND event_type IN ('hit', 'miss') LIMIT 1",
        (request_id, singer_id, block_index),
    ).fetchone()
    if already_scored:
        return None

    event_type = "hit" if hit else "miss"
    inserted = database.execute(
        "INSERT OR IGNORE INTO score_events(event_id, request_id, singer_id, event_type, block_index) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_id, request_id, singer_id, event_type, block_index),
    )
    if not inserted.rowcount:
        return None
    database.execute(
        "INSERT OR IGNORE INTO song_scores(request_id, singer_id) VALUES (?, ?)",
        (request_id, singer_id),
    )
    score = database.execute(
        "SELECT hit_blocks, boosted_blocks, penalties, score_units FROM song_scores "
        "WHERE request_id = ? AND singer_id = ?",
        (request_id, singer_id),
    ).fetchone()
    hit_blocks = set(json.loads(score["hit_blocks"]))
    boosted_blocks = set(json.loads(score["boosted_blocks"]))
    score_units = _score_units(score, len(bars))
    boosted_hit = False
    if hit:
        if block_index not in hit_blocks:
            hit_blocks.add(block_index)
            score_units = min(len(bars), score_units + 1)
            if boosted:
                boosted_blocks.add(block_index)
                boosted_hit = True
    database.execute(
        "UPDATE song_scores SET hit_blocks = ?, boosted_blocks = ?, score_units = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE request_id = ? AND singer_id = ?",
        (json.dumps(sorted(hit_blocks)), json.dumps(sorted(boosted_blocks)), score_units, request_id, singer_id),
    )
    total_blocks = len(bars)
    points = _points_for_units(score_units, total_blocks, len(boosted_blocks))
    bonus_points = round(max_score() * (boost_multiplier() - 1) / total_blocks, 1) if boosted_hit else 0.0
    singer_name = database.execute("SELECT name FROM singers WHERE id = ?", (singer_id,)).fetchone()[0]
    return {
        "type": "score_update",
        "request_id": request_id,
        "singer_id": singer_id,
        "name": singer_name,
        "block_index": block_index,
        "result": event_type,
        "hits": len(hit_blocks),
        "boosted": boosted_hit,
        "boosted_hits": len(boosted_blocks),
        "bonus_points": bonus_points,
        "penalties": score["penalties"],
        "total_blocks": total_blocks,
        "points": points,
        "percent": round(points * 100 / max_score(), 1),
        "ranking_max": max_score(),
    }


def record_offcue_penalty(
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
    total_blocks = len(captions.get("bars", []))
    if total_blocks == 0 or not 0 <= position_ms <= captions.get("duration_ms", 0):
        return None

    previous_penalty = database.execute(
        "SELECT 1 FROM score_events WHERE request_id = ? AND singer_id = ? "
        "AND event_type = 'off_cue_live' AND offcue_window > ? AND offcue_window <= ? LIMIT 1",
        (request_id, singer_id, position_ms - off_cue_rearm_ms(), position_ms),
    ).fetchone()
    event_type = "off_cue_live_repeat" if previous_penalty else "off_cue_live"
    inserted = database.execute(
        "INSERT OR IGNORE INTO score_events(event_id, request_id, singer_id, event_type, offcue_window) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_id, request_id, singer_id, event_type, position_ms),
    )
    if not inserted.rowcount:
        return None
    database.execute(
        "INSERT OR IGNORE INTO song_scores(request_id, singer_id) VALUES (?, ?)",
        (request_id, singer_id),
    )
    score = database.execute(
        "SELECT hit_blocks, boosted_blocks, penalties, score_units FROM song_scores "
        "WHERE request_id = ? AND singer_id = ?",
        (request_id, singer_id),
    ).fetchone()
    penalties = score["penalties"] + (0 if previous_penalty else 1)
    score_units = _score_units(score, total_blocks)
    if not previous_penalty:
        score_units = _apply_off_cue_penalty(score_units, total_blocks)
    database.execute(
        "UPDATE song_scores SET penalties = ?, score_units = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE request_id = ? AND singer_id = ?",
        (penalties, score_units, request_id, singer_id),
    )
    points = _points_for_units(score_units, total_blocks, len(json.loads(score["boosted_blocks"])))
    singer_name = database.execute("SELECT name FROM singers WHERE id = ?", (singer_id,)).fetchone()[0]
    return {
        "type": "score_update",
        "request_id": request_id,
        "singer_id": singer_id,
        "name": singer_name,
        "block_index": None,
        "result": "off_cue" if not previous_penalty else "off_cue_repeat",
        "hits": len(json.loads(score["hit_blocks"])),
        "penalties": penalties,
        "penalty_value": off_cue_penalty(),
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
        "SELECT song_scores.singer_id, singers.name, song_scores.hit_blocks, song_scores.boosted_blocks, "
        "song_scores.penalties, song_scores.score_units "
        "FROM song_scores JOIN singers ON singers.id = song_scores.singer_id "
        "WHERE song_scores.request_id = ? ORDER BY singers.name",
        (request_id,),
    ).fetchall()
    scores = []
    for row in rows:
        hits = len(json.loads(row["hit_blocks"]))
        points = _points_for_units(_score_units(row, total_blocks), total_blocks,
                                   len(json.loads(row["boosted_blocks"])))
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


def finalize_performance(
    database: sqlite3.Connection,
    request_id: str,
    caption_path: Path,
    preview_path: Path,
) -> dict | None:
    item = database.execute(
        "SELECT singer_id, video_id, title FROM requests WHERE id = ?", (request_id,)
    ).fetchone()
    if item is None:
        return None
    scores = score_snapshot(database, request_id, caption_path)
    lead_score = next((score for score in scores if score["singer_id"] == item["singer_id"]), None)
    try:
        preview = preview_path.read_bytes() if preview_path.is_file() else None
    except OSError:
        preview = None
    participants = [{"singer_id": item["singer_id"], "name": database.execute(
        "SELECT name FROM singers WHERE id = ?", (item["singer_id"],)
    ).fetchone()[0]}, *[dict(row) for row in database.execute(
            "SELECT singers.id AS singer_id, singers.name FROM backvocals "
            "JOIN singers ON singers.id = backvocals.singer_id "
            "WHERE backvocals.request_id = ? AND backvocals.joined = 1 ORDER BY backvocals.rowid",
            (request_id,),
        )]]
    database.executemany(
        "INSERT OR IGNORE INTO score_history(request_id, singer_id, title, video_id, points, preview, participants) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(request_id, participant["singer_id"], item["title"] or item["video_id"], item["video_id"],
          lead_score["points"] if lead_score else 0, preview, json.dumps(participants))
         for participant in participants],
    )
    return performance_record(database, request_id)


def performance_record(database: sqlite3.Connection, request_id: str) -> dict | None:
    row = database.execute(
        "SELECT request_id, title, video_id, points, preview IS NOT NULL AS has_preview, participants "
        "FROM score_history WHERE request_id = ? LIMIT 1",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        **{key: row[key] for key in ("request_id", "title", "video_id", "points", "has_preview")},
        "participants": json.loads(row["participants"]),
    }


def scoreboard_snapshot(database: sqlite3.Connection, singer_id: str | None = None) -> dict:
    personal = None
    rank = None
    if singer_id:
        personal_row = database.execute(
            "SELECT request_id FROM score_history WHERE singer_id = ? "
            "ORDER BY points DESC, finished_at DESC, rowid DESC LIMIT 1",
            (singer_id,),
        ).fetchone()
        if personal_row:
            personal = performance_record(database, personal_row["request_id"])
            rank = 1 + database.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT singer_id, MAX(points) AS best_points FROM score_history GROUP BY singer_id"
                ") WHERE best_points > ?",
                (personal["points"],),
            ).fetchone()[0]
    party_row = database.execute(
            "SELECT request_id FROM score_history ORDER BY points DESC, "
            "CASE WHEN singer_id = ? THEN 0 ELSE 1 END, finished_at DESC, rowid DESC LIMIT 1",
            (singer_id,),
    ).fetchone()
    return {
        "personal_best": personal,
        "personal_rank": rank,
        "party_best": performance_record(database, party_row["request_id"]) if party_row else None,
    }