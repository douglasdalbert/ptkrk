import unittest
from unittest.mock import MagicMock, patch

from app.search import search_karaoke, search_with_captions, video_summary


def fake_downloader(results: dict):
    def factory(options):
        downloader = MagicMock()
        downloader.__enter__.return_value = downloader
        downloader.extract_info.side_effect = lambda url, download: results[url]
        return downloader
    return factory


class SearchTests(unittest.TestCase):
    def test_summary_rejects_invalid_live_and_long_videos(self):
        self.assertIsNone(video_summary({"id": "bad"}))
        self.assertIsNone(video_summary({"id": "dQw4w9WgXcQ", "live_status": "is_live"}))
        self.assertIsNone(video_summary({"id": "dQw4w9WgXcQ", "duration": 13 * 60}))
        self.assertEqual(video_summary({"id": "dQw4w9WgXcQ", "title": "Song", "channel": "Band", "duration": 200.0}),
                         {"video_id": "dQw4w9WgXcQ", "title": "Song", "channel": "Band", "duration": 200})

    def test_karaoke_search_prefixes_query_and_limits(self):
        results = {"ytsearch2:Karaoke evidências": {"entries": [
            {"id": "aaaaaaaaaaa", "title": "A"}, {"id": "aaaaaaaaaaa", "title": "A"}, {"id": "bbbbbbbbbbb", "title": "B"},
        ]}}
        with patch("app.search.YoutubeDL", side_effect=fake_downloader(results)):
            videos = search_karaoke("evidências", 2)
        self.assertEqual([video["video_id"] for video in videos], ["aaaaaaaaaaa", "bbbbbbbbbbb"])

    def test_caption_search_keeps_only_videos_with_captions(self):
        watch = "https://www.youtube.com/watch?v="
        results = {
            "ytsearch4:song": {"entries": [{"id": "aaaaaaaaaaa"}, {"id": "bbbbbbbbbbb"}, {"id": "ccccccccccc"}]},
            f"{watch}aaaaaaaaaaa": {"language": "pt", "automatic_captions": {"pt-orig": []}},
            f"{watch}bbbbbbbbbbb": {"language": "pt", "automatic_captions": {}},
            f"{watch}ccccccccccc": {"subtitles": {"en": []}},
        }
        with patch("app.search.YoutubeDL", side_effect=fake_downloader(results)):
            videos = search_with_captions("song", 2)
        self.assertEqual([video["video_id"] for video in videos], ["aaaaaaaaaaa", "ccccccccccc"])


if __name__ == "__main__":
    unittest.main()
