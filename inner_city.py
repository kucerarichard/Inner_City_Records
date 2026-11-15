#!/usr/bin/env python3
"""
One registry for the Inner City Records catalogue, built from both sources
(Discogs API and the NerdyJazz label page), with Spotify matching and a
Spotify playlist that never gets duplicate tracks.

Setup:
    pip install requests reportlab playwright
    playwright install chromium

Credentials (PowerShell):
    $env:SPOTIFY_CLIENT_ID="..."
    $env:SPOTIFY_CLIENT_SECRET="..."
    $env:DISCOGS_TOKEN="..."          # optional, faster Discogs fetch
The Spotify app must list this Redirect URI:  http://127.0.0.1:8888/callback

Steps (each one is safe to rerun):
    python inner_city.py collect      # Discogs + NerdyJazz -> registry.json
    python inner_city.py spotify      # match registry albums on Spotify
    python inner_city.py report       # registry -> .md and .pdf
    python inner_city.py playlist     # create/update the Spotify playlist
    python inner_city.py all          # collect, spotify, report

Files:
    registry.json            the single source of truth (one entry per album)
    .spotify_user_token.json your Spotify login for playlist edits; keep private
"""
import argparse
import base64
import difflib
import http.server
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
import webbrowser
from datetime import date
from pathlib import Path

import requests

from inner_city_discogs_spotify import (
    Spotify, clean_artist as _clean_artist, fetch_discogs_releases, norm, sim,
)


def clean_artist(name):
    # Discogs marks alternate name spellings with '*' ("Michal Urbaniak*")
    return re.sub(r"\s+", " ", _clean_artist(name).replace("*", "")).strip()

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "registry.json"
TOKEN_FILE = HERE / ".spotify_user_token.json"
MD_FILE = HERE / "inner_city_registry.md"
PDF_FILE = HERE / "inner_city_registry.pdf"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private"
API = "https://api.spotify.com/v1"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
def load_registry():
    if REGISTRY.exists():
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    return {"releases": {}, "playlist": {}}


def save_registry(reg):
    reg["releases"] = dict(sorted(reg["releases"].items()))
    REGISTRY.write_text(json.dumps(reg, indent=1, ensure_ascii=False), encoding="utf-8")


def make_key(artist, album):
    return f"{norm(clean_artist(artist))}||{norm(album)}"


ARTIST_FILLER = set("""the and featuring feat with his her orchestra quartet quintet
    quinteto quartette trio sextet band group arkestra et son guest guests artist
    artists""".split())


def artist_words(artist):
    return {w for w in norm(artist).split() if w not in ARTIST_FILLER and len(w) > 1}


def title_variants(album, words):
    """'X = Translation' -> X too; 'Zoot Sims' Party' -> 'Party' (artist name dropped)."""
    out = set()
    for t in [album] + album.split(" = ")[:1]:
        n = norm(t)
        out.add(n)
        stripped = " ".join(w for w in n.split() if w not in words and w != "s")
        if stripped:
            out.add(stripped)
    return out


def find_entry(reg, artist, album):
    """Exact normalized match first, then a fuzzy match that tolerates the
    two sources crediting musicians differently ('A, B' vs 'A Featuring B',
    'Dave' vs 'David') and small title differences."""
    key = make_key(artist, album)
    if key in reg["releases"]:
        return key
    nums = re.findall(r"\d+", norm(album.split(" = ")[0]))
    aw = artist_words(artist)
    tv = title_variants(album, aw)
    best, best_score = None, 0.0
    for k, e in reg["releases"].items():
        if re.findall(r"\d+", norm(e["album"].split(" = ")[0])) != nums:
            continue  # "Vol. 1" and "Vol. 2" are different albums
        ew = artist_words(e["artist"])
        shared = len(aw & ew) / max(1, min(len(aw), len(ew)))
        a = max(shared, sim(artist, e["artist"]))
        if a < 0.5:
            continue
        t = max(difflib.SequenceMatcher(None, x, y).ratio()
                for x in tv for y in title_variants(e["album"], ew))
        if t >= 0.88 and a + t > best_score:
            best, best_score = k, a + t
    return best


