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
            max_block_ms=1000,
        )["bars"]
        self.assertEqual([bar["text"] for bar in bars], ["one complete lyric phrase"])
        self.assertEqual([bar["score_duration_ms"] for bar in bars], [1000])
        self.assertEqual(points_for_caption_onset(bars[0], 4900), 1.0)
        self.assertEqual(points_for_caption_onset(bars[0], 5100), 1.0)
        self.assertEqual(points_for_caption_onset(bars[0], 5251), 0.0)

    def test_rolling_cues_emit_only_new_words_within_block_interval(self):
        vtt = """WEBVTT

00:00:01.000 --> 00:00:01.700
>> amanhã eu vou

00:00:01.700 --> 00:00:02.400
eu vou passear com você

00:00:02.400 --> 00:00:03.000
passear com você amanhã

00:00:05.000 --> 00:00:06.000
amanhã eu vou
"""
        bars = parse_vtt_caption_bars(vtt, 7000, max_block_ms=1000)["bars"]
        self.assertEqual([bar["text"] for bar in bars], [
            "amanhã eu vou", "passear com você", "amanhã",
        ])

    def test_inline_timestamps_keep_new_words_at_their_actual_times(self):
        vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
<c>amanhã</c><00:00:01.600><c> você vai torcer</c>
"""
        bars = parse_vtt_caption_bars(vtt, 4000)["bars"]
        self.assertEqual([(bar["start_ms"], bar["text"]) for bar in bars], [
            (1000, "amanhã"), (1600, "você vai torcer"),
        ])

    def test_same_instant_fragments_form_one_block_and_later_cues_remove_history(self):
        vtt = """WEBVTT

00:00:01.000 --> 00:00:02.000
amanhã<00:00:01.000> você vai torcer

00:00:01.800 --> 00:00:02.800
amanhã você vai torcer bem
"""
        bars = parse_vtt_caption_bars(vtt, 4000)["bars"]
        self.assertEqual([(bar["start_ms"], bar["text"]) for bar in bars], [
            (1000, "amanhã você vai torcer"), (1800, "bem"),
        ])

    def test_whitespace_line_does_not_detach_real_vtt_words(self):
        vtt = """WEBVTT

00:00:13.920 --> 00:00:17.230 align:start position:0%
\x20
sonhei<00:00:15.280><c> e</c><00:00:15.719><c> esperei</c>
"""
        bars = parse_vtt_caption_bars(vtt, 20000, max_block_ms=3000)["bars"]
        self.assertEqual([(bar["start_ms"], bar["text"], bar["end_ms"]) for bar in bars], [
            (13920, "sonhei", 15280), (15280, "e", 15719), (15719, "esperei", 17230),
        ])

    def test_repeated_escaped_speaker_markers_do_not_leak_into_words(self):
        vtt = """WEBVTT

00:00:23.000 --> 00:00:24.000
&gt;&gt; [música]
&gt;&gt; Chamo<00:00:23.400><c> o</c>
"""
        bars = parse_vtt_caption_bars(vtt, 25000)["bars"]
        self.assertEqual([bar["text"] for bar in bars], ["Chamo", "o"])

    def test_recent_repeated_phrases_are_discarded_but_expired_phrases_return(self):
        vtt = """WEBVTT

00:00:01.000 --> 00:00:01.700
amanhã eu irei

00:00:01.700 --> 00:00:02.400
eu irei passear com você

00:00:20.000 --> 00:00:21.000
amanhã eu irei

00:01:01.800 --> 00:01:02.800
amanhã eu irei
"""
        bars = parse_vtt_caption_bars(vtt, 64000, max_block_ms=3000, history_ms=60000)["bars"]
        self.assertEqual([(bar["start_ms"], bar["text"]) for bar in bars], [
            (1000, "amanhã eu irei"), (1700, "passear com você"),
            (61800, "amanhã eu irei"),
        ])

    def test_near_duplicate_cue_after_timed_words_is_discarded(self):
        vtt = """WEBVTT

00:02:44.400 --> 00:02:46.361
quero<00:02:44.800><c> você</c><00:02:45.239><c> e</c><00:02:45.440><c> não</c><00:02:45.640><c> vão</c><00:02:45.959><c> querer</c>

00:02:46.361 --> 00:02:46.371
Eu só quero você e não vão querer
"""
        bars = parse_vtt_caption_bars(vtt, 170000, max_block_ms=3000, repeat_percent=70)["bars"]
        self.assertEqual([bar["text"] for bar in bars], ["quero", "você", "e", "não", "vão", "querer"])

    def test_repeat_threshold_is_strict_and_configurable(self):
        vtt = """WEBVTT

00:00:01.000 --> 00:00:02.000
a b c d e f g

00:00:02.000 --> 00:00:03.000
x y z a b c d e f g
"""
        default = parse_vtt_caption_bars(vtt, 4000, repeat_percent=70)["bars"]
        lower = parse_vtt_caption_bars(vtt, 4000, repeat_percent=69)["bars"]
        self.assertEqual([bar["text"] for bar in default], ["a b c d e f g", "x y z a b c d e f g"])
        self.assertEqual([bar["text"] for bar in lower], ["a b c d e f g"])


if __name__ == "__main__":
    unittest.main()
