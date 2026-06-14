#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Scrape external links from YouTube channel pages listed in subscriptions.csv."""

from __future__ import annotations

import argparse
import csv
import html as _html_mod
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def resolve_channel_id(url: str, timeout: int = DEFAULT_TIMEOUT) -> Optional[str]:
    """Follow a YouTube /@handle redirect to extract the real channel ID."""
    if "/channel/" in url:
        match = re.search(r"/channel/([^/?#]+)", url)
        return match.group(1) if match else None
    if "/@" not in url and "/c/" not in url and "/user/" not in url:
        return None
    req = urllib.request.Request(
        url if "://" in url else f"{YOUTUBE_ORIGIN}{url}",
        headers={"User-Agent": USER_AGENT},
        method="HEAD",
    )
    try:
        with _make_opener().open(req, timeout=timeout) as resp:
            final_url = resp.geturl()
        match = re.search(r"/channel/([^/?#]+)", final_url)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


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


def _scrape_one_channel(
    args: tuple[int, Subscription],
    *,
    timeout: int,
    use_proxy: bool,
    max_retries: int,
    rate_limiter: SlidingWindowRateLimiter | None,
    url_filters: list[str] | None,
    resume_from: set[str] | None,
    on_error: Callable[[str, str], None] | None,
    progress: bool,
    total: int,
) -> tuple[int, dict[str, object] | None, str | None, str | None]:
    index, subscription = args
    if progress:
        print(f"[{index}/{total}] Fetching links for {subscription.title!r}...", flush=True)

    if resume_from is not None and subscription.title in resume_from:
        if progress:
            print("    Already scraped, skipping.", flush=True)
        return (index, None, None, "_skipped")

    try:
        about_url = subscription.about_url
    except ValueError:
        if on_error:
            on_error(subscription.title, "Missing channel URL")
        return (index, None, "Missing channel URL", None)

    try:
        page_text = fetch_about_page(
            about_url, timeout=timeout, use_proxy=use_proxy,
            rate_limiter=rate_limiter, max_retries=max_retries,
        )
    except (_FETCH_ERRORS, urllib.error.HTTPError) as exc:
        err = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else str(exc)
        if on_error:
            on_error(subscription.title, err)
        return (index, None, err, None)

    links = parse_channel_links(page_text)
    if url_filters:
        links = [link for link in links if any(f in link.lower() for f in url_filters)]

    categories = [categorise_link(link) for link in links]
    canonical_url = normalise_channel_url(subscription.url, subscription.channel_id)
    result = {
        "channel_title": subscription.title,
        "channel_url": canonical_url or subscription.url,
        "links": links,
        "categories": categories,
    }
    if progress:
        if links:
            unique_cats = {c for c in categories if c}
            cat_str = f" ({', '.join(sorted(unique_cats))})" if unique_cats else ""
            print(f"    Found {len(links)} link(s){cat_str}.", flush=True)
        else:
            print(f"    No links found.", flush=True)
    return (index, result, None, None)


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
    workers: int = 1,
    rate_limiter: SlidingWindowRateLimiter | None = None,
    on_update: Callable[[list[dict[str, object]]], None] | None = None,
    on_error: Callable[[str, str], None] | None = None,
    resume_from: set[str] | None = None,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    subscriptions_list = list(subscriptions)

    if channel_filter:
        lower_filter = channel_filter.lower()
        subscriptions_list = [
            s for s in subscriptions_list
            if lower_filter in s.title.lower() or lower_filter in s.url.lower()
        ]
        if not subscriptions_list:
            print(f"No channels matching '{channel_filter}'", file=sys.stderr)
            return results

    if limit is not None and limit > 0:
        subscriptions_list = subscriptions_list[:limit]

    total = len(subscriptions_list)
    lowered_filters = [item.lower() for item in url_filters] if url_filters else None
    skipped_count = 0
    error_count = 0
    start_time = time.monotonic()
    results_lock = threading.Lock()

    if workers <= 1:
        try:
            for index, subscription in enumerate(subscriptions_list, start=1):
                _, result, error, _skip = _scrape_one_channel(
                    (index, subscription),
                    timeout=timeout, use_proxy=use_proxy, max_retries=max_retries,
                    rate_limiter=rate_limiter, url_filters=lowered_filters,
                    resume_from=resume_from, on_error=on_error,
                    progress=progress, total=total,
                )
                if result is not None:
                    with results_lock:
                        results.append(result)
                    if on_update is not None:
                        on_update(list(results))
                elif error:
                    error_count += 1
                elif _skip:
                    skipped_count += 1
        except KeyboardInterrupt:
            if progress:
                print("\nInterrupted, saving collected links...", flush=True)
            return results
    else:
        # Parallel mode with semaphore for rate limit
        sem = threading.Semaphore(max(1, workers))
        limiter_for_sem = rate_limiter
        fetch_args = [
            (i + 1, sub) for i, sub in enumerate(subscriptions_list)
            if resume_from is None or sub.title not in resume_from
        ]
        remaining = len(subscriptions_list) - len(fetch_args)
        skipped_count = remaining

        def _fetch_with_sem(item: tuple[int, Subscription]) -> tuple[int, dict | None, str | None, str | None]:
            with sem:
                return _scrape_one_channel(
                    item, timeout=timeout, use_proxy=use_proxy, max_retries=max_retries,
                    rate_limiter=limiter_for_sem, url_filters=lowered_filters,
                    resume_from=None, on_error=on_error, progress=progress,
                    total=total,
                )

        try:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(_fetch_with_sem, item): item for item in fetch_args}
                for future in as_completed(futures):
                    _, result, error, _skip = future.result()
                    if result is not None:
                        with results_lock:
                            results.append(result)
                        if on_update is not None:
                            on_update(list(results))
                    elif error:
                        error_count += 1
        except KeyboardInterrupt:
            if progress:
                print("\nInterrupted, saving collected links...", flush=True)
            return results

    elapsed = time.monotonic() - start_time
    if progress and total > 0:
        print(f"\nScraped {len(results)}/{total} channels, "
              f"found {sum(len(r['links']) for r in results)} links total, "
              f"skipped {skipped_count}, errors {error_count}, "
              f"took {elapsed:.0f}s", flush=True)

    return results