def upsert(reg, artist, album, year, source, url, **extra):
    artist = clean_artist(artist)
    key = find_entry(reg, artist, album)
    if key is None:
        key = make_key(artist, album)
        reg["releases"][key] = {
            "artist": artist, "album": album, "year": None, "labels": [],
            "catnos": [], "formats": [], "style": "", "sources": {},
            "spotify": {"checked": None, "album_id": None, "url": None},
        }
    e = reg["releases"][key]
    if year and (not e["year"] or year < e["year"]):
        e["year"] = year
    e["sources"][source] = url
    for field in ("labels", "catnos", "formats"):
        for v in extra.get(field, []):
            if v and v not in e[field]:
                e[field].append(v)
    if extra.get("style") and not e["style"]:
        e["style"] = extra["style"]
    return key


def cmd_collect(args):
    reg = load_registry()
    before = len(reg["releases"])

    if not args.skip_discogs:
        log("Fetching Discogs...")
        for r in fetch_discogs_releases():
            upsert(reg, r["artist"], r["album"], r["year"] or None, "discogs",
                   r["discogs"], labels=[r["label"]], catnos=r["catnos"],
                   formats=r["formats"])
        save_registry(reg)

    if not args.skip_nerdy:
        log("Scraping NerdyJazz...")
        from inner_city_nerdy_spotify import scrape_releases
        new = matched = 0
        for r in scrape_releases():
            n = len(reg["releases"])
            y = int(r["year"]) if str(r["year"]).isdigit() else None
            upsert(reg, r["artist"], r["album"], y, "nerdyjazz", r["page"],
                   style=r["style"])
            if len(reg["releases"]) > n:
                new += 1
            else:
                matched += 1
        log(f"  NerdyJazz: {matched} matched existing entries, {new} new")
        save_registry(reg)

    both = sum(1 for e in reg["releases"].values() if len(e["sources"]) > 1)
    log(f"Registry: {len(reg['releases'])} albums ({len(reg['releases']) - before} new), "
        f"{both} confirmed by both sources -> {REGISTRY.name}")


# --------------------------------------------------------------------------
# Spotify matching (app credentials, no login needed)
# --------------------------------------------------------------------------
def creds():
    cid, secret = os.getenv("SPOTIFY_CLIENT_ID"), os.getenv("SPOTIFY_CLIENT_SECRET")
    if not (cid and secret):
        sys.exit("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET first.")
    return cid, secret


MAX_WAIT = 120   # seconds; longer Spotify waits mean the daily quota is used up


def quota_exit(retry_after):
    hours = int(retry_after) / 3600
    resume = time.strftime("%a %H:%M", time.localtime(time.time() + int(retry_after)))
    sys.exit(f"\nSpotify's daily request quota for this app is used up "
             f"(wait {hours:.1f} h, until about {resume}). Progress is saved in "
             f"{REGISTRY.name}; rerun the same command then and it picks up where it stopped.")


class SearchSpotify(Spotify):
    """App-credential search that stops on a quota block instead of sleeping for hours."""
    def _search(self, q):
        for _ in range(5):
            self._auth()
            r = self.s.get(f"{API}/search", params={"q": q, "type": "album", "limit": 10},
                           headers={"Authorization": f"Bearer {self.token}"}, timeout=20)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "5"))
                if wait > MAX_WAIT:
                    raise QuotaExceeded(wait)
                time.sleep(wait + 1)
                continue
            if r.status_code >= 500:
                time.sleep(2)
                continue
            r.raise_for_status()
            return r.json().get("albums", {}).get("items", [])
        raise requests.RequestException(f"search kept failing: {q}")


class QuotaExceeded(Exception):
    pass


