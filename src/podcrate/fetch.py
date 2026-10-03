from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
from mutagen.mp3 import EasyMP3

from .config import Config
from .library import LibraryIndex, UNKNOWN_ALBUM, album_for, target_path
from .playlist import Playlist, Song, _song_from_single_column

logging.getLogger("yt_dlp").setLevel(logging.WARNING)

_MIN_YTDLP = (2026, 8, 19)
_ytdlp_version_warned = False


class FetchError(Exception):
    pass


def _tag(file_path: Path, artist: str, title: str, album: str) -> None:
    audio = EasyMP3(file_path)
    if audio.tags is None:
        audio.add_tags()
    audio["artist"] = artist
    audio["title"] = title
    audio["album"] = album
    audio.save()


def _album_from_info(info: dict, song: Song) -> str:
    if song.album.strip():
        return album_for(song)
    for key in ("album", "album_artist"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return album_for(Song(artist=song.artist, title=song.title, album=value))
    return UNKNOWN_ALBUM


_SEARCH_CANDIDATES = 8


def _search_queries(song: Song) -> list[str]:
    artist = song.artist.strip()
    title = song.title.strip()
    if not title:
        return []

    title_variants = [title]
    stripped_feat = re.sub(r"\s*\((?:feat\.|ft\.|featuring)[^)]+\)", "", title, flags=re.I).strip()
    if stripped_feat and stripped_feat not in title_variants:
        title_variants.append(stripped_feat)
    stripped_remaster = re.sub(
        r"\s*[-–]\s*\d{4}\s+Remaster(?:\s+Version)?\s*$", "", title, flags=re.I
    ).strip()
    if stripped_remaster and stripped_remaster not in title_variants:
        title_variants.append(stripped_remaster)

    queries: list[str] = []
    if artist and not artist.startswith("spotify:"):
        for t in title_variants:
            queries.extend(
                [
                    f"{artist} {t}",
                    f"{artist} {t} official audio",
                    f"{t} {artist}",
                ]
            )
    for t in title_variants:
        queries.extend([f"{t} official audio", t])

    seen: set[str] = set()
    ordered: list[str] = []
    for q in queries:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in seen:
            seen.add(q)
            ordered.append(q)
    return ordered


def _search_youtube(song: Song, cfg: Config) -> list[dict]:
    search_opts = {
        **_common_opts(cfg),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": "in_playlist",
    }
    seen_ids: set[str] = set()
    entries: list[dict] = []

    for query_text in _search_queries(song):
        query = f"ytsearch{_SEARCH_CANDIDATES}:{query_text}"
        try:
            with yt_dlp.YoutubeDL(search_opts) as ydl:
                search_info = ydl.extract_info(query, download=False)
        except Exception:
            continue
        for entry in (search_info or {}).get("entries") or []:
            if not entry:
                continue
            video_id = entry.get("id")
            if video_id and video_id in seen_ids:
                continue
            if video_id:
                seen_ids.add(video_id)
            entries.append(entry)
        if entries:
            break
    return entries

_PRIVATE_PLAYLIST_HINT = (
    "\n\nIf this playlist is private, export youtube.com cookies from the "
    "browser where you're logged in and set PODCRATE_YTDLP_COOKIES_FILE="
    "~/cookies.txt in .env (or PODCRATE_YTDLP_COOKIES_FROM_BROWSER=firefox)."
)


def _watch_url(entry: dict) -> str:
    video_id = entry.get("id")
    raw = entry.get("url") or entry.get("webpage_url") or ""
    if isinstance(raw, str) and raw.startswith("http") and "youtube.com/playlist" not in raw:
        return raw
    if video_id and isinstance(video_id, str) and not video_id.startswith("http"):
        return f"https://www.youtube.com/watch?v={video_id}"
    if isinstance(raw, str) and raw.startswith("http"):
        return raw
    return ""


def _song_from_youtube_entry(entry: dict) -> Song | None:
    url = _watch_url(entry)
    if not url:
        return None

    artist = str(entry.get("artist") or entry.get("album_artist") or "").strip()
    title = str(entry.get("track") or "").strip()
    album = str(entry.get("album") or "").strip()
    video_title = str(entry.get("title") or entry.get("fulltitle") or "").strip()
    uploader = str(
        entry.get("uploader") or entry.get("channel") or entry.get("creator") or ""
    ).strip()

    if artist and title:
        return Song(artist=artist, title=title, album=album, source_url=url)

    parsed = _song_from_single_column(video_title)
    if parsed:
        parsed.source_url = url
        if album:
            parsed.album = album
        return parsed

    if not video_title:
        return None
    return Song(artist=uploader or "Unknown", title=video_title, album=album, source_url=url)


def _format_youtube_playlist_error(exc: Exception) -> str:
    msg = _format_fetch_error(exc)
    low = msg.lower()
    if any(token in low for token in ("private", "sign in", "login", "not exist", "unavailable")):
        return msg + _PRIVATE_PLAYLIST_HINT
    return msg


def load_youtube_playlist(
    url: str, cfg: Config, *, name: str | None = None
) -> Playlist:
    """Expand a YouTube playlist or video URL into a tracked Playlist (no download)."""
    from .library import resolve_playlist_name

    _warn_if_ytdlp_outdated()
    opts = {
        **_common_opts(cfg),
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "noplaylist": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise FetchError(_format_youtube_playlist_error(exc)) from exc

    if not info:
        raise FetchError("could not read that YouTube URL." + _PRIVATE_PLAYLIST_HINT)

    entries = info.get("entries")
    if entries is None:
        entries = [info]

    songs: list[Song] = []
    seen: set[str] = set()
    for entry in entries:
        if not entry:
            continue
        song = _song_from_youtube_entry(entry)
        if song is None or song.source_url in seen:
            continue
        seen.add(song.source_url)
        songs.append(song)

    if not songs:
        raise FetchError(
            "YouTube playlist had no usable videos." + _PRIVATE_PLAYLIST_HINT
        )

    title = str(info.get("title") or info.get("playlist_title") or "YouTube").strip()
    playlist_name = resolve_playlist_name(explicit=name, fallback=title)
    return Playlist(name=playlist_name, query=url, songs=songs)


_403_HINT = (
    "\n\nYouTube blocked the download. Try, in order:\n"
    "  1. uv sync && uv tool install --editable . --force  "
    "(updates yt-dlp in both project and global podcrate)\n"
    "  2. yt-dlp --rm-cache-dir\n"
    "  3. Install a JS runtime: deno or node (YouTube requires one)\n"
    "  4. Export youtube.com cookies from your browser to a file and set "
    "PODCRATE_YTDLP_COOKIES_FILE=~/cookies.txt in .env\n"
    "     (browser cookie auto-load often fails on Linux — see README)"
)


def _parse_ytdlp_version(version: str) -> tuple[int, int, int]:
    parts: list[int] = []
    for chunk in version.split(".")[:3]:
        match = re.match(r"(\d+)", chunk)
        parts.append(int(match.group(1)) if match else 0)
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def _warn_if_ytdlp_outdated() -> None:
    global _ytdlp_version_warned
    if _ytdlp_version_warned:
        return
    _ytdlp_version_warned = True
    installed = _parse_ytdlp_version(yt_dlp.version.__version__)
    if installed < _MIN_YTDLP:
        logging.warning(
            "yt-dlp %s is too old for YouTube (need %s+). "
            "Run: uv sync && uv tool install --editable . --force",
            yt_dlp.version.__version__,
            ".".join(str(p) for p in _MIN_YTDLP),
        )


def _format_fetch_error(exc: Exception) -> str:
    msg = str(exc).strip()
    if "403" in msg:
        return msg + _403_HINT
    return msg


def _js_runtimes() -> dict[str, dict]:
    runtimes: dict[str, dict] = {}
    if shutil.which("deno"):
        runtimes["deno"] = {}
    if shutil.which("node"):
        runtimes["node"] = {}
    return runtimes


def _common_opts(cfg: Config) -> dict:
    opts: dict = {
        "remote_components": {"ejs:github"},
    }
    runtimes = _js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes
    if cfg.ytdlp_cookies_file:
        opts["cookiefile"] = str(cfg.ytdlp_cookies_file)
    elif cfg.ytdlp_cookies_from_browser:
        # BROWSER[+KEYRING], same as yt-dlp --cookies-from-browser. The keyring
        # suffix is needed where yt-dlp guesses wrong (e.g. Hyprland is detected
        # as "OTHER" and falls back to BASICTEXT, which cannot decrypt v11 cookies).
        browser, _, keyring = cfg.ytdlp_cookies_from_browser.partition("+")
        opts["cookiesfrombrowser"] = (browser, None, keyring.upper() or None, None)
    return opts


def fetch_song(
    song: Song, cfg: Config, index: LibraryIndex, *, force: bool = False
) -> Path:
    """Download+tag a single song, or return the existing file if already fetched.

    Raises FetchError if the song can't be found/downloaded.
    """
    _warn_if_ytdlp_outdated()
    if not force:
        existing = index.get(song.artist, song.title)
        if existing:
            return existing

    dest = target_path(cfg.library_dir, song)
    dest.parent.mkdir(parents=True, exist_ok=True)
    outtmpl = str(dest.with_suffix("")) + ".%(ext)s"

    if song.source_url:
        entries = [{"url": song.source_url}]
    else:
        try:
            entries = _search_youtube(song, cfg)
        except Exception as exc:
            raise FetchError(_format_fetch_error(exc)) from exc

        if not entries:
            raise FetchError(
                f"no YouTube match for {song.display()} — "
                "check the artist/title are correct"
            )

    download_opts = {
        **_common_opts(cfg),
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "writethumbnail": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            },
            {"key": "EmbedThumbnail"},
            {"key": "FFmpegMetadata"},
        ],
    }

    last_error: Exception | None = None
    info = None
    for entry in entries:
        video_id = entry.get("id")
        video_url = entry.get("url") or (
            f"https://www.youtube.com/watch?v={video_id}" if video_id else None
        )
        if not video_url:
            continue
        try:
            with yt_dlp.YoutubeDL(download_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
            last_error = None
            break
        except Exception as exc:  # yt-dlp raises many different exception types
            last_error = exc
            continue

    if last_error is not None:
        raise FetchError(_format_fetch_error(last_error)) from last_error
    if not info:
        raise FetchError("no search result could be downloaded")
    if not dest.exists():
        raise FetchError(f"download finished but file missing at {dest}")

    album = _album_from_info(info, song)
    final_dest = target_path(cfg.library_dir, song, album=album)
    if final_dest != dest:
        final_dest.parent.mkdir(parents=True, exist_ok=True)
        dest.replace(final_dest)
        dest = final_dest

    _tag(dest, song.artist, song.title, album)
    index.add(song.artist, song.title, dest)
    return dest


@dataclass
class FetchResult:
    song: Song
    path: Path | None
    error: str | None
    skipped: bool = False


def fetch_playlist(
    songs: list[Song], cfg: Config, index: LibraryIndex, *, force: bool = False
) -> list[FetchResult]:
    results = []
    for song in songs:
        already = None if force else index.get(song.artist, song.title)
        try:
            path = fetch_song(song, cfg, index, force=force)
            results.append(FetchResult(song, path, None, skipped=already is not None))
        except FetchError as exc:
            results.append(FetchResult(song, None, str(exc)))
    return results