# --- Dead link checking ---------------------------------------------------------

def check_links(
    results: list[dict[str, object]],
    *,
    timeout: int = 15,
    progress: bool = True,
    workers: int = 10,
) -> list[dict[str, object]]:
    """HEAD-check every link and annotate results with status codes."""
    # Collect all unique links
    all_links: dict[str, tuple[int, int]] = {}  # url -> (channel_idx, link_idx)
    for ci, ch in enumerate(results):
        for li, link in enumerate(ch.get("links", [])):
            if link not in all_links:
                all_links[link] = (ci, li)

    urls = list(all_links.keys())
    total = len(urls)
    if total == 0:
        return results

    if progress:
        print(f"Checking {total} link(s) for dead/broken...", flush=True)

    checked: dict[str, Optional[int]] = {}
    lock = threading.Lock()
    completed = 0

    def _check_one(url: str) -> None:
        nonlocal completed
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
            with _make_opener().open(req, timeout=timeout) as resp:
                code = resp.getcode()
        except urllib.error.HTTPError as exc:
            code = exc.code
        except Exception:
            code = None
        with lock:
            checked[url] = code
            completed += 1
            if progress and completed % 20 == 0:
                print(f"  Checked {completed}/{total}...", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(_check_one, urls))

    # Annotate results
    live_count = 0
    dead_count = 0
    for ch in results:
        ch["link_statuses"] = []
        for link in ch.get("links", []):
            code = checked.get(link)
            ch["link_statuses"].append(code)
            if code and 200 <= code < 400:
                live_count += 1
            else:
                dead_count += 1

    if progress:
        print(f"  {live_count} live, {dead_count} dead/broken", flush=True)

    return results


# --- Change diffing -------------------------------------------------------------

