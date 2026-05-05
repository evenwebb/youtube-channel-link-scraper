"""Tests for redirect URL extraction and ordering."""

from __future__ import annotations

import unittest

import scrape_links


class TestParseChannelLinks(unittest.TestCase):
    def test_orders_header_before_description(self) -> None:
        page = (
            "pad https://www.youtube.com/redirect?event=channel_description"
            "&q=https%3A%2F%2Flate.example pad "
            "pad https://www.youtube.com/redirect?event=channel_header"
            "&q=https%3A%2F%2Ffirst.example tail"
        )
        self.assertEqual(
            scrape_links.parse_channel_links(page),
            ["https://first.example", "https://late.example"],
        )

    def test_deduplicates_same_target(self) -> None:
        page = (
            "https://www.youtube.com/redirect?event=channel_header&q=https%3A%2F%2Fsame "
            "https://www.youtube.com/redirect?event=channel_description&q=https%3A%2F%2Fsame"
        )
        self.assertEqual(scrape_links.parse_channel_links(page), ["https://same"])

    def test_skips_redirect_without_q(self) -> None:
        page = "https://www.youtube.com/redirect?event=channel_header"
        self.assertEqual(scrape_links.parse_channel_links(page), [])


if __name__ == "__main__":
    unittest.main()
