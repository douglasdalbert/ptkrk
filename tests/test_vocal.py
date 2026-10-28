import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.scoring import finalize_performance, score_snapshot, scoreboard_snapshot
from app.storage import connection, initialize
from app.vocal import has_timed_captions, record_vocal_sample, vocal_calibration_ms, vocal_useful_percent


class VocalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database_patch = patch("app.storage.DATABASE_PATH", self.root / "test.sqlite3")
        self.media_patch = patch("app.storage.MEDIA_ROOT", self.root)
        self.environment = patch.dict("os.environ", {
            "KARAOKE_SCORE_MAX": "1000", "KARAOKE_VOCAL_CALIBRATION_MS": "20000",
            "KARAOKE_VOCAL_USEFUL_PERCENT": "80",
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

    def test_remaining_time_formula_and_calibration_uses_only_voiced_time(self):
        score = self.calibrate()
        self.assertEqual(score["calibration_ms"], 20000)
        self.assertEqual(score["points"], 0)
        self.assertEqual(score["level"], 2)
        self.assertAlmostEqual(score["point_rate"], 1000 / 150 / 0.8)
        self.assertEqual(self.sample(31000)["points"], 8.3)
        self.assertEqual(self.sample(32000, jitter=90)["points"], 4.2)
        self.assertEqual(self.sample(33000, voiced=500)["points"], 8.3)

    def test_short_and_long_songs_have_same_normalized_opportunity(self):
        for duration in (60000, 180000, 360000):
            with self.subTest(duration=duration):
                self.database.execute("DELETE FROM vocal_scores")
                self.database.execute("DELETE FROM score_events")
                for position in range(11000, 31000, 1000):
                    self.sample(position, duration=duration)
                for position in range(31000, duration + 1, 1000):
                    score = self.sample(position, duration=duration)
                self.assertEqual(score["points"], 1250)

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
        self.assertEqual(score["points"], 0)

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
        self.assertEqual(self.sample(33000, loudness=0.1)["points"], 0)
        self.assertEqual(self.sample(34000, loudness=0.1)["points"], 0)

    def test_caption_tracks_with_only_empty_text_use_vocal_scoring(self):
        self.captions.write_text(json.dumps({"duration_ms": 180000, "bars": [
            {"block_index": index, "text": text, "start_ms": index * 1000, "end_ms": (index + 1) * 1000}
            for index, text in enumerate(["", "  ", "\n\t", None])
        ]}), encoding="utf-8")
        self.assertFalse(has_timed_captions(self.captions))
        self.calibrate()
        self.assertEqual(self.sample(31000)["result"], "hit")
        self.assertEqual(score_snapshot(self.database, "r", self.captions)[0]["mode"], "vocal")

    def test_live_overflow_is_capped_in_history_and_after_reinitialization(self):
        self.calibrate()
        for position in range(31000, 181000, 1000):
            self.sample(position)
        final = finalize_performance(self.database, "r", self.captions, self.root / "missing.jpg")
        self.assertEqual(score_snapshot(self.database, "r", self.captions)[0]["points"], 1250)
        self.assertEqual(final["points"], 1000)
        self.assertEqual(scoreboard_snapshot(self.database, "s")["personal_best"]["points"], 1000)
        self.database.execute("UPDATE score_history SET points = 1250 WHERE request_id = 'r'")
        self.database.commit()
        initialize()
        self.assertEqual(scoreboard_snapshot(self.database, "s")["personal_best"]["points"], 1000)

    def test_live_overflow_absorbs_penalties_before_final_cap(self):
        self.calibrate()
        for position in range(31000, 171000, 1000):
            self.sample(position)
        score = self.sample(171000, jitter=100)
        self.assertGreater(score["points"], 1000)
        self.assertLess(score["points"], 1250)
        final = finalize_performance(self.database, "r", self.captions, self.root / "missing.jpg")
        self.assertEqual(final["points"], 1000)

    def test_settings_are_bounded(self):
        with patch.dict("os.environ", {"KARAOKE_VOCAL_CALIBRATION_MS": "0", "KARAOKE_VOCAL_USEFUL_PERCENT": "101"}):
            self.assertEqual(vocal_calibration_ms(), 1000)
            self.assertEqual(vocal_useful_percent(), 100)
