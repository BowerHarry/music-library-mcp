"""Tests against a fake Music app (the real one needs macOS: see library_mcp.cli for live checks)."""
from __future__ import annotations

import copy
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="library-mcp-test-"))
os.environ["DATA_DIR"] = str(TMP / "data")
os.environ["LIBRARY_MCP_NO_ENV_FILES"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from library_mcp import bridge as bridge_mod, musicbrainz, service  # noqa: E402
from library_mcp.index import Library, norm  # noqa: E402
from library_mcp.store import Store  # noqa: E402


def track(i, name, artist, album, **kw):
    t = {"id": f"ID{i:04d}", "name": name, "artist": artist, "album": album, "albumArtist": artist,
         "genre": "", "year": 2023, "trackNumber": kw.get("trackNumber", 1), "trackCount": 3,
         "discNumber": 1, "discCount": 1, "compilation": False, "composer": "", "comment": "",
         "grouping": "", "sortName": "", "sortArtist": "", "sortAlbum": "", "sortAlbumArtist": "",
         "rating": 0, "kind": "Apple Lossless audio file", "cloudStatus": "matched", "bitRate": 1411,
         "sampleRate": 44100, "size": 20_000_000, "duration": 200.0, "playedCount": 0,
         "dateAdded": "2026-10-18T10:00:00Z", "favorited": False, "location": None}
    t.update(kw)
    return t


class FakeMusic:
    """Implements the bridge.js ops in memory."""

    def __init__(self, tracks):
        self.tracks = {t["id"]: t for t in tracks}
        self.playlists = {"PL1": {"name": "Gym", "smart": False, "ids": ["ID0001"]},
                          "PL2": {"name": "Recently Added", "smart": True, "ids": []}}
        self.bin = {}
        self.calls = []
        self._n = 1000

    def call(self, op, timeout=None, **a):
        self.calls.append(op)
        return getattr(self, "op_" + op)(**a)

    def op_ping(self):
        return {"musicRunning": True, "version": "1.4", "tracks": len(self.tracks)}

    def op_dump(self):
        rows = list(self.tracks.values())
        keys = rows[0].keys() if rows else []
        return {"count": len(rows), "cols": {k: [r[k] for r in rows] for k in keys}, "missing": []}

    def op_get(self, ids):
        out = []
        for i in ids:
            if i not in self.tracks:
                out.append({"id": i, "error": f"track not found: {i}"})
                continue
            d = copy.deepcopy(self.tracks[i])
            d["playlists"] = [{"id": p, "name": v["name"]} for p, v in self.playlists.items() if i in v["ids"]]
            out.append(d)
        return out

    def op_set(self, edits):
        res = []
        for e in edits:
            t = self.tracks.get(e["id"])
            if t is None:
                res.append({"id": e["id"], "error": "track not found"})
                continue
            before = {k: t[k] for k in e["changes"]}
            t.update(e["changes"])
            res.append({"id": e["id"], "before": before, "after": dict(e["changes"])})
        return res

    def op_playlists(self):
        return [{"id": k, "name": v["name"], "smart": v["smart"], "folder": False, "count": len(v["ids"])}
                for k, v in self.playlists.items()]

    def op_playlistTracks(self, id):
        p = self.playlists[id]
        return {"id": id, "name": p["name"], "smart": p["smart"], "trackIds": list(p["ids"])}

    def op_createPlaylist(self, name):
        self._n += 1
        pid = f"PL{self._n}"
        self.playlists[pid] = {"name": name, "smart": False, "ids": []}
        return {"id": pid, "name": name}

    def op_deleteEmptyPlaylist(self, id):
        assert not self.playlists[id]["ids"]
        del self.playlists[id]
        return {"id": id, "deleted": True}

    def op_renamePlaylist(self, id, name):
        before = self.playlists[id]["name"]
        self.playlists[id]["name"] = name
        return {"id": id, "before": before, "after": name}

    def op_addToPlaylist(self, id, trackIds):
        p = self.playlists[id]
        added = [t for t in trackIds if t not in p["ids"]]
        p["ids"] += added
        return {"added": added, "alreadyThere": [t for t in trackIds if t not in added], "errors": []}

    def op_removeFromPlaylist(self, id, trackIds):
        p = self.playlists[id]
        removed = [t for t in trackIds if t in p["ids"]]
        p["ids"] = [t for t in p["ids"] if t not in removed]
        return {"removed": removed, "notThere": [t for t in trackIds if t not in removed]}

    def op_delete(self, ids):
        out = []
        for i in ids:
            t = self.tracks.pop(i)
            for p in self.playlists.values():
                p["ids"] = [x for x in p["ids"] if x != i]
            loc = f"/Music/{t['album']}/{t['name']}.m4a"
            self.bin[loc] = t
            out.append({"id": i, "name": t["name"], "artist": t["artist"], "album": t["album"],
                        "location": loc, "removedFromLibrary": True, "fileBinned": True,
                        "binPath": "/.Trashes/" + t["name"]})
        return out

    def op_restore(self, items):
        out = []
        for it in items:
            t = self.bin.pop(it["originalPath"])
            self._n += 1
            t = dict(t, id=f"NEW{self._n}")
            self.tracks[t["id"]] = t
            out.append({"originalPath": it["originalPath"], "newId": t["id"]})
        return out

    def op_rematch(self, ids, backupDir=None):
        out = []
        for i in ids:
            t = self.tracks.pop(i)
            self._n += 1
            t = dict(t, id=f"NEW{self._n}")
            self.tracks[t["id"]] = t
            out.append({"id": i, "newId": t["id"], "ok": True})
        return out


def library():
    return [
        track(1, "all-american bitch", "Olivia Rodrigo", "GUTS", trackNumber=1),
        track(2, "bad idea right?", "Olivia Rodrigo", "GUTS", trackNumber=2, year=2024),
        track(3, "vampire", "Olivia Rodrigo", "GUTS", trackNumber=3, kind="AAC audio file", bitRate=256),
        track(4, "Money", "Pink Floyd", "The Dark Side of the Moon", genre="Rock", trackNumber=6),
        track(5, "Time", "Pink Floyd", "The Dark Side of the Moon", genre="Rock", trackNumber=4),
        track(6, "Like a Rolling Stone", "Bob Dylan", "Hits", compilation=True, albumArtist="Various Artists"),
        track(7, "Mr. Tambourine Man", "Bob Dylan", "Hits", albumArtist="Bob Dylan"),
        track(8, "Time", "Pink Floyd", "The Dark Side of the Moon", genre="Rock", trackNumber=4,
              kind="AAC audio file", bitRate=256, cloudStatus="error"),
    ]


class Base(unittest.TestCase):
    def setUp(self):
        self.music = FakeMusic(library())
        self.store = Store(TMP / f"{self.id()}.db")
        self.svc = service.Service(self.store, self.music)


class Reading(Base):
    def test_search(self):
        res = self.svc.search_tracks("rodrigo")
        self.assertEqual(res["total"], 3)
        self.assertEqual([t["track"] for t in res["tracks"]], [1, 2, 3])
        self.assertEqual(self.svc.search_tracks(missing="genre")["total"], 5)
        self.assertEqual(self.svc.search_tracks("time", artist="pink floyd")["total"], 2)

    def test_album_inconsistencies_and_fix(self):
        a = self.svc.get_album("guts")
        self.assertEqual(a["track_count"], 3)
        self.assertIn("year", a["inconsistencies"])
        self.assertEqual(a["suggested_fix"], {"year": 2023})

    def test_unknown_album_suggests(self):
        a = self.svc.get_album("dark side")
        self.assertIn("error", a)
        self.assertEqual(a["similar"][0]["album"], "The Dark Side of the Moon")

    def test_issues(self):
        i = self.svc.find_issues()
        self.assertEqual(i["missing_genre"]["count"], 3)  # GUTS + the two "Hits" parts
        self.assertEqual(i["split_albums"]["count"], 1)
        self.assertEqual(i["split_albums"]["items"][0]["album"], "Hits")
        self.assertEqual(i["duplicates"]["count"], 1)
        self.assertEqual(i["icloud_problems"]["count"], 1)
        self.assertEqual(i["lossy_albums"]["count"], 2)

    def test_snapshot_cached(self):
        self.svc.search_tracks("a")
        self.svc.search_tracks("b")
        self.assertEqual(self.music.calls.count("dump"), 1)


class Editing(Base):
    def test_edit_and_undo(self):
        res = self.svc.edit_tracks(["ID0001", "ID0002"], {"genre": "Pop", "stars": 4})
        self.assertEqual(res["changed"], 2)
        self.assertEqual(self.music.tracks["ID0001"]["genre"], "Pop")
        self.assertEqual(self.music.tracks["ID0001"]["rating"], 80)
        self.assertEqual(self.svc.search_tracks(genre="pop")["total"], 2)  # snapshot refreshed
        self.svc.undo(res["change_id"])
        self.assertEqual(self.music.tracks["ID0001"]["genre"], "")
        self.assertEqual(self.music.tracks["ID0001"]["rating"], 0)
        with self.assertRaises(service.UserError):
            self.svc.undo(res["change_id"])

    def test_bad_field(self):
        with self.assertRaises(service.UserError):
            self.svc.edit_tracks(["ID0001"], {"bpm": 120})
        with self.assertRaises(service.UserError):
            self.svc.edit_tracks(["NOPE"], {"genre": "Pop"})

    def test_bulk_edit_needs_confirmation(self):
        many = [track(100 + i, f"Song {i}", "Band", "Big Album", trackNumber=i) for i in range(30)]
        self.music.tracks.update({t["id"]: t for t in many})
        ids = [t["id"] for t in many]
        prev = self.svc.edit_album("big album", {"genre": "Jazz"})
        self.assertTrue(prev["needs_confirmation"])
        self.assertEqual(self.music.tracks[ids[0]]["genre"], "")
        with self.assertRaises(service.UserError):  # token is tied to the exact edit
            self.svc.edit_tracks(ids, {"genre": "Blues"}, confirm_token=prev["confirm_token"])
        prev = self.svc.edit_album("big album", {"genre": "Jazz"})
        res = self.svc.edit_album("big album", {"genre": "Jazz"}, confirm_token=prev["confirm_token"])
        self.assertEqual(res["changed"], 30)
        self.assertEqual(self.music.tracks[ids[29]]["genre"], "Jazz")

    def test_fill_genres(self):
        musicbrainz.album_genre = lambda album, artist: (
            {"genre": "Pop", "source": "album"} if album == "GUTS" else {"genre": "", "source": ""})
        prev = self.svc.fill_genres()
        self.assertEqual([p["album"] for p in prev["proposals"]], ["GUTS"])
        self.assertEqual(len(prev["not_found"]), 2)
        self.assertEqual(self.music.tracks["ID0001"]["genre"], "")
        res = self.svc.fill_genres(confirm_token=prev["confirm_token"])
        self.assertEqual(res["changed"], 3)
        self.assertEqual(self.music.tracks["ID0003"]["genre"], "Pop")
        # Rock tracks untouched without overwrite, replaced with it
        musicbrainz.album_genre = lambda album, artist: {"genre": "Alternative", "source": "artist"}
        prev = self.svc.fill_genres(albums=[{"album": "The Dark Side of the Moon"}])
        self.assertEqual(prev["proposals"], [])
        prev = self.svc.fill_genres(albums=[{"album": "The Dark Side of the Moon"}], overwrite=True)
        self.assertEqual(prev["proposals"][0]["tracks"], 3)

    def test_rematch(self):
        res = self.svc.rematch_tracks(["ID0003"])
        self.assertEqual(res["readded"], 1)
        self.assertNotIn("ID0003", self.music.tracks)
        self.assertEqual(self.svc.search_tracks("vampire")["tracks"][0]["id"], res["results"][0]["newId"])


class Playlists(Base):
    def test_add_remove_undo(self):
        res = self.svc.add_to_playlist("gym", ["ID0001", "ID0004"])
        self.assertEqual(res["added"], ["ID0004"])
        self.assertEqual(res["alreadyThere"], ["ID0001"])
        self.svc.undo(res["change_id"])
        self.assertEqual(self.music.playlists["PL1"]["ids"], ["ID0001"])
        res = self.svc.remove_from_playlist("Gym", ["ID0001"])
        self.assertEqual(self.music.playlists["PL1"]["ids"], [])
        self.svc.undo(res["change_id"])
        self.assertEqual(self.music.playlists["PL1"]["ids"], ["ID0001"])

    def test_create_on_add_and_smart_refused(self):
        res = self.svc.add_to_playlist("Road Trip", ["ID0004"], create=True)
        self.assertEqual(res["added"], ["ID0004"])
        with self.assertRaises(service.UserError):
            self.svc.add_to_playlist("Road Trip 2", ["ID0004"])
        with self.assertRaises(service.UserError):
            self.svc.add_to_playlist("Recently Added", ["ID0004"])

    def test_rename_and_get(self):
        res = self.svc.rename_playlist("gym", "Gym 2026")
        self.assertEqual(self.svc.get_playlist("gym 2026")["tracks"][0]["name"], "all-american bitch")
        self.svc.undo(res["change_id"])
        self.assertEqual(self.music.playlists["PL1"]["name"], "Gym")


class Deleting(Base):
    def test_preview_then_confirm(self):
        prev = self.svc.delete_tracks(["ID0004"])
        self.assertTrue(prev["needs_confirmation"])
        self.assertIn("ID0004", self.music.tracks)  # nothing happened yet
        self.assertNotIn("delete", self.music.calls)
        res = self.svc.delete_tracks(["ID0004"], confirm_token=prev["confirm_token"])
        self.assertEqual(res["deleted"], 1)
        self.assertNotIn("ID0004", self.music.tracks)
        with self.assertRaises(service.UserError):  # tokens are single use
            self.svc.delete_tracks(["ID0005"], confirm_token=prev["confirm_token"])
        back = self.svc.undo(res["change_id"])
        self.assertEqual(self.svc.search_tracks("money")["total"], 1)
        self.assertTrue(back["results"][0]["newId"].startswith("NEW"))

    def test_token_bound_to_ids(self):
        prev = self.svc.delete_tracks(["ID0004"])
        with self.assertRaises(service.UserError):
            self.svc.delete_tracks(["ID0004", "ID0005"], confirm_token=prev["confirm_token"])
        self.assertIn("ID0005", self.music.tracks)
        with self.assertRaises(service.UserError):
            self.svc.delete_tracks(["ID0004"], confirm_token="WRONG1")

    def test_edit_token_cannot_delete(self):
        many = [track(200 + i, f"S{i}", "B", "Box", trackNumber=i) for i in range(30)]
        self.music.tracks.update({t["id"]: t for t in many})
        prev = self.svc.edit_album("box", {"genre": "Jazz"})
        with self.assertRaises(service.UserError):
            self.svc.delete_tracks([t["id"] for t in many], confirm_token=prev["confirm_token"])


class BridgeProcess(unittest.TestCase):
    def _fake_osascript(self, body: str) -> str:
        p = TMP / f"osascript-{abs(hash(body))}"
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return str(p)

    def test_ok_and_errors(self):
        b = bridge_mod.Bridge(self._fake_osascript("echo '{\"ok\": true, \"result\": {\"x\": 1}}'"))
        self.assertEqual(b.call("ping"), {"x": 1})
        b = bridge_mod.Bridge(self._fake_osascript("echo '{\"ok\": false, \"error\": \"track not found\"}'"))
        with self.assertRaisesRegex(bridge_mod.BridgeError, "track not found"):
            b.call("get", ids=["x"])
        b = bridge_mod.Bridge(self._fake_osascript("echo 'execution error: Not authorized (-1743)' >&2; exit 1"))
        with self.assertRaisesRegex(bridge_mod.BridgeError, "Automation"):
            b.call("ping")
        b = bridge_mod.Bridge(self._fake_osascript("sleep 5"), timeout=1)
        with self.assertRaisesRegex(bridge_mod.BridgeError, "dialog"):
            b.call("ping")

    def test_request_file_passed(self):
        b = bridge_mod.Bridge(self._fake_osascript(
            'python3 -c "import json,sys;r=json.load(open(sys.argv[1]));'
            'print(json.dumps({\'ok\':True,\'result\':r}))" "$4"'))
        self.assertEqual(b.call("get", ids=["A"]), {"op": "get", "ids": ["A"]})


class EnvFiles(unittest.TestCase):
    def test_env_and_extra_file(self):
        from library_mcp import config
        root = TMP / "envroot"
        root.mkdir(exist_ok=True)
        (TMP / "docker.env").write_text('MUSIC_AGENT_PASSWORD="from docker"\nOTHER=x\n')
        (root / ".env").write_text(f"PUBLIC_URL=https://lib.example\nENV_FILES={TMP / 'docker.env'}\n")
        old = config.ROOT
        config.ROOT = root
        try:
            for k in ("PUBLIC_URL", "MUSIC_AGENT_PASSWORD", "ENV_FILES"):
                os.environ.pop(k, None)
            config.load_env_files()
            self.assertEqual(os.environ["PUBLIC_URL"], "https://lib.example")
            self.assertEqual(os.environ["MUSIC_AGENT_PASSWORD"], "from docker")
            self.assertNotIn("OTHER", os.environ)  # unrelated keys aren't imported
        finally:
            config.ROOT = old
            for k in ("PUBLIC_URL", "MUSIC_AGENT_PASSWORD", "ENV_FILES", "OTHER"):
                os.environ.pop(k, None)


class Genres(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(musicbrainz.apple_genre("indie rock"), "Alternative")
        self.assertEqual(musicbrainz.apple_genre("pop"), "Pop")
        self.assertEqual(norm("Bad Idea Right?"), "bad idea right")


class Tools(unittest.TestCase):
    def test_registered(self):
        import anyio
        from library_mcp import server
        names = {t.name for t in anyio.run(server.mcp.list_tools)}
        for n in ("search_tracks", "get_album", "edit_tracks", "edit_album", "fill_genres",
                  "add_to_playlist", "delete_tracks", "undo", "rematch_tracks", "find_issues"):
            self.assertIn(n, names)


if __name__ == "__main__":
    unittest.main()