def diff_links(
    results: list[dict[str, object]],
    previous_path: Path,
) -> Optional[dict[str, dict[str, list[str]]]]:
    """Compare current results against a previous JSON run. Returns {title: {added: [], removed: []}}."""
    try:
        prev = json.loads(previous_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    prev_map: dict[str, set[str]] = {}
    for ch in prev:
        prev_map[ch.get("channel_title", "")] = set(ch.get("links", []))

    diffs: dict[str, dict[str, list[str]]] = {}
    for ch in results:
        title = ch.get("channel_title", "")
        current_links = set(ch.get("links", []))
        prev_links = prev_map.get(title, set())
        added = sorted(current_links - prev_links)
        removed = sorted(prev_links - current_links)
        if added or removed:
            diffs[str(title)] = {"added": added, "removed": removed}
    return diffs if diffs else None


# --- HTML generation ------------------------------------------------------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>YouTube Channel Links</title>
<style>
:root{{--bg:#0f0f0f;--surface:#1a1a1a;--border:#333;--text:#e0e0e0;--muted:#999;--accent:#3ea6ff;--dead:#f87171;--live:#4ade80}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);margin:0;padding:2rem}}
h1{{font-size:1.5rem;margin-bottom:.25rem}} .meta{{color:var(--muted);font-size:.85rem;margin-bottom:2rem}}
.filters{{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1.5rem}}
.filters input,.filters select{{padding:.5rem .75rem;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);font-size:.85rem}}
.channel{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:1.25rem;margin-bottom:1rem}}
.channel h2{{margin:0 0 .25rem;font-size:1.1rem}} .channel h2 a{{color:var(--accent);text-decoration:none}}
.channel h2 a:hover{{text-decoration:underline}} .channel .url{{color:var(--muted);font-size:.8rem;margin-bottom:.75rem}}
.links{{display:flex;flex-wrap:wrap;gap:.35rem}}
.link{{display:inline-flex;align-items:center;gap:.3rem;padding:.25rem .6rem;border-radius:4px;font-size:.8rem;text-decoration:none;background:rgba(62,166,255,.1);color:var(--accent);border:1px solid rgba(62,166,255,.15)}}
.link:hover{{background:rgba(62,166,255,.2)}}
.link.dead{{background:rgba(248,113,113,.1);color:var(--dead);border-color:rgba(248,113,113,.2);text-decoration:line-through}}
.cat{{font-size:.65rem;padding:1px 5px;border-radius:3px;background:rgba(255,255,255,.06);color:var(--muted);text-transform:uppercase;letter-spacing:.5px}}
.sort-btn{{cursor:pointer;user-select:none;padding:.35rem .7rem;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);font-size:.8rem}}
.sort-btn.active{{border-color:var(--accent);color:var(--accent)}}
.diff-added{{background:rgba(74,222,128,.15)}} .diff-removed{{background:rgba(248,113,113,.15);text-decoration:line-through}}
.summary{{display:flex;gap:1.5rem;margin-bottom:2rem;flex-wrap:wrap}}
.summary-card{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:1rem 1.5rem;text-align:center}}
.summary-card .num{{font-size:2rem;font-weight:700;color:var(--accent)}}
.summary-card .label{{font-size:.75rem;color:var(--muted);text-transform:uppercase;margin-top:.25rem}}
</style>
</head>
<body>
<h1>YouTube Channel Links</h1>
<p class="meta">{total_channels} channels &middot; {total_links} links &middot; generated {generated_at}</p>
<div class="summary">{summary_cards}</div>
<div class="filters">
  <input type="search" id="search" placeholder="Filter channels or links..." oninput="filter()">
  <select id="catFilter" onchange="filter()">
    <option value="">All categories</option>{cat_options}
  </select>
  <button class="sort-btn active" onclick="sortChannels('default')">Default</button>
  <button class="sort-btn" onclick="sortChannels('title')">A-Z</button>
  <button class="sort-btn" onclick="sortChannels('links')">Most Links</button>
