#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Scrape external links from YouTube channel pages listed in subscriptions.csv."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# --- Constants ------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

REDIRECT_PREFIX = "https://www.youtube.com/redirect"
YOUTUBE_ORIGIN = "https://www.youtube.com"
PROXY_PREFIX = "https://r.jina.ai/"

DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 2
RATE_LIMIT_REQUESTS_PER_MINUTE = 20
RATE_LIMIT_WINDOW_SECONDS = 62.0
RETRY_DELAY_SECONDS = 5.0
RETRY_BACKOFF_MULTIPLIER = 2.0

_REDIRECT_URL_RE = re.compile(r"https://www\.youtube\.com/redirect[^\s)]+")

# --- Link categorisation --------------------------------------------------------

_LINK_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Social", ["twitter.com", "x.com", "facebook.com", "instagram.com", "tiktok.com",
                "reddit.com", "discord.gg", "discord.com", "linkedin.com", "threads.net",
                "bsky.app", "mastodon.", "snapchat.com", "tumblr.com"]),
    ("Support", ["patreon.com", "ko-fi.com", "buymeacoffee.com", "paypal.me",
                 "subscribestar.com", "onlyfans.com", "gofundme.com"]),
    ("Gaming", ["steampowered.com", "store.steampowered.com", "epicgames.com",
                "nintendo.com", "playstation.com", "xbox.com", "itch.io"]),
    ("Store", ["amazon.com", "amzn.to", "ebay.com", "etsy.com", "shopify.com",
               "merch.", "teespring.com", "spreadshirt."]),
    ("Streaming", ["twitch.tv", "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com"]),
    ("Website", ["github.io", "github.com", "gitlab.com", "linktr.ee", "carrd.co",
                 "about.me", "bio.link", "beacons.ai", "solo.to"]),
    ("Music", ["spotify.com", "apple.music", "music.apple", "soundcloud.com",
               "bandcamp.com", "deezer.com", "tidal.com"]),
    ("Email", ["mailto:"]),
]

DOMAIN_CATEGORY_MAP: dict[str, str] = {}
for _cat, _domains in _LINK_CATEGORIES:
    for _d in _domains:
        DOMAIN_CATEGORY_MAP[_d] = _cat


def categorise_link(url: str) -> str | None:
    """Return a category label for a URL, or None if unrecognised."""
    lowered = url.lower()
    for domain, category in DOMAIN_CATEGORY_MAP.items():
        if domain in lowered:
            return category
    return None


def _make_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener()


# --- Rate limiter ---------------------------------------------------------------

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
            while self._timestamps and now - self._timestamps[0] >= self._window_seconds:
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


# --- URL normalisation ----------------------------------------------------------

def normalise_channel_url(url: str, channel_id: Optional[str] = None) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    # Strip query and fragment before parsing
    parsed = urllib.parse.urlparse(url, scheme="https")
    if parsed.query:
        parsed = parsed._replace(query="")
    if parsed.fragment:
        parsed = parsed._replace(fragment="")
    if not parsed.netloc:
        url = urllib.parse.urljoin(YOUTUBE_ORIGIN, parsed.path)
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


# --- Subscription model ---------------------------------------------------------

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
        self._skipped_rows = 0
        self._skipped_reasons: list[str] = []

    @property
    def skipped_count(self) -> int:
        return self._skipped_rows

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
        # Only normalise the columns we actually need
        title = channel_url = channel_id = None
        for k, v in row.items():
            nk = _normalise_column_name(k)
            val = (v or "").strip()
            if not val:
                continue
            if nk in ("channel title", "title") and not title:
                title = val
            elif nk in ("channel url", "url") and not channel_url:
                channel_url = val
            elif nk in ("channel id", "id") and not channel_id:
                channel_id = val
            if title and channel_url and channel_id:
                break

        if channel_id and channel_id == "":
            channel_id = None
        if not channel_url and channel_id:
            channel_url = f"{YOUTUBE_ORIGIN}/channel/{channel_id}"
        if not title:
            self._skipped_rows += 1
            self._skipped_reasons.append(f"Missing title (channel_url={channel_url or '?'})")
            return None
        if not channel_url:
            self._skipped_rows += 1
            self._skipped_reasons.append(f"Missing URL for '{title}'")
            return None
        return Subscription(title=title, url=channel_url, channel_id=channel_id)


