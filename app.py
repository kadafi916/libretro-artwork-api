#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""libretro-artwork-api - self-hosted local mirror of libretro-thumbnails,
serving box art / screenshots / title screens over a simple HTTP API.

Built for mister_turing_client (a MiSTer FPGA status HUD): the client
already caches whatever it fetches, so this only needs to answer "find me
this game's art" reasonably well and reasonably fast - not be a CDN.

Data layout expected under THUMBS_DIR (matches libretro-thumbnails' own
per-repo layout exactly - each subdirectory here is one of their repos,
cloned as-is, nothing renamed):

    THUMBS_DIR/
      Nintendo_-_Nintendo_Entertainment_System/
        Named_Boxarts/*.png
        Named_Snaps/*.png
        Named_Titles/*.png
        Named_Logos/*.png
      NEC_-_PC_Engine_-_TurboGrafx_16/
        ...

Usage:
    THUMBS_DIR=/data PORT=8478 python3 app.py

API:
    GET /artwork?system=<mister core_raw or system name>&game=<title>&type=boxart|snap|title|logo
        -> 200 image/png on match (X-Match-Method: exact|prefix|fuzzy), 404 {"error": ...} otherwise
    GET /health
        -> 200 {"status": "ok", "systems_loaded": [...]}
    GET /coverage
        -> 200 {"systems_loaded": [...], "mapped_not_cloned": [...],
                 "cloned_not_mapped": [...], "aliases_by_repo": {...},
                 "title_counts": {repo: {type_dir: N, ...}, ...}}
           SYSTEM_MAP/THUMBS_DIR gap report - see do_GET's comment.
    POST /reindex
        -> 200 {"status": "ok", "systems_loaded": [...], "titles_indexed": N, "elapsed_seconds": T}
           Rescans THUMBS_DIR - call this after `git clone`ing a new repo
           (or `git pull`ing an existing one) into it, no restart needed.
"""

import http.server
import json
import os
import re
import socketserver
import sys
import time
import urllib.parse
from difflib import SequenceMatcher, get_close_matches

THUMBS_DIR = os.environ.get("THUMBS_DIR", "/data")
PORT = int(os.environ.get("PORT", "8478"))
FUZZY_CUTOFF = float(os.environ.get("FUZZY_CUTOFF", "0.72"))

TYPE_DIRS = {
    "boxart": "Named_Boxarts",
    "snap": "Named_Snaps",
    "title": "Named_Titles",
    "logo": "Named_Logos",
}

# MiSTer core_raw (lowercased) -> RetroArch playlist repo directory name.
#
# Only the entries below have actually been confirmed against the real
# libretro-thumbnails org (GET https://api.github.com/orgs/libretro-thumbnails/repos) -
# this is intentionally a SMALL, verified table rather than a guessed-at
# full mapping, after this exact project got burned more than once this
# session by confidently-guessed assumptions that turned out wrong (the
# ScreenScraper JSON envelope shape, the RA_ CORENAME-prefix assumption).
# Extend it by checking the real repo list first, not by pattern-guessing
# the "Publisher_-_System" naming convention, which isn't reliably regular
# across all 131 repos.
#
# A system missing here (or present here but not yet `git clone`d into
# THUMBS_DIR) just 404s cleanly - see do_GET's logging, which names exactly
# what came in unmapped, so gaps are easy to spot from actual usage.
SYSTEM_MAP = {
    "nes": "Nintendo_-_Nintendo_Entertainment_System",
    "tgfx16": "NEC_-_PC_Engine_-_TurboGrafx_16",
    "pcengine": "NEC_-_PC_Engine_-_TurboGrafx_16",
    "turbografx16": "NEC_-_PC_Engine_-_TurboGrafx_16",
    "snes": "Nintendo_-_Super_Nintendo_Entertainment_System",
    "genesis": "Sega_-_Mega_Drive_-_Genesis",
    "megadrive": "Sega_-_Mega_Drive_-_Genesis",
    "md": "Sega_-_Mega_Drive_-_Genesis",
    "atarilynx": "Atari_-_Lynx",
    "atarilynx2p": "Atari_-_Lynx",
    "neogeo": "SNK_-_Neo_Geo",
    "neo-geo": "SNK_-_Neo_Geo",
    "saturn": "Sega_-_Saturn",
    "psx": "Sony_-_PlayStation",
    "playstation": "Sony_-_PlayStation",
    "fds": "Nintendo_-_Family_Computer_Disk_System",
    "gameboy": "Nintendo_-_Game_Boy",
    "gb": "Nintendo_-_Game_Boy",
    "gba": "Nintendo_-_Game_Boy_Advance",
    "gameboyadvance": "Nintendo_-_Game_Boy_Advance",
    "gba2p": "Nintendo_-_Game_Boy_Advance",
    "n64": "Nintendo_-_Nintendo_64",
    "nintendo64": "Nintendo_-_Nintendo_64",
    "gg": "Sega_-_Game_Gear",
    "mastersystem": "Sega_-_Master_System_-_Mark_III",
    "sms": "Sega_-_Master_System_-_Mark_III",
    "megacd": "Sega_-_Mega-CD_-_Sega_CD",
    "segacd": "Sega_-_Mega-CD_-_Sega_CD",
    "gbc": "Nintendo_-_Game_Boy_Color",
    "gameboycolor": "Nintendo_-_Game_Boy_Color",
    "s32x": "Sega_-_32X",
    "wonderswan": "Bandai_-_WonderSwan",
    "wonderswancolor": "Bandai_-_WonderSwan_Color",
    "3do": "The_3DO_Company_-_3DO",
    # Same unreachable-but-cloned situation as SNK_-_Neo_Geo_CD below:
    # NEC_-_PC_Engine_CD_-_TurboGrafx-CD (MiSTer's TGFX16 core plays both
    # cartridge and CD-ROM² games under the same "tgfx16" core_raw),
    # Nintendo_-_Nintendo_64DD, and Sega_-_SG-1000 (likely played via the
    # Master System core, sharing "mastersystem"/"sms" - already mapped to
    # the Master System repo, so leaving SG-1000 unmapped rather than
    # wrongly routing all SMS art requests to the smaller SG-1000 set) all
    # have no MiSTer core_raw of their own to route through - left
    # deliberately unmapped rather than guessed at.
    #
    # SNK_-_Neo_Geo_CD has no MiSTer core_raw of its own to map from: the
    # NeoGeo core reports core_raw "neogeo" (see above) for both cartridge
    # and CD games alike, so there's no signal here to route a CD title to
    # this repo instead - it stays cloned-but-unreachable via this API
    # until/unless mister_status_server exposes something that tells the
    # two apart (e.g. the loaded image's extension).
    #
    # Verified repo name, not yet cloned locally - safe to leave mapped; it
    # simply 404s with a clear "no local data" message until cloned.
    "arcade": "MAME",
    "mame": "MAME",
}

_TAG_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_REGION_PRIORITY = ["usa", "world", "europe", "japan"]

_index = {}          # (repo_dir, type_dir) -> {"by_title": {norm: filename}, "titles": [norm, ...]}
_systems_loaded = []  # repo_dirs actually found under THUMBS_DIR at startup


def normalize_title(name: str) -> str:
    """Strip the extension and every (region)/[hack-flag] style tag, collapse
    whitespace, lowercase. Used both to index real filenames and to match an
    incoming query title against them, so both sides go through this."""
    name = os.path.splitext(name)[0]
    name = _TAG_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip().lower()
    return name


def _differentiator_tokens(norm_title: str) -> set:
    """The short (single-character or purely-numeric) words in a normalized
    title - the tokens that usually distinguish entries in a series
    ("x" in "metal slug x", "3" in "bonk 3"). Deliberately narrow: only
    single characters and pure digits, not e.g. roman numerals (too easy to
    false-positive on ordinary short words - "mix", "ix" - to parse safely
    without real roman-numeral-aware tokenizing, which this doesn't attempt).

    Used to keep whole-string fuzzy matching (SequenceMatcher, via
    get_close_matches - good at absorbing spacing/typo variance, e.g. the
    documented "Super Dodgeball" -> "Super Dodge Ball" case) from also
    accepting a *different* entry in the same series just because most of
    the title matches. Verified real failure this was built for: a query
    for "Metal Slug X" fuzzy-matched an entry with no "x" anywhere in it at
    all - character-level similarity alone can't tell "same title, minor
    variance" apart from "different entry, mostly-shared name".

    Only single letters and pure digits count - "-" would otherwise also
    qualify (len 1) despite being a plain separator, not a differentiator,
    which would wrongly treat "Metal Slug - Super Vehicle-001" as *having*
    a differentiator ("-") and break the prefix-match tier below for the
    plain "Metal Slug" query."""
    return {w for w in norm_title.split() if w.isdigit() or (len(w) == 1 and w.isalpha())}


def _prefix_match(norm: str, titles):
    """A real box-art title is very often the query *plus a subtitle*
    libretro-thumbnails includes and a game's short title doesn't ("Metal
    Slug X" -> "Metal Slug X - Super Vehicle-001", "Neo Turf Masters" ->
    "Neo Turf Masters _ Big Tournament Golf") - both real, verified cases
    where get_close_matches' whole-string character ratio undershoots
    FUZZY_CUTOFF simply because the real title is so much longer than the
    query, even though the query matches its start exactly. Checked before
    the fuzzy fallback, and restricted to a clean word boundary (the
    character right after the shared prefix must be a space) so "super" a
    prefix match on any title that just happens to overlap.

    Differentiator-checked the same way as the fuzzy path: a query with no
    differentiator of its own must match a candidate that *also* has none
    (so plain "Metal Slug" doesn't grab "Metal Slug X"'s file merely
    because it makes a valid prefix too), and a query that does name one
    must find a candidate sharing it. Ties (multiple valid prefix matches)
    resolve to the shortest title - the closest, most minimal superset of
    the query."""
    query_diff = _differentiator_tokens(norm)

    def _diff_ok(t):
        cand_diff = _differentiator_tokens(t)
        return bool(query_diff & cand_diff) if query_diff else not cand_diff

    candidates = [t for t in titles if t == norm or t.startswith(norm + " ")]
    good = [t for t in candidates if _diff_ok(t)]
    return min(good, key=len) if good else None


def _candidate_rank(filename: str):
    """Lower is better. Region match first (USA > World > Europe > Japan >
    unknown), then shortest filename as a tiebreaker - prefers a plain
    original release ("Foo (USA).png") over a same-region special edition
    ("Foo (USA, Europe) (Wii U Virtual Console).png"), which is common
    enough in this data to be worth the explicit tiebreak (seen firsthand:
    Bonk 3 has exactly this pair for PC Engine)."""
    lower = filename.lower()
    for i, r in enumerate(_REGION_PRIORITY):
        if r in lower:
            return (i, len(filename))
    return (len(_REGION_PRIORITY), len(filename))


def build_index():
    """Scan THUMBS_DIR and replace the live index. Safe to call again at any
    time (see do_POST's /reindex) even while GETs are being served
    concurrently on other threads: the new index is built entirely in a
    local dict first, and only swapped into the global via _index_ready()
    once complete - a single reference reassignment, atomic under the GIL,
    so a concurrent reader always sees either the complete old index or the
    complete new one, never a partially-rebuilt one."""
    global _systems_loaded
    index = {}
    loaded = []
    if not os.path.isdir(THUMBS_DIR):
        print(f"[artwork-api] WARNING: THUMBS_DIR {THUMBS_DIR} does not exist", file=sys.stderr)
        _index_ready(index, loaded)
        return

    for repo_dir in sorted(os.listdir(THUMBS_DIR)):
        repo_path = os.path.join(THUMBS_DIR, repo_dir)
        if not os.path.isdir(repo_path):
            continue
        loaded.append(repo_dir)
        for media_type, type_dir in TYPE_DIRS.items():
            type_path = os.path.join(repo_path, type_dir)
            if not os.path.isdir(type_path):
                continue
            best = {}  # norm -> (filename, rank_tuple)
            for fname in os.listdir(type_path):
                if not fname.lower().endswith(".png"):
                    continue
                norm = normalize_title(fname)
                rank = _candidate_rank(fname)
                if norm not in best or rank < best[norm][1]:
                    best[norm] = (fname, rank)
            by_title = {k: v[0] for k, v in best.items()}
            index[(repo_dir, type_dir)] = {"by_title": by_title, "titles": list(by_title.keys())}
            print(f"[artwork-api] indexed {repo_dir}/{type_dir}: {len(by_title)} titles")

    _index_ready(index, loaded)


def _index_ready(index, loaded):
    global _index, _systems_loaded
    _index = index
    _systems_loaded = loaded


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "libretro-artwork-api/1.0"

    def log_message(self, fmt, *args):
        print(f"[artwork-api] {self.address_string()} - {fmt % args}")

    def _respond_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/health":
            self._respond_json(200, {"status": "ok", "systems_loaded": _systems_loaded})
            return

        if parsed.path == "/coverage":
            # Makes SYSTEM_MAP/THUMBS_DIR gaps visible directly instead of
            # discovered one 404 at a time - the two ways this setup drifts
            # out of sync: a repo cloned into THUMBS_DIR with no core_raw
            # alias pointing to it yet (cloned_not_mapped - needs a
            # SYSTEM_MAP entry, see app.py's own comments on which systems
            # can't have one at all), and a SYSTEM_MAP alias pointing at a
            # repo not actually cloned yet (mapped_not_cloned - needs a
            # `git clone` + /reindex, or is deliberately left mapped ahead
            # of time, like "arcade"/"mame" -> MAME).
            mapped_repos = set(SYSTEM_MAP.values())
            cloned_repos = set(_systems_loaded)
            aliases_by_repo = {}
            for alias, repo in SYSTEM_MAP.items():
                aliases_by_repo.setdefault(repo, []).append(alias)
            title_counts = {}
            for (repo_dir, type_dir), data in _index.items():
                title_counts.setdefault(repo_dir, {})[type_dir] = len(data["titles"])
            self._respond_json(200, {
                "systems_loaded": sorted(cloned_repos),
                "mapped_not_cloned": sorted(mapped_repos - cloned_repos),
                "cloned_not_mapped": sorted(cloned_repos - mapped_repos),
                "aliases_by_repo": {r: sorted(a) for r, a in aliases_by_repo.items()},
                "title_counts": title_counts,
            })
            return

        if parsed.path != "/artwork":
            self._respond_json(404, {"error": "not found"})
            return

        qs = urllib.parse.parse_qs(parsed.query)
        system = (qs.get("system") or [""])[0].strip().lower()
        game = (qs.get("game") or [""])[0].strip()
        media_type = (qs.get("type") or ["boxart"])[0].strip().lower()

        if not system or not game:
            self._respond_json(400, {"error": "system and game query params are required"})
            return

        type_dir = TYPE_DIRS.get(media_type)
        if not type_dir:
            self._respond_json(400, {"error": f"unknown type '{media_type}', use one of {list(TYPE_DIRS)}"})
            return

        repo_dir = SYSTEM_MAP.get(system)
        if not repo_dir:
            print(f"[artwork-api] unmapped system: '{system}' (add it to SYSTEM_MAP in app.py)")
            self._respond_json(404, {"error": f"unmapped system: {system}"})
            return

        entry = _index.get((repo_dir, type_dir))
        if entry is None:
            self._respond_json(
                404,
                {"error": f"no local data for {repo_dir}/{type_dir} - clone that repo "
                          f"into {THUMBS_DIR} and POST /reindex"},
            )
            return

        norm = normalize_title(game)
        filename = entry["by_title"].get(norm)
        match_method = "exact"
        rejected_series_conflict = []

        if filename is None:
            prefix_hit = _prefix_match(norm, entry["titles"])
            if prefix_hit is not None:
                filename = entry["by_title"][prefix_hit]
                match_method = "prefix"

        if filename is None:
            query_diff = _differentiator_tokens(norm)
            for cand in get_close_matches(norm, entry["titles"], n=5, cutoff=FUZZY_CUTOFF):
                if query_diff and not (query_diff & _differentiator_tokens(cand)):
                    # The query names a specific series entry ("x", "3", ...)
                    # this candidate doesn't share anywhere - likely a
                    # different entry in the same series, not a fuzzy
                    # variant of this one. Try the next-best candidate
                    # instead of confidently serving the wrong game's art.
                    rejected_series_conflict.append(cand)
                    continue
                filename = entry["by_title"][cand]
                match_method = "fuzzy"
                break

        if filename is None:
            if rejected_series_conflict:
                # Distinct from a plain no-match: candidates existed and
                # cleared FUZZY_CUTOFF, but every one of them named a
                # different series entry than the query did - see
                # _differentiator_tokens(). Worth telling apart from "index
                # is empty" / "nothing was even close", since this case
                # means the *right* title probably isn't indexed at all
                # (not that the query or the cutoff needs tuning).
                print(f"[artwork-api] no match: {system}/{media_type} '{game}' - "
                      f"{len(rejected_series_conflict)} candidate(s) cleared the cutoff but named a "
                      f"different series entry: {rejected_series_conflict}")
            else:
                # Log the closest title regardless of cutoff (not just
                # whether one cleared FUZZY_CUTOFF) - previously a no-match
                # here left no trace at all, making it impossible to tell
                # "the query title was wildly different from anything on
                # file" (e.g. a raw MAME short-name reaching this instead of
                # a real title) apart from "it was close but just missed
                # the cutoff".
                closest = get_close_matches(norm, entry["titles"], n=1, cutoff=0.0)
                if closest:
                    ratio = SequenceMatcher(None, norm, closest[0]).ratio()
                    print(f"[artwork-api] no match: {system}/{media_type} '{game}' - "
                          f"closest on file: '{closest[0]}' (ratio {ratio:.2f}, cutoff {FUZZY_CUTOFF})")
                else:
                    print(f"[artwork-api] no match: {system}/{media_type} '{game}' - index is empty")
            self._respond_json(404, {"error": "no match", "system": repo_dir, "game": game})
            return

        path = os.path.join(THUMBS_DIR, repo_dir, type_dir, filename)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self._respond_json(404, {"error": "matched file missing on disk", "path": path})
            return

        print(f"[artwork-api] {system}/{media_type} '{game}' -> '{filename}' ({match_method})")
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Match-Method", match_method)
        # Lets a caller (or a curl -I from wherever this API isn't
        # directly SSH-reachable from) see what actually matched without
        # needing the server's own stdout log - the difference between "the
        # right game, wrong media" and "the wrong game entirely" isn't
        # visible from the image bytes alone.
        self.send_header("X-Match-Filename", urllib.parse.quote(filename))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path != "/reindex":
            self._respond_json(404, {"error": "not found"})
            return

        # Same build_index() the startup path uses, run again on demand -
        # see build_index()'s docstring for why this is safe against
        # concurrent GETs (a full replacement dict, swapped in atomically,
        # never mutated in place).
        started = time.time()
        build_index()
        elapsed = time.time() - started
        titles = sum(len(v["titles"]) for v in _index.values())
        print(f"[artwork-api] reindexed: {len(_systems_loaded)} systems, "
              f"{titles} titles, {elapsed:.2f}s")
        self._respond_json(200, {
            "status": "ok",
            "systems_loaded": _systems_loaded,
            "titles_indexed": titles,
            "elapsed_seconds": round(elapsed, 2),
        })


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    build_index()
    if not _systems_loaded:
        print(f"[artwork-api] WARNING: no repos found under {THUMBS_DIR} - "
              f"every request will 404 until at least one is cloned there.")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[artwork-api] listening on 0.0.0.0:{PORT}, THUMBS_DIR={THUMBS_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
