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
         "dateAdded": "2026-10-18T10:00:00Z", "favorited": False, "location": None, "hasFile": True}
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

    def op_add(self, paths):
        out = []
        for path in paths:
            self._n += 1
            t = track(self._n, Path(path).stem, "Someone", "Added", location=path, hasFile=True)
            self.tracks[t["id"]] = t
            out.append({"path": path, "newId": t["id"], "name": t["name"], "location": path})
        return out

    def op_removeEntries(self, ids):
        for i in ids:
            self.tracks.pop(i)
        return [{"id": i, "removed": True} for i in ids]

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
        self.data = TMP / "d" / self.id()
        self.svc = service.Service(self.store, self.music, data_dir=self.data)


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


class Safety(Base):
    def test_snapshot_before_first_write_only(self):
        snaps = self.data / "snapshots"
        before = 0
        self.svc.edit_tracks(["ID0001"], {"genre": "Pop"})
        self.svc.edit_tracks(["ID0002"], {"genre": "Pop"})
        self.assertEqual(len(list(snaps.glob("*.gz"))), before + 1)  # second edit reuses it
        import gzip, json
        latest = max(snaps.glob("*.gz"), key=lambda f: f.stat().st_mtime)
        data = json.load(gzip.open(latest, "rt"))
        self.assertEqual(data["count"], 8)
        self.assertEqual(data["cols"]["genre"][0], "")  # taken before the change

    def test_no_snapshot_no_write(self):
        service.SNAPSHOT_EVERY = 0  # force a new snapshot
        try:
            orig = self.music.op_dump
            self.svc.lib()  # snapshot of the library loaded first
            self.music.op_dump = lambda: (_ for _ in ()).throw(RuntimeError("disk full"))
            with self.assertRaisesRegex(service.UserError, "nothing was changed"):
                self.svc.edit_tracks(["ID0001"], {"genre": "Pop"})
            self.assertEqual(self.music.tracks["ID0001"]["genre"], "")
            self.music.op_dump = orig
        finally:
            service.SNAPSHOT_EVERY = 600


