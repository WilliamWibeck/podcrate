from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from mutagen.mp3 import EasyMP3

from .playlist import Playlist, Song, load_playlist, save_playlist

INDEX_FILENAME = ".podcrate-index.json"
PLAYLISTS_DIRNAME = ".playlists"
UNKNOWN_ALBUM = "Singles"

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')
_WHITESPACE = re.compile(r"\s+")
_FILLER_SUFFIXES = re.compile(
    r"(?:"
    r"[,.\s]+(?:artists?\s+like|for\s+example|such\s+as|and\s+similar|with\s+artists?|including|e\.g\.)\b.*"
    r")$",
    re.IGNORECASE,
)
_DEFAULT_MAX_NAME_LENGTH = 48


def sanitize_filename(name: str) -> str:
    cleaned = _INVALID_CHARS.sub("", name).strip()
    cleaned = _WHITESPACE.sub(" ", cleaned)
    return cleaned or "Unknown"


def normalize_playlist_name(name: str, *, max_length: int | None = _DEFAULT_MAX_NAME_LENGTH) -> str:
    """Turn a prompt, filename, or raw label into a short readable playlist name."""
    cleaned = name.strip().replace("_", " ")
    cleaned = _WHITESPACE.sub(" ", cleaned)
    cleaned = _FILLER_SUFFIXES.sub("", cleaned).strip()
    cleaned = cleaned.rstrip(" ,.;:-")

    if max_length and len(cleaned) > max_length and "," in cleaned:
        first = cleaned.split(",", 1)[0].strip()
        if len(first) >= 10:
            cleaned = first

    if max_length and len(cleaned) > 32 and "." in cleaned:
        cleaned = cleaned.split(".", 1)[0].strip()

    cleaned = sanitize_filename(cleaned)

    if max_length and len(cleaned) > max_length:
        truncated = cleaned[:max_length].rsplit(" ", 1)[0]
        cleaned = truncated if len(truncated) >= max_length // 2 else cleaned[:max_length]

    return cleaned or "Unknown"


def resolve_playlist_name(
    *,
    explicit: str | None = None,
    suggested: str | None = None,
    fallback: str | None = None,
) -> str:
    """Pick a playlist name, preferring explicit --name, then Gemini, then fallback."""
    if explicit:
        return normalize_playlist_name(explicit, max_length=None)
    if suggested and suggested.strip():
        return normalize_playlist_name(suggested.strip())
    if fallback and fallback.strip():
        return normalize_playlist_name(fallback.strip())
    return "Playlist"


def song_key(artist: str, title: str) -> str:
    def norm(s: str) -> str:
        s = s.lower()
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return _WHITESPACE.sub(" ", s).strip()

    return f"{norm(artist)}|||{norm(title)}"


def album_for(song: Song, *, fallback: str | None = None) -> str:
    album = song.album.strip()
    if album:
        return sanitize_filename(album)
    if fallback:
        return sanitize_filename(fallback)
    return UNKNOWN_ALBUM


def track_filename(song: Song) -> str:
    return f"{sanitize_filename(song.artist)} - {sanitize_filename(song.title)}.mp3"


def target_path(library_dir: Path, song: Song, *, album: str | None = None) -> Path:
    artist_dir = library_dir / sanitize_filename(song.artist)
    album_dir = artist_dir / album_for(song, fallback=album)
    return album_dir / track_filename(song)


class LibraryIndex:
    """Tracks artist/title -> file path so repeat fetches skip work already done."""

    def __init__(self, library_dir: Path):
        self.library_dir = library_dir
        self.path = library_dir / INDEX_FILENAME
        self._data: dict[str, str] = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text())

    def get(self, artist: str, title: str) -> Path | None:
        rel = self._data.get(song_key(artist, title))
        if rel is None:
            return None
        candidate = self.library_dir / rel
        return candidate if candidate.exists() else None

    def add(self, artist: str, title: str, file_path: Path) -> None:
        rel = file_path.relative_to(self.library_dir).as_posix()
        self._data[song_key(artist, title)] = rel
        self.save()

    def save(self) -> None:
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))


