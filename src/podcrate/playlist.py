from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import BaseModel

from .config import Config, require_api_key

SYSTEM_PROMPT = """\
You are a music curator with deep, specific knowledge of real recorded songs \
across all genres and eras. Given a listener's request, you pick a concrete \
playlist of real songs that exist and can plausibly be found on YouTube.

First, silently classify the request:
- STRUCTURED: it names a specific, real source with its own fixed contents — an \
album, EP, soundtrack, an artist's discography/back catalog, a named compilation \
or mixtape, "side B of X", "Y's first three albums", etc. There is one correct \
answer here.
- OPEN-ENDED: it describes a vibe, genre, mood, era, occasion, or a "similar to X" \
reference, without naming one fixed source. Many different answers are valid.

For STRUCTURED requests:
- Return the real, accurate contents of that source, in its natural order (album \
running order, chronological for a discography, etc.), or the subset the request \
implies (e.g. "side B", "the first three albums").
- Do not add, omit, reorder, or swap in tracks for the sake of variety — accuracy \
to the real source beats curation.
- Credit each track to whoever actually performed it on that release — that may \
mean many tracks by one artist (an album/discography) or several different \
artists (a soundtrack/compilation).
- The requested song count is only a target for OPEN-ENDED requests. For \
STRUCTURED requests, ignore it and return exactly what the source actually \
contains, even if that's more or fewer songs.

For OPEN-ENDED requests:
- Avoid duplicate artists unless the request implies repetition.
- Prefer some variety (not just the most obvious greatest hits) while staying true \
to the requested vibe, genre, era, or reference.
- Aim for the requested song count.

Always: every song must be a real, released track by a real artist. Never invent \
songs. You have a search tool — use it whenever you are not fully confident of the \
real, exact tracklist or song titles, especially for STRUCTURED requests and for \
lesser-known artists/albums. Prefer a verified search result over your own memory.

Also provide `suggested_name` for every request — a short, readable playlist title \
suitable for browsing on an iPod (ideally under 40 characters). Never echo the \
full listener prompt verbatim.
- For STRUCTURED requests naming a single artist's release (album, EP, \
discography, etc.), use "Artist - Title" (e.g. "LOAM - X", \
"Interpol - Turn on the Bright Lights", "Radiohead - Discography").
- For STRUCTURED requests without one clear artist (a various-artists soundtrack \
or compilation), use the source's own name (e.g. "Guardians of the Galaxy: \
Awesome Mix Vol. 1").
- For OPEN-ENDED requests, use a concise vibe/genre title (e.g. "Spoken Words", \
"90s Shoegaze", "Dark Emotional Rock") — not the full prompt text.
"""


FILL_SYSTEM_PROMPT = """\
You are a music curator with deep, specific knowledge of real recorded songs \
across all genres and eras. You will be given a list of songs already in a \
folder or playlist. Analyze what they have in common (genre, era, mood, \
artists, production style) and suggest more real songs that fit the same \
vibe and would belong alongside them.

Rules:
- Every suggested song must be a real, released track by a real artist. Never \
invent songs.
- Do not repeat any song already in the given list.
- Prefer some artist variety over repeating the same artist many times, unless \
the existing list is clearly built around one artist or discography.
- You have a search tool — use it whenever you are not fully confident of a \
real, exact song title/artist. Prefer a verified search result over memory.
"""


class _SongItem(BaseModel):
    artist: str
    title: str
    album: str = ""
    reason: str = ""


class _PlaylistResponse(BaseModel):
    suggested_name: str | None = None
    songs: list[_SongItem]


@dataclass
class Song:
    artist: str
    title: str
    album: str = ""
    reason: str = ""
    source_url: str = ""

    def display(self) -> str:
        return f"{self.artist} - {self.title}"


@dataclass
class Playlist:
    name: str
    query: str
    songs: list[Song]