</div>
<div id="channels">{channel_html}</div>
<script>
function filter(){{var q=(document.getElementById('search').value||'').toLowerCase();var cat=document.getElementById('catFilter').value;document.querySelectorAll('.channel').forEach(function(c){{var text=c.textContent.toLowerCase();var matchCat=!cat||c.querySelectorAll('.cat').length==0||c.querySelector('.cat[data-cat='+CSS.escape(cat)+']');var matchSearch=!q||text.includes(q);c.style.display=matchSearch&&matchCat?'':'none'}})}}
function sortChannels(mode){{document.querySelectorAll('.sort-btn').forEach(function(b){{b.classList.toggle('active',b.textContent.trim().toLowerCase().includes(mode))}});var container=document.getElementById('channels');var channels=Array.from(container.querySelectorAll('.channel'));if(mode==='title')channels.sort(function(a,b){{return(a.querySelector('h2 a').textContent||'').localeCompare(b.querySelector('h2 a').textContent||'')}});else if(mode==='links')channels.sort(function(a,b){{return b.querySelectorAll('.link').length-a.querySelectorAll('.link').length}});else channels.sort(function(a,b){{return parseInt(a.dataset.idx)-parseInt(b.dataset.idx)}});channels.forEach(function(c){{return container.appendChild(c)}})}}
</script>
</body>
</html>"""


def generate_html(
    results: list[dict[str, object]],
    output_path: Path,
    *,
    diff_data: Optional[dict[str, dict[str, list[str]]]] = None,
) -> None:
    """Generate a self-contained HTML page from scrape results."""
    _esc = _html_mod.escape

    total_links = sum(len(ch.get("links", [])) for ch in results)
    all_cats: set[str] = set()
    for ch in results:
        for c in ch.get("categories", []):
            if c:
                all_cats.add(str(c))

    cat_options = "\n".join(
        f'<option value="{_esc(c)}">{_esc(c)}</option>' for c in sorted(all_cats)
    )

    dead_count = sum(
        1 for ch in results
        for s in (ch.get("link_statuses") or [])
        if s is None or not (200 <= s < 400)
    )

    summary_cards = (
        f'<div class="summary-card"><div class="num">{len(results)}</div><div class="label">Channels</div></div>'
        f'<div class="summary-card"><div class="num">{total_links}</div><div class="label">Links</div></div>'
        f'<div class="summary-card"><div class="num">{len(all_cats)}</div><div class="label">Categories</div></div>'
    )
    if dead_count > 0:
        summary_cards += (
            f'<div class="summary-card"><div class="num" style="color:var(--dead)">{dead_count}</div>'
            f'<div class="label">Dead Links</div></div>'
        )

    channel_parts = []
    for idx, ch in enumerate(results):
        title = _esc(str(ch.get("channel_title", "")))
        url = _esc(str(ch.get("channel_url", "")))
        links = ch.get("links", [])
        categories = ch.get("categories", [])
        statuses = ch.get("link_statuses") or []
        diff_info = (diff_data or {}).get(str(ch.get("channel_title", "")), {})

        added = set(diff_info.get("added", []))
        removed = set(diff_info.get("removed", []))

        link_html_parts = []
        for li, link in enumerate(links):
            cat = categories[li] if li < len(categories) else None
            status = statuses[li] if li < len(statuses) else None
            is_dead = status is not None and not (200 <= status < 400)
            classes = ["link"]
            if is_dead:
                classes.append("dead")
            if link in added:
                classes.append("diff-added")
            if link in removed:
                classes.append("diff-removed")

            cat_tag = f'<span class="cat" data-cat="{_esc(cat or "")}">{_esc(cat or "other")}</span>' if cat else ""
            link_html_parts.append(
                f'<a href="{_esc(link)}" class="{" ".join(classes)}" target="_blank" rel="noopener">'
                f'{_esc(link[:60] + ("..." if len(link) > 60 else ""))}{cat_tag}</a>'
            )

        links_html = "".join(link_html_parts) or '<span class="cat">No links</span>'
        channel_parts.append(
            f'<div class="channel" data-idx="{idx}">'
            f'<h2><a href="{url}" target="_blank" rel="noopener">{title}</a></h2>'
            f'<div class="url">{url}</div>'
            f'<div class="links">{links_html}</div>'
            f'</div>'
        )

    generated_at = _esc(time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
    html = _HTML_TEMPLATE.format(
        total_channels=len(results),
        total_links=total_links,
        generated_at=generated_at,
        summary_cards=summary_cards,
        cat_options=cat_options,
        channel_html="\n".join(channel_parts),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=str(output_path.parent)
    ) as tmp:
        tmp.write(html)
        os.replace(tmp.name, output_path)


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
                        help="Destination file (.json, .csv, or .html)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help=f"Max retries for transient errors (default: {DEFAULT_RETRIES})")
    parser.add_argument("--workers", type=int, default=1,
                        help="Number of parallel workers for scraping (default: 1)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only scrape the first N channels")
    parser.add_argument("--channel", dest="channel_filter", default=None,
                        help="Only scrape channels whose title or URL contains this string")
    parser.add_argument("--sort", dest="sort_order", default=None,
                        choices=["title", "links", "categories"],
                        help="Sort output by title, link count, or category count")
    parser.add_argument("-f", "--filter", dest="filters", action="append",
                        help="Only include links containing this substring (can repeat)")
    parser.add_argument("--check-links", action="store_true",
                        help="HEAD-check every link and annotate with HTTP status")
    parser.add_argument("--diff", dest="diff_path", default=None,
                        help="Compare against a previous JSON output and show changes")
    parser.add_argument("--html", action="store_true",
                        help="Also generate an HTML output page")
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
        workers=args.workers,
        rate_limiter=rate_limiter,
        on_update=write_results if args.workers <= 1 else None,
        on_error=on_error,
        resume_from=resume_from,
    )

    # Sort results if requested
    if args.sort_order == "title":
        results.sort(key=lambda r: str(r.get("channel_title", "")).lower())
    elif args.sort_order == "links":
        results.sort(key=lambda r: len(r.get("links", [])), reverse=True)
    elif args.sort_order == "categories":
        results.sort(key=lambda r: len(set(c for c in r.get("categories", []) if c)), reverse=True)

    # Dead link checking
    if args.check_links:
        results = check_links(results, timeout=min(args.timeout, 15), progress=not args.no_progress)

    # Change diffing
    diff_data = None
    if args.diff_path:
        diff_data = diff_links(results, Path(args.diff_path))
        if diff_data:
            print(f"\nChanges from {args.diff_path}:")
            for title, changes in diff_data.items():
                if changes["added"]:
                    for link in changes["added"]:
                        print(f"  + [{title}] {link}")
                if changes["removed"]:
                    for link in changes["removed"]:
                        print(f"  - [{title}] {link}")
        else:
            print(f"\nNo changes from {args.diff_path}")

    # Write main output
    write_results(results)

    # Generate HTML page
    if args.html or output_path.suffix.lower() == ".html":
        html_path = output_path.with_suffix(".html")
        generate_html(results, html_path, diff_data=diff_data)
        print(f"Generated HTML page at {html_path.resolve()}")

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