def cmd_spotify(args):
    reg = load_registry()
    if not reg["releases"]:
        sys.exit("Registry is empty; run 'collect' first.")
    sp = SearchSpotify(*creds())
    todo = [e for e in reg["releases"].values()
            if args.recheck or not e["spotify"]["checked"]]
    if args.limit:
        todo = todo[:args.limit]
    log(f"Searching Spotify for {len(todo)} albums...")
    for i, e in enumerate(todo, 1):
        # "Abbey Lincoln ( Aminata Moseka ) Featuring Archie Shepp" -> "Abbey Lincoln"
        lead = re.sub(r"\(.*?\)|\s+(featuring|feat\.?|with)\s.*$", "", e["artist"],
                      flags=re.I).strip() or e["artist"]
        try:
            url = sp.find_album(lead, e["album"])
        except QuotaExceeded as q:
            save_registry(reg)
            quota_exit(q.args[0])
        except requests.RequestException as ex:
            log(f"  ! {e['artist']} - {e['album']}: {ex}")
            continue
        e["spotify"] = {"checked": date.today().isoformat(),
                        "album_id": url.rsplit("/", 1)[-1] if url else None,
                        "url": url}
        if i % 20 == 0:
            save_registry(reg)
            log(f"  {i}/{len(todo)}")
        time.sleep(0.15)
    save_registry(reg)
    found = sum(1 for e in reg["releases"].values() if e["spotify"]["album_id"])
    log(f"{found}/{len(reg['releases'])} albums matched on Spotify.")


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def sorted_entries(reg):
    return sorted(reg["releases"].values(),
                  key=lambda e: (e["artist"].lower(), e["year"] or 9999, e["album"].lower()))


def cmd_report(args):
    reg = load_registry()
    rows = sorted_entries(reg)
    found = sum(1 for e in rows if e["spotify"]["url"])
    src = lambda e: ", ".join(  # noqa: E731
        f"[{s}]({u})" if u else s for s, u in sorted(e["sources"].items()))
    md = ["# Inner City Records catalogue", "",
          f"{len(rows)} albums from Discogs and NerdyJazz, {found} on Spotify.", "",
          "| Artist | Album | Year | Label | Cat # | Sources | Spotify |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    esc = lambda s: s.replace("|", "\\|")  # noqa: E731
    for e in rows:
        sp = f"[Listen]({e['spotify']['url']})" if e["spotify"]["url"] else ""
        md.append(f"| {esc(e['artist'])} | {esc(e['album'])} | {e['year'] or ''} | "
                  f"{esc(', '.join(e['labels']))} | {esc(', '.join(e['catnos'][:3]))} | "
                  f"{src(e)} | {sp} |")
    MD_FILE.write_text("\n".join(md) + "\n", encoding="utf-8")

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    ss = getSampleStyleSheet()
    cell = ss["BodyText"].clone("cell")
    cell.fontSize, cell.leading = 8, 10
    link = lambda u, t: f'<link href="{escape(u)}" color="#1a56c4">{t}</link>' if u else ""  # noqa: E731
    data = [["Artist", "Album", "Year", "Label", "Cat #", "Sources", "Spotify"]]
    for e in rows:
        data.append([
            Paragraph(escape(e["artist"]), cell), Paragraph(escape(e["album"]), cell),
            str(e["year"] or ""), Paragraph(escape(", ".join(e["labels"])), cell),
            Paragraph(escape(", ".join(e["catnos"][:3])), cell),
            Paragraph(" ".join(link(u, s) or s for s, u in sorted(e["sources"].items())), cell),
            Paragraph(link(e["spotify"]["url"], "Listen"), cell),
        ])
    tbl = Table(data, repeatRows=1, colWidths=[
        2.1 * inch, 2.6 * inch, 0.45 * inch, 1.1 * inch, 1.35 * inch, 0.95 * inch, 0.65 * inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222222")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f3f3")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
    ]))
    SimpleDocTemplate(str(PDF_FILE), pagesize=landscape(letter),
                      leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                      topMargin=0.5 * inch, bottomMargin=0.5 * inch).build([
        Paragraph("Inner City Records catalogue", ss["Title"]),
        Paragraph(f"{len(rows)} albums from Discogs and NerdyJazz, {found} on Spotify.",
                  ss["Normal"]),
        Spacer(1, 12), tbl])
    log(f"Wrote {MD_FILE.name} and {PDF_FILE.name}")