def generate_playlist(
    query: str, count: int, cfg: Config
) -> tuple[list[Song], str | None]:
    api_key = require_api_key(cfg)
    client = genai.Client(api_key=api_key)

    user_prompt = (
        f'Listener request: "{query}"\n'
        f"Target {count} songs for an open-ended request. If this is a structured "
        "request for a specific real source (album, discography, soundtrack, etc.), "
        "ignore that number and return exactly what the source contains instead."
    )

    response = client.models.generate_content(
        model=cfg.model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=_PlaylistResponse,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )

    parsed = response.parsed
    if parsed is None:
        try:
            data = json.loads(response.text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise SystemExit(
                f"Gemini did not return valid JSON. Raw response:\n{response.text}"
            ) from exc
        parsed = _PlaylistResponse(**data)

    songs = [
        Song(artist=i.artist, title=i.title, album=i.album, reason=i.reason)
        for i in parsed.songs
    ]
    return songs, parsed.suggested_name


def generate_similar_songs(
    existing: list[Song], count: int, cfg: Config, extra_query: str | None = None
) -> list[Song]:
    """Given songs already in a folder/playlist, ask Gemini for more that fit the vibe."""
    api_key = require_api_key(cfg)
    client = genai.Client(api_key=api_key)

    existing_list = "\n".join(f"- {s.display()}" for s in existing)
    user_prompt = (
        f"Existing songs in this collection:\n{existing_list}\n\n"
        f"Suggest {count} more real songs that fit the same vibe, without "
        "repeating any of the above."
    )
    if extra_query:
        user_prompt += f"\n\nAdditional guidance: {extra_query}"

    response = client.models.generate_content(
        model=cfg.model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=FILL_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=_PlaylistResponse,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )

    parsed = response.parsed
    if parsed is None:
        try:
            data = json.loads(response.text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise SystemExit(
                f"Gemini did not return valid JSON. Raw response:\n{response.text}"
            ) from exc
        parsed = _PlaylistResponse(**data)

    return [
        Song(artist=i.artist, title=i.title, album=i.album, reason=i.reason)
        for i in parsed.songs
    ]


def save_playlist(playlist: Playlist, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": playlist.name,
                "query": playlist.query,
                "songs": [_song_to_dict(s) for s in playlist.songs],
            },
            indent=2,
        )
    )


def load_playlist(path: Path) -> Playlist:
    data = json.loads(path.read_text())
    return Playlist(
        name=data["name"],
        query=data["query"],
        songs=[_song_from_dict(item) for item in data["songs"]],
    )


def _song_to_dict(song: Song) -> dict:
    data = {
        "artist": song.artist,
        "title": song.title,
        "album": song.album,
        "reason": song.reason,
    }
    if song.source_url:
        data["source_url"] = song.source_url
    return data


def is_youtube_url(value: str) -> bool:
    """True for youtube.com / youtu.be / music.youtube.com links."""
    lowered = value.strip().lower()
    if not lowered.startswith(("http://", "https://")):
        return False
    host = lowered.split("/", 3)[2] if lowered.count("/") >= 2 else ""
    host = host.split("@")[-1].split(":")[0]
    return host == "youtu.be" or host.endswith(".youtu.be") or host == "youtube.com" or host.endswith(".youtube.com") or host == "youtube-nocookie.com" or host.endswith(".youtube-nocookie.com")


def _song_from_dict(item: dict) -> Song:
    album = item.get("album", "")
    reason = item.get("reason", "")
    # Older Spotify imports stored album name in reason.
    if not album and reason and "artist" not in reason.lower():
        album = reason
        reason = ""
    return Song(
        artist=item["artist"],
        title=item["title"],
        album=album,
        reason=reason,
        source_url=item.get("source_url", ""),
    )


_HEADER_ALIASES: dict[str, set[str]] = {
    "artist": {"artist", "performer", "band", "artist_name", "artists"},
    "title": {"title", "track", "song", "name", "track_name"},
    "album": {"album", "album_name"},
    "reason": {"reason", "why", "notes", "note"},
}

_HEADERISH_VALUES = {
    "track uri",
    "track name",
    "album name",
    "artist name(s)",
    "artist names",
    "track_uri",
    "track_name",
    "album_name",
    "artist_name",
}