# --- Page fetching --------------------------------------------------------------

_FETCH_ERRORS = (urllib.error.URLError, OSError, TimeoutError)


def fetch_about_page(
    about_url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    use_proxy: bool = True,
    rate_limiter: SlidingWindowRateLimiter | None = None,
    max_retries: int = DEFAULT_RETRIES,
) -> str:
    """Return HTML or markdown-like text for the channel About page."""
    limiter = rate_limiter if use_proxy else None
    target = f"{PROXY_PREFIX}{about_url}" if use_proxy else about_url
    req = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
    opener = _make_opener()

    for attempt in range(max_retries + 1):
        if limiter is not None:
            limiter.acquire()
            limiter.record(time.time())
        try:
            with opener.open(req, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and use_proxy:
                delay = RETRY_DELAY_SECONDS * (RETRY_BACKOFF_MULTIPLIER ** attempt)
                print(
                    f"Received HTTP 429 when fetching {about_url}. "
                    f"Retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries + 1})...",
                    file=sys.stderr, flush=True,
                )
                time.sleep(delay)
                continue
            raise
        except _FETCH_ERRORS:
            if attempt < max_retries:
                delay = RETRY_DELAY_SECONDS * (RETRY_BACKOFF_MULTIPLIER ** attempt)
                print(
                    f"Transient error fetching {about_url}. "
                    f"Retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries + 1})...",
                    file=sys.stderr, flush=True,
                )
                time.sleep(delay)
                continue
            raise
    return ""  # unreachable


# --- Link parsing ---------------------------------------------------------------

_EVENT_PRIORITY = (
    "channel_header",
    "channel_about_metadata",
    "channel_description",
)
_EVENT_ORDER = {name: i for i, name in enumerate(_EVENT_PRIORITY)}
_FALLBACK_ORDER = len(_EVENT_PRIORITY)


def _iter_redirect_urls(page_text: str) -> Iterable[str]:
    for m in _REDIRECT_URL_RE.finditer(page_text):
        yield m.group(0)


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

    structured.sort(key=lambda item: (_EVENT_ORDER.get(item[0], _FALLBACK_ORDER), item[2]))

    links: list[str] = []
    seen: set[str] = set()
    for _, dest, _ in structured:
        if not dest or dest in seen:
            continue
        seen.add(dest)
        links.append(dest)
    return links


# --- Main scraping logic --------------------------------------------------------

@dataclass
class ScrapeResult:
    channel_title: str
    channel_url: str
    links: list[str] = field(default_factory=list)
    categories: list[str | None] = field(default_factory=list)
    error: str | None = None


def scrape_links(
    subscriptions: Iterable[Subscription],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    url_filters: list[str] | None = None,
    channel_filter: str | None = None,
    limit: int | None = None,
    progress: bool = True,
    use_proxy: bool = True,
    max_retries: int = DEFAULT_RETRIES,
    rate_limiter: SlidingWindowRateLimiter | None = None,
    on_update: Callable[[list[dict[str, object]]], None] | None = None,
    on_error: Callable[[str, str], None] | None = None,
    resume_from: set[str] | None = None,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    subscriptions_list = list(subscriptions)

    # Apply channel filter
    if channel_filter:
        lower_filter = channel_filter.lower()
        subscriptions_list = [
            s for s in subscriptions_list
            if lower_filter in s.title.lower() or lower_filter in s.url.lower()
        ]
        if not subscriptions_list:
            print(f"No channels matching '{channel_filter}'", file=sys.stderr)
            return results

    # Apply limit
    if limit is not None and limit > 0:
        subscriptions_list = subscriptions_list[:limit]

    total = len(subscriptions_list)
    lowered_filters = [item.lower() for item in url_filters] if url_filters else None
    skipped_count = 0
    error_count = 0
    start_time = time.monotonic()

    try:
        for index, subscription in enumerate(subscriptions_list, start=1):
            if progress:
                print(
                    f"[{index}/{total}] Fetching links for {subscription.title!r}...",
                    flush=True,
                )

            # Resume: skip already-scraped channels
            if resume_from is not None and subscription.title in resume_from:
                if progress:
                    print("    Already scraped, skipping.", flush=True)
                skipped_count += 1
                continue

            try:
                about_url = subscription.about_url
            except ValueError:
                print(f"Skipping {subscription.title!r}: missing channel URL", file=sys.stderr)
                skipped_count += 1
                if on_error:
                    on_error(subscription.title, "Missing channel URL")
                continue

            try:
                page_text = fetch_about_page(
                    about_url, timeout=timeout,
                    use_proxy=use_proxy, rate_limiter=rate_limiter,
                    max_retries=max_retries,
                )
            except _FETCH_ERRORS as exc:
                print(f"Failed to fetch {about_url}: {exc}", file=sys.stderr)
                error_count += 1
                if on_error:
                    on_error(subscription.title, str(exc))
                continue
            except urllib.error.HTTPError as exc:
                print(f"HTTP {exc.code} for {about_url}", file=sys.stderr)
                error_count += 1
                if on_error:
                    on_error(subscription.title, f"HTTP {exc.code}")
                continue

            links = parse_channel_links(page_text)
            if lowered_filters is not None:
                links = [link for link in links if any(f in link.lower() for f in lowered_filters)]

            categories = [categorise_link(link) for link in links]
            canonical_url = normalise_channel_url(subscription.url, subscription.channel_id)
            results.append({
                "channel_title": subscription.title,
                "channel_url": canonical_url or subscription.url,
                "links": links,
                "categories": categories,
            })

            if on_update is not None:
                on_update(list(results))  # pass a shallow copy

            if progress:
                if links:
                    cat_str = ""
                    unique_cats = {c for c in categories if c}
                    if unique_cats:
                        cat_str = f" ({', '.join(sorted(unique_cats))})"
                    print(f"    Found {len(links)} link(s){cat_str}.", flush=True)
                else:
                    msg = (
                        "    No matching links found."
                        if lowered_filters is not None
                        else "    No links found."
                    )
                    print(msg, flush=True)

    except KeyboardInterrupt:
        if progress:
            print("\nInterrupted by user, saving collected links...", flush=True)
        return results

    elapsed = time.monotonic() - start_time
    if progress and total > 0:
        print(f"\nScraped {len(results)}/{total} channels, "
              f"found {sum(len(r['links']) for r in results)} links total, "
              f"skipped {skipped_count}, errors {error_count}, "
              f"took {elapsed:.0f}s", flush=True)

    return results


# --- Output formats -------------------------------------------------------------

def _write_json(data: list[dict[str, object]], output_path: Path) -> None:
    output_dir = output_path.parent
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False,
        dir=str(output_dir) if output_dir else None,
    ) as tmp_handle:
        json.dump(data, tmp_handle, ensure_ascii=False, indent=2)
        tmp_name = tmp_handle.name
    os.replace(tmp_name, output_path)


def _write_csv(data: list[dict[str, object]], output_path: Path) -> None:
    output_dir = output_path.parent
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", delete=False,
        dir=str(output_dir) if output_dir else None,
    ) as tmp_handle:
        writer = csv.writer(tmp_handle)
        writer.writerow(["channel_title", "channel_url", "link", "category"])
        for row in data:
            title = row["channel_title"]
            url = row["channel_url"]
            links = row.get("links", [])
            categories = row.get("categories", [])
            if not links:
                writer.writerow([title, url, "", ""])
            else:
                for i, link in enumerate(links):
                    cat = categories[i] if i < len(categories) else ""
                    writer.writerow([title, url, link, cat or ""])
        tmp_name = tmp_handle.name
    os.replace(tmp_name, output_path)


