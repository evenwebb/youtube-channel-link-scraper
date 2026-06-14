"""Tests for Takeout CSV reading."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import scrape_links

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SAMPLE_CSV = _REPO_ROOT / "sample_subscriptions.csv"


class TestSubscriptionReader(unittest.TestCase):
    def test_reads_sample_csv(self) -> None:
        reader = scrape_links.SubscriptionReader(_SAMPLE_CSV)
        subs = reader.read()
        self.assertEqual(len(subs), 2)
        self.assertEqual(subs[0].title, "T90Official - Age Of Empires 2")
        self.assertIn("T90Official", subs[0].url)
        self.assertTrue(subs[0].about_url.endswith("/about"))
        self.assertEqual(reader.skipped_count, 0)

    def test_iter_subscriptions_matches_read(self) -> None:
        reader = scrape_links.SubscriptionReader(_SAMPLE_CSV)
        self.assertEqual(list(reader.iter_subscriptions()), reader.read())

    def test_skips_rows_missing_title(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".csv", encoding="utf-8", delete=False
        ) as f:
            f.write("Channel Id,Channel Url,Channel Title\n")
            f.write(",https://youtube.com/@NoTitle,\n")
            f.write("UCabc,https://youtube.com/@Good,Good Channel\n")
            tmp = f.name
        try:
            reader = scrape_links.SubscriptionReader(tmp)
            subs = reader.read()
            self.assertEqual(len(subs), 1)
            self.assertEqual(subs[0].title, "Good Channel")
            self.assertEqual(reader.skipped_count, 1)
        finally:
            Path(tmp).unlink()

    def test_skips_rows_missing_url_and_id(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".csv", encoding="utf-8", delete=False
        ) as f:
            f.write("Channel Id,Channel Url,Channel Title\n")
            f.write(",,Missing Everything\n")
            f.write("UCabc,https://youtube.com/@Good,Good Channel\n")
            tmp = f.name
        try:
            reader = scrape_links.SubscriptionReader(tmp)
            subs = reader.read()
            self.assertEqual(len(subs), 1)
            self.assertEqual(reader.skipped_count, 1)
        finally:
            Path(tmp).unlink()

    def test_constructs_url_from_channel_id(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".csv", encoding="utf-8", delete=False
        ) as f:
            f.write("Channel Id,Channel Url,Channel Title\n")
            f.write("UCabc,,Channel With ID Only\n")
            tmp = f.name
        try:
            reader = scrape_links.SubscriptionReader(tmp)
            subs = reader.read()
            self.assertEqual(len(subs), 1)
            self.assertIn("/channel/UCabc", subs[0].url)
        finally:
            Path(tmp).unlink()

    def test_handles_different_column_names(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".csv", encoding="utf-8", delete=False
        ) as f:
            f.write("channel_id,channel_url,channel_title\n")
            f.write("UCabc,https://youtube.com/@Example,Example Channel\n")
            tmp = f.name
        try:
            reader = scrape_links.SubscriptionReader(tmp)
            subs = reader.read()
            self.assertEqual(len(subs), 1)
            self.assertEqual(subs[0].title, "Example Channel")
        finally:
            Path(tmp).unlink()


if __name__ == "__main__":
    unittest.main()
