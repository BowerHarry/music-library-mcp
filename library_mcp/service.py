"""Library operations behind the MCP tools. Reads come from a cached snapshot; writes go to Music
one at a time through the bridge and are recorded so they can be undone."""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from . import musicbrainz
from .bridge import Bridge
from .config import CONFIG
from .index import BOOL_FIELDS, EDITABLE, INT_FIELDS, Library, compact, norm, short_kind
from .store import Store

log = logging.getLogger("library_mcp")

MAX_DELETE = 100
MAX_REMATCH = 30


class UserError(Exception):
    """A problem with the request, reported back to Claude as {"error": ...}."""


class Service:
    def __init__(self, store: Store | None = None, bridge: Bridge | None = None):
        self.store = store or Store(CONFIG.data_dir / "library.db")
        self.bridge = bridge or Bridge()
        self._lib: Library | None = None
        self._lib_at = 0.0
        self._lock = threading.RLock()

    # ------------------------------------------------------------ snapshot
    def lib(self, refresh: bool = False) -> Library:
        with self._lock:
            if refresh or self._lib is None or time.time() - self._lib_at > CONFIG.index_ttl:
                self._lib = Library(self.bridge.call("dump"))
                self._lib_at = time.time()
            return self._lib

    def _dirty(self) -> None:
        with self._lock:
            self._lib = None

    def _track(self, tid: str) -> dict:
        r = self.lib().by_id.get(tid)
        if r is None:
            r = self.lib(refresh=True).by_id.get(tid)
        if r is None:
            raise UserError(f"no track with id {tid} (ids change when a track is re-added; search again)")
        return r

    # ------------------------------------------------------------ read
    def status(self) -> dict:
        info = self.bridge.call("ping", timeout=30)
        lib = self.lib(refresh=True)
        return {**info, "albums": len({(norm(r.get("album")), norm(r.get("albumArtist") or r.get("artist")))
                                       for r in lib.rows})}

    def search_tracks(self, query: str = "", artist: str = "", album: str = "", genre: str = "",
                      missing: str = "", limit: int = 50) -> dict:
        if missing and missing not in EDITABLE:
            raise UserError(f"missing must be one of: {', '.join(EDITABLE)}")
        limit = max(1, min(int(limit or 50), 200))
        hits = self.lib().search(query, artist, album, genre, missing, limit=100000)
        return {"total": len(hits), "shown": min(limit, len(hits)), "tracks": [compact(r) for r in hits[:limit]]}

    def get_tracks(self, track_ids: list[str]) -> list[dict]:
        if not track_ids:
            raise UserError("give at least one track id")
        if len(track_ids) > 50:
            raise UserError("at most 50 tracks at a time")
        out = self.bridge.call("get", ids=list(track_ids))
        for d in out:
            if isinstance(d.get("rating"), int):
                d["stars"] = round(d["rating"] / 20, 1)
            if d.get("duration"):
                d["duration"] = round(d["duration"], 1)
        return out

    def get_album(self, album: str, artist: str = "") -> dict:
        lib = self.lib()
        tracks = lib.album_tracks(album, artist)
        if not tracks:
            return {"error": f"no album called {album!r}" + (f" by {artist}" if artist else ""),
                    "similar": lib.albums_matching(album, artist) or lib.albums_matching(album)}
        artists = {(t.get("albumArtist") or t.get("artist")) for t in tracks}
        out = {"album": tracks[0].get("album"), "album_artists": sorted(a or "" for a in artists),
               "track_count": len(tracks), "tracks": [compact(t) for t in tracks]}
        bad = Library.album_inconsistencies(tracks)
        if bad:
            out["inconsistencies"] = bad
            out["suggested_fix"] = Library.suggested_fix(tracks)
        if len(artists) > 1 and not artist:
            out["note"] = "more than one album artist: pass artist to narrow it down, or set albumArtist to merge"
        return out

    def find_issues(self, limit: int = 20) -> dict:
        return self.lib(refresh=True).issues(limit=max(1, min(int(limit or 20), 100)))

    # ------------------------------------------------------------ edit
    @staticmethod
    def _clean_changes(changes: dict) -> dict:
        if not changes:
            raise UserError("no changes given")
        out = {}
        for k, v in changes.items():
            if k == "stars":
                k, v = "rating", round(float(v) * 20)
            if k not in EDITABLE:
                raise UserError(f"{k} can't be edited. Editable: {', '.join(EDITABLE)} (and stars, 0-5)")
            if k in INT_FIELDS:
                try:
                    v = int(v or 0)
                except (TypeError, ValueError):
                    raise UserError(f"{k} must be a number") from None
            elif k in BOOL_FIELDS:
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
            else:
                v = "" if v is None else str(v)
            if k == "rating" and not 0 <= v <= 100:
                raise UserError("stars must be 0 to 5")
            out[k] = v
        return out

    def _preview_edit(self, ids: list[str], changes: dict, label: str) -> dict:
        lib = self.lib()
        sample = []
        for tid in ids[:15]:
            r = lib.by_id.get(tid, {"id": tid})
            sample.append({"id": tid, "name": r.get("name"), "album": r.get("album"),
                           "now": {k: r.get(k) for k in changes}})
        token = self.store.new_confirmation("edit", {"ids": ids, "changes": changes, "label": label})
        return {"needs_confirmation": True, "confirm_token": token, "tracks": len(ids),
                "changes": changes, "sample": sample,
                "note": f"{len(ids)} tracks is a bulk edit: show the user what will change and call again "
                        f"with confirm_token={token} once they agree (valid 10 minutes)."}

    def _apply_edits(self, edits: list[dict], kind: str, summary: str) -> dict:
        results = self.bridge.call("set", timeout=60 + 3 * len(edits), edits=edits)
        self._dirty()
        done = [{"id": r["id"], "before": r["before"]} for r in results if not r.get("error")]
        errors = [{"id": r["id"], "error": r["error"]} for r in results if r.get("error")]
        cid = self.store.record_change(kind, summary, {"tracks": done}) if done else None
        out = {"changed": len(done), "change_id": cid}
        if errors:
            out["errors"] = errors
        if len(results) <= 5:
            out["results"] = results
        return out

    def edit_tracks(self, track_ids: list[str], changes: dict, confirm_token: str = "") -> dict:
        ids = list(dict.fromkeys(track_ids or []))
        if not ids:
            raise UserError("give at least one track id")
        changes = self._clean_changes(changes)
        if confirm_token:
            p = self.store.take_confirmation(confirm_token, "edit")
            if p is None or sorted(p["ids"]) != sorted(ids) or p["changes"] != changes:
                raise UserError("confirmation token is unknown, expired, or for a different edit: preview again")
        elif len(ids) > CONFIG.bulk_threshold:
            return self._preview_edit(ids, changes, "edit")
        for tid in ids:
            self._track(tid)
        summary = f"set {', '.join(f'{k}={v!r}' for k, v in changes.items())} on {len(ids)} track(s)"
        return self._apply_edits([{"id": t, "changes": changes} for t in ids], "edit", summary)

    def edit_album(self, album: str, changes: dict, artist: str = "", confirm_token: str = "") -> dict:
        tracks = self.lib().album_tracks(album, artist)
        if not tracks:
            return self.get_album(album, artist)
        return self.edit_tracks([t["id"] for t in tracks], changes, confirm_token)

    # ------------------------------------------------------------ genres
    def fill_genres(self, albums: list[dict] | None = None, limit: int = 10, overwrite: bool = False,
                    confirm_token: str = "") -> dict:
        if confirm_token:
            p = self.store.take_confirmation(confirm_token, "genres")
            if p is None:
                raise UserError("confirmation token is unknown or expired: run fill_genres again")
            edits = [{"id": tid, "changes": {"genre": a["genre"]}} for a in p for tid in a["track_ids"]]
            res = self._apply_edits(edits, "genres", f"genres for {len(p)} album(s)")
            return {**res, "albums": [f"{a['album']} - {a['genre']}" for a in p]}

        lib = self.lib(refresh=True)
        targets: list[list[dict]] = []
        if albums:
            for a in albums:
                ts = lib.album_tracks(a.get("album", ""), a.get("artist", ""))
                if ts:
                    targets.append(ts)
        else:
            groups: dict[tuple, list[dict]] = {}
            for r in lib.rows:
                if not r.get("genre"):
                    key = (norm(r.get("album")), norm(r.get("albumArtist") or r.get("artist")))
                    groups.setdefault(key, [])
            for r in lib.rows:
                key = (norm(r.get("album")), norm(r.get("albumArtist") or r.get("artist")))
                if key in groups:
                    groups[key].append(r)
            targets = list(groups.values())
        remaining = max(0, len(targets) - limit)
        proposals, not_found = [], []
        for ts in targets[:max(1, min(int(limit or 10), 25))]:
            t0 = ts[0]
            artist = t0.get("albumArtist") or t0.get("artist") or ""
            ids = [t["id"] for t in ts if overwrite or not t.get("genre")]
            if not ids:
                continue
            try:
                hit = musicbrainz.album_genre(t0.get("album") or "", artist)
            except Exception as e:
                hit = {"genre": "", "error": str(e)}
            item = {"album": t0.get("album"), "artist": artist, "tracks": len(ids)}
            if hit.get("genre"):
                proposals.append({**item, "genre": hit["genre"], "source": hit["source"], "track_ids": ids})
            else:
                not_found.append({**item, **({"error": hit["error"]} if hit.get("error") else {})})
        out: dict[str, Any] = {"proposals": [{k: v for k, v in p.items() if k != "track_ids"} for p in proposals],
                               "not_found": not_found, "albums_left_after_these": remaining}
        if proposals:
            out["confirm_token"] = self.store.new_confirmation("genres", proposals)
            out["note"] = ("Show the user these genres; call fill_genres(confirm_token=...) to apply. "
                           "Use edit_album to set any of them differently.")
        return out

    # ------------------------------------------------------------ playlists
    def list_playlists(self) -> list[dict]:
        return self.bridge.call("playlists", timeout=120)

    def _playlist(self, playlist: str) -> dict:
        pls = [p for p in self.list_playlists() if not p.get("folder")]
        hit = [p for p in pls if p["id"] == playlist] or \
              [p for p in pls if norm(p["name"]) == norm(playlist)]
        if not hit:
            hit = [p for p in pls if norm(playlist) in norm(p["name"])]
        if not hit:
            raise UserError(f"no playlist matching {playlist!r}")
        if len(hit) > 1:
            raise UserError(f"{playlist!r} matches several playlists: "
                            + ", ".join(f"{p['name']} ({p['id']})" for p in hit[:10]))
        return hit[0]

    def get_playlist(self, playlist: str, limit: int = 200) -> dict:
        p = self._playlist(playlist)
        res = self.bridge.call("playlistTracks", id=p["id"])
        lib = self.lib()
        tracks = [compact(lib.by_id[t]) if t in lib.by_id else {"id": t} for t in res["trackIds"]]
        return {"id": p["id"], "name": res["name"], "smart": res["smart"], "count": len(tracks),
                "tracks": tracks[:limit]}

    def create_playlist(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise UserError("give the playlist a name")
        res = self.bridge.call("createPlaylist", name=name)
        cid = self.store.record_change("playlist_create", f"created playlist {name}", res)
        return {**res, "change_id": cid}

    def add_to_playlist(self, playlist: str, track_ids: list[str], create: bool = False) -> dict:
        try:
            p = self._playlist(playlist)
        except UserError:
            if not create:
                raise
            p = self.create_playlist(playlist)
        if p.get("smart"):
            raise UserError("that's a smart playlist; its contents come from its rules")
        for tid in track_ids:
            self._track(tid)
        res = self.bridge.call("addToPlaylist", timeout=60 + 3 * len(track_ids), id=p["id"],
                               trackIds=list(track_ids))
        cid = None
        if res["added"]:
            cid = self.store.record_change("playlist_add", f"added {len(res['added'])} track(s) to {p['name']}",
                                           {"playlist": p["id"], "ids": res["added"]})
        return {"playlist": p["name"], **res, "change_id": cid}

    def remove_from_playlist(self, playlist: str, track_ids: list[str]) -> dict:
        p = self._playlist(playlist)
        res = self.bridge.call("removeFromPlaylist", timeout=60 + 3 * len(track_ids), id=p["id"],
                               trackIds=list(track_ids))
        cid = None
        if res["removed"]:
            cid = self.store.record_change("playlist_remove",
                                           f"removed {len(res['removed'])} track(s) from {p['name']}",
                                           {"playlist": p["id"], "ids": res["removed"]})
        return {"playlist": p["name"], **res, "change_id": cid,
                "note": "removed from the playlist only; the songs stay in the library"}

    def rename_playlist(self, playlist: str, new_name: str) -> dict:
        p = self._playlist(playlist)
        res = self.bridge.call("renamePlaylist", id=p["id"], name=new_name.strip())
        cid = self.store.record_change("playlist_rename", f"renamed {res['before']} to {res['after']}",
                                       {"playlist": p["id"], "before": res["before"]})
        return {**res, "change_id": cid}

    # ------------------------------------------------------------ delete
    def delete_tracks(self, track_ids: list[str], confirm_token: str = "") -> dict:
        ids = list(dict.fromkeys(track_ids or []))
        if not ids:
            raise UserError("give at least one track id")
        if len(ids) > MAX_DELETE:
            raise UserError(f"at most {MAX_DELETE} tracks per delete")
        if not confirm_token:
            details = self.bridge.call("get", ids=ids)
            items, total = [], 0
            for d in details:
                if d.get("error"):
                    raise UserError(d["error"])
                total += d.get("size") or 0
                items.append({"id": d["id"], "name": d.get("name"), "artist": d.get("artist"),
                              "album": d.get("album"), "format": short_kind(d), "file": d.get("location"),
                              "in_playlists": [p["name"] for p in d.get("playlists") or []]})
            token = self.store.new_confirmation("delete", {"ids": sorted(ids)})
            return {"needs_confirmation": True, "confirm_token": token, "tracks": items,
                    "total_size_mb": round(total / 1e6, 1),
                    "what_happens": "Each song is removed from the library (and so from iCloud and every "
                                    "device) and its file is moved to the Bin on the Mac. undo can bring "
                                    "the files back while they're still in the Bin, but plays, ratings "
                                    "and playlist entries are lost.",
                    "note": f"List these to the user and ask them to confirm. Only after a clear yes, call "
                            f"delete_tracks again with the same ids and confirm_token={token} "
                            f"(valid 10 minutes)."}
        p = self.store.take_confirmation(confirm_token, "delete")
        if p is None or p["ids"] != sorted(ids):
            raise UserError("confirmation token is unknown, expired, or for different tracks: preview again "
                            "and ask the user")
        res = self.bridge.call("delete", timeout=60 + 5 * len(ids), ids=ids)
        self._dirty()
        done = [r for r in res if r.get("removedFromLibrary")]
        cid = None
        if done:
            cid = self.store.record_change("delete", f"deleted {len(done)} track(s)", {"items": [
                {"name": r.get("name"), "artist": r.get("artist"), "originalPath": r.get("location"),
                 "binPath": r.get("binPath")} for r in done if r.get("location")]})
        return {"deleted": len(done), "files_binned": sum(1 for r in res if r.get("fileBinned")),
                "change_id": cid, "results": res}

    # ------------------------------------------------------------ re-add
    def rematch_tracks(self, track_ids: list[str]) -> dict:
        ids = list(dict.fromkeys(track_ids or []))
        if not ids:
            raise UserError("give at least one track id")
        if len(ids) > MAX_REMATCH:
            raise UserError(f"at most {MAX_REMATCH} tracks per call (each takes a few seconds)")
        kw = {"backupDir": CONFIG.rematch_backup_dir} if CONFIG.rematch_backup_dir else {}
        res = self.bridge.call("rematch", timeout=60 + 20 * len(ids), ids=ids, **kw)
        self._dirty()
        ok = [r for r in res if r.get("ok")]
        self.store.record_change("rematch", f"re-added {len(ok)} track(s)", {"results": res})
        return {"readded": len(ok), "results": res,
                "note": "New ids were assigned. iCloud takes a while (often up to an hour) to match them."}

    # ------------------------------------------------------------ history
    def list_changes(self, limit: int = 20) -> list[dict]:
        return [{"change_id": r["id"], "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"])),
                 "what": r["summary"], "undone": bool(r["undone"]),
                 "undoable": r["kind"] not in ("rematch",)} for r in self.store.changes(limit)]

    def undo(self, change_id: str) -> dict:
        row = self.store.change(change_id)
        if row is None:
            raise UserError(f"no change {change_id}")
        if row["undone"]:
            raise UserError("that change was already undone")
        kind, p = row["kind"], json.loads(row["payload"])
        if kind in ("edit", "genres"):
            res = self.bridge.call("set", timeout=60 + 3 * len(p["tracks"]),
                                   edits=[{"id": t["id"], "changes": t["before"]} for t in p["tracks"]])
            self._dirty()
            out = {"restored": sum(1 for r in res if not r.get("error")),
                   "errors": [r for r in res if r.get("error")]}
        elif kind == "playlist_add":
            out = self.bridge.call("removeFromPlaylist", id=p["playlist"], trackIds=p["ids"])
        elif kind == "playlist_remove":
            out = self.bridge.call("addToPlaylist", id=p["playlist"], trackIds=p["ids"])
        elif kind == "playlist_rename":
            out = self.bridge.call("renamePlaylist", id=p["playlist"], name=p["before"])
        elif kind == "playlist_create":
            out = self.bridge.call("deleteEmptyPlaylist", id=p["id"])
        elif kind == "delete":
            out = {"results": self.bridge.call("restore", timeout=60 + 5 * len(p["items"]), items=p["items"]),
                   "note": "files are back in the library; plays, ratings and playlists aren't restored"}
            self._dirty()
        else:
            raise UserError(f"{kind} changes can't be undone")
        self.store.x("UPDATE changes SET undone=1 WHERE id=?", (change_id,))
        return {"undone": row["summary"], **out}
