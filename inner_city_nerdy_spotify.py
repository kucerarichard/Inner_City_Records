#!/usr/bin/env python3
"""
Scrape the Inner City Records discography from NerdyJazz, look each release
up on Spotify, and write a Markdown file and a PDF.

Setup:
    pip install playwright requests reportlab
    playwright install chromium

Spotify credentials (free): create an app at https://developer.spotify.com/dashboard
then export:
    export SPOTIFY_CLIENT_ID=...
    export SPOTIFY_CLIENT_SECRET=...

Run:
    python inner_city_spotify.py
    python inner_city_spotify.py --no-spotify     # scrape only
    python inner_city_spotify.py --limit 25       # quick test run

Outputs (in the current directory):
    inner_city_releases.md
    inner_city_releases.pdf
    spotify_cache.json     (lookups are cached; reruns resume where they stopped)
"""
import argparse
import base64
import difflib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

URL = "https://nerdyjazz.com/labels/inner-city-records/"
CACHE_FILE = Path("spotify_cache.json")
MD_FILE = Path("inner_city_releases.md")
PDF_FILE = Path("inner_city_releases.pdf")

# Finds the discography table (the one with Artist/Album/Style/Year headers)
# and returns its rows as [artist, album, style, year, album_href_or_null].
JS_READ_ROWS = r"""
() => {
  const tables = [...document.querySelectorAll('table')];
  const t = tables.find(t => {
    const h = t.innerText.toLowerCase();
    return h.includes('artist') && h.includes('album') && h.includes('year');
  });
  if (!t) return null;
  return [...t.querySelectorAll('tbody tr')].map(tr => {
    const c = [...tr.querySelectorAll('td')];
    if (c.length < 4) return null;
    const a = c[1].querySelector('a');
    return [c[0].innerText.trim(), c[1].innerText.trim(),
            c[2].innerText.trim(), c[3].innerText.trim(), a ? a.href : null];
  }).filter(Boolean);
}
"""


