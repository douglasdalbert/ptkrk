import json
import math
import sqlite3
from pathlib import Path

from app.scoring import boost_multiplier, integer_setting, max_score, usable_caption_bars


def vocal_calibration_ms() -> int:
    return integer_setting("KARAOKE_VOCAL_CALIBRATION_MS", 5000, 1000, 120000)


def vocal_evaluation_ms() -> int:
    return integer_setting("KARAOKE_VOCAL_EVALUATION_MS", 40000, 1000, 120000)


def vocal_boost_fill_ms() -> int:
    return integer_setting("KARAOKE_VOCAL_BOOST_FILL_MS", 40000, 1000, 720000)


def vocal_boost_charge(database: sqlite3.Connection, request_id: str) -> float:
    row = database.execute(
        "SELECT calibration_ms + evaluated_ms AS singing_ms FROM vocal_scores "
        "JOIN requests ON requests.id = vocal_scores.request_id "
        "AND requests.singer_id = vocal_scores.singer_id WHERE requests.id = ?", (request_id,),
    ).fetchone()
    return min(1.0, row["singing_ms"] / vocal_boost_fill_ms()) if row else 0.0


def vocal_points(hit_ms: int, evaluated_ms: int, boosted_ms: int = 0) -> float:
    if evaluated_ms <= 0:
        return 0.0
    quality = hit_ms / evaluated_ms
    bonus = max_score() * boosted_ms / vocal_evaluation_ms() * (boost_multiplier() - 1)
    return max_score() * quality * min(1, evaluated_ms / vocal_evaluation_ms()) + bonus


def has_timed_captions(path: Path | None) -> bool:
    if path is None or not path.is_file():
        return False
    try:
        return bool(usable_caption_bars(json.loads(path.read_text(encoding="utf-8"))))
    except (OSError, json.JSONDecodeError):
        return False


def vocal_snapshot(database: sqlite3.Connection, request_id: str) -> list[dict]:
    return [{
        "singer_id": row["singer_id"],
        "request_id": request_id,
        "name": row["name"],
        "mode": "vocal",
        "points": round(vocal_points(row["hit_ms"], row["evaluated_ms"], row["boosted_ms"]), 1),
        "percent": round(vocal_points(row["hit_ms"], row["evaluated_ms"], row["boosted_ms"]) * 100 / max_score(), 1),
        "ranking_max": max_score(),
        "hits": row["hits"],
        "penalties": row["penalties"],
        "calibration_ms": row["calibration_ms"],
        "calibration_target_ms": vocal_calibration_ms(),
        "level": row["level"] if row["point_rate"] is not None else -1,
        "evaluated_ms": row["evaluated_ms"],
        "singing_ms": row["calibration_ms"] + row["evaluated_ms"],
        "boosted_ms": row["boosted_ms"],
        "evaluation_target_ms": vocal_evaluation_ms(),
    } for row in database.execute(
        "SELECT vocal_scores.*, singers.name FROM vocal_scores "
        "JOIN singers ON singers.id = vocal_scores.singer_id WHERE request_id = ? ORDER BY singers.name",
        (request_id,),
    )]


def record_vocal_sample(
    database: sqlite3.Connection, request_id: str, singer_id: str, event_id: str,
    position_ms: int, duration_ms: int, voiced_ms: int, loudness: float, jitter: float,
    caption_path: Path | None,
) -> dict | None:
    if has_timed_captions(caption_path):
        return None
    if (not 500 <= voiced_ms <= 1000 or not 1000 <= position_ms <= duration_ms <= 720000
            or not math.isfinite(loudness) or not 0 < loudness <= 1
            or not math.isfinite(jitter) or not 0 <= jitter <= 200):
        return None
    database.execute(
        "INSERT OR IGNORE INTO vocal_scores(request_id, singer_id) VALUES (?, ?)", (request_id, singer_id)
    )
    row = database.execute(
        "SELECT * FROM vocal_scores WHERE request_id = ? AND singer_id = ?", (request_id, singer_id)
    ).fetchone()
    if position_ms - row["last_position_ms"] < 1000:
        return None
    inserted = database.execute(
        "INSERT OR IGNORE INTO score_events(event_id, request_id, singer_id, event_type) VALUES (?, ?, ?, 'vocal_sample')",
        (event_id, request_id, singer_id),
    )
    if not inserted.rowcount:
        return None

    calibration = row["calibration_ms"]
    loudness_sum, jitter_sum = row["loudness_sum"], row["jitter_sum"]
    rate = row["point_rate"]
    points, hits, penalties = row["points"], row["hits"], row["penalties"]
    hit_ms, evaluated_ms = row["hit_ms"], row["evaluated_ms"]
    boosted_ms = row["boosted_ms"]
    boosted = False
    level, good, bad = row["level"], row["good_streak"], row["bad_streak"]
    result = "calibrating"
    feedback = "calibrating"
    if rate is None:
        calibration += voiced_ms
        loudness_sum += loudness * voiced_ms
        jitter_sum += jitter * voiced_ms
        if calibration >= vocal_calibration_ms():
            # The legacy nullable rate column now only marks completed calibration.
            rate = 0
    else:
        # Compare short-term changes, not absolute notes: melodic transitions are handled by the singer.
        tolerance = max(45, min(70, jitter_sum / calibration * 1.8))
        hit = jitter <= tolerance and loudness >= loudness_sum / calibration * 0.55
        feedback = ("recovering" if bad else "streak" if good >= 2 else "steady") if hit else (
            "unstable" if jitter > tolerance else "quiet"
        )
        result = "hit" if hit else "miss"
        evaluated_ms += voiced_ms
        hit_ms += voiced_ms if hit else 0
        boost_setting = database.execute("SELECT value FROM runtime_state WHERE key = 'boost_state'").fetchone()
        boost = json.loads(boost_setting["value"]) if boost_setting else None
        boosted = bool(hit and boost and boost.get("request_id") == request_id and boost.get("active"))
        boosted_ms += voiced_ms if boosted else 0
        points = vocal_points(hit_ms, evaluated_ms, boosted_ms)
        hits += int(hit)
        penalties += int(not hit)
        good, bad = (good + 1, 0) if hit else (0, bad + 1)
        if good and good % 3 == 0:
            level = min(3, level + 1)
        if bad and bad % 5 == 0:
            level = max(0, level - 1)
    database.execute(
        "UPDATE vocal_scores SET calibration_ms = ?, loudness_sum = ?, jitter_sum = ?, point_rate = ?, "
        "points = ?, hits = ?, penalties = ?, level = ?, good_streak = ?, bad_streak = ?, last_position_ms = ?, "
        "hit_ms = ?, evaluated_ms = ?, boosted_ms = ? "
        "WHERE request_id = ? AND singer_id = ?",
        (calibration, loudness_sum, jitter_sum, rate, points, hits, penalties, level, good, bad,
         position_ms, hit_ms, evaluated_ms, boosted_ms, request_id, singer_id),
    )
    score = next(score for score in vocal_snapshot(database, request_id) if score["singer_id"] == singer_id)
    return {**score, "type": "score_update", "result": result, "feedback": feedback, "boosted": boosted}
