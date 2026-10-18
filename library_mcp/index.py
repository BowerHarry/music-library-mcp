"""An in-memory snapshot of the library (one bulk read from Music) for searching and health checks."""
from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter, defaultdict

LOSSLESS_KINDS = ("lossless", "aiff", "wav", "flac")
AUDIO_EXTS = {".m4a", ".mp3", ".aac", ".aif", ".aiff", ".wav", ".m4p", ".alac", ".flac"}
MEDIA_MARK = "/Media.localized/Music/"
HEALTHY_CLOUD = {"matched", "uploaded", "purchased", "subscription", "downloaded"}
EDITABLE = ("name", "artist", "album", "albumArtist", "genre", "year", "trackNumber", "trackCount",
            "discNumber", "discCount", "compilation", "composer", "comment", "grouping", "sortName",
            "sortArtist", "sortAlbum", "sortAlbumArtist", "rating", "favorited")
INT_FIELDS = {"year", "trackNumber", "trackCount", "discNumber", "discCount", "rating"}
BOOL_FIELDS = {"compilation", "favorited"}
# Fields every track of one album should agree on.
ALBUM_FIELDS = ("albumArtist", "year", "genre", "compilation", "discCount")


def norm(s) -> str:
    """Lowercase, accents and punctuation dropped: "Bad Idea Right?" -> "bad idea right". Names that
    are nothing but symbols (Ed Sheeran's "+", "÷", "×") keep their symbols, or they'd all match."""
    raw = str(s or "")
    s = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode().lower()
    out = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return out or unicodedata.normalize("NFC", raw).strip().lower()


def fmt_duration(sec) -> str:
    if not sec:
        return "?"
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def is_lossless(row: dict) -> bool:
    kind = (row.get("kind") or "").lower()
    return any(k in kind for k in LOSSLESS_KINDS)


def compact(r: dict) -> dict:
    """One track in a short form suitable for listing."""
    out = {"id": r["id"], "name": r.get("name"), "artist": r.get("artist"), "album": r.get("album")}
    if r.get("albumArtist") and r.get("albumArtist") != r.get("artist"):
        out["albumArtist"] = r["albumArtist"]
    if r.get("discCount", 0) and r.get("discCount", 0) > 1:
        out["track"] = f"{r.get('discNumber') or '?'}-{r.get('trackNumber') or '?'}"
    elif r.get("trackNumber"):
        out["track"] = r["trackNumber"]
    for k in ("genre", "year"):
        if r.get(k):
            out[k] = r[k]
    out["time"] = fmt_duration(r.get("duration"))
    out["format"] = short_kind(r)
    out["cloud"] = r.get("cloudStatus")
    return out


def short_kind(r: dict) -> str:
    kind = (r.get("kind") or "").replace(" audio file", "")
    if r.get("bitRate") and not is_lossless(r):
        return f"{kind} {r['bitRate']}k"
    return kind or "?"


