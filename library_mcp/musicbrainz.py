"""MusicBrainz genre lookups (WS/2 JSON), rate-limited to 1 request/second."""
from __future__ import annotations

import threading
import time
import urllib.parse

from . import http
from .config import CONFIG

BASE = "https://musicbrainz.org/ws/2"
_lock = threading.Lock()
_last = 0.0
VARIOUS = ("various artists", "various")

def _get(path: str, **params) -> dict:
    """GET with MusicBrainz's 1 req/s limit; 503 means "slow down", so back off and retry."""
    global _last
    params["fmt"] = "json"
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(5):
        with _lock:
            wait = 1.1 - (time.monotonic() - _last)
            if wait > 0:
                time.sleep(wait)
            _last = time.monotonic()
        try:
            return http.request("GET", url, headers={"User-Agent": CONFIG.mb_user_agent})
        except http.HttpError as e:
            if e.status not in (429, 502, 503, 504) or attempt == 4:
                raise
            time.sleep(2 * 2 ** attempt)  # 2, 4, 8, 16 s
    raise RuntimeError("unreachable")


def _q(s: str) -> str:
    return '"' + s.replace("\\", " ").replace('"', " ") + '"'


def _artist(credit: list[dict]) -> str:
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in credit or [])


# MusicBrainz genres are fine-grained and lowercase ("bedroom pop", "pop rock"); Apple Music
# uses a short list of broad ones. First rule whose keyword appears in the name wins.
_APPLE_GENRES = [
    ("dance-pop", "Pop"), ("synth-pop", "Pop"), ("synthpop", "Pop"), ("electropop", "Pop"),
    ("pop rock", "Pop"), ("pop punk", "Alternative"), ("garage rock", "Rock"),
    ("christmas", "Holiday"), ("soundtrack", "Soundtrack"), ("film score", "Soundtrack"),
    ("musical", "Soundtrack"), ("k-pop", "K-Pop"), ("j-pop", "J-Pop"),
    ("hip hop", "Hip-Hop/Rap"), ("hip-hop", "Hip-Hop/Rap"), ("rap", "Hip-Hop/Rap"),
    ("trap", "Hip-Hop/Rap"), ("drill", "Hip-Hop/Rap"), ("grime", "Hip-Hop/Rap"),
    ("r&b", "R&B/Soul"), ("rhythm and blues", "R&B/Soul"), ("soul", "R&B/Soul"),
    ("funk", "R&B/Soul"), ("neo soul", "R&B/Soul"),
    ("house", "Dance"), ("techno", "Dance"), ("trance", "Dance"), ("disco", "Dance"),
    ("drum and bass", "Dance"), ("dance", "Dance"), ("edm", "Dance"), ("garage", "Dance"),
    ("electro", "Electronic"), ("ambient", "Electronic"),
    ("indie", "Alternative"), ("alternative", "Alternative"), ("emo", "Alternative"),
    ("grunge", "Alternative"), ("shoegaze", "Alternative"), ("post-punk", "Alternative"),
    ("punk", "Alternative"), ("metal", "Metal"), ("rock", "Rock"),
    ("singer-songwriter", "Singer/Songwriter"), ("folk", "Folk"), ("country", "Country"),
    ("americana", "Country"), ("bluegrass", "Country"), ("jazz", "Jazz"), ("blues", "Blues"),
    ("reggae", "Reggae"), ("dancehall", "Reggae"), ("latin", "Latin"), ("reggaeton", "Latin"),
    ("classical", "Classical"), ("opera", "Classical"), ("gospel", "Christian"),
    ("christian", "Christian"), ("afrobeat", "Afrobeats"), ("comedy", "Comedy"), ("pop", "Pop"),
]


def apple_genre(name: str) -> str:
    n = name.lower().strip()
    for key, genre in _APPLE_GENRES:
        if key in n:
            return genre
    return n.title()


def _top_genre(entity: dict) -> str:
    """Most-voted genre; ties keep MusicBrainz's order. Empty if nobody has tagged it."""
    gs = [g for g in entity.get("genres") or [] if g.get("name")]
    if not gs:
        return ""
    return apple_genre(max(gs, key=lambda g: g.get("count", 0))["name"])


def artist_genre(artist: str = "", artist_id: str = "") -> str:
    """Most-voted genre of an artist, found by name if no id is given."""
    if not artist_id:
        if not artist or artist.lower() in VARIOUS:
            return ""
        hits = _get("artist", query=f"artist:{_q(artist)}", limit=1).get("artists") or []
        if not hits or hits[0].get("score", 0) < 90:
            return ""
        artist_id = hits[0]["id"]
    return _top_genre(_get(f"artist/{artist_id}", inc="genres"))


def album_genre(album: str, artist: str) -> dict:
    """{"genre": "Pop", "source": "album"|"artist"|""} for an album in the library."""
    if not album.strip():
        g = artist_genre(artist)
        return {"genre": g, "source": "artist" if g else ""}
    q = f"releasegroup:{_q(album)}"
    if artist and artist.lower() not in VARIOUS:
        q += f" AND artist:{_q(artist)}"
    hits = _get("release-group", query=q, limit=3).get("release-groups") or []
    hit = next((h for h in hits if h.get("score", 0) >= 90), None)
    if hit:
        g = _top_genre(_get(f"release-group/{hit['id']}", inc="genres"))
        if g:
            return {"genre": g, "source": "album", "musicbrainz_id": hit["id"]}
        credit = hit.get("artist-credit") or []
        aid = (credit[0].get("artist") or {}).get("id") if credit else ""
        if aid and _artist(credit).lower() not in VARIOUS:
            g = artist_genre(artist_id=aid)
            if g:
                return {"genre": g, "source": "artist"}
    g = artist_genre(artist)
    return {"genre": g, "source": "artist" if g else ""}