def _playlists_dir(library_dir: Path) -> Path:
    return library_dir / PLAYLISTS_DIRNAME


def playlist_record_path(library_dir: Path, name: str) -> Path:
    return _playlists_dir(library_dir) / f"{sanitize_filename(name)}.json"


def playlist_song_keys(playlist: Playlist) -> frozenset[str]:
    return frozenset(song_key(s.artist, s.title) for s in playlist.songs)


@dataclass(frozen=True)
class PlaylistDuplicate:
    kind: str  # "name_collision" | "identical_songs"
    playlist: str
    other: str
    detail: str


def find_playlist_duplicates(
    library_dir: Path,
    candidate: Playlist,
) -> list[PlaylistDuplicate]:
    """Find existing playlists that collide with candidate by filename or track list."""
    duplicates: list[PlaylistDuplicate] = []
    candidate_keys = playlist_song_keys(candidate)
    candidate_path = playlist_record_path(library_dir, candidate.name)
    colliding_name: str | None = None

    if candidate_path.exists():
        existing = load_playlist(candidate_path)
        if existing.name != candidate.name:
            colliding_name = existing.name
            duplicates.append(
                PlaylistDuplicate(
                    kind="name_collision",
                    playlist=candidate.name,
                    other=existing.name,
                    detail=f"both save to {candidate_path.name}",
                )
            )

    for other in list_playlist_records(library_dir):
        if other.name == candidate.name:
            continue
        if colliding_name and other.name == colliding_name:
            continue
        other_keys = playlist_song_keys(other)
        if candidate_keys and candidate_keys == other_keys:
            duplicates.append(
                PlaylistDuplicate(
                    kind="identical_songs",
                    playlist=candidate.name,
                    other=other.name,
                    detail=f"{len(candidate_keys)} identical tracks",
                )
            )

    return duplicates


def find_all_playlist_duplicates(library_dir: Path) -> list[PlaylistDuplicate]:
    """Find duplicate pairs among all tracked playlists."""
    duplicates: list[PlaylistDuplicate] = []
    playlists = list_playlist_records(library_dir)
    seen_song_pairs: set[tuple[str, str]] = set()
    seen_name_pairs: set[tuple[str, str]] = set()

    by_sanitized: dict[str, list[Playlist]] = {}
    for pl in playlists:
        by_sanitized.setdefault(sanitize_filename(pl.name), []).append(pl)

    for sanitized, group in by_sanitized.items():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                pair = tuple(sorted((a.name, b.name)))
                if pair in seen_name_pairs:
                    continue
                seen_name_pairs.add(pair)
                duplicates.append(
                    PlaylistDuplicate(
                        kind="name_collision",
                        playlist=a.name,
                        other=b.name,
                        detail=f"both map to {sanitized}.json",
                    )
                )

    for i, a in enumerate(playlists):
        a_keys = playlist_song_keys(a)
        for b in playlists[i + 1 :]:
            b_keys = playlist_song_keys(b)
            if not a_keys or a_keys != b_keys:
                continue
            pair = tuple(sorted((a.name, b.name)))
            if pair in seen_song_pairs:
                continue
            seen_song_pairs.add(pair)
            duplicates.append(
                PlaylistDuplicate(
                    kind="identical_songs",
                    playlist=a.name,
                    other=b.name,
                    detail=f"{len(a_keys)} identical tracks",
                )
            )

    return duplicates


@dataclass(frozen=True)
class PlaylistRenamePlan:
    old_name: str
    new_name: str
    old_path: Path
    new_path: Path
    action: str  # "rename" | "update" | "remove"
    detail: str = ""


def _dedupe_name(
    base: str,
    *,
    taken: set[str],
    library_dir: Path,
    keep_path: Path | None,
) -> str:
    candidate = base
    suffix = 2
    while True:
        path = playlist_record_path(library_dir, candidate)
        if candidate not in taken and (path == keep_path or not path.exists()):
            return candidate
        candidate = f"{base} ({suffix})"
        suffix += 1


