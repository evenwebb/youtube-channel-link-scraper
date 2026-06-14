"""Tests for redirect URL extraction, ordering, and link categorisation."""

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

    def test_handles_markdown_link_wrapping(self) -> None:
        page = "[text](https://www.youtube.com/redirect?event=channel_header&q=https%3A%2F%2Fexample.com)"
        self.assertEqual(scrape_links.parse_channel_links(page), ["https://example.com"])

    def test_handles_nested_parens(self) -> None:
        page = "([link](https://www.youtube.com/redirect?event=channel_header&q=https%3A%2F%2Fexample.com))"
        links = scrape_links.parse_channel_links(page)
        self.assertIn("https://example.com", links)


class TestCategoriseLink(unittest.TestCase):
    def test_social_twitter(self) -> None:
        self.assertEqual(scrape_links.categorise_link("https://twitter.com/example"), "Social")

    def test_social_instagram(self) -> None:
        self.assertEqual(scrape_links.categorise_link("https://instagram.com/example"), "Social")

    def test_social_discord(self) -> None:
        self.assertEqual(scrape_links.categorise_link("https://discord.gg/invite"), "Social")

    def test_social_bluesky(self) -> None:
        self.assertEqual(scrape_links.categorise_link("https://bsky.app/profile/example"), "Social")

    def test_support_patreon(self) -> None:
        self.assertEqual(scrape_links.categorise_link("https://patreon.com/creator"), "Support")

    def test_support_ko_fi(self) -> None:
        self.assertEqual(scrape_links.categorise_link("https://ko-fi.com/creator"), "Support")

    def test_store_amazon(self) -> None:
        self.assertEqual(scrape_links.categorise_link("https://amazon.com/shop"), "Store")

    def test_streaming_twitch(self) -> None:
        self.assertEqual(scrape_links.categorise_link("https://twitch.tv/channel"), "Streaming")

    def test_website_github(self) -> None:
        self.assertEqual(scrape_links.categorise_link("https://github.com/example"), "Website")

    def test_music_spotify(self) -> None:
        self.assertEqual(scrape_links.categorise_link("https://spotify.com/artist"), "Music")

    def test_gaming_steam(self) -> None:
        self.assertEqual(scrape_links.categorise_link("https://store.steampowered.com/app/123"), "Gaming")

    def test_email_mailto(self) -> None:
        self.assertEqual(scrape_links.categorise_link("mailto:example@test.com"), "Email")

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(scrape_links.categorise_link("https://random-blog.example.com"))

    def test_case_insensitive(self) -> None:
        self.assertEqual(scrape_links.categorise_link("HTTPS://TWITTER.COM/user"), "Social")


if __name__ == "__main__":
    unittest.main()
