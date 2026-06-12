#!/usr/bin/env python3
"""Pull the most-clicked stations from radio-browser.info into stations.json.

New top-clicked stations are prepended to the front (deduplicated by stream URL
only), then *every* station — existing and new — is
health checked and dead streams are pruned (always on). Finally the list is
capped, dropping the oldest (now at the tail) first (FIFO).

Usage:
    python update_stations.py

All settings are hardcoded constants below.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import urllib.error
import urllib.request

# ---- hardcoded settings ----
LIMIT = 1000        # number of top-clicked stations to request per run
TIMEOUT = 8.0      # per-stream probe timeout in seconds
MAX_ITEMS = 2500   # hard cap on stations.json; oldest dropped first (FIFO)

# radio-browser mirrors (https://api.radio-browser.info). The official advice is
# to resolve all.api.radio-browser.info and pick a random mirror; we hard-code the
# current mirrors as a fallback and shuffle them.
MIRRORS = [
    "https://de1.api.radio-browser.info",
    "https://de2.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
]

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(REPO_ROOT, "stations.json")

def clean_language(language: str) -> str:
    first = (language or "").split(",")[0].strip()
    return first.title() if first else "Unknown"


def norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "")).strip()


def fetch_top_clicked(limit: int) -> list[dict]:
    """Fetch the most-clicked, non-broken stations, trying mirrors in turn."""
    path = (
        f"/json/stations/search?order=clickcount&reverse=true"
        f"&hidebroken=true&limit={limit}"
    )
    mirrors = MIRRORS[:]
    random.shuffle(mirrors)
    last_err = None
    for base in mirrors:
        try:
            req = urllib.request.Request(base + path, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            print(f"  mirror failed ({base}): {e}", file=sys.stderr)
    raise SystemExit(f"All radio-browser mirrors failed; last error: {last_err}")


def stream_ok(url: str, timeout: float = 8.0, retries: int = 1) -> bool:
    """Best-effort liveness check: 200/206 with an audio-ish content type.

    urllib follows 3xx redirects automatically, so ``resp.status`` reflects the
    final hop. Retries once by default so a transient blip doesn't prune a good
    station. Only response headers are read, so this never blocks on the
    (infinite) stream body.
    """
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct = (resp.headers.get("Content-Type") or "").lower()
                return resp.status in (200, 206) and (
                    ct.startswith("audio/") or "mpegurl" in ct or "ogg" in ct
                    or "octet-stream" in ct or "aacp" in ct or "video/mp2t" in ct
                )
        except Exception:
            if attempt == retries:
                return False
    return False


def load_existing(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dedupe_by_url(stations: list[dict]) -> tuple[list[dict], int]:
    """Drop stations whose stream URL was already seen (keep first)."""
    seen: set[str] = set()
    unique: list[dict] = []
    removed = 0
    for s in stations:
        key = (s.get("url") or "").strip()
        if not key or key in seen:
            removed += 1
            continue
        seen.add(key)
        unique.append(s)
    return unique, removed


def main() -> None:
    stations = load_existing(DEFAULT_FILE)
    https_only = [s for s in stations if (s.get("url") or "").strip().lower().startswith("https://")]
    non_https_removed = len(stations) - len(https_only)
    stations = https_only
    if non_https_removed:
        print(f"Removed {non_https_removed} non-https station(s) from stations.json")
    stations, dup_removed = dedupe_by_url(stations)
    if dup_removed:
        print(f"Removed {dup_removed} pre-existing duplicate URL(s) from stations.json")
    seen_urls = {s["url"].strip() for s in stations}
    start_count = len(stations)

    print(f"Fetching top {LIMIT} most-clicked stations from radio-browser…")
    candidates = fetch_top_clicked(LIMIT)
    print(f"  received {len(candidates)} candidates")

    new_stations = []
    for c in candidates:
        name = norm_name(c.get("name", ""))
        url = (c.get("url_resolved") or c.get("url") or "").strip()
        if not name or not url or len(name) > 60:
            continue
        if not url.lower().startswith("https://"):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        new_stations.append({
            "name": name,
            "url": url,
            "language": clean_language(c.get("language", "")),
        })
    added = len(new_stations)

    # Newly added stations go first, ahead of the existing ones.
    stations = new_stations + stations

    # Health check every station (existing + newly added) and prune the dead.
    removed = 0
    print(f"Verifying {len(stations)} streams (existing + new)…")
    live = []
    for i, s in enumerate(stations, 1):
        if stream_ok(s["url"].strip(), timeout=TIMEOUT):
            live.append(s)
        else:
            removed += 1
            print(f"  [dead] {s['name']} -> {s['url']}")
        if i % 100 == 0:
            print(f"  …checked {i}/{len(stations)}")
    stations = live

    # Enforce the FIFO cap: keep the newest MAX_ITEMS, drop the oldest. Newest
    # are at the front, so the oldest to drop are at the tail.
    dropped = 0
    if len(stations) > MAX_ITEMS:
        dropped = len(stations) - MAX_ITEMS
        stations = stations[:MAX_ITEMS]

    with open(DEFAULT_FILE, "w", encoding="utf-8") as f:
        json.dump(stations, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"Added {added}, removed {removed} dead, dropped {dropped} over cap "
          f"({start_count} -> {len(stations)} total, cap {MAX_ITEMS}).")


if __name__ == "__main__":
    main()
