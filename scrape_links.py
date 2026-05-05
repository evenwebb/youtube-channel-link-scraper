#!/usr/bin/env python3
# Copyright (C) 2026 evenwebb
# SPDX-License-Identifier: GPL-3.0-or-later
# See the LICENSE file for full terms.
"""Scrape external links from YouTube channel pages listed in subscriptions.csv."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# --- HTTP / proxy configuration ------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

REDIRECT_PREFIX = "https://www.youtube.com/redirect"
YOUTUBE_ORIGIN = "https://www.youtube.com"
PROXY_PREFIX = "https://r.jina.ai/"

RATE_LIMIT_REQUESTS_PER_MINUTE = 20
RATE_LIMIT_WINDOW_SECONDS = 62.0
RETRY_DELAY_SECONDS = 5.0


class SlidingWindowRateLimiter:
    """Sliding-window cap for outbound proxy requests."""

    __slots__ = ("_requests_per_window", "_window_seconds", "_timestamps")

    def __init__(self, requests_per_window: int, window_seconds: float) -> None:
        self._requests_per_window = requests_per_window
        self._window_seconds = window_seconds
        self._timestamps: deque[float] = deque()

    def acquire(self) -> None:
        if self._requests_per_window <= 0:
            return

        while True:
            now = time.time()
            while (
                self._timestamps
                and now - self._timestamps[0] >= self._window_seconds
            ):
                self._timestamps.popleft()

            if len(self._timestamps) < self._requests_per_window:
                return

            wait_time = self._window_seconds - (now - self._timestamps[0])
            if wait_time > 0:
                time.sleep(wait_time)
            else:
                time.sleep(0)

    def record(self, timestamp: float) -> None:
        if self._requests_per_window <= 0:
            return
        self._timestamps.append(timestamp)


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
        cid = channel_id.strip()
        base = f"{YOUTUBE_ORIGIN}/channel/{cid}"
    return base


def _normalise_column_name(name: str) -> str:
    return name.strip().lower().replace("_", " ")


@dataclass(frozen=True, slots=True)
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

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def iter_subscriptions(self) -> Iterator[Subscription]:
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                sub = self._row_to_subscription(row)
                if sub is not None:
                    yield sub

    def read(self) -> list[Subscription]:
        return list(self.iter_subscriptions())

    def _row_to_subscription(self, row: dict[str, str]) -> Optional[Subscription]:
        norm = {_normalise_column_name(k): (v or "").strip() for k, v in row.items()}
        title = norm.get("channel title") or norm.get("title")
        url = norm.get("channel url") or norm.get("url")
        channel_id = norm.get("channel id") or norm.get("id") or None
        if channel_id == "":
            channel_id = None

        if not url and channel_id:
            url = f"{YOUTUBE_ORIGIN}/channel/{channel_id}"
        if not title or not url:
            return None
        return Subscription(title=title, url=url, channel_id=channel_id)


_DEFAULT_LIMITER = SlidingWindowRateLimiter(
    requests_per_window=RATE_LIMIT_REQUESTS_PER_MINUTE,
    window_seconds=RATE_LIMIT_WINDOW_SECONDS,
)
_OPENER = urllib.request.build_opener()


def fetch_about_page(
    about_url: str,
    *,
    timeout: int = 30,
    use_proxy: bool = True,
    rate_limiter: SlidingWindowRateLimiter | None = None,
) -> str:
    """Return HTML or markdown-like text for the channel About page."""
    if use_proxy:
        limiter = rate_limiter or _DEFAULT_LIMITER
    else:
        limiter = None

    target = f"{PROXY_PREFIX}{about_url}" if use_proxy else about_url
    req = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})

    while True:
        if limiter is not None:
            limiter.acquire()
            limiter.record(time.time())
        try:
            with _OPENER.open(req, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and use_proxy:
                retry_seconds = int(RETRY_DELAY_SECONDS)
                print(
                    f"Received HTTP 429 when fetching {about_url}. "
                    f"Retrying in {retry_seconds} seconds...",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            raise


_EVENT_PRIORITY = (
    "channel_header",
    "channel_about_metadata",
    "channel_description",
)
_EVENT_ORDER = {name: i for i, name in enumerate(_EVENT_PRIORITY)}
_FALLBACK_ORDER = len(_EVENT_PRIORITY)


def _iter_redirect_urls(page_text: str) -> Iterable[str]:
    start = 0
    while True:
        idx = page_text.find(REDIRECT_PREFIX, start)
        if idx == -1:
            break
        end = idx
        page_len = len(page_text)
        while end < page_len and not page_text[end].isspace() and page_text[end] != ")":
            end += 1
        yield page_text[idx:end]
        start = end


def parse_channel_links(page_text: str) -> list[str]:
    """Decode youtube.com/redirect targets and order by on-page section priority."""
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

    structured.sort(
        key=lambda item: (
            _EVENT_ORDER.get(item[0], _FALLBACK_ORDER),
            item[2],
        )
    )

    links: list[str] = []
    seen: set[str] = set()
    for _, dest, _ in structured:
        if not dest or dest in seen:
            continue
        seen.add(dest)
        links.append(dest)
    return links


_FETCH_ERRORS = (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError)


def scrape_links(
    subscriptions: Iterable[Subscription],
    *,
    delay: float = 0.5,
    url_filters: list[str] | None = None,
    progress: bool = True,
    use_proxy: bool = True,
    rate_limiter: SlidingWindowRateLimiter | None = None,
    on_update: Callable[[list[dict[str, object]]], None] | None = None,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
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
                print(
                    f"Skipping {subscription.title!r}: missing channel URL",
                    file=sys.stderr,
                )
                continue
            try:
                page_text = fetch_about_page(
                    about_url,
                    use_proxy=use_proxy,
                    rate_limiter=rate_limiter,
                )
            except _FETCH_ERRORS as exc:
                print(f"Failed to fetch {about_url}: {exc}", file=sys.stderr)
                continue
            links = parse_channel_links(page_text)
            if lowered_filters is not None:
                links = [
                    link
                    for link in links
                    if any(f in link.lower() for f in lowered_filters)
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
                on_update(results)
            if progress:
                if links:
                    print(f"    Found {len(links)} link(s).", flush=True)
                else:
                    msg = (
                        "    No matching links found."
                        if lowered_filters is not None
                        else "    No links found."
                    )
                    print(msg, flush=True)
            if delay > 0 and index < total:
                time.sleep(delay)
    except KeyboardInterrupt:
        if progress:
            print("\nInterrupted by user, saving collected links...", flush=True)
        return results
    return results


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "subscriptions_csv",
        help="Path to Google Takeout subscriptions.csv",
    )
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
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Fetch youtube.com directly instead of via r.jina.ai (may be rate-limited).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    reader = SubscriptionReader(args.subscriptions_csv)
    try:
        subscriptions = reader.read()
    except FileNotFoundError:
        print(
            f"Could not open subscriptions file: {args.subscriptions_csv}",
            file=sys.stderr,
        )
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

    write_results([])

    results = scrape_links(
        subscriptions,
        delay=max(args.delay, 0.0),
        url_filters=args.filters,
        progress=not args.no_progress,
        use_proxy=not args.no_proxy,
        on_update=write_results,
    )
    write_results(results)
    print(f"Saved channel links for {len(results)} channels to {output_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
