import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.scoring import finalize_performance, record_block_result, score_snapshot, scoreboard_snapshot, usable_caption_bars
from app.storage import connection, initialize
from app.vocal import has_timed_captions, record_vocal_sample, vocal_calibration_ms, vocal_evaluation_ms, vocal_points


class VocalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database_patch = patch("app.storage.DATABASE_PATH", self.root / "test.sqlite3")
        self.media_patch = patch("app.storage.MEDIA_ROOT", self.root)
        self.environment = patch.dict("os.environ", {
            "KARAOKE_SCORE_MAX": "1000", "KARAOKE_VOCAL_CALIBRATION_MS": "20000",
            "KARAOKE_VOCAL_EVALUATION_MS": "40000",
        })
        self.database_patch.start()
        self.media_patch.start()
        self.environment.start()
        initialize()
        self.context = connection()
        self.database = self.context.__enter__()
        self.database.execute("INSERT INTO singers(id, name, session_hash) VALUES ('s', 'Cantor', 'hash')")
        self.database.execute(
            "INSERT INTO requests(id, singer_id, video_id, status) VALUES ('r', 's', 'abcdefghijk', 'ready')"
        )
        self.captions = self.root / "missing.json"

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.environment.stop()
        self.media_patch.stop()
        self.database_patch.stop()
        self.directory.cleanup()

    def sample(self, position, voiced=1000, loudness=0.2, jitter=10, event=None, duration=180000):
        return record_vocal_sample(
            self.database, "r", "s", event or f"sample-{position}", position, duration,
            voiced, loudness, jitter, self.captions,
        )

    def calibrate(self):
        for position in range(11000, 31000, 1000):
            score = self.sample(position)
        return score

    def test_quality_formula_and_calibration_uses_only_voiced_time(self):
        score = self.calibrate()
        self.assertEqual(score["calibration_ms"], 20000)
        self.assertEqual(score["points"], 0)
        self.assertEqual(score["level"], 2)
        self.assertEqual(score["evaluated_ms"], 0)
        self.assertEqual(self.sample(31000)["points"], 25)
        self.assertEqual(self.sample(32000, jitter=90)["points"], 25)
        self.assertEqual(self.sample(33000, voiced=500)["points"], 37.5)

    def test_short_and_long_songs_have_same_normalized_opportunity(self):
        for duration in (60000, 180000, 360000):
            with self.subTest(duration=duration):
                self.database.execute("DELETE FROM vocal_scores")
                self.database.execute("DELETE FROM score_events")
                for position in range(1000, 21000, 1000):
                    self.sample(position, duration=duration)
                for position in range(21000, duration + 1, 1000):
                    score = self.sample(position, duration=duration)
                self.assertEqual(score["points"], 1000)

    def test_three_hits_raise_five_misses_lower_and_opposite_resets(self):
        self.calibrate()
        self.sample(31000)
        self.sample(32000)
        self.assertEqual(self.sample(33000)["level"], 3)
        for position in range(34000, 38000, 1000):
            self.assertEqual(self.sample(position, jitter=100)["level"], 3)
        self.assertEqual(self.sample(38000, jitter=100)["level"], 2)
        self.sample(39000)
        self.sample(40000)
        self.sample(41000, jitter=100)
        self.assertEqual(self.sample(42000)["level"], 2)
        self.assertEqual(self.sample(43000)["level"], 2)
        self.assertEqual(self.sample(44000)["level"], 3)
        for position in range(45000, 70000, 1000):
            score = self.sample(position, jitter=100)
        self.assertEqual(score["level"], 0)
        self.assertGreater(score["points"], 0)
        self.assertLess(score["points"], 1000)

    def test_duplicates_overlap_invalid_and_uncertain_samples_do_not_count(self):
        score = self.sample(1000)
        self.assertEqual(score["calibration_ms"], 1000)
        self.assertIsNone(self.sample(2000, event="sample-1000"))
        self.assertIsNone(self.sample(1500))
        for kwargs in ({"voiced": 0}, {"voiced": 499}, {"voiced": 1001},
                       {"loudness": float("nan")}, {"loudness": 0}, {"jitter": float("inf")}):
            self.assertIsNone(self.sample(3000, **kwargs))
        self.assertEqual(score_snapshot(self.database, "r", self.captions)[0]["calibration_ms"], 1000)
        self.assertEqual(self.sample(30000)["calibration_ms"], 2000)

    def test_timed_captions_exclusively_use_existing_scoring(self):
        self.captions.write_text('{"bars": []}', encoding="utf-8")
        self.assertFalse(has_timed_captions(self.captions))
        self.calibrate()
        self.assertEqual(score_snapshot(self.database, "r", self.captions)[0]["mode"], "vocal")
        self.captions.write_text('{"bars": [{"block_index": 0, "start_ms": 1000, "end_ms": 2000}]}',
                                 encoding="utf-8")
        self.assertTrue(has_timed_captions(self.captions))
        self.assertIsNone(self.sample(31000))
        self.assertEqual(score_snapshot(self.database, "r", self.captions), [])

    def test_quieter_voice_penalty_and_nonnegative_balance(self):
        self.calibrate()
        self.sample(31000)
        self.assertEqual(self.sample(32000, loudness=0.1)["result"], "miss")
        self.assertEqual(self.sample(33000, loudness=0.1)["points"], 25)
        self.assertEqual(self.sample(34000, loudness=0.1)["points"], 25)

    def test_caption_tracks_with_only_empty_text_use_vocal_scoring(self):
        self.captions.write_text(json.dumps({"duration_ms": 180000, "bars": [
            {"block_index": index, "text": text, "start_ms": index * 1000, "end_ms": (index + 1) * 1000}
            for index, text in enumerate(["", "  ", "\n\t", None])
        ]}), encoding="utf-8")
        self.assertFalse(has_timed_captions(self.captions))
        self.calibrate()
        self.assertEqual(self.sample(31000)["result"], "hit")
        self.assertEqual(score_snapshot(self.database, "r", self.captions)[0]["mode"], "vocal")

    def test_mixed_captions_ignore_empty_blocks_and_preserve_valid_indices(self):
        self.captions.write_text(json.dumps({"duration_ms": 10000, "bars": [
            {"block_index": index, "text": text, "start_ms": index * 1000, "end_ms": (index + 1) * 1000}
            for index, text in enumerate(["", "Primeiro verso", " \t ", "Segundo verso"])
        ]}), encoding="utf-8")
        self.assertTrue(has_timed_captions(self.captions))
        self.assertIsNone(self.sample(31000))
        self.assertIsNone(record_block_result(self.database, "r", "s", "empty", 0, True, self.captions))
        first = record_block_result(self.database, "r", "s", "first", 1, True, self.captions)
        self.assertEqual(first["total_blocks"], 2)
        self.assertEqual(first["points"], 500)
        second = record_block_result(self.database, "r", "s", "second", 3, True, self.captions)
        self.assertEqual(second["points"], 1000)
        self.assertEqual(score_snapshot(self.database, "r", self.captions)[0]["total_blocks"], 2)

    def test_caption_coverage_threshold_boundaries_overlap_and_configuration(self):
        for covered, accepted in [(1499, False), (1500, True), (1501, True)]:
            with self.subTest(covered=covered):
                captions = {"duration_ms": 10000, "bars": [
                    {"text": "Verso", "start_ms": 0, "end_ms": covered},
                ]}
                self.assertEqual(bool(usable_caption_bars(captions)), accepted)
        captions = {"duration_ms": 10000, "bars": [
            {"text": "Verso", "start_ms": -500, "end_ms": 1000},
            {"text": "Repetido", "start_ms": 0, "end_ms": 1000},
            {"text": " ", "start_ms": 1000, "end_ms": 10000},
        ]}
        self.assertEqual(usable_caption_bars(captions), [])
        with patch.dict("os.environ", {"KARAOKE_CAPTION_MIN_COVERAGE_PERCENT": "10"}):
            self.assertEqual(len(usable_caption_bars(captions)), 2)
        with patch.dict("os.environ", {"KARAOKE_CAPTION_MIN_COVERAGE_PERCENT": "0"}):
            self.assertEqual(len(usable_caption_bars(captions)), 2)

    def test_sparse_caption_file_uses_vocal_calibration_and_score(self):
        self.captions.write_text(json.dumps({"duration_ms": 180000, "bars": [
            {"block_index": 0, "text": "Verso", "start_ms": 120000, "end_ms": 143000},
        ]}), encoding="utf-8")
        self.assertFalse(has_timed_captions(self.captions))
        self.calibrate()
        self.assertEqual(self.sample(31000)["result"], "hit")
        self.assertEqual(score_snapshot(self.database, "r", self.captions)[0]["mode"], "vocal")

    def test_quality_score_is_preserved_in_history_and_after_reinitialization(self):
        self.calibrate()
        for position in range(31000, 181000, 1000):
            self.sample(position)
        final = finalize_performance(self.database, "r", self.captions, self.root / "missing.jpg")
        self.assertEqual(score_snapshot(self.database, "r", self.captions)[0]["points"], 1000)
        self.assertEqual(final["points"], 1000)
        self.assertEqual(scoreboard_snapshot(self.database, "s")["personal_best"]["points"], 1000)
        self.database.execute("UPDATE score_history SET points = 1250 WHERE request_id = 'r'")
        self.database.commit()
        initialize()
        self.assertEqual(scoreboard_snapshot(self.database, "s")["personal_best"]["points"], 1000)

    def test_errors_reduce_quality_after_minimum_evidence(self):
        self.calibrate()
        for position in range(31000, 171000, 1000):
            self.sample(position)
        score = self.sample(171000, jitter=100)
        self.assertGreater(score["points"], 990)
        self.assertLess(score["points"], 1000)
        final = finalize_performance(self.database, "r", self.captions, self.root / "missing.jpg")
        self.assertEqual(final["points"], score["points"])

    def test_exact_quality_examples_and_minimum_evidence(self):
        for hits, errors, expected in [
            (20, 0, 500), (39, 0, 975), (40, 0, 1000),
            (40, 5, 888.9), (40, 10, 800), (20, 20, 500),
        ]:
            self.assertEqual(round(vocal_points(hits * 1000, (hits + errors) * 1000), 1), expected)
        self.assertEqual(vocal_points(1000, 1000), 25)
        self.assertEqual(vocal_points(10000, 10000), 250)
        self.assertEqual(vocal_points(0, 20000), 0)
        self.assertEqual(vocal_points(0, 0), 0)
        with patch.dict("os.environ", {"KARAOKE_SCORE_MAX": "2000", "KARAOKE_VOCAL_EVALUATION_MS": "10000"}):
            self.assertEqual(vocal_points(10000, 10000), 2000)

    def test_quality_durations_survive_reinitialization_without_double_counting(self):
        self.calibrate()
        self.sample(31000, voiced=500)
        self.sample(32000, jitter=100)
        self.database.commit()
        initialize()
        initialize()
        score = score_snapshot(self.database, "r", self.captions)[0]
        self.assertEqual(score["evaluated_ms"], 1500)
        self.assertEqual(score["points"], 12.5)

    def test_legacy_active_scores_migrate_to_quality_without_resetting_calibration(self):
        self.database.execute("ALTER TABLE vocal_scores DROP COLUMN hit_ms")
        self.database.execute("ALTER TABLE vocal_scores DROP COLUMN evaluated_ms")
        self.database.execute(
            "INSERT INTO vocal_scores(request_id,singer_id,calibration_ms,point_rate,hits,penalties,points) "
            "VALUES ('r','s',20000,17.35,39,5,633)"
        )
        self.database.commit()
        initialize()
        score = score_snapshot(self.database, "r", self.captions)[0]
        self.assertEqual(score["level"], 2)
        self.assertEqual(score["evaluated_ms"], 44000)
        self.assertEqual(score["points"], 886.4)

    def test_default_evaluation_requires_forty_seconds(self):
        with patch.dict("os.environ"):
            os.environ.pop("KARAOKE_VOCAL_EVALUATION_MS", None)
            self.assertEqual(vocal_evaluation_ms(), 40000)
            self.assertEqual(vocal_points(20000, 20000), 500)
            self.assertEqual(vocal_points(40000, 40000), 1000)

    def test_settings_are_bounded(self):
        with patch.dict("os.environ", {"KARAOKE_VOCAL_CALIBRATION_MS": "0", "KARAOKE_VOCAL_EVALUATION_MS": "999999"}):
            self.assertEqual(vocal_calibration_ms(), 1000)
            self.assertEqual(vocal_evaluation_ms(), 120000)
