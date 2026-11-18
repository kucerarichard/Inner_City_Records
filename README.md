# Inner City Records

## Why Am I Collecting All Inner City Releases?

Objectively stated, Inner City is widely respected by collectors and critics for making vital European and contemporary jazz-fusion accessible in the late 1970s and early 1980s.  What I've found is that Inner City studios, producers and engineers produced unusually clear and tastefully balanced recordings of jazz artists and groups from the late 70s.

For example, the difference made by Inner City can be heard in the Ron Malo (Rolling Stones) produced-and-engineered Arrowhead by the Wayne Johnson Trio (versus followup releases by the same group under other labels or independently).  Malo managed to capture something crystalline, fresh and original, which went missing later, and this is not explained solely by the unfortunately overshadowing and suffocating influence of Pat Metheny over Johnson,  but also the absence of Flim and the BB's contributions, previously experienced within the intimacy of Malo's direct approach and Inner City's studios.   It's good stuff.

## Retrieval Repo

This repo builds a deduplicated catalogue of the Inner City Records jazz label and its subsidiaries City Sounds and Aural Explorer, matches each album on Spotify, and creates a Spotify playlist with every track, in release-year order.

The catalogue comes from two sources:

- The [Discogs](https://www.discogs.com) API, for labels 24555 (Inner City Records), 72280 (City Sounds), and 30900 (Aural Explorer).
- The [NerdyJazz label page](https://nerdyjazz.com/labels/inner-city-records/), scraped with a headless browser.

Both sources are merged into `registry.json`, one entry per album. The Spotify matches and the playlist ID are stored in that file too, so every step can be rerun safely.

### Files

| File | Purpose |
| --- | --- |
| `inner_city.py` | Main tool: builds the registry, matches albums on Spotify, writes the reports, and creates the playlist. |
| `inner_city_discogs_spotify.py` | Standalone script for Discogs only. `inner_city.py` imports its Discogs fetch and Spotify search code. |
| `inner_city_nerdy_spotify.py` | Standalone script for NerdyJazz only. `inner_city.py` imports its scraper. |
| `registry.json` | Generated catalogue, with one entry per album. |
| `inner_city_registry.md`, `.pdf` | Generated reports. |
| `.spotify_user_token.json` | Spotify login saved by the `playlist` command. Keep it private. |

The two standalone scripts still run on their own. Both write `inner_city_releases.md`, `inner_city_releases.pdf`, and `spotify_cache.json`, so running one overwrites the other's output.

### Requirements

- Python 3.10 or later. Tested on 3.14 under Windows 11.
- A Spotify account with Premium. Since March 2026, Spotify's developer mode only works for app owners with Premium.
- Optional: a Discogs personal access token, which raises the Discogs rate limit from 25 to 60 requests per minute.

Install the dependencies:

```bash
pip install requests reportlab playwright
```
```bash
python -m playwright install chromium
```

### Spotify app setup

1. Log in at <https://developer.spotify.com/dashboard>.
2. Create an app. The name and description can be anything.
3. Add the Redirect URI `http://127.0.0.1:8888/callback`. Spotify rejects `localhost`, so use the IP address.
4. Under APIs used, select Web API.
5. Copy the Client ID and Client Secret from the app's settings.

Set the credentials as environment variables in your shell. Don't commit them.

PowerShell:

```powershell
$env:SPOTIFY_CLIENT_ID="..."
$env:SPOTIFY_CLIENT_SECRET="..."
$env:DISCOGS_TOKEN="..."      # optional
```

bash:

```bash
export SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... DISCOGS_TOKEN=...
```

### Usage

```bash
python inner_city.py collect      # Discogs + NerdyJazz -> registry.json
python inner_city.py spotify      # match unchecked albums on Spotify
python inner_city.py report       # registry.json -> inner_city_registry.md/.pdf
python inner_city.py playlist     # create or update the Spotify playlist
python inner_city.py all          # collect, spotify, report
```

Options:

| Command | Option | Effect |
| --- | --- | --- |
| `collect` | `--skip-discogs`, `--skip-nerdy` | Fetch from one source only. |
| `spotify` | `--limit N` | Check only the next N unchecked albums. |
| `spotify` | `--recheck` | Search again for every album, including ones already checked. |
| `playlist` | `--name "..."` | Playlist name. Default: `Inner City Records`. |
| `playlist` | `--public` | Make the playlist public. Default: private. |

The first `playlist` run opens Spotify in your browser and asks you to approve access to your playlists. After you click Agree, the script saves the login to `.spotify_user_token.json` and later runs reuse it.

### Registry format

`registry.json` has two top-level keys. `releases` maps a normalized `artist||album` key to an album entry. `playlist` holds the ID, URL, and track count of the playlist the script created.

```json
"52nd street||scrapple to the apple": {
  "artist": "52nd Street",
  "album": "Scrapple To The Apple",
  "year": 1985,
  "labels": ["Inner City Records"],
  "catnos": ["IC 1165", "PCD-22041"],
  "formats": ["LP, Album", "CD, Album, RE", "LP, Album, Promo"],
  "style": "",
  "sources": {
    "discogs": "https://www.discogs.com/release/5916632",
    "nerdyjazz": null
  },
  "spotify": {
    "checked": "2026-11-15",
    "album_id": "4rcSh0bR3GiLfEwCNeAtS4",
    "url": "https://open.spotify.com/album/4rcSh0bR3GiLfEwCNeAtS4"
  }
}
```

`sources` lists every source that has the album. A `nerdyjazz` value of `null` means NerdyJazz lists the album without a link to its own page. In `spotify`, `checked` is the date of the last search, and an `album_id` of `null` with a date set means the search found no match.

Rerunning `collect` adds new albums and source details to the registry. It keeps the existing Spotify data, and it never removes albums.

### Album matching

Discogs lists each pressing as a separate release. The Discogs fetch first merges pressings with the same label, artist, and title. Then `inner_city.py` merges albums across both sources in two passes.

1. Exact match on the normalized key. Normalization converts to lowercase ASCII and drops bracketed text and punctuation. Discogs artist suffixes such as `(2)` and `*` are removed, and `Beatles, The` becomes `The Beatles`.
2. Fuzzy match, for cases where the sources credit the same album differently:

   | Source A | Source B |
   | --- | --- |
   | Bob Mover, Tom Harrell | Bob Mover Featuring Tom Harrell |
   | Dave Liebman Quartet, Mike Nock | David Liebman Quartet Featuring Mike Nock |
   | Zoot Sims' Party | Party |
   | Un Rêve Sans Conséquence Spéciale | Un Rêve Sans Conséquence Spéciale = A Dream Without Reason |

   Artists match when at least half of their name words are shared, ignoring words like "Quartet", "Orchestra", and "Featuring", or when the full names are similar. Titles match at a similarity ratio of 0.88 or higher. Titles are also compared with the artist's name removed and with any ` = translation` part dropped. Titles containing different numbers never match, so "The Meeting Vol. 1" and "The Meeting Vol. 2" stay separate.

The registry built on 2026-11-15 has 429 albums: 320 from both sources, 84 from Discogs only, and 25 from NerdyJazz only. Most NerdyJazz-only albums are 1950s and 1960s European recordings that Inner City reissued but that Discogs lists under other labels.

### Spotify matching

The `spotify` command searches each album twice, first with an `album:"..." artist:"..."` field query and then with a plain text query. It accepts a result when the title similarity is at least 0.75 and the artist similarity is at least 0.6. Before searching, it drops bracketed text and anything after "Featuring" or "With" from the artist name. On 2026-11-15, 184 of the first 405 albums checked had a match.

### Playlist deduplication

The `playlist` command reads the matched albums in year order and adds every track from each one. It skips a track in these cases:

- The track's Spotify ID is already on the playlist.
- An earlier registry entry matched the same Spotify album.
- A track with the same title and lead artist, within 3 seconds of the same length, is already on the playlist. This catches reissues and compilations. Alternate takes usually differ in length, so they stay.

On each run the command first reads the playlist's current tracks, so a rerun adds only tracks that are missing. If the saved playlist has been deleted in Spotify, the command creates a new one.

### Rate limits

Discogs allows 25 requests per minute without a token and 60 with one. The fetch waits between pages to stay under the limit.

Spotify limits developer-mode apps to a daily request quota. When the quota runs out, Spotify returns HTTP 429 with reason `QUOTA_EXCEEDED` and a `Retry-After` of about 24 hours. If Spotify asks for a wait longer than 120 seconds, the script saves its progress and exits:

```
Spotify's daily request quota for this app is used up (wait 23.9 h, until about Mon 13:16).
Progress is saved in registry.json; rerun the same command then and it picks up where it stopped.
```

A full `spotify` run on about 430 albums used up the daily quota. A full `playlist` run needs about one request per matched album plus one per 100 tracks added.

### Spotify API version

The code uses the Web API endpoints as changed in February 2026: `POST /me/playlists` to create a playlist and `/playlists/{id}/items` to read and add tracks. Search returns at most 10 results. See the [February 2026 changelog](https://developer.spotify.com/documentation/web-api/references/changes/february-2026).

### Limitations

- The NerdyJazz scraper depends on the page's table layout and its "Load More" button. It removes the newsletter popup that covers the button. If the site changes, `collect` reports zero NerdyJazz albums or fails while clicking.
- Albums that aren't on Spotify, or are listed there under a very different title, stay unmatched. Open the registry entry and set `spotify.album_id` and `spotify.url` by hand, and the next `playlist` run includes the album.
- Fuzzy matching can merge two different albums with similar titles by the same artist, such as "Live At Montreux" and "Live In Montreux". Check entries whose `sources` list is unexpected.
