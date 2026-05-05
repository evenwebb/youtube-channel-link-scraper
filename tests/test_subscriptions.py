"""Tests for Takeout CSV reading."""

from __future__ import annotations

import unittest
from pathlib import Path

import scrape_links

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SAMPLE_CSV = _REPO_ROOT / "sample_subscriptions.csv"


class TestSubscriptionReader(unittest.TestCase):
    def test_reads_sample_csv(self) -> None:
        reader = scrape_links.SubscriptionReader(_SAMPLE_CSV)
        subs = reader.read()
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0].title, "T90Official - Age Of Empires 2")
        self.assertIn("T90Official", subs[0].url)
        self.assertTrue(subs[0].about_url.endswith("/about"))

    def test_iter_subscriptions_matches_read(self) -> None:
        reader = scrape_links.SubscriptionReader(_SAMPLE_CSV)
        self.assertEqual(list(reader.iter_subscriptions()), reader.read())


if __name__ == "__main__":
    unittest.main()