class Files(Base):
    def test_ghosts_orphans_and_add(self):
        media = TMP / f"vol-{self.id()}" / "Media.localized" / "Music" / "Olivia Rodrigo" / "GUTS"
        media.mkdir(parents=True)
        for n in ("01 all-american bitch.m4a", "05 ballad.m4a", ".hidden.m4a", "cover.jpg"):
            (media / n).write_bytes(b"x")
        self.music.tracks["ID0001"]["location"] = str(media / "01 all-american bitch.m4a")
        self.music.tracks["ID0002"].update(hasFile=False, location=None)  # a ghost entry
        issues = self.svc.find_issues()
        self.assertEqual([t["id"] for t in issues["entries_without_file"]["items"]], ["ID0002"])
        self.assertEqual(issues["files_not_in_library"]["items"], [str(media / "05 ballad.m4a")])
        res = self.svc.add_files([str(media / "05 ballad.m4a")])
        self.assertEqual(res["added"], 1)
        self.assertEqual(self.svc.find_issues()["files_not_in_library"]["count"], 0)
        with self.assertRaisesRegex(service.UserError, "already in the library"):
            self.svc.add_files([str(media / "05 ballad.m4a")])
        with self.assertRaisesRegex(service.UserError, "not audio"):
            self.svc.add_files([str(media / "cover.jpg")])
        self.svc.undo(res["change_id"])
        self.assertTrue((media / "05 ballad.m4a").exists())  # undo leaves the file alone
        self.assertEqual(self.svc.find_issues()["files_not_in_library"]["count"], 1)

    def test_icloud_ghost_cannot_be_deleted_by_script(self):
        self.music.tracks["ID0002"].update(hasFile=False, location=None)
        res = self.svc.delete_tracks(["ID0002"])
        self.assertIn("in_icloud", res)


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
    def setUp(self):
        super().setUp()
        for i in ("ID0004", "ID0005"):  # local-only songs (not in iCloud) can be deleted
            self.music.tracks[i]["cloudStatus"] = "duplicate"

    def test_icloud_songs_refused(self):
        res = self.svc.delete_tracks(["ID0001", "ID0004"])
        self.assertIn("error", res)
        self.assertEqual([t["id"] for t in res["in_icloud"]], ["ID0001"])
        self.assertEqual(res["deletable"], ["ID0004"])
        self.assertNotIn("confirm_token", res)

    def test_kept_by_icloud_is_reported(self):
        prev = self.svc.delete_tracks(["ID0004"])
        self.music.op_delete = lambda ids: [{"id": "ID0004", "removedFromLibrary": False,
                                             "fileBinned": False, "restoredId": "X"}]
        res = self.svc.delete_tracks(["ID0004"], confirm_token=prev["confirm_token"])
        self.assertEqual(res["deleted"], 0)
        self.assertIn("warning", res)

    def test_crash_part_way_is_reported(self):
        prev = self.svc.delete_tracks(["ID0004"])
        self.music.op_delete = lambda ids: (_ for _ in ()).throw(RuntimeError("osascript failed"))
        with self.assertRaisesRegex(service.UserError, "stopped part-way"):
            self.svc.delete_tracks(["ID0004"], confirm_token=prev["confirm_token"])

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
        b = bridge_mod.Bridge(self._fake_osascript("sleep 5"), timeout=1, min_gap=0)
        with self.assertRaisesRegex(bridge_mod.BridgeError, "dialog"):
            b.call("ping")

    def test_circuit_breaker(self):
        counter = TMP / "calls.txt"
        counter.write_text("")
        b = bridge_mod.Bridge(self._fake_osascript(
            f"echo x >> {counter}; echo '{{\"ok\": false, \"error\": \"Cannot get object.\"}}'"),
            min_gap=0, trip_after=3, pause=60)
        for _ in range(3):
            with self.assertRaisesRegex(bridge_mod.BridgeError, "get object"):
                b.call("addToPlaylist")
        with self.assertRaisesRegex(bridge_mod.BridgeError, "Paused"):
            b.call("ping")
        self.assertEqual(len(counter.read_text().split()), 3)  # the 4th never reached Music

    def test_success_resets_failures(self):
        flag = TMP / "fail.flag"
        b = bridge_mod.Bridge(self._fake_osascript(
            f"if [ -e {flag} ]; then echo '{{\"ok\": false, \"error\": \"boom\"}}'; "
            f"else echo '{{\"ok\": true, \"result\": 1}}'; fi"), min_gap=0, trip_after=3, pause=60)
        flag.write_text("")
        for _ in range(2):
            with self.assertRaises(bridge_mod.BridgeError):
                b.call("x")
        flag.unlink()
        self.assertEqual(b.call("x"), 1)
        flag.write_text("")
        for _ in range(2):  # counting starts again after a success
            with self.assertRaisesRegex(bridge_mod.BridgeError, "boom"):
                b.call("x")
        flag.unlink()

    def test_gap_between_calls(self):
        import time as _t
        b = bridge_mod.Bridge(self._fake_osascript("echo '{\"ok\": true, \"result\": 1}'"), min_gap=0.3)
        b.call("a")
        t0 = _t.monotonic()
        b.call("b")
        self.assertGreaterEqual(_t.monotonic() - t0, 0.3)

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
    def test_find_in_bin(self):
        home = TMP / "home"
        (home / ".Trash").mkdir(parents=True, exist_ok=True)
        (home / ".Trash" / "03 Centuries 2.mp3").write_bytes(b"x")
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            self.assertEqual(service.find_in_bin("/Users/x/Music/03 Centuries.mp3"),
                             str(home / ".Trash" / "03 Centuries 2.mp3"))
            self.assertIsNone(service.find_in_bin("/Users/x/Music/other.mp3"))
        finally:
            os.environ["HOME"] = old

    def test_mapping(self):
        self.assertEqual(musicbrainz.apple_genre("indie rock"), "Alternative")
        self.assertEqual(musicbrainz.apple_genre("pop"), "Pop")
        self.assertEqual(norm("Bad Idea Right?"), "bad idea right")
        self.assertNotEqual(norm("+"), norm("÷"))
        self.assertEqual(norm("Who\u2019d Have Known"), norm("Who'd Have Known"))
        self.assertEqual(norm("all\u2010american bitch"), norm("all-american bitch"))

    def test_symbol_albums_stay_apart(self):
        rows = [track(i, f"S{i}", "Ed Sheeran", alb) for i, alb in enumerate(["+", "+", "÷", "×"])]
        lib = Library({"count": 4, "cols": {k: [r[k] for r in rows] for k in rows[0]}})
        self.assertEqual(len(lib.album_tracks("+")), 2)
        self.assertEqual(len(lib.album_tracks("÷", "Ed Sheeran")), 1)


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
