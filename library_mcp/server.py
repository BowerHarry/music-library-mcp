"""MCP server: read and edit an Apple Music library from Claude."""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import CONFIG
from .service import Service, UserError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("library_mcp")

INSTRUCTIONS = """\
You look after the user's Apple Music library: finding songs, fixing details (genre, album
artist, track numbers...), playlists, and removing songs. Changes are made through the Music app
on the Mac that hosts the library and sync to their devices through iCloud.

- Find things with search_tracks or get_album first; every other tool takes track ids from them.
  Ids change when a song is re-added or deleted, so look them up again rather than reusing old ones.
- Edits apply straight away and can be undone (undo with the change_id). Edits to more than 25
  songs, and fill_genres, return a preview first: summarise it and apply only once the user agrees.
- Deleting: ALWAYS call delete_tracks without a token first, show the user exactly which songs
  (name, artist, album) will go, say the files go to the Bin, and ask them to confirm. Songs in
  iCloud Music Library (matched/uploaded/purchased) can't be deleted by these tools: the preview
  says so, and the user has to delete them in the Music app. Only after a clear yes in their reply call it again with the token.
  Never delete on an inferred or earlier yes, and never delete more than they named.
- A snapshot of every song's details is saved before changes; if a tool says it couldn't save
  one, stop and tell the user rather than retrying around it.
- Songs greyed out on their devices (matched but won't play) are fixed by rematch_tracks.
- Albums showing up twice usually disagree on album artist or compilation: get_album shows the
  inconsistencies and a suggested fix to apply with edit_album.
- Prefer Apple's genre names (Pop, Hip-Hop/Rap, Alternative, R&B/Soul, Dance, Electronic...).
- Keep replies short. Report what changed in one line, with the change_id if they may want to undo.
""" + (f"\nThe user's name is {CONFIG.owner_name}.\n" if CONFIG.owner_name else "")


def _auth():
    """OAuth is on when a password and public URL are configured (always, in production)."""
    if not (CONFIG.password and CONFIG.public_url):
        return {}
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from .auth import SCOPE, SingleUserProvider
    from .store import Store
    provider = SingleUserProvider(Store(CONFIG.data_dir / "library.db"), CONFIG.password, CONFIG.public_url)
    settings = AuthSettings(
        issuer_url=CONFIG.public_url,
        resource_server_url=f"{CONFIG.public_url}/mcp",
        validate_token_resource=False,
        required_scopes=[SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
        revocation_options=RevocationOptions(enabled=True),
    )
    return {"auth_server_provider": provider, "auth": settings}


_AUTH = _auth()
mcp = MCPServer("music-library", instructions=INSTRUCTIONS, **_AUTH)
_svc: Service | None = None

if _AUTH:
    @mcp.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request):
        return await _AUTH["auth_server_provider"].login_page(request)


def service() -> Service:
    global _svc
    if _svc is None:
        _svc = Service()
    return _svc


async def _run(tool: str, args: dict, fn, *a, **kw) -> Any:
    s = service()
    try:
        result = await anyio.to_thread.run_sync(lambda: fn(s, *a, **kw))
    except UserError as e:
        result = {"error": str(e)}
    except Exception as e:
        log.exception("%s failed", tool)
        result = {"error": f"{type(e).__name__}: {e}"}
    try:
        s.store.audit(tool, args, json.dumps(result, default=str))
    except Exception:
        pass
    return result


# ------------------------------------------------------------------ read
@mcp.tool()
async def library_status() -> Any:
    """Is the Music app reachable, and how big is the library."""
    return await _run("library_status", {}, Service.status)


@mcp.tool()
async def search_tracks(query: str = "", artist: str = "", album: str = "", genre: str = "",
                        missing: str = "", limit: int = 50) -> Any:
    """Search the library. query matches words in title, artist, album and genre; artist/album/genre
    narrow it further; missing="genre" (or another field) finds tracks where that field is empty.
    Returns ids plus name, artist, album, track number, genre, year, length, format, iCloud status."""
    return await _run("search_tracks", locals(), Service.search_tracks, query, artist, album, genre,
                      missing, limit)


@mcp.tool()
async def get_tracks(track_ids: list[str]) -> Any:
    """Every detail of up to 50 tracks: all tags, stars, plays, date added, file path, whether the
    file exists, artwork count, iCloud status and the playlists each one is in."""
    return await _run("get_tracks", locals(), Service.get_tracks, track_ids)


@mcp.tool()
async def get_album(album: str, artist: str = "") -> Any:
    """An album's tracks, plus any fields its tracks disagree on (album artist, year, genre,
    compilation, disc/track counts) and a suggested fix. Suggests close names if there's no match."""
    return await _run("get_album", locals(), Service.get_album, album, artist)


@mcp.tool()
async def find_issues(limit: int = 20) -> Any:
    """Library health check: albums without a genre, albums split in two, inconsistent albums,
    duplicates, iCloud problems, missing files, and albums with lossy (AAC/MP3) tracks."""
    return await _run("find_issues", locals(), Service.find_issues, limit)


# ------------------------------------------------------------------ edit
@mcp.tool()
async def edit_tracks(track_ids: list[str], changes: dict, confirm_token: str = "") -> Any:
    """Set fields on tracks, e.g. changes={"genre": "Pop"} or {"albumArtist": "Olivia Rodrigo",
    "compilation": false}. Fields: name, artist, album, albumArtist, genre, year, trackNumber,
    trackCount, discNumber, discCount, compilation, composer, comment, grouping, sortName,
    sortArtist, sortAlbum, sortAlbumArtist, stars (0-5), favorited. More than 25 tracks returns a
    preview and confirm_token first. Returns a change_id for undo."""
    return await _run("edit_tracks", locals(), Service.edit_tracks, track_ids, changes, confirm_token)