def _normalize_header(value: str) -> str:
    s = value.strip().lower()
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def _detect_csv_columns(first_row: list[str]) -> dict[str, int] | None:
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(first_row):
        key = _normalize_header(cell)
        for field, aliases in _HEADER_ALIASES.items():
            if key in aliases:
                mapping[field] = idx
        if "artist" not in mapping and "artist" in key and "uri" not in key:
            mapping["artist"] = idx
        if "title" not in mapping and key in {"track_name", "track"}:
            mapping["title"] = idx
        if "album" not in mapping and key in {"album_name", "album"}:
            mapping["album"] = idx
    if "artist" in mapping and "title" in mapping:
        return mapping
    return None


def _first_artist(raw: str) -> str:
    for sep in (";", "|", "/"):
        if sep in raw:
            raw = raw.split(sep, 1)[0]
    return raw.split(",", 1)[0].strip()


def _should_skip_csv_row(artist: str, title: str) -> bool:
    if not artist or not title:
        return True
    if artist.startswith("spotify:"):
        return True
    if artist.lower() in _HEADERISH_VALUES or title.lower() in _HEADERISH_VALUES:
        return True
    return False


def _song_from_csv_row(row: list[str], columns: dict[str, int]) -> Song | None:
    def cell(field: str) -> str:
        idx = columns.get(field)
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    artist = _first_artist(cell("artist"))
    title = cell("title")
    if _should_skip_csv_row(artist, title):
        return None
    return Song(
        artist=artist,
        title=title,
        album=cell("album"),
        reason=cell("reason"),
    )


def _song_from_single_column(cell: str) -> Song | None:
    cell = cell.strip()
    if not cell:
        return None
    if " - " in cell:
        artist, title = cell.split(" - ", 1)
        artist, title = artist.strip(), title.strip()
        if artist and title:
            return Song(artist=artist, title=title)
    return None


def _parse_csv_songs(text: str) -> list[Song]:
    reader = csv.reader(StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("CSV file is empty")

    songs: list[Song] = []
    columns = _detect_csv_columns(rows[0])
    data_rows = rows[1:] if columns else rows

    for row in data_rows:
        song: Song | None
        if columns:
            song = _song_from_csv_row(row, columns)
        elif len(row) >= 2:
            artist = _first_artist(row[0].strip())
            title = row[1].strip()
            if _should_skip_csv_row(artist, title):
                song = None
            else:
                song = Song(
                    artist=artist,
                    title=title,
                    album=row[2].strip() if len(row) > 2 else "",
                    reason=row[3].strip() if len(row) > 3 else "",
                )
        elif len(row) == 1:
            song = _song_from_single_column(row[0])
        else:
            song = None

        if song is None:
            continue
        songs.append(song)

    if not songs:
        raise ValueError("CSV file contains no songs")
    return songs


def load_playlist_from_csv(path: Path, *, name: str | None = None) -> Playlist:
    from .library import resolve_playlist_name

    songs = _parse_csv_songs(path.read_text(encoding="utf-8-sig"))
    playlist_name = resolve_playlist_name(explicit=name, fallback=path.stem)
    return Playlist(name=playlist_name, query=f"csv:{path.name}", songs=songs)


def load_playlist_file(path: Path, *, name: str | None = None) -> Playlist:
    suffix = path.suffix.lower()
    if suffix == ".json":
        pl = load_playlist(path)
        if name:
            from .library import resolve_playlist_name

            return Playlist(
                name=resolve_playlist_name(explicit=name),
                query=pl.query,
                songs=pl.songs,
            )
        return pl
    if suffix == ".csv":
        return load_playlist_from_csv(path, name=name)
    raise ValueError(f"Unsupported playlist format {suffix!r} — use .json or .csv")


def csv_files_in_dir(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise ValueError(f"{directory} is not a directory")
    files = sorted(p for p in directory.glob("*.csv") if p.is_file())
    if not files:
        raise ValueError(f"No .csv files found in {directory}")
    return files


def resolve_playlist_inputs(path: Path) -> list[Path]:
    """Return one or more playlist files. Directories expand to their .csv files."""
    path = path.expanduser()
    if path.is_dir():
        return csv_files_in_dir(path)
    if path.is_file():
        return [path]
    raise ValueError(f"{path} does not exist")