# --- CLI ------------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subscriptions_csv", help="Path to Google Takeout subscriptions.csv")
    parser.add_argument("-o", "--output", default="channel_links.json",
                        help="Destination file (.json or .csv)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help=f"Max retries for transient errors (default: {DEFAULT_RETRIES})")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only scrape the first N channels")
    parser.add_argument("--channel", dest="channel_filter", default=None,
                        help="Only scrape channels whose title or URL contains this string")
    parser.add_argument("-f", "--filter", dest="filters", action="append",
                        help="Only include links containing this substring (can repeat)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List channels that would be scraped without making requests")
    parser.add_argument("--resume", action="store_true",
                        help="Skip channels already present in the output file")
    parser.add_argument("--no-progress", action="store_true",
                        help="Disable progress output")
    parser.add_argument("--no-proxy", action="store_true",
                        help="Fetch youtube.com directly instead of via r.jina.ai")
    parser.add_argument("--error-log", default=None,
                        help="Write fetch errors to a JSON file")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    reader = SubscriptionReader(args.subscriptions_csv)
    try:
        subscriptions = reader.read()
    except FileNotFoundError:
        print(f"Could not open subscriptions file: {args.subscriptions_csv}", file=sys.stderr)
        return 1

    # Report skipped rows from CSV parsing
    if reader.skipped_count > 0:
        print(f"Warning: skipped {reader.skipped_count} row(s) with missing title/URL",
              file=sys.stderr)
        for reason in reader._skipped_reasons[:5]:
            print(f"  - {reason}", file=sys.stderr)
        if reader.skipped_count > 5:
            print(f"  ... and {reader.skipped_count - 5} more", file=sys.stderr)

    if not subscriptions:
        print("No subscriptions found in the provided CSV.", file=sys.stderr)
        return 1

    # Dry run: just list channels
    if args.dry_run:
        print(f"Would scrape {len(subscriptions)} channel(s):")
        for sub in subscriptions:
            print(f"  {sub.title} ({sub.url})")
        return 0

    output_path = Path(args.output)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Unable to prepare output directory {output_path.parent}: {exc}", file=sys.stderr)
        return 1

    # Resume: load already-scraped channel titles
    resume_from: set[str] | None = None
    if args.resume and output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
            resume_from = {item["channel_title"] for item in existing if "channel_title" in item}
            if resume_from:
                print(f"Resuming: {len(resume_from)} channel(s) already in output file")
        except (json.JSONDecodeError, OSError):
            pass

    # Error log collection
    error_entries: list[dict[str, str]] = []

    def on_error(title: str, error: str) -> None:
        error_entries.append({"channel_title": title, "error": error})

    is_csv = output_path.suffix.lower() == ".csv"
    write_func = _write_csv if is_csv else _write_json

    def write_results(data: list[dict[str, object]]) -> None:
        write_func(data, output_path)

    if not args.resume or resume_from is None:
        write_results([])

    rate_limiter = SlidingWindowRateLimiter(
        RATE_LIMIT_REQUESTS_PER_MINUTE, RATE_LIMIT_WINDOW_SECONDS
    ) if not args.no_proxy else None

    results = scrape_links(
        subscriptions,
        timeout=args.timeout,
        url_filters=args.filters,
        channel_filter=args.channel_filter,
        limit=args.limit,
        progress=not args.no_progress,
        use_proxy=not args.no_proxy,
        max_retries=args.retries,
        rate_limiter=rate_limiter,
        on_update=write_results,
        on_error=on_error,
        resume_from=resume_from,
    )
    write_results(results)

    # Write error log if requested
    if args.error_log and error_entries:
        error_path = Path(args.error_log)
        try:
            error_path.parent.mkdir(parents=True, exist_ok=True)
            error_path.write_text(json.dumps(error_entries, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
            print(f"Wrote {len(error_entries)} error(s) to {error_path.resolve()}")
        except OSError as exc:
            print(f"Failed to write error log: {exc}", file=sys.stderr)

    print(f"Saved channel links for {len(results)} channels to {output_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
