import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.captions import parse_vtt_caption_bars
from app.scoring import block_duration_ms, matching_block, max_score, microphone_rms_threshold, microphone_silence_ms, onset_tolerance_ms, record_offcue_penalty, record_onset, score_lane_count, score_snapshot, score_value
from app.storage import connection, initialize


class ScoringTests(unittest.TestCase):
    def test_hit_and_penalty_normalization_matches_expected_example(self):
        with patch("app.scoring.off_cue_penalty", return_value=1):
            self.assertEqual(score_value(90, 5, 100, ranking_max=1000), 850.0)
            self.assertEqual(score_value(90, 0, 100, ranking_max=1000), 900.0)

    def test_score_is_clamped_and_reports_one_decimal(self):
        self.assertEqual(score_value(200, 0, 100, ranking_max=1000), 1000.0)
        self.assertEqual(score_value(1, 0, 3, ranking_max=1000), 333.3)
        self.assertEqual(score_value(10, 20, 100, ranking_max=1000), 0.0)

    def test_score_configuration_is_environment_controlled(self):
        with patch.dict("os.environ", {
            "KARAOKE_SCORE_MAX": "2000",
            "KARAOKE_SCORE_BLOCK_MS": "750",
            "KARAOKE_SCORE_LANES": "4",
            "KARAOKE_SCORE_TOLERANCE_MS": "100",
            "KARAOKE_SCORE_RMS_THRESHOLD": "0.08",
            "KARAOKE_SCORE_SILENCE_MS": "450",
        }):
            self.assertEqual(max_score(), 2000)
            self.assertEqual(block_duration_ms(), 750)
            self.assertEqual(score_lane_count(), 4)
            self.assertEqual(onset_tolerance_ms(), 100)
            self.assertEqual(microphone_rms_threshold(), 0.08)
            self.assertEqual(microphone_silence_ms(), 450)
            result = parse_vtt_caption_bars(
                """WEBVTT

00:00:00.000 --> 00:00:02.000
This is a phrase
""",
                2000,
            )
            self.assertEqual([bar["score_duration_ms"] for bar in result["bars"]], [750])
            self.assertEqual(result["bars"][0]["score_window_end_ms"], 100)

    def test_score_lane_count_defaults_to_three_and_is_bounded(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(score_lane_count(), 3)
        with patch.dict("os.environ", {"KARAOKE_SCORE_LANES": "0"}):
            self.assertEqual(score_lane_count(), 1)
        with patch.dict("os.environ", {"KARAOKE_SCORE_LANES": "99"}):
            self.assertEqual(score_lane_count(), 10)

    def test_match_requires_onset_window_not_caption_duration(self):
        bars = [
            {"block_index": 0, "start_ms": 1000, "score_window_start_ms": 900, "score_window_end_ms": 1100},
            {"block_index": 1, "start_ms": 2000, "score_window_start_ms": 1900, "score_window_end_ms": 2100},
        ]
        self.assertEqual(matching_block(bars, 899), None)
        self.assertEqual(matching_block(bars, 900), 0)
        self.assertEqual(matching_block(bars, 1100), 0)
        self.assertEqual(matching_block(bars, 1101), None)

    def test_one_hundred_ms_margin_rejects_899_and_1101_for_bar_at_1000(self):
        bar = {
            "block_index": 0,
            "start_ms": 1000,
            "score_window_start_ms": 900,
            "score_window_end_ms": 1100,
        }
        self.assertEqual(matching_block([bar], 899), None)
        self.assertEqual(matching_block([bar], 900), 0)
        self.assertEqual(matching_block([bar], 1100), 0)
        self.assertEqual(matching_block([bar], 1101), None)

    def test_persisted_hit_is_deduplicated_and_off_cue_onset_is_penalized(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "karaoke.sqlite3"
            with patch("app.storage.DATABASE_PATH", database_path):
                initialize()
                with connection() as database:
                    database.execute(
                        "INSERT INTO singers(id,name,session_hash) VALUES ('singer','Cantor','hash')"
                    )
                    database.execute(
                        "INSERT INTO requests(id,singer_id,video_id,status) "
                        "VALUES ('song','singer','abcdefghijk','ready')"
                    )
                    sidecar = Path(directory) / "captions.json"
                    sidecar.write_text(json.dumps({"duration_ms": 10000, "bars": [
                        {"block_index": 0, "start_ms": 1000, "end_ms": 2000,
                         "score_window_start_ms": 900, "score_window_end_ms": 1100},
                        {"block_index": 1, "start_ms": 5000, "end_ms": 6000,
                         "score_window_start_ms": 4900, "score_window_end_ms": 5100},
                    ]}), encoding="utf-8")
                    hit = record_onset(database, "song", "singer", "hit-1", 1000, sidecar)
                    duplicate_hit = record_onset(database, "song", "singer", "hit-2", 1050, sidecar)
                    off_cue = record_onset(database, "song", "singer", "off-1", 3000, sidecar)
                    repeated_off_cue = record_onset(database, "song", "singer", "off-2", 3200, sidecar)
                    later_off_cue = record_onset(database, "song", "singer", "off-3", 3600, sidecar)
                    self.assertEqual(hit["result"], "hit")
                    self.assertEqual(duplicate_hit["hits"], 1)
                    self.assertEqual(off_cue["result"], "off_cue")
                    self.assertEqual(repeated_off_cue["result"], "off_cue_repeat")
                    self.assertEqual(repeated_off_cue["penalties"], 1)
                    self.assertEqual(later_off_cue["penalties"], 2)
                    result = score_snapshot(database, "song", sidecar)[0]
                    self.assertEqual(result["hits"], 1)
                    self.assertEqual(result["penalties"], 2)
                    self.assertEqual(result["points"], 0.0)

    def test_offcue_speech_penalty_uses_configured_amount_and_rearm_window(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "karaoke.sqlite3"
            with patch("app.storage.DATABASE_PATH", database_path), patch.dict(
                "os.environ", {"KARAOKE_SCORE_OFF_CUE_PENALTY": "2", "KARAOKE_SCORE_OFF_CUE_REARM_MS": "500"}
            ):
                initialize()
                with connection() as database:
                    database.execute(
                        "INSERT INTO singers(id,name,session_hash) VALUES ('singer','Cantor','hash')"
                    )
                    database.execute(
                        "INSERT INTO requests(id,singer_id,video_id,status) "
                        "VALUES ('song','singer','abcdefghijk','ready')"
                    )
                    sidecar = Path(directory) / "captions.json"
                    sidecar.write_text(json.dumps({"duration_ms": 10000, "bars": [
                        {"block_index": 0, "start_ms": 1000, "end_ms": 2000},
                    ]}), encoding="utf-8")
                    first = record_offcue_penalty(database, "song", "singer", "off-1", 3000, sidecar)
                    repeated = record_offcue_penalty(database, "song", "singer", "off-2", 3200, sidecar)
                    before_rearm = record_offcue_penalty(database, "song", "singer", "off-3", 3499, sidecar)
                    after_rearm = record_offcue_penalty(database, "song", "singer", "off-4", 3500, sidecar)
                    self.assertEqual(first["penalties"], 1)
                    self.assertEqual(first["penalty_value"], 2)
                    self.assertEqual(first["points"], 0.0)
                    self.assertEqual(repeated["result"], "off_cue_repeat")
                    self.assertEqual(repeated["penalties"], 1)
                    self.assertEqual(before_rearm["penalties"], 1)
                    self.assertEqual(after_rearm["penalties"], 2)


if __name__ == "__main__":
    unittest.main()
