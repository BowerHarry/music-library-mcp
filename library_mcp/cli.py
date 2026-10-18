"""Read-only checks from the terminal, to test the Music connection without Claude.

    python -m library_mcp.cli status
    python -m library_mcp.cli search "olivia rodrigo"
    python -m library_mcp.cli album GUTS "Olivia Rodrigo"
    python -m library_mcp.cli issues
    python -m library_mcp.cli playlists
"""
from __future__ import annotations

import json
import sys

from .service import Service


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    s = Service()
    cmd, args = argv[0], argv[1:]
    if cmd == "status":
        out = s.status()
    elif cmd == "search":
        out = s.search_tracks(" ".join(args), limit=20)
    elif cmd == "album":
        out = s.get_album(args[0], args[1] if len(args) > 1 else "")
    elif cmd == "issues":
        out = s.find_issues(limit=5)
    elif cmd == "playlists":
        out = s.list_playlists()
    elif cmd == "track":
        out = s.get_tracks(args)
    else:
        print(__doc__)
        return 1
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
