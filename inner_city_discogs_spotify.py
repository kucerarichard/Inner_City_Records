#!/usr/bin/env python3
"""
Fetch the Inner City Records, City Sounds and Aural Explorer discographies
from the Discogs API, look each
release up on Spotify, and write a Markdown file and a PDF.

Setup:
    pip install requests reportlab

Spotify credentials (free): https://developer.spotify.com/dashboard
    export SPOTIFY_CLIENT_ID=...
    export SPOTIFY_CLIENT_SECRET=...

Discogs token (optional, raises 25 -> 60 requests/min):
    https://www.discogs.com/settings/developers
    export DISCOGS_TOKEN=...

Run:
    python inner_city_discogs_spotify.py
    python inner_city_discogs_spotify.py --no-spotify
    python inner_city_discogs_spotify.py --limit 25    # quick Spotify test

Outputs: inner_city_releases.md, inner_city_releases.pdf, spotify_cache.json
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

# Discogs label ID -> display name (Inner City Records and its subsidiaries)
LABELS = {
    24555: "Inner City Records",
    72280: "City Sounds",
    30900: "Aural Explorer",
}
USER_AGENT = "InnerCityRecordsLister/1.0"
CACHE_FILE = Path("spotify_cache.json")
MD_FILE = Path("inner_city_releases.md")
PDF_FILE = Path("inner_city_releases.pdf")


# --------------------------------------------------------------------------
# 1. Discogs
# --------------------------------------------------------------------------
def clean_artist(name):
    """'Dexter Gordon (2)' -> 'Dexter Gordon'; 'Beatles, The' -> 'The Beatles'."""
    name = re.sub(r"\s*\(\d+\)", "", name).strip()
    m = re.match(r"^(.*), (The|A|An)$", name)
    return f"{m.group(2)} {m.group(1)}" if m else name


def fetch_discogs_releases():
    headers = {"User-Agent": USER_AGENT}
    token = os.getenv("DISCOGS_TOKEN")
    if token:
        headers["Authorization"] = f"Discogs token={token}"
    delay = 1.1 if token else 2.5   # stay under 60/min or 25/min

    raw = []
    for label_id, label_name in LABELS.items():
        page, pages = 1, 1
        while page <= pages:
            for _ in range(5):
                r = requests.get(
                    f"https://api.discogs.com/labels/{label_id}/releases",
                    params={"page": page, "per_page": 100},
                    headers=headers, timeout=30)
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After", "30")) + 1)
                    continue
                r.raise_for_status()
                break
            else:
                sys.exit("Discogs kept rate-limiting; try again later or set DISCOGS_TOKEN.")
            j = r.json()
            pages = j["pagination"]["pages"]
            for x in j["releases"]:
                x["_label"] = label_name
            raw.extend(j["releases"])
            print(f"  {label_name}: page {page}/{pages} ({len(raw)} entries total)",
                  file=sys.stderr)
            page += 1
            time.sleep(delay)

    # Discogs lists every pressing separately: merge by label + artist + title.
    merged = {}
    for x in raw:
        artist = clean_artist(x.get("artist", ""))
        title = x.get("title", "").strip()
        key = (x["_label"], norm(artist), norm(title))
        year = x.get("year") or 0
        rel = merged.setdefault(key, {
            "label": x["_label"], "artist": artist, "album": title, "year": year,
            "catnos": [], "formats": [], "pressings": 0, "discogs": None,
        })
        rel["pressings"] += 1
        if year and (not rel["year"] or year < rel["year"]):
            rel["year"] = year
        for field, val in (("catnos", x.get("catno")), ("formats", x.get("format"))):
            if val and val not in rel[field]:
                rel[field].append(val)
        if not rel["discogs"] and x.get("id"):
            rel["discogs"] = f"https://www.discogs.com/release/{x['id']}"

    releases = list(merged.values())
    releases.sort(key=lambda r: (r["artist"].lower(), r["year"] or 9999, r["album"].lower()))
    return releases


# --------------------------------------------------------------------------
# 2. Spotify
# --------------------------------------------------------------------------
def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
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
        r = self.s.post("https://accounts.spotify.com/api/token",
                        data={"grant_type": "client_credentials"},
                        headers={"Authorization": f"Basic {basic}"}, timeout=20)
        r.raise_for_status()
        j = r.json()
        self.token, self.expires = j["access_token"], time.time() + j["expires_in"]

    def _search(self, q):
        for _ in range(5):
            self._auth()
            r = self.s.get("https://api.spotify.com/v1/search",
                           params={"q": q, "type": "album", "limit": 10},
                           headers={"Authorization": f"Bearer {self.token}"}, timeout=20)
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
        splitter = r",| & | and |/"
        first = re.split(splitter, artist)[0].strip()
        want = [a.strip() for a in re.split(splitter, artist) if a.strip()] + [artist]
        best, best_score = None, 0.0
        for q in (f'album:"{album}" artist:"{first}"', f"{album} {first}"):
            for item in self._search(q):
                a_sim = sim(album, item["name"])
                sp = [a["name"] for a in item["artists"]]
                ar_sim = max([sim(w, s) for w in want for s in sp] or [0])
                score = 0.65 * a_sim + 0.35 * ar_sim
                if a_sim >= 0.75 and ar_sim >= 0.6 and score > best_score:
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
        "# Inner City Records, City Sounds & Aural Explorer: Releases", "",
        "Labels: " + ", ".join(
            f"[{n}](https://www.discogs.com/label/{i})" for i, n in LABELS.items()) + "  ",
        f"{len(releases)} unique releases (pressings merged), {found} found on Spotify.", "",
        "| Artist | Album | Year | Label | Cat # | Discogs | Spotify |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in releases:
        sp = f"[Listen]({r['spotify']})" if r.get("spotify") else ""
        dg = f"[View]({r['discogs']})" if r.get("discogs") else ""
        lines.append(
            f"| {md_escape(r['artist'])} | {md_escape(r['album'])} | {r['year'] or ''} | "
            f"{r['label']} | {md_escape(', '.join(r['catnos'][:3]))} | {dg} | {sp} |"
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

    def link(url, text):
        return (f'<link href="{escape(url)}" color="#1a56c4">{text}</link>' if url else "")

    found = sum(1 for r in releases if r.get("spotify"))
    story = [
        Paragraph("Inner City Records, City Sounds &amp; Aural Explorer", ss["Title"]),
        Paragraph(f"{len(releases)} unique releases, {found} found on Spotify.", ss["Normal"]),
        Spacer(1, 12),
    ]
    data = [["Artist", "Album", "Year", "Label", "Cat #", "Discogs", "Spotify"]]
    for r in releases:
        data.append([
            Paragraph(escape(r["artist"]), cell),
            Paragraph(escape(r["album"]), cell),
            str(r["year"] or ""),
            Paragraph(escape(r["label"]), cell),
            Paragraph(escape(", ".join(r["catnos"][:3])), cell),
            Paragraph(link(r["discogs"], "View"), cell),
            Paragraph(link(r.get("spotify"), "Listen"), cell),
        ])
    tbl = Table(data, repeatRows=1, colWidths=[
        2.2 * inch, 2.7 * inch, 0.45 * inch, 1.1 * inch, 1.5 * inch, 0.6 * inch, 0.7 * inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222222")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f3f3")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
    ]))
    story.append(tbl)
    SimpleDocTemplate(str(PDF_FILE), pagesize=landscape(letter),
                      leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                      topMargin=0.5 * inch, bottomMargin=0.5 * inch).build(story)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-spotify", action="store_true")
    ap.add_argument("--limit", type=int, help="only look up the first N releases on Spotify")
    args = ap.parse_args()

    print("Fetching Discogs...", file=sys.stderr)
    releases = fetch_discogs_releases()
    print(f"{len(releases)} unique releases.", file=sys.stderr)

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
