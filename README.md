# libretro-artwork-api

A small self-hosted HTTP API serving box art / screenshots / title screens
from a local mirror of [libretro-thumbnails](https://github.com/libretro-thumbnails),
built for a MiSTer FPGA status HUD but not specific to it - any client
that can make an HTTP GET can use this.

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
`THUMBS_DIR` and calling `POST /reindex` (see API below) - no restart, no
code change unless the system also needs a new `SYSTEM_MAP` entry (see
below).

## Running it

```
# directly
THUMBS_DIR=/path/to/libretro-thumbd PORT=8478 python3 app.py

# containerized - defaults to ./thumbs alongside docker-compose.yml;
# override with a THUMBS_DIR env var or a .env file to point elsewhere
docker compose up --build -d
```

No dependencies beyond the Python 3 standard library - `difflib` for
fuzzy matching, `http.server` for the API itself, same minimal-footprint
approach the rest of this ecosystem (`mister_status_server.py`,
`mister_turing_client`) already uses.

Deployed and confirmed running in production via `docker compose` on a
Synology NAS (not the sandbox this was developed in, which has no Docker
daemon - `app.py`'s logic was verified directly there via `python3 app.py`
against real cloned data first, then the containerized deploy separately
confirmed with a real `/health` check and a real `/artwork` fetch over the
network).

## API

```
GET /artwork?system=<mister core_raw>&game=<title>&type=boxart|snap|title|logo
    200  image/png                     on a match (X-Match-Method: exact|fuzzy)
    400  {"error": "..."}              missing/bad query params
    404  {"error": "..."}              unmapped system, system not cloned locally, or no title match

GET /health
    200  {"status": "ok", "systems_loaded": [...]}   repo directories actually found under THUMBS_DIR

GET /coverage
    200  {"systems_loaded": [...], "mapped_not_cloned": [...],
           "cloned_not_mapped": [...], "aliases_by_repo": {repo: [alias, ...]},
           "title_counts": {repo: {type_dir: N, ...}, ...},
           "dropped_unresolvable_count": N, "dropped_unresolvable_sample": [...]}
         SYSTEM_MAP/THUMBS_DIR gap report: mapped_not_cloned is a SYSTEM_MAP
         alias pointing at a repo not actually cloned yet; cloned_not_mapped
         is a repo cloned with no core_raw alias pointing to it at all - see
         "SYSTEM_MAP" below for why some repos can never have one.
         dropped_unresolvable_* - see "Non-image placeholder entries" below.

GET /random?system=<optional>&type=boxart|snap|title|logo|<comma-list>|random
    200  image/png  (X-Type/X-System/X-Filename: what got picked)
    400  {"error": "..."}              unknown type(s)
    404  {"error": "..."}              unmapped system, or nothing indexed for that system/type
         Picks a real, already-indexed file at random - no title lookup
         involved. Built for a slideshow-style client that just wants
         "any game art", not a specific game's (see the sibling
         random-art-display project). Omit `system` to draw from every
         indexed repo. `type` defaults to boxart, same as `/artwork`,
         but also accepts a comma-separated list ("boxart,snap") to pick
         randomly among just those, or the literal "random" to pick
         among all four - X-Type in the response says which one actually
         got picked, since in list/random mode the caller doesn't
         already know.

POST /reindex
    200  {"status": "ok", "systems_loaded": [...], "titles_indexed": N, "elapsed_seconds": T}
         Rescans THUMBS_DIR from scratch - call this after git clone-ing a
         new repo (or git pull-ing an existing one) into it. Safe to call
         while the server is handling other requests: the index is rebuilt
         into a fresh dict and swapped in atomically, so a concurrent GET
         never sees a partially-rebuilt index.
```

Matching: filenames are normalized by stripping the extension and every
`(region)`/`[hack-flag]`-style tag, lowercasing, and collapsing whitespace.
An incoming `game` query goes through the same normalization, then three
tiers are tried in order:

1. **Exact** - the normalized query equals a normalized title exactly.
2. **Prefix** - the query is a clean word-boundary prefix of a title
   (candidate == query, or `candidate.startswith(query + " ")`). Real box
   art titles very often carry a subtitle a game's short title doesn't
   ("Metal Slug X" is really indexed as "Metal Slug X - Super
   Vehicle-001 (NGM-2500)(NGH-2500)", "Neo Turf Masters" as "Neo Turf
   Masters _ Big Tournament Golf") - both real, verified cases where tier
   3's whole-string ratio undershoots the cutoff purely because the real
   title is so much longer, even though the start matches exactly.
3. **Fuzzy** - `difflib.get_close_matches` (cutoff `0.72`, tunable via
   `FUZZY_CUTOFF`, checking the 5 best candidates) finds the closest
   normalized title - for spacing/typo variance tier 2 doesn't catch
   (the documented "Super Dodgeball" -> "Super Dodge Ball" case).

Tiers 2 and 3 both apply the same guard: a candidate is only accepted if
its short "differentiator" tokens (single characters or pure digits, e.g.
the "x" in "metal slug x", the "3" in "bonk 3") don't conflict with the
query's - and when the query has none, the candidate can't have one
either (so plain "Metal Slug" correctly gets the base game's file via
tier 2, not "Metal Slug X"'s, even though both are valid prefixes).
Character-level/prefix similarity alone can't tell "same title, minor
variance" apart from "different entry in the same series, mostly-shared
name" - a real, verified failure mode ("Metal Slug X" fuzzy-matched an
unrelated entry with no "x" anywhere in it) this closes: a query naming a
specific entry 404s rather than confidently serving a different game's
box art when the right one isn't indexed. See `_differentiator_tokens()`
and `_prefix_match()` in `app.py`.

When multiple files share a normalized title (region variants,
re-releases), the best one is picked by region priority (USA > World >
Europe > Japan > unknown) then by shortest filename - verified against a
real case in the data itself (`Bonk 3 - Bonk's Big Adventure` has both a
plain `(USA).png` and a `(USA, Europe) (Wii U Virtual Console).png`; this
correctly picks the plain one).

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

## Non-image placeholder entries

A small fraction of files in these thumbnail repos aren't images at all.
Found in the field: `Atari_-_Lynx/Named_Boxarts/ZZZ-UNK-Jimmy Conners'
Tennis (UE).png` is a genuine, intentionally-committed **regular file**
(not a symlink) whose entire content is the text `Jimmy Connors' Tennis
(USA, Europe).png` - a manual "the real file is over there" pointer
some contributors use for placeholder/unknown-variant entries, rather
than a git symlink. Serving it as-is (as `/artwork` originally did, and
as `/random` would have) means `Content-Type: image/png` with a couple
dozen bytes of plain text and no error anywhere - a client only finds
out when it tries to decode the "image" and fails.

**Ruled out, so the next person doesn't re-chase it:** this looks exactly
like the well-known "git checked out a symlink as a text file containing
its target" failure mode (happens when `core.symlinks` is `false` for a
clone), and that was the first theory here too. It's wrong for this
data: `core.symlinks` was unset (not `false`) on every one of the 25
cloned repos, this filesystem verifiably creates real symlinks fine
(tested directly with `ln -s`), and forcing `core.symlinks=true` +
`git checkout-index -f -a` across all 25 repos changed nothing - the
real symlink counts were identical before and after. These files were
never symlinks; they're plain files, by design, in the upstream data.

**The actual fix** (`_resolve_real_filename()` in `app.py`, run once per
winning candidate at index time, not per-request): check the file for
the PNG magic header; if absent, try its own content as a sibling
filename in the same directory (exactly the convention above), and use
*that* file's bytes instead. An entry that resolves neither way is
dropped from the index entirely rather than ever served. Visible via
`/coverage`'s `dropped_unresolvable_count`/`_sample` without needing
filesystem access.

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
- All 25 repos actually cloned (111,519 candidate entries): 329 (~0.3%)
  were non-image placeholder files - see "Non-image placeholder entries"
  above. All 329 dropped from the index rather than ever served.
