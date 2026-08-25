# libretro-artwork-api

A small self-hosted HTTP API serving box art / screenshots / title screens
from a local mirror of [libretro-thumbnails](https://github.com/libretro-thumbnails),
built for [mister_turing_client](../mister_turing_client) (a MiSTer FPGA
status HUD) but not specific to it - any client that can make an HTTP GET
can use this.

## Why this exists, and why self-hosted

libretro-thumbnails has no formal API (raw GitHub file access only) and no
explicit license for the image content - fine for RetroArch's own internal
use, murkier for building a public redistribution service on top of it.
Self-hosting a private mirror for personal use sidesteps that, and lets
this do real fuzzy matching against a local index instead of requiring the
caller to know the exact No-Intro-style filename (region tags, revision
tags, and all).

## Data layout

Clone whichever [libretro-thumbnails org repos](https://github.com/orgs/libretro-thumbnails/repositories)
you want into one directory, unmodified - this app reads their layout
directly:

```
THUMBS_DIR/
  Nintendo_-_Nintendo_Entertainment_System/
    Named_Boxarts/*.png
    Named_Snaps/*.png
    Named_Titles/*.png
    Named_Logos/*.png
  NEC_-_PC_Engine_-_TurboGrafx_16/
    ...
```

Adding a new system later is just `git clone`ing another repo into
`THUMBS_DIR` and restarting the container - no code change unless the
system also needs a new `SYSTEM_MAP` entry (see below).

## Running it

```
# directly
THUMBS_DIR=/path/to/libretro-thumbd PORT=8090 python3 app.py

# containerized (edit THUMBS_DIR in docker-compose.yml, or export it)
docker compose up --build -d
```

No dependencies beyond the Python 3 standard library - `difflib` for
fuzzy matching, `http.server` for the API itself, same minimal-footprint
approach the rest of this ecosystem (`mister_status_server.py`,
`mister_turing_client`) already uses.

**Not yet actually run in a container** - the dev sandbox this was built in
has no Docker daemon available, so `app.py`'s logic was verified directly
(`python3 app.py`, real queries against real cloned data - see below), but
`docker compose up --build` itself hasn't been exercised. The Dockerfile
is straightforward (stdlib-only, no build step beyond copying one file),
so this is low-risk, but worth an actual `docker compose up` smoke test
before relying on it.

## API

```
GET /artwork?system=<mister core_raw>&game=<title>&type=boxart|snap|title|logo
    200  image/png                     on a match (X-Match-Method: exact|fuzzy)
    400  {"error": "..."}              missing/bad query params
    404  {"error": "..."}              unmapped system, system not cloned locally, or no title match

GET /health
    200  {"status": "ok", "systems_loaded": [...]}   repo directories actually found under THUMBS_DIR
```

Matching: filenames are normalized by stripping the extension and every
`(region)`/`[hack-flag]`-style tag, lowercasing, and collapsing whitespace.
An incoming `game` query goes through the same normalization for an exact
lookup; on a miss, `difflib.get_close_matches` (cutoff `0.72`, tunable via
`FUZZY_CUTOFF`) finds the closest normalized title. When multiple files
share a normalized title (region variants, re-releases), the best one is
picked by region priority (USA > World > Europe > Japan > unknown) then by
shortest filename - verified against a real case in the data itself
(`Bonk 3 - Bonk's Big Adventure` has both a plain `(USA).png` and a
`(USA, Europe) (Wii U Virtual Console).png`; this correctly picks the
plain one).

## `SYSTEM_MAP`: MiSTer core name -> RetroArch playlist directory

This is the one piece of real, unsolved-elsewhere work here: MiSTer's own
system naming, RetroArch's playlist naming, and (separately)
ScreenScraper's numeric system IDs are three different schemes that don't
translate to each other automatically. `app.py`'s `SYSTEM_MAP` only
contains entries actually **confirmed** against the real
`libretro-thumbnails` org listing - deliberately not a large table of
pattern-guessed names, after this exact project got burned more than once
by confidently-guessed assumptions elsewhere in this session that turned
out wrong. Extend it by checking the real repo list
(`GET https://api.github.com/orgs/libretro-thumbnails/repos`) before
adding an entry, not by guessing the naming pattern.

A query for a system not in the table, or mapped but not yet cloned into
`THUMBS_DIR`, 404s cleanly rather than crashing - and logs exactly what
came in unmapped, so gaps are easy to spot from real usage instead of
guessed at up front.

## Tested against real data

With `NEC_-_PC_Engine_-_TurboGrafx_16` and `Nintendo_-_Nintendo_Entertainment_System`
(13,439 boxart files alone) actually cloned locally:

- Exact match: `system=tgfx16&game=Bonk 3 - Bonk's Big Adventure` -> the
  real 624,847-byte `(USA).png`, confirmed byte-identical to the file on
  disk, not the 151,468-byte Wii U Virtual Console variant.
- Exact match: `system=nes&game=Super Dodge Ball` -> real 297,460-byte PNG.
- Fuzzy match: `system=nes&game=Super Dodgeball` (missing the space) ->
  same 297,460-byte file as the exact match above.
- Unmapped system -> clean `404 {"error": "unmapped system: bogus"}`.
- `type=snap` -> a real, much smaller in-game-screenshot PNG, distinct
  from the boxart response for the same game.