def plan_playlist_renames(library_dir: Path) -> list[PlaylistRenamePlan]:
    """Plan readable renames for tracked playlists, merging identical track lists."""
    playlists_dir = _playlists_dir(library_dir)
    if not playlists_dir.is_dir():
        return []

    entries = [(path, load_playlist(path)) for path in sorted(playlists_dir.glob("*.json"))]
    plans: list[PlaylistRenamePlan] = []
    removed_paths: set[Path] = set()
    taken_names: set[str] = set()

    by_songs: dict[frozenset[str], list[tuple[Path, Playlist]]] = {}
    for path, pl in entries:
        by_songs.setdefault(playlist_song_keys(pl), []).append((path, pl))

    survivors: list[tuple[Path, Playlist]] = []
    for group in by_songs.values():
        if len(group) == 1:
            survivors.append(group[0])
            continue

        def sort_key(item: tuple[Path, Playlist]) -> tuple[int, int, str]:
            path, pl = item
            normalized = normalize_playlist_name(pl.name)
            return (
                0 if normalized == pl.name else 1,
                len(pl.name),
                pl.name.lower(),
            )

        keep_path, keep_pl = min(group, key=sort_key)
        survivors.append((keep_path, keep_pl))
        for path, pl in group:
            if path == keep_path:
                continue
            plans.append(
                PlaylistRenamePlan(
                    old_name=pl.name,
                    new_name=keep_pl.name,
                    old_path=path,
                    new_path=playlist_record_path(library_dir, keep_pl.name),
                    action="remove",
                    detail=f"duplicate of '{keep_pl.name}' ({len(pl.songs)} tracks)",
                )
            )
            removed_paths.add(path)

    for path, pl in survivors:
        if path in removed_paths:
            continue

        new_name = normalize_playlist_name(pl.name)
        new_path = playlist_record_path(library_dir, new_name)
        final_name = _dedupe_name(
            new_name,
            taken=taken_names,
            library_dir=library_dir,
            keep_path=path if new_path == path else None,
        )
        final_path = playlist_record_path(library_dir, final_name)
        taken_names.add(final_name)

        if final_name == pl.name and final_path == path:
            continue

        action = "update" if final_path == path else "rename"
        detail = ""
        if final_name != new_name:
            detail = f"name collision, using '{final_name}'"
        plans.append(
            PlaylistRenamePlan(
                old_name=pl.name,
                new_name=final_name,
                old_path=path,
                new_path=final_path,
                action=action,
                detail=detail,
            )
        )

    return plans


def apply_playlist_renames(
    library_dir: Path,
    plans: list[PlaylistRenamePlan],
    *,
    dry_run: bool = False,
) -> list[PlaylistRenamePlan]:
    """Apply rename/remove plans to playlist records on disk."""
    applied: list[PlaylistRenamePlan] = []

    for plan in plans:
        if plan.action == "remove":
            if not dry_run:
                plan.old_path.unlink(missing_ok=True)
            applied.append(plan)
            continue

        pl = load_playlist(plan.old_path)
        updated = Playlist(name=plan.new_name, query=pl.query, songs=pl.songs)
        if not dry_run:
            save_playlist(updated, plan.new_path)
            if plan.new_path != plan.old_path:
                plan.old_path.unlink(missing_ok=True)
        applied.append(plan)

    return applied


def save_playlist_record(library_dir: Path, playlist: Playlist) -> Path:
    """Persist a playlist definition so it's tracked for future syncs,
    regardless of whether the caller also wrote an external --save copy."""
    path = playlist_record_path(library_dir, playlist.name)
    save_playlist(playlist, path)
    return path


def list_playlist_records(library_dir: Path) -> list[Playlist]:
    playlists_dir = _playlists_dir(library_dir)
    if not playlists_dir.is_dir():
        return []
    return [load_playlist(p) for p in sorted(playlists_dir.glob("*.json"))]


def get_playlist_record(library_dir: Path, name: str) -> Playlist | None:
    path = playlist_record_path(library_dir, name)
    return load_playlist(path) if path.exists() else None


