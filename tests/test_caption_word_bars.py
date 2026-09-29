import unittest

from app.captions import parse_vtt_caption_bars, points_for_caption_onset, select_native_caption_language


class CaptionWordBarTests(unittest.TestCase):
    def test_native_caption_language_prefers_original_variant(self):
        metadata = {
            "language": "en-US",
            "automatic_captions": {"pt": [], "en": [], "en-orig": []},
        }
        self.assertEqual(select_native_caption_language(metadata), "en-orig")

    def test_one_full_cue_becomes_one_bar_with_onset_window_and_full_score(self):
        vtt = """WEBVTT

00:00:00.000 --> 00:00:04.000 align:start position:0%
[Music] Hello, bright world!

00:00:04.000 --> 00:00:06.000 align:start position:0%
[歌]
"""
        result = parse_vtt_caption_bars(vtt, 6000, onset_tolerance_ms=250, max_block_ms=5000)
        self.assertEqual(len(result["bars"]), 1)
        bar = result["bars"][0]
        self.assertEqual(bar["text"], "Hello, bright world!")
        self.assertEqual(bar["start_ms"], 0)
        self.assertEqual(bar["end_ms"], 4000)
        self.assertEqual(bar["score_window_start_ms"], 0)
        self.assertEqual(bar["score_window_end_ms"], 250)
        self.assertEqual(bar["score_duration_ms"], 4000)
        self.assertEqual(result["bracketed_annotation_count"], 2)
        self.assertEqual(result["ignored_annotation_cue_count"], 1)

    def test_preserves_multiple_words_in_same_caption_bar(self):
        result = parse_vtt_caption_bars(
            """WEBVTT

00:00:01.000 --> 00:00:03.000 align:start position:0%
We are singing together
""",
            5000,
            max_block_ms=5000,
        )
        self.assertEqual(result["bar_count"], 1)
        self.assertEqual(result["bars"][0]["text"], "We are singing together")
        self.assertEqual(result["bars"][0]["score_duration_ms"], 2000)

    def test_correct_onset_awards_full_cue_duration_only_inside_tolerance(self):
        bars = parse_vtt_caption_bars(
            """WEBVTT

00:00:05.000 --> 00:00:07.500
one complete lyric phrase
""",
            10000,
            onset_tolerance_ms=250,
        )["bars"]
        self.assertEqual([bar["score_duration_ms"] for bar in bars], [1000, 1000, 500])
        self.assertEqual(points_for_caption_onset(bars[0], 4900), 1.0)
        self.assertEqual(points_for_caption_onset(bars[0], 5100), 1.0)
        self.assertEqual(points_for_caption_onset(bars[0], 5251), 0.0)
        self.assertEqual(points_for_caption_onset(bars[2], 7150), 0.5)


if __name__ == "__main__":
    unittest.main()
