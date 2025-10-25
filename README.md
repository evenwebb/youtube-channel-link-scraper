# YouTube Channel Link Scraper

A small Python utility that reads the `subscriptions.csv` file exported by Google Takeout and resolves every subscribed channel's external links (Patreon, Twitter, merch stores, etc.). The links are collected from each channel's **About** page and saved into a single JSON file so you can quickly locate creators on their other platforms without opening the channel pages one at a time.

The scraper relies on the [`r.jina.ai`](https://r.jina.ai) proxy to fetch public
YouTube pages because direct access to youtube.com is often rate-limited or
blocked in headless environments. Be courteous and keep the default delay
between requests to avoid overwhelming the proxy.

## Requirements

* Python 3.10 or newer (the script only uses the Python standard library)
* An exported `subscriptions.csv` file from Google Takeout

## Usage

1. Place your `subscriptions.csv` file in this directory (or note its path).
2. Run the scraper, pointing it to the CSV and choosing an output location:

   ```bash
   python scrape_links.py /path/to/subscriptions.csv -o channel_links.json
   ```

3. Inspect `channel_links.json` to see a list of objects containing the channel
   title, a canonical channel URL, and the ordered list of external links.

The script accepts a few optional flags:

* `--delay`: seconds to wait between requests (defaults to `0.5`). Increase this
  if you process a large number of subscriptions.
* `-o/--output`: location of the JSON file that will be produced. Defaults to
  `channel_links.json` in the current directory. The file is created
  immediately and updated after each processed channel so you can inspect
  partial results if the run is interrupted.
* `-f/--filter`: limit collected links to those containing the provided
  substring. Repeat the flag to match multiple substrings (e.g.
  `-f facebook.com -f instagram.com`).
* `--no-progress`: suppress the progress messages that are printed for each
  processed channel.

## Example

A small `sample_subscriptions.csv` file is included for quick testing. Running

```bash
python scrape_links.py sample_subscriptions.csv -o sample_links.json --delay 0
```

produces a JSON file with all of the external links advertised by the channel in
order.

## Output format

The resulting JSON file contains a list of entries like the following:

```json
[
  {
    "channel_title": "T90Official - Age Of Empires 2",
    "channel_url": "https://www.youtube.com/channel/UCZUT79WUUpZlZ-XMF7l4CFg",
    "links": [
      "http://bit.ly/2z4T4rm",
      "https://www.facebook.com/T90Official",
      "https://teespring.com/stores/t90officials-store",
      "https://www.patreon.com/T90Official",
      "https://twitter.com/t90official",
      "https://www.instagram.com/t90official/",
      "https://www.twitch.tv/t90official"
    ]
  }
]
```

Each `links` array preserves the order used on the channel's About page and
omits duplicates that appear in both the header and description.
