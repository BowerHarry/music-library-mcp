# music-library-mcp

An MCP server that lets Claude look after an Apple Music library: search it, fix tags, fill in
genres from MusicBrainz, sort out albums that show up twice, manage playlists, delete songs (with
confirmation, to the Bin) and re-add songs that iCloud has stopped serving. Every change can be
undone.

It runs on the Mac that hosts the library and drives the Music app through Apple Events
(JavaScript for Automation). Nothing is clicked and no windows move; changes go through Music, so
the file, the library and iCloud Music Library stay in step.

## Tools

| Read | Edit | Playlists | Safety |
|---|---|---|---|
| `library_status` | `edit_tracks` | `list_playlists` | `delete_tracks` (preview, then confirm) |
| `search_tracks` | `edit_album` | `get_playlist` | `list_changes` |
| `get_tracks` | `fill_genres` | `create_playlist` | `undo` |
| `get_album` | `rematch_tracks` | `add_to_playlist` | |
| `find_issues` | `add_files` | `remove_from_playlist`, `rename_playlist` | `snapshot_library` |

- **Bulk edits** (more than 25 tracks) and **genre fills** return a preview and a one-time
  confirmation token; nothing changes until the call is repeated with the token.
- **Deletes** always preview first. The token is tied to the exact tracks, expires after 10
  minutes and works once. Files go to the Bin, so `undo` can bring them back.
- **Re-adding** (`rematch_tracks`) fixes songs that are "matched" but greyed out on other devices:
  each file is backed up, the library entry removed and the file added again, keeping stars,
  plays, favourite and playlists.
- **Snapshots**: before changes, every track's details are saved to `data/snapshots/` (gzipped
  JSON, at most one per 10 minutes, last 50 kept). If the snapshot can't be saved, nothing changes.
- **Health check** finds library entries without a file ("ghosts" left when a file was replaced),
  entries whose file has gone, and audio files in the Music folder that aren't in the library.
- One Apple Event request runs at a time, so a slow Mac isn't flooded.
- Back up `Music Library.musiclibrary` too before big clean-ups: it holds playlists, plays and
  ratings, which snapshots record but can't recreate.

## Requirements

- macOS with the Music app, signed in to the library you want to manage, in a logged-in session
- [uv](https://docs.astral.sh/uv/) (installs Python and the one dependency, `mcp`)
- A way for Claude to reach it over HTTPS, e.g. a Cloudflare Tunnel (OAuth is built in)

## Setup

```sh
git clone https://github.com/BowerHarry/music-library-mcp.git && cd music-library-mcp
curl -LsSf https://astral.sh/uv/install.sh | sh     # if you don't have uv
cp .env.example .env                                 # set PUBLIC_URL and a password
uv run python -m library_mcp.cli status              # macOS asks to let Terminal control Music
uv run python -m library_mcp.cli issues              # read-only health check
./scripts/install-launch-agent.sh                    # run at login; approve the Music prompt again
```

Point your tunnel at `http://localhost:8091` (from a container: `http://host.docker.internal:8091`),
then add `https://<your host>/mcp` as a custom connector in Claude and sign in with the password.

Logs are in `data/server.log`; the change history and OAuth state in `data/library.db`.

## Tests

```sh
uv run python -m unittest tests/test_all.py
```

The tests run against an in-memory fake of the Music app, so they work on any OS. `library_mcp.cli`
gives read-only checks against the real library.

## Layout

```
library_mcp/
  bridge.js     Apple Events side: every operation on the Music app
  bridge.py     runs bridge.js via osascript, one call at a time, with timeouts
  index.py      in-memory snapshot of the library: search, album checks, health check
  service.py    the operations behind each tool: confirmations, change history, undo
  musicbrainz.py  genre lookups (1 request/second), mapped to Apple's genre names
  server.py     MCP tools and instructions; auth.py single-user OAuth
```

## Licence

MIT
