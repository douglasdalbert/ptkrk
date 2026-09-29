import unittest

from app.captions import parse_vtt_caption_bars, select_native_caption_language, to_milliseconds


class CaptionBarTests(unittest.TestCase):
    def test_timestamp_conversion(self):
        self.assertEqual(to_milliseconds("00:02:03.456"), 123456)

    def test_ignores_bracketed_text_independent_of_language(self):
        vtt = """WEBVTT

00:00:00.000 --> 00:00:02.000
[Música]

00:00:02.000 --> 00:00:04.000
[CANTO]

00:00:04.000 --> 00:00:06.000
<c.color>Estou cantando</c>

00:00:06.000 --> 00:00:08.000
 [Musique] [歌]

00:00:08.000 --> 00:00:10.000
[Singing] Estou cantando

00:00:10.000 --> 00:00:12.000
♪ [Music] ♪
"""
        result = parse_vtt_caption_bars(vtt, 12000)
        self.assertEqual(
            [bar["text"] for bar in result["bars"]],
            ["Estou cantando"] * 4,
        )
        self.assertEqual(result["ignored_annotation_cue_count"], 4)
        self.assertEqual(result["bracketed_annotation_count"], 6)
        self.assertEqual(result["bar_count"], 4)

    def test_prefers_automatic_caption_in_native_language(self):
        metadata = {
            "language": "en-US",
            "automatic_captions": {"pt": [], "en": [], "en-orig": []},
        }
        self.assertEqual(select_native_caption_language(metadata), "en-orig")

    def test_uses_native_language_when_original_variant_is_unavailable(self):
        metadata = {"language": "pt-BR", "automatic_captions": {"en": [], "pt": []}}
        self.assertEqual(select_native_caption_language(metadata), "pt")

    def test_does_not_silently_fall_back_to_a_translated_caption(self):
        metadata = {"language": "ja", "automatic_captions": {"en": [], "fr": []}}
        with self.assertRaises(ValueError):
            select_native_caption_language(metadata)


if __name__ == "__main__":
    unittest.main()