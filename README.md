# spotify-nicotine

Download the songs of a Spotify playlist from the Soulseek network, using a
locally running [Nicotine+](https://nicotine-plus.org) client with the
[api-nicotine-plus](https://github.com/palaueb/api-nicotine-plus) REST plugin.

For every track in the playlist it:

1. reads the track info (artist, title, album, duration) from the Spotify Web API,
2. searches Soulseek through your Nicotine+ client,
3. scores every result for **match confidence** (right song?) and **quality**
   (preferred format/bitrate, uploader availability),
4. queues the best match for download in Nicotine+,
5. remembers everything in a state file, so you can stop with `Ctrl+C` at any
   time and re-run the same command to resume.

Tracks where no result clears the confidence threshold are never downloaded
blindly and never hold up the run: they are collected and presented to you
afterwards in an interactive picker.

---

## What you need

- A Mac (these instructions assume macOS) with **Python 3.9 or newer**
- **Nicotine+** (free Soulseek client) with the **api-nicotine-plus** plugin
- A **Spotify developer app** (gives you a Client ID), authorized once in
  your browser. Since Spotify's February 2026 API changes: the app owner
  needs an active **Spotify Premium** subscription, and playlist contents
  are only readable for playlists the authorized account **owns or
  collaborates on** (public or private) — arbitrary strangers' playlists are
  no longer accessible to personal developer apps

Everything below walks through each piece from scratch.

---

## Part 1 — Get this project onto your Mac

Copy this whole `spotify-nicotine` folder to the Mac that will run it (AirDrop,
git, USB drive — anything works). Then open **Terminal** (press `Cmd+Space`,
type "Terminal", press Enter) and move into the folder, e.g.:

```bash
cd ~/Developer/spotify-nicotine
```

(Adjust the path to wherever you put the folder. From here on, all commands
are run inside this folder.)

## Part 2 — Install Python requirements

First check that Python 3.9+ is available:

```bash
python3 --version
```

If that prints `Python 3.9.x` or higher, you're good. If the command is not
found, macOS will offer to install the Command Line Tools — accept, then try
again (or install Python from <https://www.python.org/downloads/>).

Now create a *virtual environment* (a private folder holding this project's
Python packages, so nothing touches your system) and install the requirements:

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

That's the Python side done. You will always run the tool as
`.venv/bin/python -m spotify_nicotine ...` — no need to "activate" anything.

## Part 3 — Install Nicotine+ and log in to Soulseek

1. Download Nicotine+ for macOS from <https://nicotine-plus.org> (the download
   links point to the project's GitHub releases — pick the macOS `.dmg`).
2. Open the `.dmg` and drag Nicotine+ into Applications, then launch it.
   If macOS blocks it ("unidentified developer"), right-click the app →
   **Open** → **Open**.
3. On first launch Nicotine+ asks for a Soulseek **username and password**.
   Soulseek has no signup form — the first login with a new username creates
   the account. Pick anything memorable.
4. Set your download folder and (important for community etiquette) **share
   some music** in Preferences → Shares. Soulseek is peer-to-peer; users who
   share nothing often get refused downloads.
5. Leave Nicotine+ running — the script talks to it while it's open.

## Part 4 — Install the API plugin into Nicotine+

The plugin is a small folder of Python files that makes Nicotine+ listen on
`http://127.0.0.1:12339` for local commands (only your own Mac can reach it).

1. Download the plugin: <https://github.com/palaueb/api-nicotine-plus> →
   green **Code** button → **Download ZIP**, then unzip it. You get a folder
   named something like `api-nicotine-plus-main` — rename it to
   `api-nicotine-plus`.
2. In Nicotine+, open **Preferences → Plugins** and click **+ Add Plugins**.
   This opens Nicotine+'s personal plugin folder in Finder.
3. Copy the whole `api-nicotine-plus` folder into that plugin folder.
4. Back in Preferences → Plugins, quit and reopen Nicotine+ (or toggle the
   plugin list refresh), then tick the checkbox next to **API Nicotine Plus**
   to enable it.
5. Verify it's alive — in Terminal:

```bash
curl http://127.0.0.1:12339/health
```

   You should see: `{"status": "ok", "plugin": "API Nicotine Plus"}`.

Optional: in the plugin's settings (select it in the plugin list →
Preferences/Settings) you can change the port or set an **API token**. If you
set a token, give it to this tool via `NICOTINE_API_TOKEN` in the `.env` file
(Part 5) or the `--api-token` flag.

## Part 5 — Connect to Spotify

1. Go to <https://developer.spotify.com/dashboard> and log in with the
   Spotify account that owns your playlists. Since Spotify's February 2026
   rules, the account that creates the app must have (and keep) an active
   **Premium** subscription.
2. **Create app** — name and description can be anything (e.g.
   "playlist-export"). For **Redirect URI** enter *exactly*:

   ```
   http://127.0.0.1:8080/callback
   ```

   (that's where Spotify sends your browser back after you approve access —
   it points at your own Mac). Tick *Web API* and save.
3. Open the app's **Settings** and copy the **Client ID** (the Client Secret
   is not needed).
4. In the project folder, create your `.env` file from the template and paste
   the id into it (`open -e .env` opens it in TextEdit):

```bash
cp .env.example .env
```

   ```
   SPOTIFY_CLIENT_ID=paste_your_client_id_here
   ```

5. Authorize once — this opens your browser; click **Agree**:

```bash
.venv/bin/python -m spotify_nicotine auth
```

   When the page says "Authorized ✓", the terminal verifies the login and
   prints **`Authorized as: <name> (account id: ...)`** — check that this is
   the account you expect! The browser silently uses whichever Spotify
   account is already signed in at accounts.spotify.com, which is a common
   source of confusing 403 errors. Tokens are cached in
   `.spotify-tokens.json` (treat it like a password file; it's already
   git-ignored) and refreshed automatically from then on — no more browser
   steps.

**Why the browser step?** Spotify no longer allows "app-only" server
credentials to read playlists for newly created developer apps — those
requests fail with **403 Forbidden**, even for public playlists you own.
Authorizing once as your own account is the supported way, and it also lets
the tool read your private playlists.

**What the tool can read (Spotify's Feb 2026 rules for personal apps):**
playlists that the authorized account **owns or collaborates on** — public or
private. Not readable: other people's playlists (follow/save them is not
enough — collaborate or recreate them in your account) and Spotify's own
editorial/algorithmic playlists (Today's Top Hits, Discover Weekly, ...).
The app owner must keep an active **Premium** subscription or the app stops
working.

## Part 6 — First run

With Nicotine+ running and logged in (and Spotify authorized from Part 5 —
if you skipped that, the command below will detect it and offer to open the
browser), do a **dry run** first: it searches and scores 3 tracks but
downloads nothing, so you can sanity-check the matching.

```bash
.venv/bin/python -m spotify_nicotine download "https://open.spotify.com/playlist/YOUR_PLAYLIST_ID" --limit 3 --dry-run
```

If the proposed matches look right, run it for real:

```bash
.venv/bin/python -m spotify_nicotine download "https://open.spotify.com/playlist/YOUR_PLAYLIST_ID"
```

What happens:

- Tracks are searched in overlapping batches (default 6 at once), while new
  searches are dispatched no faster than one per `--search-delay` seconds —
  Soulseek servers temporarily ban clients that search too fast. A
  100-track playlist typically searches in a few minutes.
- Confident matches are queued in Nicotine+'s **Downloads** tab; the files
  land in your normal Nicotine+ download folder (or `--dest-dir`).
- Low-confidence tracks are collected; at the end you're offered an
  interactive picker (see below). They never block the run.
- `Ctrl+C` any time — progress is saved in `./state/`. Re-run the same
  command to resume; already-queued and finished downloads are recognized,
  not re-queued.
- Downloads that are waiting in a remote user's queue keep going inside
  Nicotine+ even after the script exits. Re-run later (or use `status`) to
  update their state.

### Reviewing low-confidence tracks

```bash
.venv/bin/python -m spotify_nicotine resolve "https://open.spotify.com/playlist/YOUR_PLAYLIST_ID"
```

For each unresolved track you get a table like:

```
[1/3] Eagles — Obscure B-Side  [3:23]  (album: Y)
    #  conf  fmt  kbps   dur     size      user               slots queue
    1  0.55  mp3  320    7:31+60 9.0MB     peer9              yes   0
    2  0.50  mp3  -      ?       7.0MB     peer3              no    14
Choice [number=queue, s=skip, r=re-search, q=quit, ?=help]:
```

(In the `kbps` column, `*` means the bitrate was estimated from file size,
and `!` marks files below your `--min-bitrate` — pickable here, never
downloaded automatically.)

- a **number** queues that candidate,
- **s** skips the track,
- **r** lets you type a custom search query (e.g. fix a weird spelling) and
  re-searches live,
- **q** quits; everything is saved, `resolve` continues where you left off.

### Checking progress later

```bash
.venv/bin/python -m spotify_nicotine status "https://open.spotify.com/playlist/YOUR_PLAYLIST_ID"
```

---

## Options reference

All options go after the subcommand. Defaults in parentheses.

| Flag | Meaning |
| --- | --- |
| `--formats mp3,flac` | Accepted file formats in order of preference (`mp3,m4a,flac,wav,aiff`). Among equally-confident matches, bitrate is compared first and format order breaks ties — see "How matching works". |
| `--prefer-bitrate 320` | Target bitrate in kbps (`320`). Files at or above it (including lossless) count as full marks; there is no extra credit beyond it. |
| `--min-bitrate 192` | Bitrate floor (`192`, `0` = off). Files below it are never auto-downloaded; if a track's only confident match is below the floor, you get a notice and can accept it manually in `resolve`. |
| `--min-confidence 0.65` | Match-confidence gate for automatic download (`0.65`). Lower = more automatic downloads, more risk of wrong files. |
| `--search-timeout 20` | Hard cap on waiting for one search's results (`20`). Most searches conclude much sooner — as soon as results stop arriving. |
| `--search-delay 2.0` | Minimum seconds between search dispatches (`2.0`, floor `0.5`). This is the Soulseek-server politeness limit; lowering it speeds things up but risks a temporary search ban. |
| `--search-concurrency 6` | How many track searches run at once (`6`, max `15`). Searches overlap their waiting time; the dispatch rate above still applies globally. |
| `--max-fallbacks 3` | If a download dies permanently (cancelled, filtered, disk error), how many alternative candidates to try (`3`). |
| `--stall-retry-mins 5` | How long a download may sit stalled (stuck queue, connection dropped, uploader offline) before it gets nudged — the equivalent of clicking *Retry* in Nicotine+ (`5`). |
| `--max-retries 3` | Nudges per uploader before giving up on them and falling back to the next candidate (`3`, `0` = never nudge or drop). A remote queue that is still *moving* doesn't consume nudges. |
| `--monitor-mins 10` | After queueing, watch transfers for up to this many minutes (`10`, `0` = don't wait). |
| `--limit N` | Only process the first N unfinished tracks (testing). |
| `--dest-dir PATH` | Ask Nicotine+ to save these files into a specific folder (e.g. one folder per playlist). |
| `--dry-run` | Search and score only; queue nothing. |
| `--state-dir ./state` | Where per-playlist progress files live (`./state`). |
| `--api-url URL` | Nicotine+ API address (`http://127.0.0.1:12339`). |
| `--api-token TOKEN` | API token, if you set one in the plugin settings. |
| `--auth-mode MODE` | Spotify auth: `user` (browser tokens), `client` (app-only tokens, old apps only), `auto` (default: cached user tokens if present, else app-only if a secret is configured). |
| `--token-cache PATH` | Where Spotify user tokens are cached (`./.spotify-tokens.json`). |
| `--config ./config.json` | JSON file with any of these settings as defaults — see `config.example.json`. |
| `--env-file ./.env` | Where to read `KEY=VALUE` secrets from (`./.env`). |

The `auth` subcommand additionally takes `--redirect-uri` (must exactly match
a Redirect URI in your Spotify app settings; default
`http://127.0.0.1:8080/callback`).

Precedence: command-line flag > environment variable / `.env` > `config.json` > built-in default.

### Exit codes

`0` all processed · `2` tracks awaiting review · `3` some tracks failed ·
`4` configuration/connection error · `130` interrupted (state saved).

## How matching works (short version)

Every search result gets two scores:

- **Confidence** — is this the right recording? Fuzzy title match (with
  noise like *remastered/deluxe* ignored on both sides), artist match against
  the file's name and folder, and duration compared to Spotify's duration.
  Files that are a *different* recording (live, remix, karaoke, cover,
  instrumental...) are penalized unless the Spotify track itself is that
  kind. A file with **no trace of the artist anywhere in its path** is also
  penalized — that's the classic same-title/wrong-artist trap — unless other
  evidence (matching album folder, exact duration) backs it up.
- **Ranking** among candidates:
  1. **confidence tier first**: an *excellent* match (≥ 0.9) always beats a
     merely-eligible one, whatever the formats involved — downloading the
     wrong song is worse than no song;
  2. then **achieved bitrate**, capped at `--prefer-bitrate` (a 320kbps m4a
     beats a 256kbps mp3; genuine lossless counts as full marks, so a 320
     mp3 and a FLAC tie here),
  3. then **format order** from `--formats` (that tie goes to the mp3),
  4. then uploader availability (free slot, queue length, speed).

Fallback searches (e.g. title-only, without the artist) only run when a
search produced **no** auto-queueable match — never to "top up" an already
good result, since broader queries are exactly where wrong-artist files come
from. Searches conclude as soon as an excellent match is in hand (typically
4–5 seconds) rather than waiting out the full result stream.

Only candidates whose *confidence* clears `--min-confidence` **and** whose
bitrate meets `--min-bitrate` are auto-queued; the ranking above picks the
winner. Everything else goes to the review picker — including
below-`--min-bitrate` matches, which are flagged with `!` in the picker table
and announced during the run ("match found only below the minimum bitrate")
so you can accept or reject them deliberately.

## Troubleshooting

- **"Nicotine+ API ... is not responding"** — Nicotine+ isn't running, or the
  plugin isn't enabled (Part 4), or it listens on a different port
  (`--api-url`).
- **"running but not connected"** — Nicotine+ hasn't finished logging in to
  the Soulseek server; give it a few seconds.
- **Spotify 403 Forbidden (no browser auth done yet)** — you're on app-only
  (Client Credentials) auth, which Spotify blocks for playlist reads on
  developer apps created since 2025. Run `auth` once (Part 5, step 5); the
  download command also offers to do this for you when it hits a 403.
- **Spotify 403 Forbidden *after* successful browser auth** — the error
  includes Spotify's reason when it provides one; the bare `'forbidden'`
  variants come from the February 2026 API restrictions:
  - *Playlist contents*: Development Mode apps only get contents of
    playlists the authorized account **owns or collaborates on**. Ask the
    owner to add you as a collaborator, or recreate the playlist in your
    account. The tool's error says this when both content endpoints refuse.
  - *Premium lapsed*: the app owner must have an active Premium
    subscription; the app stops working without it.
  - *"User not registered in the Developer Dashboard"*: only the app-owner
    account and accounts added under **Settings → User Management** may use
    the app. Run `auth` again and check the `Authorized as:` line (the
    browser silently uses whoever is signed in at accounts.spotify.com), or
    add the account's name and email in User Management.
- **Spotify 404** — wrong URL/id, a playlist the authorized account can't
  see, or a Spotify-owned editorial playlist (not accessible to new
  developer apps).
- **"INVALID_CLIENT: Invalid redirect URI" in the browser** — the Redirect
  URI in your Spotify app settings doesn't exactly match
  `http://127.0.0.1:8080/callback` (or whatever you set via
  `--redirect-uri`). Fix it in the app settings and run `auth` again.
- **Port 8080 already in use during `auth`** — register a different redirect
  URI (e.g. `http://127.0.0.1:8181/callback`) in the app settings and pass it
  with `--redirect-uri`.
- **`NotOpenSSLWarning` from urllib3** — harmless on macOS system Python;
  everything works.
- **Searches feel slow** — the dispatch rate (`--search-delay`) is the
  deliberate bottleneck; the Soulseek server temporarily blocks clients that
  fire searches rapidly. You can try `--search-delay 1` for double the rate.
- **Every search suddenly returns zero results** — that's what a Soulseek
  server search-throttle looks like. Stop the run, wait a few minutes, and
  restart with a higher `--search-delay` and/or lower `--search-concurrency`
  (resume picks up where it left off).
- **A download sits at "Queued" or "User logged off"** — connection blips
  and remote queues are handled patiently, because falling back to another
  uploader immediately can end in *two* copies of the file (Nicotine+
  auto-retries these itself, and the API has no cancel). The script nudges a
  stalled download every `--stall-retry-mins` (like clicking *Retry*), and
  only after `--max-retries` fruitless nudges drops that uploader and moves
  to the next candidate. When it does drop one, it warns you: the abandoned
  transfer stays in Nicotine+'s list and may still finish later — remove it
  there if you don't want a second copy. Only permanently-dead transfers
  (cancelled, filtered, disk errors) trigger an immediate fallback.

## For developers

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The test suite (227 tests) runs fully offline. `scripts/smoke.sh` is a live
end-to-end checklist to run on the machine where Nicotine+ is installed.
State files are plain JSON in `./state/` — safe to inspect, and deleting one
makes the tool treat that playlist as brand new.