# --------------------------------------------------------------------------
# Playlist (needs your Spotify login once)
# --------------------------------------------------------------------------
class UserSpotify:
    def __init__(self, cid, secret):
        self.cid, self.secret = cid, secret
        self.s = requests.Session()
        self.tok = json.loads(TOKEN_FILE.read_text()) if TOKEN_FILE.exists() else {}

    def _token_request(self, data):
        basic = base64.b64encode(f"{self.cid}:{self.secret}".encode()).decode()
        r = self.s.post("https://accounts.spotify.com/api/token", data=data,
                        headers={"Authorization": f"Basic {basic}"}, timeout=20)
        r.raise_for_status()
        j = r.json()
        j.setdefault("refresh_token", self.tok.get("refresh_token"))
        j["expires_at"] = time.time() + j["expires_in"]
        self.tok = j
        TOKEN_FILE.write_text(json.dumps(j))

    def _login(self):
        state = secrets.token_urlsafe(16)
        url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
            "client_id": self.cid, "response_type": "code", "redirect_uri": REDIRECT_URI,
            "scope": SCOPES, "state": state})
        got = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                got.update({k: v[0] for k, v in q.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>Done. You can close this tab.</h2>")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 8888), Handler)
        log("Opening Spotify in your browser. Click Agree to let this script edit playlists.")
        log(f"If no browser opens, visit:\n  {url}")
        webbrowser.open(url)
        while "code" not in got and "error" not in got:
            srv.handle_request()
        srv.server_close()
        if got.get("state") != state or "code" not in got:
            sys.exit(f"Spotify login failed: {got.get('error', 'state mismatch')}")
        self._token_request({"grant_type": "authorization_code", "code": got["code"],
                             "redirect_uri": REDIRECT_URI})

    def _ensure(self):
        if self.tok.get("access_token") and time.time() < self.tok.get("expires_at", 0) - 60:
            return
        if self.tok.get("refresh_token"):
            try:
                return self._token_request({"grant_type": "refresh_token",
                                            "refresh_token": self.tok["refresh_token"]})
            except requests.HTTPError:
                pass
        self._login()

    def call(self, method, path, **kw):
        for _ in range(6):
            self._ensure()
            r = self.s.request(method, path if path.startswith("http") else API + path,
                               headers={"Authorization": f"Bearer {self.tok['access_token']}"},
                               timeout=30, **kw)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "5"))
                if wait > MAX_WAIT:
                    quota_exit(wait)   # playlist adds are checked on rerun, so this is safe
                time.sleep(wait + 1)
                continue
            if r.status_code == 401:
                self.tok["expires_at"] = 0
                continue
            if r.status_code >= 500:
                time.sleep(2)
                continue
            if r.status_code == 404:
                return None
            if not r.ok:
                sys.exit(f"Spotify {method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}
        sys.exit(f"Spotify {method} {path} kept failing; try again later.")

    def pages(self, path, **params):
        page = self.call("GET", path, params={"limit": 50, **params})
        while page:
            yield from page.get("items", [])
            page = self.call("GET", page["next"]) if page.get("next") else None


def track_key(t):
    artist = t["artists"][0]["name"] if t.get("artists") else ""
    return (norm(t["name"]), norm(artist))


def cmd_playlist(args):
    reg = load_registry()
    albums = [e for e in reg["releases"].values() if e["spotify"]["album_id"]]
    if not albums:
        sys.exit("No Spotify matches in the registry; run 'spotify' first.")
    # Chronological: the label's history from first release to last.
    albums.sort(key=lambda e: (e["year"] or 9999, e["artist"].lower(), e["album"].lower()))
    sp = UserSpotify(*creds())

    pl = reg.get("playlist") or {}
    if pl.get("id") and not sp.call("GET", f"/playlists/{pl['id']}"):
        log("Saved playlist no longer exists; creating a new one.")
        pl = {}
    if not pl.get("id"):
        made = sp.call("POST", "/me/playlists", json={
            "name": args.name, "public": args.public,
            "description": "Inner City Records, City Sounds and Aural Explorer, "
                           "matched from Discogs and NerdyJazz."})
        pl = {"id": made["id"], "url": made["external_urls"]["spotify"], "name": args.name}
        reg["playlist"] = pl
        save_registry(reg)
        log(f"Created playlist: {pl['url']}")

    # What is already on the playlist (so reruns only add what's missing).
    have_ids, have_keys = set(), {}
    for it in sp.pages(f"/playlists/{pl['id']}/items"):
        t = it.get("item") or it.get("track")
        if t and t.get("id"):
            have_ids.add(t["id"])
            have_keys.setdefault(track_key(t), []).append(t.get("duration_ms", 0))
    log(f"Playlist already has {len(have_ids)} tracks.")

    def is_dup(t):
        if t["id"] in have_ids:
            return True
        # Same song on a reissue/compilation: same title + artist, ~same length.
        # Alternate takes usually differ in length, so they are kept.
        return any(abs(d - t.get("duration_ms", 0)) <= 3000
                   for d in have_keys.get(track_key(t), []))

    to_add, seen_albums, skipped = [], set(), 0
    for i, e in enumerate(albums, 1):
        aid = e["spotify"]["album_id"]
        if aid in seen_albums:
            continue
        seen_albums.add(aid)
        for t in sp.pages(f"/albums/{aid}/tracks"):
            if not t.get("id"):
                continue
            if is_dup(t):
                skipped += 1
                continue
            to_add.append(t["uri"])
            have_ids.add(t["id"])
            have_keys.setdefault(track_key(t), []).append(t.get("duration_ms", 0))
        if i % 25 == 0:
            log(f"  read {i}/{len(albums)} albums, {len(to_add)} new tracks queued")

    for i in range(0, len(to_add), 100):
        sp.call("POST", f"/playlists/{pl['id']}/items", json={"uris": to_add[i:i + 100]})
        log(f"  added {min(i + 100, len(to_add))}/{len(to_add)}")

    pl["track_count"] = len(have_ids)
    pl["updated"] = date.today().isoformat()
    save_registry(reg)
    log(f"Added {len(to_add)} tracks, skipped {skipped} duplicates. "
        f"Playlist now has {len(have_ids)} tracks: {pl['url']}")


# --------------------------------------------------------------------------
def main():
    for stream in (sys.stdout, sys.stderr):   # Windows consoles can't print every name
        stream.reconfigure(errors="replace")
    ap =argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--skip-discogs", action="store_true")
    c.add_argument("--skip-nerdy", action="store_true")
    s = sub.add_parser("spotify")
    s.add_argument("--limit", type=int)
    s.add_argument("--recheck", action="store_true", help="search again for every album")
    sub.add_parser("report")
    p = sub.add_parser("playlist")
    p.add_argument("--name", default="Inner City Records")
    p.add_argument("--public", action="store_true")
    a = sub.add_parser("all")
    a.set_defaults(skip_discogs=False, skip_nerdy=False, limit=None, recheck=False)
    args = ap.parse_args()

    if args.cmd == "all":
        cmd_collect(args)
        if os.getenv("SPOTIFY_CLIENT_ID"):
            cmd_spotify(args)
        else:
            log("No Spotify credentials set; skipping Spotify matching.")
        cmd_report(args)
    else:
        {"collect": cmd_collect, "spotify": cmd_spotify, "report": cmd_report,
         "playlist": cmd_playlist}[args.cmd](args)


if __name__ == "__main__":
    main()