def resolve_playlist_record(library_dir: Path, name: str) -> Playlist:
    """Look up a tracked playlist by exact, case-insensitive, or sanitized name."""
    pl = get_playlist_record(library_dir, name)
    if pl is not None:
        return pl

    needle = name.casefold()
    matches = [
        candidate
        for candidate in list_playlist_records(library_dir)
        if candidate.name.casefold() == needle
    ]
    if len(matches) == 1:
        return matches[0]

    sanitized = sanitize_filename(name).casefold()
    matches = [
        candidate
        for candidate in list_playlist_records(library_dir)
        if sanitize_filename(candidate.name).casefold() == sanitized
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(f"'{candidate.name}'" for candidate in matches)
        raise ValueError(f"Ambiguous playlist name {name!r}: {names}")

    raise ValueError(f"No tracked playlist named {name!r}")


def dedupe_songs(songs: list[Song]) -> list[Song]:
    seen: set[str] = set()
    out: list[Song] = []
    for song in songs:
        key = song_key(song.artist, song.title)
        if key in seen:
            continue
        seen.add(key)
        out.append(song)
    return out


def merge_songs(*groups: list[Song]) -> list[Song]:
    out: list[Song] = []
    seen: set[str] = set()
    for group in groups:
        for song in group:
            key = song_key(song.artist, song.title)
            if key in seen:
                continue
            seen.add(key)
            out.append(song)
    return out


def rename_playlist(
    library_dir: Path, old_name: str, new_name: str
) -> tuple[Playlist, str]:
    """Rename a tracked playlist record. Returns (updated, previous_name)."""
    source = resolve_playlist_record(library_dir, old_name)
    previous = source.name
    target_name = resolve_playlist_name(explicit=new_name)
    if not target_name or target_name == "Unknown":
        raise ValueError(f"Invalid playlist name {new_name!r}")

    if target_name == previous:
        return source, previous

    source_path = playlist_record_path(library_dir, previous)
    dest_path = playlist_record_path(library_dir, target_name)
    if dest_path.exists() and dest_path != source_path:
        existing = load_playlist(dest_path)
        raise ValueError(f"A playlist named '{existing.name}' already exists")

    updated = Playlist(name=target_name, query=source.query, songs=source.songs)
    save_playlist_record(library_dir, updated)
    if dest_path != source_path:
        source_path.unlink(missing_ok=True)
    return updated, previous


def delete_playlist_record(library_dir: Path, name: str) -> Path:
    pl = resolve_playlist_record(library_dir, name)
    path = playlist_record_path(library_dir, pl.name)
    path.unlink()
    return path


def load_songs_from_source(library_dir: Path, source: str) -> tuple[str, list[Song]]:
    """Load songs from a tracked playlist name or a .json/.csv file or folder."""
    from .playlist import csv_files_in_dir, is_youtube_url, load_playlist_file

    if is_youtube_url(source):
        from .config import Config
        from .fetch import FetchError, load_youtube_playlist

        try:
            playlist = load_youtube_playlist(source, Config.load())
        except FetchError as exc:
            raise ValueError(str(exc)) from exc
        return playlist.name, playlist.songs

    from .spotify import is_spotify_url, load_spotify_playlist

    if is_spotify_url(source):
        from .config import Config

        playlist = load_spotify_playlist(source, Config.load())
        return playlist.name, playlist.songs

    path = Path(source).expanduser()
    if path.exists():
        if path.is_dir():
            songs: list[Song] = []
            for csv_path in csv_files_in_dir(path):
                songs.extend(load_playlist_file(csv_path).songs)
            return path.name, dedupe_songs(songs)
        playlist = load_playlist_file(path)
        return playlist.name, playlist.songs

    playlist = resolve_playlist_record(library_dir, source)
    return playlist.name, playlist.songs


def add_songs_to_playlist(
    library_dir: Path,
    target_name: str,
    sources: list[str],
) -> tuple[Playlist, int]:
    """Append songs from sources to a tracked playlist. Returns playlist and count added."""
    target = resolve_playlist_record(library_dir, target_name)
    existing_keys = set(playlist_song_keys(target))
    added: list[Song] = []

    for source in sources:
        _, songs = load_songs_from_source(library_dir, source)
        for song in songs:
            key = song_key(song.artist, song.title)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            added.append(song)

    if not added:
        return target, 0

    updated = Playlist(
        name=target.name,
        query=target.query,
        songs=[*target.songs, *added],
    )
    save_playlist_record(library_dir, updated)
    return updated, len(added)


def remove_songs_from_playlist(
    library_dir: Path,
    target_name: str,
    *,
    indices: set[int] | None = None,
    song_specs: list[str] | None = None,
) -> tuple[Playlist, int]:
    """Remove songs by 0-based indices and/or 'Artist - Title' specs."""
    target = resolve_playlist_record(library_dir, target_name)
    remove_keys: set[str] = set()

    if indices:
        for idx in indices:
            if 0 <= idx < len(target.songs):
                song = target.songs[idx]
                remove_keys.add(song_key(song.artist, song.title))

    for spec in song_specs or []:
        spec = spec.strip()
        if not spec:
            continue
        if " - " in spec:
            artist, title = spec.split(" - ", 1)
            remove_keys.add(song_key(artist.strip(), title.strip()))
            continue
        needle = spec.casefold()
        for song in target.songs:
            if song.title.casefold() == needle or song.display().casefold() == needle:
                remove_keys.add(song_key(song.artist, song.title))

    if not remove_keys:
        return target, 0

    kept = [
        song
        for song in target.songs
        if song_key(song.artist, song.title) not in remove_keys
    ]
    removed = len(target.songs) - len(kept)
    if removed == 0:
        return target, 0

    updated = Playlist(name=target.name, query=target.query, songs=kept)
    save_playlist_record(library_dir, updated)
    return updated, removed


def combine_playlists(
    library_dir: Path,
    target_name: str,
    sources: list[str],
    *,
    remove_sources: bool = True,
) -> tuple[Playlist, list[str]]:
    """Merge sources into target_name, deduping by artist/title."""
    target_name = resolve_playlist_name(explicit=target_name)
    existing = get_playlist_record(library_dir, target_name)

    source_groups: list[list[Song]] = []
    source_labels: list[str] = []
    for source in sources:
        label, songs = load_songs_from_source(library_dir, source)
        if existing and label == existing.name:
            continue
        source_groups.append(songs)
        source_labels.append(label)

    base = existing.songs if existing else []
    merged_songs = merge_songs(base, *source_groups)
    query_parts = [existing.query] if existing and existing.query else []
    query_parts.extend(f"combined:{label}" for label in source_labels)
    query = " | ".join(part for part in query_parts if part) or f"combined:{target_name}"

    updated = Playlist(name=target_name, query=query, songs=merged_songs)
    save_playlist_record(library_dir, updated)

    removed: list[str] = []
    if remove_sources:
        for label in source_labels:
            path = playlist_record_path(library_dir, label)
            if path.exists() and label != target_name:
                path.unlink()
                removed.append(label)

    return updated, removed


def _read_tags(path: Path) -> tuple[str | None, str | None, str | None]:
    try:
        audio = EasyMP3(path)
        artist = (audio.get("artist") or [None])[0]
        title = (audio.get("title") or [None])[0]
        album = (audio.get("album") or [None])[0]
    except Exception:
        artist = title = album = None
    if artist and title:
        return artist, title, album
    stem = path.stem
    if " - " in stem:
        a, t = stem.split(" - ", 1)
        return a.strip() or None, t.strip() or None, album
    return None, None, album


def read_folder_songs(folder: Path) -> list[Song]:
    """Read artist/title/album for mp3s under folder (recursive)."""
    songs = []
    for path in sorted(folder.rglob("*.mp3")):
        artist, title, album = _read_tags(path)
        if artist and title:
            songs.append(
                Song(
                    artist=artist,
                    title=title,
                    album=album or "",
                )
            )
    return songs
