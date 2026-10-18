"""Settings from environment variables (scripts/run.sh loads them from .env)."""
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent


def load_env_files() -> None:
    """KEY=VALUE lines from ROOT/.env, then from any files listed in its ENV_FILES (comma separated),
    e.g. a docker .env that already holds the shared password. Only the keys in ENV_FILES_KEYS are
    taken from those extra files, so unrelated secrets stay out. Real environment variables win."""
    def read(path: Path) -> dict[str, str]:
        out = {}
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return out
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().removeprefix("export ").strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            out[k] = v
        return out

    first = read(ROOT / ".env")
    extra = [Path(f.strip()) for f in (os.environ.get("ENV_FILES") or first.get("ENV_FILES", "")).split(",")
             if f.strip()]
    wanted = set((os.environ.get("ENV_FILES_KEYS") or first.get("ENV_FILES_KEYS")
                  or "LIBRARY_PASSWORD,MUSIC_AGENT_PASSWORD,OWNER_NAME").split(","))
    for k, v in first.items():
        os.environ.setdefault(k, v)
    for f in extra:
        for k, v in read(f).items():
            if k in wanted:
                os.environ.setdefault(k, v)


if not os.environ.get("LIBRARY_MCP_NO_ENV_FILES"):
    load_env_files()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _default_hosts() -> list[str]:
    hosts = ["localhost:*", "127.0.0.1:*", "host.docker.internal:*", "host.orb.internal:*"]
    public = urlparse(_env("PUBLIC_URL")).hostname
    if public:
        hosts += [public, public + ":*"]
    return hosts


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path(_env("DATA_DIR", str(ROOT / "data")))
    # OAuth login for the public endpoint. LIBRARY_PASSWORD, or MUSIC_AGENT_PASSWORD to share one.
    password: str = _env("LIBRARY_PASSWORD") or _env("MUSIC_AGENT_PASSWORD")
    public_url: str = _env("PUBLIC_URL").rstrip("/")
    host: str = _env("HOST", "127.0.0.1")
    port: int = int(_env("PORT", "8091"))
    allowed_hosts: tuple[str, ...] = tuple(
        h for h in (_env("ALLOWED_HOSTS") or ",".join(_default_hosts())).split(",") if h
    )
    owner_name: str = _env("OWNER_NAME")
    mb_user_agent: str = _env("MB_USER_AGENT") or \
        f"music-library-mcp/0.1 ( {_env('PUBLIC_URL') or 'https://github.com/'} )"
    # Where re-add backups go (default: next to the Music media folder, deleted after each track).
    rematch_backup_dir: str = _env("REMATCH_BACKUP_DIR")
    osascript: str = _env("OSASCRIPT", "/usr/bin/osascript")
    bridge_timeout: int = int(_env("BRIDGE_TIMEOUT", "300"))
    index_ttl: int = int(_env("INDEX_TTL", "300"))
    # Edits touching more tracks than this need a preview and confirmation first.
    bulk_threshold: int = int(_env("BULK_THRESHOLD", "25"))


CONFIG = Config()
