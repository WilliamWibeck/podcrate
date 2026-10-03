from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import spotipy
from rich.console import Console
from spotipy.oauth2 import SpotifyOauthError, SpotifyPKCE

from .config import Config
from .playlist import Playlist, Song

SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"
LIKED = "liked"  # pseudo playlist id for the user's Liked Songs
TOKEN_CACHE = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    / "podcrate"
    / "spotify-token.json"
)


EMBED_LIMIT = 100  # the public embed page lists at most this many tracks

# We turn API errors into SpotifyError ourselves; spotipy's own error log is noise.
logging.getLogger("spotipy").setLevel(logging.CRITICAL)


class SpotifyError(ValueError):
    pass


def _playlist_id(source: str) -> str:
    if source.startswith("spotify:playlist:"):
        return source.rsplit(":", 1)[1]
    m = re.search(r"/playlist/([A-Za-z0-9]+)", source)
    return m.group(1) if m else source


def _load_from_embed(playlist_id: str) -> tuple[str, list[Song]]:
    """Read a public playlist from open.spotify.com/embed (no auth, first 100 tracks).

    Used when the Web API refuses a playlist the user doesn't own. Scrapes the
    page's __NEXT_DATA__ JSON, so it can break if Spotify changes the embed page.
    """
    req = urllib.request.Request(
        f"https://open.spotify.com/embed/playlist/{playlist_id}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8")
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except Exception as exc:
        raise SpotifyError(
            "Spotify refused that playlist and its public page couldn't be read either "
            "(private playlist?). Export it with Exportify and fetch the .csv."
        ) from exc
    songs = []
    for t in entity.get("trackList") or []:
        artist = (t.get("subtitle") or "").split(",")[0].strip()
        title = (t.get("title") or "").strip()
        if t.get("entityType", "track") == "track" and artist and title:
            songs.append(Song(artist=artist, title=title))
    return entity.get("name") or playlist_id, songs


def is_spotify_url(value: str) -> bool:
    """True for open.spotify.com playlist links and spotify:playlist: URIs."""
    if value.startswith("spotify:playlist:"):
        return True
    parsed = urlparse(value)
    return parsed.hostname == "open.spotify.com" and "/playlist/" in parsed.path


def _client(cfg: Config) -> spotipy.Spotify:
    if not cfg.spotify_client_id:
        raise SpotifyError(
            "PODCRATE_SPOTIFY_CLIENT_ID is not set. Create an app at "
            "https://developer.spotify.com/dashboard, add "
            f"{cfg.spotify_redirect_uri} as a redirect URI, and put its client ID in .env."
        )
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    auth = SpotifyPKCE(
        client_id=cfg.spotify_client_id,
        redirect_uri=cfg.spotify_redirect_uri,
        scope=SCOPES,
        cache_path=str(TOKEN_CACHE),
    )
    return spotipy.Spotify(auth_manager=auth)


def _all_pages(sp: spotipy.Spotify, page: dict) -> list[dict]:
    items = list(page["items"])
    while page.get("next"):
        page = sp.next(page)
        items.extend(page["items"])
    return items


def _song_from_item(item: dict) -> Song | None:
    # Feb 2026 API renamed the item payload from "track" to "item"; accept either.
    track = item.get("item") or item.get("track")
    if not track or track.get("type", "track") != "track":
        return None
    artists = [a["name"] for a in track.get("artists") or [] if a.get("name")]
    title = (track.get("name") or "").strip()
    if not artists or not title:
        return None
    return Song(
        artist=artists[0],
        title=title,
        album=(track.get("album") or {}).get("name", ""),
    )


def list_my_playlists(cfg: Config) -> list[dict]:
    """Playlists in the user's library: [{id, name, owner, total, readable}].

    Development-mode apps (since Feb 2026) can only read the items of playlists
    the user owns or collaborates on; `readable` marks those.
    """
    try:
        sp = _client(cfg)
        me = sp.me()["id"]
        raw = _all_pages(sp, sp.current_user_playlists(limit=50))
        liked_total = sp.current_user_saved_tracks(limit=1)["total"]
    except (spotipy.SpotifyException, SpotifyOauthError) as exc:
        raise SpotifyError(f"Spotify: {exc}") from exc
    out = [{"id": LIKED, "name": "Liked Songs", "owner": "you", "total": liked_total, "readable": True}]
    for p in raw:
        if not p:
            continue
        counts = p.get("items") or p.get("tracks") or {}
        owner = p.get("owner") or {}
        out.append({
            "id": p["id"],
            "name": p["name"],
            "owner": owner.get("display_name") or "",
            "total": counts.get("total", 0),
            "readable": owner.get("id") == me or bool(p.get("collaborative")),
        })
    return out


def load_spotify_playlist(
    source: str, cfg: Config, *, name: str | None = None
) -> Playlist:
    """Expand a Spotify playlist URL/URI/ID (or LIKED) into a tracked Playlist (no download)."""
    from .library import dedupe_songs, resolve_playlist_name

    try:
        sp = _client(cfg)
        if source == LIKED:
            meta = {"name": "Liked Songs"}
            items = _all_pages(sp, sp.current_user_saved_tracks(limit=50))
        else:
            meta = sp.playlist(source, fields="name")
            items = _all_pages(sp, sp.playlist_items(source, additional_types=("track",)))
    except spotipy.SpotifyException as exc:
        if exc.http_status not in (403, 404) or source == LIKED:
            raise SpotifyError(f"Spotify: {exc.msg}") from exc
        # Dev-mode apps can't read playlists the user doesn't own; fall back to the public page.
        title, embed_songs = _load_from_embed(_playlist_id(source))
        meta, items = {"name": title}, []
    except SpotifyOauthError as exc:
        raise SpotifyError(f"Spotify login failed: {exc}") from exc
    else:
        embed_songs = []

    songs = dedupe_songs(embed_songs or [s for s in map(_song_from_item, items) if s])
    if len(embed_songs) >= EMBED_LIMIT:
        Console(stderr=True).print(
            f"[yellow]'{meta['name']}' isn't yours, so it was read from Spotify's public "
            f"page — only the first {EMBED_LIMIT} songs are available.[/yellow]"
        )
    if not songs:
        raise SpotifyError(f"Spotify playlist '{meta['name']}' has no songs.")
    return Playlist(
        name=resolve_playlist_name(explicit=name, fallback=meta["name"]),
        query=f"spotify:{source}",
        songs=songs,
    )
