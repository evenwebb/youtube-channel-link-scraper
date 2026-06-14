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

    def test_strips_trailing_slash(self) -> None:
        self.assertEqual(
            scrape_links.normalise_channel_url("https://www.youtube.com/@Example/"),
            "https://www.youtube.com/@Example",
        )

    def test_strips_query_parameters(self) -> None:
        self.assertEqual(
            scrape_links.normalise_channel_url("https://www.youtube.com/@Example?si=abc123"),
            "https://www.youtube.com/@Example",
        )

    def test_strips_both_trailing_slash_and_query(self) -> None:
        self.assertEqual(
            scrape_links.normalise_channel_url("https://www.youtube.com/@Example/?si=abc"),
            "https://www.youtube.com/@Example",
        )

    def test_channel_url_with_channel_id_path(self) -> None:
        self.assertEqual(
            scrape_links.normalise_channel_url(
                "https://www.youtube.com/channel/UCabc/about"
            ),
            "https://www.youtube.com/channel/UCabc",
        )


if __name__ == "__main__":
    unittest.main()
