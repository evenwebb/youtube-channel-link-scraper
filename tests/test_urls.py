"""Tests for channel URL normalisation."""

from __future__ import annotations

import unittest

import scrape_links


class TestNormaliseChannelUrl(unittest.TestCase):
    def test_handles_handle_url(self) -> None:
        self.assertEqual(
            scrape_links.normalise_channel_url("https://www.youtube.com/@Example"),
            "https://www.youtube.com/@Example",
        )

    def test_strips_about_suffix(self) -> None:
        self.assertEqual(
            scrape_links.normalise_channel_url("https://www.youtube.com/@Example/about"),
            "https://www.youtube.com/@Example",
        )

    def test_uses_channel_id_when_path_not_channel(self) -> None:
        self.assertEqual(
            scrape_links.normalise_channel_url(
                "https://www.youtube.com/@Example",
                channel_id="UCabc",
            ),
            "https://www.youtube.com/channel/UCabc",
        )

    def test_non_youtube_returns_none(self) -> None:
        self.assertIsNone(scrape_links.normalise_channel_url("https://example.com/foo"))


if __name__ == "__main__":
    unittest.main()