class Library:
    def __init__(self, dump: dict):
        cols = dump["cols"]
        n = dump["count"]
        keys = [k for k, v in cols.items() if v is not None]
        self.rows = [{k: cols[k][i] for k in keys} for i in range(n)]
        self.by_id = {r["id"]: r for r in self.rows}
        self.has_locations = cols.get("location") is not None
        for r in self.rows:
            r["_text"] = " ".join(norm(r.get(k)) for k in ("name", "artist", "album", "albumArtist", "genre"))

    # ------------------------------------------------------------ lookups
    def search(self, query: str = "", artist: str = "", album: str = "", genre: str = "",
               missing: str = "", limit: int = 50) -> list[dict]:
        words = norm(query).split()
        out = []
        for r in self.rows:
            if words and not all(w in r["_text"] for w in words):
                continue
            if artist and norm(artist) not in norm(r.get("artist")) + " " + norm(r.get("albumArtist")):
                continue
            if album and norm(album) not in norm(r.get("album")):
                continue
            if genre and norm(genre) != norm(r.get("genre")):
                continue
            if missing and r.get(missing) not in (None, "", 0):
                continue
            out.append(r)
        out.sort(key=self._album_order)
        return out[:limit]

    @staticmethod
    def _album_order(r: dict):
        return (norm(r.get("albumArtist") or r.get("artist")), norm(r.get("album")),
                r.get("discNumber") or 0, r.get("trackNumber") or 0, norm(r.get("name")))

    def album_tracks(self, album: str, artist: str = "") -> list[dict]:
        """Tracks whose album name matches exactly (ignoring case/punctuation), optionally by artist."""
        a, ar = norm(album), norm(artist)
        hits = [r for r in self.rows if norm(r.get("album")) == a
                and (not ar or ar in norm(r.get("albumArtist")) or ar in norm(r.get("artist")))]
        return sorted(hits, key=self._album_order)

    def albums_matching(self, album: str, artist: str = "", limit: int = 10) -> list[dict]:
        """Album names containing the words, for when an exact match fails."""
        words, ar = norm(album).split(), norm(artist)
        groups: dict[tuple, int] = Counter()
        for r in self.rows:
            if all(w in norm(r.get("album")) for w in words) and \
                    (not ar or ar in norm(r.get("albumArtist")) + " " + norm(r.get("artist"))):
                groups[(r.get("album"), r.get("albumArtist") or r.get("artist"))] += 1
        return [{"album": a, "artist": b, "tracks": n} for (a, b), n in groups.most_common(limit)]

    # ------------------------------------------------------------ consistency
    @staticmethod
    def album_inconsistencies(tracks: list[dict]) -> dict:
        """Fields with more than one value across an album's tracks, with the most common value."""
        out = {}
        for f in ALBUM_FIELDS:
            vals = Counter(t.get(f) for t in tracks)
            if len(vals) > 1:
                out[f] = {"values": {str(k): v for k, v in vals.items()}, "most_common": vals.most_common(1)[0][0]}
        per_disc = defaultdict(Counter)
        for t in tracks:
            per_disc[t.get("discNumber") or 1][t.get("trackCount")] += 1
        for disc, vals in per_disc.items():
            if len(vals) > 1:
                out[f"trackCount (disc {disc})"] = {"values": {str(k): v for k, v in vals.items()},
                                                   "most_common": vals.most_common(1)[0][0]}
        return out

    @staticmethod
    def suggested_fix(tracks: list[dict]) -> dict:
        """Changes that would make the album consistent: most common value for each album field."""
        fix = {}
        for f in ALBUM_FIELDS:
            vals = Counter(t.get(f) for t in tracks if t.get(f) not in (None, ""))
            if len({t.get(f) for t in tracks}) > 1 and vals:
                fix[f] = vals.most_common(1)[0][0]
        return fix

    # ------------------------------------------------------------ files
    def music_folder(self) -> str | None:
        """The Music media folder (…/Media.localized/Music), worked out from track locations."""
        for r in self.rows:
            loc = r.get("location") or ""
            i = loc.find(MEDIA_MARK)
            if i > 0:
                return loc[:i + len(MEDIA_MARK) - 1]
        return None

    def files_not_in_library(self, folder: str | None = None) -> list[str]:
        """Audio files inside the Music media folder that no library entry points at."""
        folder = folder or self.music_folder()
        if not folder or not os.path.isdir(folder) or not self.has_locations:
            return []
        known = {unicodedata.normalize("NFC", r["location"]) for r in self.rows if r.get("location")}
        out = []
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.startswith(".") or os.path.splitext(f)[1].lower() not in AUDIO_EXTS:
                    continue
                path = os.path.join(root, f)
                if unicodedata.normalize("NFC", path) not in known:
                    out.append(path)
        return sorted(out)

    # ------------------------------------------------------------ health check
    def issues(self, limit: int = 20) -> dict:
        albums: dict[tuple, list[dict]] = defaultdict(list)
        for r in self.rows:
            albums[(norm(r.get("album")), norm(r.get("albumArtist") or r.get("artist")))].append(r)

        def album_entry(tracks, **extra):
            t0 = tracks[0]
            return {"album": t0.get("album"), "artist": t0.get("albumArtist") or t0.get("artist"),
                    "tracks": len(tracks), **extra}

        missing_genre = [album_entry(ts, without_genre=sum(1 for t in ts if not t.get("genre")))
                         for ts in albums.values() if any(not t.get("genre") for t in ts)]

        inconsistent = []
        for ts in albums.values():
            bad = self.album_inconsistencies(ts) if len(ts) > 1 else {}
            if bad:
                inconsistent.append(album_entry(ts, fields=sorted(bad)))

        # The same album name split across album artists / compilation flags (shows as two albums).
        by_name: dict[str, list[tuple]] = defaultdict(list)
        for key in albums:
            if key[0]:
                by_name[key[0]].append(key)
        split = []
        for name, keys in by_name.items():
            if len(keys) < 2:
                continue
            artists = [{norm(t.get("artist")) for t in albums[k]} for k in keys]
            shared = set.intersection(*artists) if artists else set()
            comp = {bool(t.get("compilation")) for k in keys for t in albums[k]}
            if shared or len(comp) > 1:
                split.append({"album": albums[keys[0]][0].get("album"),
                              "parts": [album_entry(albums[k]) for k in keys]})

        dupes: dict[tuple, list[dict]] = defaultdict(list)
        for r in self.rows:
            dupes[(norm(r.get("name")), norm(r.get("artist")), norm(r.get("album")))].append(r)
        duplicates = [{"name": ts[0].get("name"), "artist": ts[0].get("artist"), "album": ts[0].get("album"),
                       "ids": [t["id"] for t in ts], "formats": [short_kind(t) for t in ts]}
                      for ts in dupes.values() if len(ts) > 1 and
                      max(t.get("duration") or 0 for t in ts) - min(t.get("duration") or 0 for t in ts) < 3]

        cloud = Counter(r.get("cloudStatus") for r in self.rows)
        cloud_problems = [compact(r) for r in self.rows if r.get("cloudStatus") not in HEALTHY_CLOUD]

        lossy_albums = [album_entry(ts, lossy=sum(1 for t in ts if not is_lossless(t)))
                        for ts in albums.values() if any(not is_lossless(t) for t in ts)]
        lossy_albums.sort(key=lambda a: -a["lossy"])

        missing_files, no_file, not_in_library = [], [], []
        if self.has_locations:
            missing_files = [compact(r) for r in self.rows if r.get("location") and not os.path.exists(r["location"])]
            no_file = [compact(r) for r in self.rows if r.get("hasFile") is False]
            not_in_library = self.files_not_in_library()

        def section(items, note=""):
            d = {"count": len(items), "items": items[:limit]}
            if note:
                d["note"] = note
            return d

        return {
            "tracks": len(self.rows), "albums": len(albums),
            "icloud_status_counts": dict(cloud),
            "missing_genre": section(missing_genre, "fill_genres can look these up on MusicBrainz"),
            "split_albums": section(split, "same album name under different album artists or "
                                           "compilation flags, so it shows as more than one album"),
            "inconsistent_albums": section(inconsistent, "tracks disagree on album-level fields; "
                                                         "get_album shows a suggested fix"),
            "duplicates": section(duplicates),
            "icloud_problems": section(cloud_problems, "statuses other than matched/uploaded/purchased; "
                                                       "rematch_tracks often fixes these"),
            "missing_files": section(missing_files, "library entries whose file has gone")
            if self.has_locations else {"note": "file locations unavailable"},
            "entries_without_file": section(no_file, "entries with no local file on this Mac: cloud-only "
                                                     "purchases, or ghosts left when a file was replaced. "
                                                     "Ghosts can be deleted (no file is touched) and the "
                                                     "real file added with add_files"),
            "files_not_in_library": section(not_in_library, "audio files in the Music folder that no "
                                                            "library entry uses; add_files can add them"),
            "lossy_albums": section(lossy_albums, "albums with AAC/MP3 tracks that could be "
                                                  "replaced with lossless copies"),
        }
