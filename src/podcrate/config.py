from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_LIBRARY_DIR = Path.home() / "Music" / "podcrate"
DEFAULT_MODEL = "gemini-flash-latest"


@dataclass
class Config:
    gemini_api_key: str | None
    library_dir: Path
    model: str
    ipod_music_subdir: str
    ipod_playlists_subdir: str
    ytdlp_cookies_from_browser: str | None
    ytdlp_cookies_file: Path | None
    spotify_client_id: str | None
    spotify_redirect_uri: str

    @classmethod
    def load(cls) -> "Config":
        cookies_file = os.environ.get("PODCRATE_YTDLP_COOKIES_FILE")
        return cls(
            gemini_api_key=os.environ.get("GEMINI_API_KEY"),
            library_dir=Path(
                os.environ.get("PODCRATE_LIBRARY_DIR", str(DEFAULT_LIBRARY_DIR))
            ).expanduser(),
            model=os.environ.get("PODCRATE_MODEL", DEFAULT_MODEL),
            ipod_music_subdir=os.environ.get("PODCRATE_IPOD_MUSIC_SUBDIR", "Music"),
            ipod_playlists_subdir=os.environ.get("PODCRATE_IPOD_PLAYLISTS_SUBDIR", "Playlists"),
            ytdlp_cookies_from_browser=os.environ.get(
                "PODCRATE_YTDLP_COOKIES_FROM_BROWSER"
            ),
            ytdlp_cookies_file=Path(cookies_file).expanduser() if cookies_file else None,
            spotify_client_id=os.environ.get("PODCRATE_SPOTIFY_CLIENT_ID"),
            spotify_redirect_uri=os.environ.get(
                "PODCRATE_SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8765/callback"
            ),
        )


def require_api_key(cfg: Config) -> str:
    if not cfg.gemini_api_key:
        raise SystemExit(
            "GEMINI_API_KEY is not set. Put it in a .env file or export it "
            "in your shell before running playlist generation."
        )
    return cfg.gemini_api_key
