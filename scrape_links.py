#!/usr/bin/env python3
"""Scrape external links from YouTube channel pages listed in subscriptions.csv."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

REDIRECT_PREFIX = "https://www.youtube.com/redirect"
YOUTUBE_ORIGIN = "https://www.youtube.com"
PROXY_PREFIX = "https://r.jina.ai/"


def _normalise_column_name(name: str) -> str:
    return name.strip().lower().replace("_", " ")


@dataclass
class Subscription:
    title: str
    url: str
    channel_id: Optional[str] = None

    @property
    def about_url(self) -> str:
        base_url = normalise_channel_url(self.url, self.channel_id)
        if not base_url:
            raise ValueError("Unable to determine channel URL")
        return f"{base_url}/about"


class SubscriptionReader:
    """Read subscriptions from a Google Takeout CSV file."""

    def __init__(self, path: str) -> None:
        self.path = path

    def read(self) -> List[Subscription]:
        with open(self.path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
        subscriptions: List[Subscription] = []
        for row in rows:
            sub = self._row_to_subscription(row)
            if sub is not None:
                subscriptions.append(sub)
        return subscriptions

    def _row_to_subscription(self, row: dict[str, str]) -> Optional[Subscription]:
        title = self._get_first(row, {"channel title", "title"})
        url = self._get_first(row, {"channel url", "url"})
        channel_id = self._get_first(row, {"channel id", "id"})
        if not url and channel_id:
            url = f"{YOUTUBE_ORIGIN}/channel/{channel_id.strip()}"
        if not title or not url:
            return None
        return Subscription(title=title.strip(), url=url.strip(), channel_id=channel_id)

    @staticmethod
    def _get_first(row: dict[str, str], keys: set[str]) -> Optional[str]:
        for raw_key, value in row.items():
            if _normalise_column_name(raw_key) in keys:
                return value
        return None


def normalise_channel_url(url: str, channel_id: Optional[str] = None) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    parsed = urllib.parse.urlparse(url, scheme="https")
    if not parsed.netloc:
        url = urllib.parse.urljoin(YOUTUBE_ORIGIN, url)
        parsed = urllib.parse.urlparse(url)
    elif not parsed.scheme:
        parsed = parsed._replace(scheme="https")
    netloc = parsed.netloc.lower()
    if "youtube" not in netloc:
        return None
    path = parsed.path.rstrip("/")
    if path.endswith("/about"):
        path = path[: -len("/about")]
    base = urllib.parse.urlunparse(("https", "www.youtube.com", path, "", "", ""))
    if channel_id and "/channel/" not in path:
        base = f"{YOUTUBE_ORIGIN}/channel/{channel_id.strip()}"
    return base


def fetch_about_page(about_url: str, timeout: int = 30) -> str:
    target = f"{PROXY_PREFIX}{about_url}"
    req = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return body


def _iter_redirect_urls(page_text: str) -> Iterable[str]:
    start = 0
    while True:
        idx = page_text.find(REDIRECT_PREFIX, start)
        if idx == -1:
            break
        end = idx
        while end < len(page_text) and not page_text[end].isspace() and page_text[end] != ')':
            end += 1
        yield page_text[idx:end]
        start = end


def parse_channel_links(page_text: str) -> List[str]:
    events_priority = [
        "channel_header",
        "channel_about_metadata",
        "channel_description",
    ]
    event_order = {event: index for index, event in enumerate(events_priority)}

    structured: list[tuple[str, str, int]] = []
    for idx, raw_url in enumerate(_iter_redirect_urls(page_text)):
        parsed = urllib.parse.urlparse(raw_url)
        params = urllib.parse.parse_qs(parsed.query)
        targets = params.get("q")
        if not targets:
            continue
        dest = urllib.parse.unquote(targets[0])
        event = params.get("event", [""])[0]
        structured.append((event, dest, idx))

    # Preserve original discovery order as a tiebreaker while favouring header links.
    structured.sort(
        key=lambda item: (
            event_order.get(item[0], len(events_priority)),
            item[2],
        )
    )

    links: List[str] = []
    seen = set()
    for event, dest, _ in structured:
        if not dest or dest in seen:
            continue
        seen.add(dest)
        links.append(dest)
    return links


def scrape_links(
    subscriptions: Iterable[Subscription],
    delay: float = 0.5,
    url_filters: Optional[list[str]] = None,
    progress: bool = True,
    on_update: Optional[Callable[[list[dict[str, object]]], None]] = None,
) -> list[dict[str, object]]:
    results = []
    subscriptions_list = list(subscriptions)
    total = len(subscriptions_list)
    lowered_filters = [item.lower() for item in url_filters] if url_filters else None
    try:
        for index, subscription in enumerate(subscriptions_list, start=1):
            if progress:
                print(
                    f"[{index}/{total}] Fetching links for {subscription.title!r}...",
                    flush=True,
                )
            try:
                about_url = subscription.about_url
            except ValueError:
                print(f"Skipping {subscription.title!r}: missing channel URL", file=sys.stderr)
                continue
            try:
                page_text = fetch_about_page(about_url)
            except Exception as exc:  # noqa: BLE001 - continue processing the rest
                print(f"Failed to fetch {about_url}: {exc}", file=sys.stderr)
                continue
            links = parse_channel_links(page_text)
            if lowered_filters is not None:
                links = [
                    link
                    for link in links
                    if any(filter_value in link.lower() for filter_value in lowered_filters)
                ]
            canonical_url = normalise_channel_url(subscription.url, subscription.channel_id)
            results.append(
                {
                    "channel_title": subscription.title,
                    "channel_url": canonical_url or subscription.url,
                    "links": links,
                }
            )
            if on_update is not None:
                on_update(list(results))
            if progress:
                if links:
                    print(
                        f"    Found {len(links)} link(s).",
                        flush=True,
                    )
                else:
                    message = (
                        "    No matching links found."
                        if lowered_filters is not None
                        else "    No links found."
                    )
                    print(message, flush=True)
            if delay > 0 and index < len(subscriptions_list):
                time.sleep(delay)
    except KeyboardInterrupt:
        if progress:
            print("\nInterrupted by user, saving collected links...", flush=True)
        return results
    return results


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subscriptions_csv", help="Path to Google Takeout subscriptions.csv")
    parser.add_argument(
        "-o",
        "--output",
        default="channel_links.json",
        help="Destination JSON file",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Delay (in seconds) between requests to avoid overwhelming the proxy",
    )
    parser.add_argument(
        "-f",
        "--filter",
        dest="filters",
        action="append",
        help=(
            "Only include links that contain the provided substring. "
            "Can be supplied multiple times to match any of the given values."
        ),
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable progress output.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    reader = SubscriptionReader(args.subscriptions_csv)
    try:
        subscriptions = reader.read()
    except FileNotFoundError:
        print(f"Could not open subscriptions file: {args.subscriptions_csv}", file=sys.stderr)
        return 1
    if not subscriptions:
        print("No subscriptions found in the provided CSV.", file=sys.stderr)
        return 1
    output_path = Path(args.output)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"Unable to prepare output directory {output_path.parent}: {exc}",
            file=sys.stderr,
        )
        return 1

    def write_results(data: list[dict[str, object]]) -> None:
        output_dir = output_path.parent
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(output_dir) if output_dir else None,
        ) as tmp_handle:
            json.dump(data, tmp_handle, ensure_ascii=False, indent=2)
            tmp_name = tmp_handle.name
        os.replace(tmp_name, output_path)

    # Touch the output file early so users can see where results will be stored.
    write_results([])

    results = scrape_links(
        subscriptions,
        delay=max(args.delay, 0.0),
        url_filters=args.filters,
        progress=not args.no_progress,
        on_update=write_results,
    )
    write_results(results)
    print(f"Saved channel links for {len(results)} channels to {output_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