# --------------------------------------------------------------------------
# 1. Scrape
# --------------------------------------------------------------------------
def scrape_releases():
    """The page only renders ~20 rows and has a 'Load More' button, so drive
    a real browser and keep clicking until all rows are present."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle")
        page.wait_for_selector("table")

        def row_count():
            rows = page.evaluate(JS_READ_ROWS)
            return len(rows) if rows else 0

        while True:
            btn = page.get_by_text(re.compile(r"load more", re.I)).first
            if btn.count() == 0 or not btn.is_visible():
                break
            before = row_count()
            # A newsletter popup can cover the button; remove it before clicking.
            page.evaluate("""() => document.querySelectorAll(
                '#newsletter-popup, .newsletter-popup, .newsletter-popup-backdrop'
            ).forEach(el => el.remove())""")
            btn.scroll_into_view_if_needed()
            btn.click()
            try:
                page.wait_for_function(
                    """(n) => {
                        const t = [...document.querySelectorAll('table')].find(t =>
                            t.innerText.toLowerCase().includes('album') &&
                            t.innerText.toLowerCase().includes('year'));
                        return t && t.querySelectorAll('tbody tr').length > n;
                    }""",
                    arg=before,
                    timeout=10000,
                )
            except PWTimeout:
                break  # nothing new loaded, so we're done
            print(f"  loaded {row_count()} rows...", file=sys.stderr)

        raw = page.evaluate(JS_READ_ROWS) or []
        browser.close()

    releases, seen = [], set()
    for artist, album, style, year, href in raw:
        key = (artist.lower(), album.lower(), year)
        if key in seen:
            continue
        seen.add(key)
        releases.append(
            {"artist": artist, "album": album, "style": style, "year": year, "page": href}
        )
    releases.sort(key=lambda r: (r["artist"].lower(), r["year"], r["album"].lower()))
    return releases


# --------------------------------------------------------------------------
# 2. Spotify
# --------------------------------------------------------------------------
def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)          # drop "(Remastered)" etc.
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def sim(a, b):
    a, b = norm(a), norm(b)
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


class Spotify:
    def __init__(self, client_id, client_secret):
        self.cid, self.secret = client_id, client_secret
        self.token, self.expires = None, 0
        self.s = requests.Session()

    def _auth(self):
        if self.token and time.time() < self.expires - 30:
            return
        basic = base64.b64encode(f"{self.cid}:{self.secret}".encode()).decode()
        r = self.s.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}"},
            timeout=20,
        )
        r.raise_for_status()
        j = r.json()
        self.token, self.expires = j["access_token"], time.time() + j["expires_in"]

    def _search(self, q):
        for _ in range(5):
            self._auth()
            r = self.s.get(
                "https://api.spotify.com/v1/search",
                params={"q": q, "type": "album", "limit": 10},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=20,
            )
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
                continue
            if r.status_code >= 500:
                time.sleep(2)
                continue
            r.raise_for_status()
            return r.json().get("albums", {}).get("items", [])
        return []

    def find_album(self, artist, album):
        """Return a Spotify album URL or None."""
        first_artist = re.split(r",| & | and |/", artist)[0].strip()
        queries = [
            f'album:"{album}" artist:"{first_artist}"',
            f"{album} {first_artist}",
        ]
        want_artists = [a.strip() for a in re.split(r",| & | and |/", artist) if a.strip()]
        best, best_score = None, 0.0
        for q in queries:
            for item in self._search(q):
                album_sim = sim(album, item["name"])
                sp_artists = [a["name"] for a in item["artists"]]
                artist_sim = max(
                    [sim(w, s) for w in want_artists + [artist] for s in sp_artists] or [0]
                )
                score = 0.65 * album_sim + 0.35 * artist_sim
                if album_sim >= 0.75 and artist_sim >= 0.6 and score > best_score:
                    best, best_score = item, score
            if best and best_score >= 0.9:
                break
        return best["external_urls"]["spotify"] if best else None


def add_spotify_links(releases, limit=None):
    cid, secret = os.getenv("SPOTIFY_CLIENT_ID"), os.getenv("SPOTIFY_CLIENT_SECRET")
    if not (cid and secret):
        sys.exit("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET (or use --no-spotify).")
    sp = Spotify(cid, secret)
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}

    todo = releases[:limit] if limit else releases
    for i, r in enumerate(todo, 1):
        key = f"{r['artist']}||{r['album']}"
        if key not in cache:
            try:
                cache[key] = sp.find_album(r["artist"], r["album"])
            except requests.RequestException as e:
                print(f"  ! {key}: {e}", file=sys.stderr)
                continue
            CACHE_FILE.write_text(json.dumps(cache, indent=1))
            time.sleep(0.15)
        r["spotify"] = cache[key]
        if i % 25 == 0:
            print(f"  spotify: {i}/{len(todo)}", file=sys.stderr)
    for r in releases:
        r.setdefault("spotify", cache.get(f"{r['artist']}||{r['album']}"))
    return releases


# --------------------------------------------------------------------------
# 3. Output
# --------------------------------------------------------------------------
def md_escape(s):
    return s.replace("|", "\\|")


def write_markdown(releases):
    found = sum(1 for r in releases if r.get("spotify"))
    lines = [
        "# Inner City Records: Releases",
        "",
        f"Source: <{URL}>  ",
        f"{len(releases)} releases, {found} found on Spotify.",
        "",
        "| Artist | Album | Style | Year | Spotify |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in releases:
        link = f"[Listen]({r['spotify']})" if r.get("spotify") else ""
        lines.append(
            f"| {md_escape(r['artist'])} | {md_escape(r['album'])} | "
            f"{md_escape(r['style'])} | {r['year']} | {link} |"
        )
    MD_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_pdf(releases):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    ss = getSampleStyleSheet()
    cell = ss["BodyText"].clone("cell")
    cell.fontSize, cell.leading = 8, 10

    found = sum(1 for r in releases if r.get("spotify"))
    story = [
        Paragraph("Inner City Records: Releases", ss["Title"]),
        Paragraph(f"{len(releases)} releases, {found} found on Spotify.", ss["Normal"]),
        Spacer(1, 12),
    ]
    data = [["Artist", "Album", "Style", "Year", "Spotify"]]
    for r in releases:
        link = (
            f'<link href="{escape(r["spotify"])}" color="#1a56c4">Listen</link>'
            if r.get("spotify")
            else ""
        )
        data.append(
            [
                Paragraph(escape(r["artist"]), cell),
                Paragraph(escape(r["album"]), cell),
                Paragraph(escape(r["style"]), cell),
                r["year"],
                Paragraph(link, cell),
            ]
        )
    tbl = Table(
        data,
        colWidths=[2.6 * inch, 3.2 * inch, 1.6 * inch, 0.6 * inch, 0.8 * inch],
        repeatRows=1,
    )
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222222")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, 0), 9),
                ("FONTSIZE", (3, 1), (3, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f3f3")]),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
            ]
        )
    )
    story.append(tbl)
    SimpleDocTemplate(
        str(PDF_FILE), pagesize=landscape(letter),
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
    ).build(story)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-spotify", action="store_true", help="skip Spotify lookups")
    ap.add_argument("--limit", type=int, help="only look up the first N releases on Spotify")
    args = ap.parse_args()

    print("Scraping NerdyJazz...", file=sys.stderr)
    releases = scrape_releases()
    print(f"Found {len(releases)} releases.", file=sys.stderr)
    if not releases:
        sys.exit("No releases found; the page layout may have changed.")

    if args.no_spotify:
        for r in releases:
            r["spotify"] = None
    else:
        print("Searching Spotify...", file=sys.stderr)
        add_spotify_links(releases, args.limit)

    write_markdown(releases)
    write_pdf(releases)
    print(f"Wrote {MD_FILE} and {PDF_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