@mcp.tool()
async def edit_album(album: str, changes: dict, artist: str = "", confirm_token: str = "") -> Any:
    """Set fields on every track of an album (same fields as edit_tracks), e.g. to apply
    get_album's suggested fix or set its genre. Returns a change_id for undo."""
    return await _run("edit_album", locals(), Service.edit_album, album, changes, artist, confirm_token)


@mcp.tool()
async def fill_genres(albums: list[dict] | None = None, limit: int = 10, overwrite: bool = False,
                      confirm_token: str = "") -> Any:
    """Look up genres on MusicBrainz. Without albums it takes the next albums missing a genre;
    or pass albums=[{"album": "GUTS", "artist": "Olivia Rodrigo"}]. overwrite also replaces
    existing genres. Returns proposals and a confirm_token; call again with only the token to apply.
    About 1-3 seconds per album."""
    return await _run("fill_genres", locals(), Service.fill_genres, albums, limit, overwrite, confirm_token)


@mcp.tool()
async def rematch_tracks(track_ids: list[str]) -> Any:
    """Fix songs that are greyed out / won't play on other devices: re-adds each one to the
    library so iCloud matches or uploads it again. Keeps the file, stars, plays, favourite and
    playlists. Up to 30 per call; new ids are assigned."""
    return await _run("rematch_tracks", locals(), Service.rematch_tracks, track_ids)


@mcp.tool()
async def add_files(paths: list[str]) -> Any:
    """Add audio files on the Mac to the library, e.g. ones find_issues lists under
    files_not_in_library. Up to 100 per call. Returns new track ids and a change_id."""
    return await _run("add_files", locals(), Service.add_files, paths)


@mcp.tool()
async def snapshot_library() -> Any:
    """Save a full snapshot of every song's details now. One is also taken automatically before
    changes (at most every 10 minutes), and changes refuse to run if it fails."""
    return await _run("snapshot_library", {}, Service.snapshot)


# ------------------------------------------------------------------ playlists
@mcp.tool()
async def list_playlists() -> Any:
    """The user's playlists (not smart ones' rules): id, name, smart, folder, track count."""
    return await _run("list_playlists", {}, Service.list_playlists)


@mcp.tool()
async def get_playlist(playlist: str) -> Any:
    """A playlist's tracks, by name or id."""
    return await _run("get_playlist", locals(), Service.get_playlist, playlist)


@mcp.tool()
async def create_playlist(name: str) -> Any:
    """Create an empty playlist."""
    return await _run("create_playlist", locals(), Service.create_playlist, name)


@mcp.tool()
async def add_to_playlist(playlist: str, track_ids: list[str], create: bool = False) -> Any:
    """Add tracks to a playlist (by name or id); ones already in it are skipped. create=True makes
    the playlist if it doesn't exist."""
    return await _run("add_to_playlist", locals(), Service.add_to_playlist, playlist, track_ids, create)


@mcp.tool()
async def remove_from_playlist(playlist: str, track_ids: list[str]) -> Any:
    """Take tracks out of a playlist. They stay in the library."""
    return await _run("remove_from_playlist", locals(), Service.remove_from_playlist, playlist, track_ids)


@mcp.tool()
async def rename_playlist(playlist: str, new_name: str) -> Any:
    """Rename a playlist."""
    return await _run("rename_playlist", locals(), Service.rename_playlist, playlist, new_name)


# ------------------------------------------------------------------ delete and undo
@mcp.tool()
async def delete_tracks(track_ids: list[str], confirm_token: str = "") -> Any:
    """Delete songs from the library AND move their files to the Bin. Without confirm_token this
    only previews (nothing changes): show the user the list and ask. Call again with the token only
    after they clearly say yes. Up to 100 per call. Songs stored in iCloud Music Library can't be
    deleted this way (the preview refuses them); the user deletes those in the Music app."""
    return await _run("delete_tracks", locals(), Service.delete_tracks, track_ids, confirm_token)


@mcp.tool()
async def list_changes(limit: int = 20) -> Any:
    """Recent changes made through these tools, newest first, with change_ids."""
    return await _run("list_changes", locals(), Service.list_changes, limit)


@mcp.tool()
async def undo(change_id: str) -> Any:
    """Undo a change: edits and genres go back to their old values, playlist changes are reversed,
    deleted songs come back from the Bin (without plays, stars or playlists)."""
    return await _run("undo", locals(), Service.undo, change_id)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _warm_up() -> None:
    """Talk to Music once at startup, so macOS asks for Automation permission straight away
    (while someone is at the Mac) rather than on Claude's first request."""
    try:
        log.info("Music reachable: %s", service().status())
    except Exception as e:
        log.warning("Music not reachable yet: %s", e)


def main() -> None:
    import threading
    threading.Thread(target=_warm_up, name="warm-up", daemon=True).start()
    if "--stdio" in sys.argv:
        mcp.run("stdio")
        return
    if not _AUTH and os.environ.get("ALLOW_NO_AUTH") != "1":
        sys.exit("Refusing to serve HTTP without auth: set LIBRARY_PASSWORD and PUBLIC_URL")
    mcp.run(
        "streamable-http",
        host=CONFIG.host,
        port=CONFIG.port,
        transport_security=TransportSecuritySettings(allowed_hosts=list(CONFIG.allowed_hosts)),
    )


if __name__ == "__main__":
    main()
