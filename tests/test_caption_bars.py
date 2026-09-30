import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.captions import (
    caption_track_id,
    caption_track_path,
    create_caption_bars,
    parse_vtt_caption_bars,
    select_caption_track,
    select_native_caption_language,
    to_milliseconds,
)


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
(music is touching something)

00:00:12.000 --> 00:00:14.000
♪ [Music] ♪
"""
        result = parse_vtt_caption_bars(vtt, 14000)
        self.assertEqual(
            [bar["text"] for bar in result["bars"]],
            ["Estou cantando"] * 2,
        )
        self.assertEqual(result["ignored_annotation_cue_count"], 5)
        self.assertEqual(result["bracketed_annotation_count"], 7)
        self.assertEqual(result["bar_count"], 2)

    def test_prefers_automatic_caption_in_native_language(self):
        metadata = {
            "language": "en-US",
            "automatic_captions": {"pt": [], "en": [], "en-orig": []},
        }
        self.assertEqual(select_native_caption_language(metadata), "en-orig")
        self.assertEqual(select_caption_track(metadata), ("en-orig", True))

    def test_prefers_available_subtitles_when_video_language_is_missing(self):
        metadata = {
            "language": None,
            "subtitles": {"en-nP7-2PuUl7o": []},
            "automatic_captions": {"en": []},
        }
        self.assertEqual(select_caption_track(metadata), ("en-nP7-2PuUl7o", False))

    def test_falls_back_to_automatic_when_no_subtitles_are_available(self):
        metadata = {"language": None, "subtitles": {}, "automatic_captions": {"en": []}}
        self.assertEqual(select_caption_track(metadata), ("en", True))

    def test_skips_translated_subtitles_when_video_language_is_missing(self):
        metadata = {
            "language": None,
            "subtitles": {"en": [{"url": "https://example.com/caption?lang=pt&tlang=en"}]},
            "automatic_captions": {"pt": [{"url": "https://example.com/caption?lang=pt"}]},
        }
        self.assertEqual(select_caption_track(metadata), ("pt", True))

    def test_uses_native_language_when_original_variant_is_unavailable(self):
        metadata = {"language": "pt-BR", "automatic_captions": {"en": [], "pt": []}}
        self.assertEqual(select_native_caption_language(metadata), "pt")

    def test_does_not_silently_fall_back_to_a_translated_caption(self):
        metadata = {"language": "ja", "automatic_captions": {"en": [], "fr": []}}
        with self.assertRaises(ValueError):
            select_native_caption_language(metadata)

    def test_downloads_every_manual_and_automatic_caption_track(self):
        metadata = {
            "id": "videoid",
            "language": "en",
            "duration": 10,
            "subtitles": {
                "pt": [],
                "fr": [{"url": "https://example.com/caption?lang=pt&tlang=fr"}],
            },
            "automatic_captions": {
                "en-orig": [{"url": "https://example.com/caption?lang=en"}],
                "pt": [],
                "es": [{"url": "https://example.com/caption?lang=en&tlang=es"}],
            },
        }
        downloaded = []

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def extract_info(self, url, download=False):
                if "subtitleslangs" not in self.options:
                    return metadata
                language = self.options["subtitleslangs"][0]
                automatic = self.options["writeautomaticsub"]
                output = Path(self.options["outtmpl"].replace("%(id)s", "videoid").replace("%(ext)s", "vtt"))
                output.write_text(
                    f"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n{language} {'auto' if automatic else 'manual'}\n",
                    encoding="utf-8",
                )
                downloaded.append((language, automatic))
                return metadata

        with tempfile.TemporaryDirectory() as temporary_directory, \
                patch("app.captions.MEDIA_ROOT", Path(temporary_directory)), \
                patch("app.captions.YoutubeDL", FakeYoutubeDL):
            result = create_caption_bars("videoid", "party")
            self.assertEqual(set(downloaded), {("pt", False), ("en-orig", True), ("pt", True)})
            self.assertEqual(len(result["tracks"]), 3)
            self.assertEqual(result["default_track_id"], caption_track_id("en-orig", True))
            self.assertEqual(result["bars"][0]["text"], "en-orig auto")
            manual = json.loads(caption_track_path("party", "videoid", caption_track_id("pt", False)).read_text())
            self.assertEqual(manual["bars"][0]["text"], "pt manual")


if __name__ == "__main__":
    unittest.main()