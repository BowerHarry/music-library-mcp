"""Playlist export: writes the user's playlists to a JSON file, by default <music folder>/
.playlists.json, so other apps that read the same music folder (e.g. over a network share) can use
them. Playlists are still only edited in Music.

    {"schemaVersion": 1, "exportedAt": "...", "musicFolder": "/…/Media.localized/Music",
     "playlists": [{"id": "<Music persistent id>", "name": "Road Trip",
                    "tracks": ["Adele/19/01 Daydreamer.m4a"]}]}

Tracks are paths relative to the music folder, in playlist order; a file outside it is listed by its
full path. Songs without a local file (cloud-only), smart playlists and folders are left out.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

log = logging.getLogger("library_mcp")

SCHEMA_VERSION = 1
FILE_NAME = ".playlists.json"
# Where Music (or iTunes) keeps the songs inside its media folder.
MUSIC_DIRS = ("/Media.localized/Music/", "/iTunes Media/Music/")
DELAY = 20  # seconds after the last playlist change before exporting, so a burst is one export


def music_folder(locations) -> str | None:
    """The music folder most of these files are in, e.g. ".../Media.localized/Music"."""
    roots: Counter[str] = Counter()
    for loc in locations:
        for marker in MUSIC_DIRS:
            i = (loc or "").find(marker)
            if i >= 0:
                roots[loc[:i + len(marker) - 1]] += 1
                break
    return roots.most_common(1)[0][0] if roots else None


def build(result: dict, root: str | None) -> dict:
    """The file's contents from the bridge's exportPlaylists result."""
    prefix = root.rstrip("/") + "/" if root else None
    playlists = []
    for p in result["playlists"]:
        tracks = [loc[len(prefix):] if prefix and loc.startswith(prefix) else loc
                  for loc in (t.get("location") for t in p["tracks"]) if loc]
        playlists.append({"id": p["id"], "name": p["name"], "tracks": list(dict.fromkeys(tracks))})
    return {"schemaVersion": SCHEMA_VERSION, "exportedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "music-library-mcp", "musicFolder": root, "playlists": playlists}


def write_if_changed(path: Path, doc: dict) -> bool:
    """Replaces the file in one step, unless only exportedAt would change: apps watching the file
    aren't told about an export that changed nothing."""
    def same(d: dict) -> dict:
        return {k: v for k, v in d.items() if k != "exportedAt"}
    try:
        if same(json.loads(path.read_text(encoding="utf-8"))) == same(doc):
            return False
    except (OSError, ValueError, AttributeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".playlists-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o644)  # mkstemp makes it owner-only; apps on the network may read it as another user
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


class PlaylistExporter:
    def __init__(self, bridge, target: str, delay: float = DELAY):
        self.bridge = bridge
        self.target = target  # "auto", or the file's full path
        self.delay = delay
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _path_and_root(self, result: dict) -> tuple[Path, str | None]:
        root = music_folder([t.get("location") for p in result["playlists"] for t in p["tracks"]]
                            + result.get("sampleLocations", []))
        if self.target != "auto":
            return Path(self.target).expanduser(), root
        if not root:
            raise RuntimeError("couldn't tell where Music keeps its files: set PLAYLIST_EXPORT to the "
                               "file's full path")
        return Path(root) / FILE_NAME, root

    def export(self) -> dict:
        result = self.bridge.call("exportPlaylists", timeout=180)
        path, root = self._path_and_root(result)
        doc = build(result, root)
        written = write_if_changed(path, doc)
        songs = sum(len(p["tracks"]) for p in doc["playlists"])
        if written:
            log.info("Exported %d playlists (%d songs) to %s", len(doc["playlists"]), songs, path)
        return {"file": str(path), "playlists": len(doc["playlists"]), "songs": songs,
                "songs_without_a_file": sum(1 for p in result["playlists"] for t in p["tracks"]
                                            if not t.get("location")),
                "written": written}

    def export_quietly(self) -> None:
        try:
            self.export()
        except Exception as e:
            log.warning("Playlist export failed: %s", e)

    def schedule(self) -> None:
        """Export `delay` seconds from now, pushing back any export already waiting."""
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self.export_quietly)
            self._timer.daemon = True
            self._timer.start()
