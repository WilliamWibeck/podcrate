"""Artist discographies from MusicBrainz (free, no API key)."""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .playlist import Song

_API = "https://musicbrainz.org/ws/2"
# MusicBrainz asks every client to identify itself and stay under 1 request/second.
_USER_AGENT = "podcrate/0.1 (https://github.com/WilliamWibeck/podcrate)"
_SKIP_SECONDARY = {"Live", "Compilation", "Remix", "DJ-mix", "Demo", "Interview"}


class MusicBrainzError(Exception):
    pass


@dataclass
class Album:
    title: str
    kind: str  # "Album" | "EP"
    year: str
    songs: list[Song] = field(default_factory=list)  # in track order

    def label(self) -> str:
        return f"{self.year or '????'}  {self.title}  ({self.kind}, {len(self.songs)} tracks)"


def _get(path: str, **params: str) -> dict:
    url = f"{_API}/{path}?{urllib.parse.urlencode({**params, 'fmt': 'json'})}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except Exception as exc:
        raise MusicBrainzError(f"MusicBrainz: {exc}") from exc


def find_artist(name: str) -> dict:
    """Best match for name: {id, name, disambiguation}."""
    artists = _get("artist/", query=f'artist:"{name}"', limit="1").get("artists") or []
    if not artists:
        raise MusicBrainzError(f"no artist called '{name}' on MusicBrainz")
    return artists[0]


def _albums_from_releases(artist_name: str, releases: list[dict]) -> list[Album]:
    """One Album per release group, using its shortest official release (the standard edition)."""
    best: dict[str, dict] = {}
    for r in releases:
        group = r.get("release-group") or {}
        if _SKIP_SECONDARY & set(group.get("secondary-types") or []):
            continue
        r["_tracks"] = [t for m in r.get("media") or [] for t in m.get("tracks") or []]
        if not r["_tracks"]:
            continue
        current = best.get(group["id"])
        if current is None or len(r["_tracks"]) < len(current["_tracks"]):
            best[group["id"]] = r

    albums = []
    for r in best.values():
        group = r["release-group"]
        album = Album(
            title=group.get("title") or r["title"],
            kind=group.get("primary-type") or "Album",
            year=(group.get("first-release-date") or r.get("date") or "")[:4],
        )
        album.songs = [
            Song(artist=artist_name, title=t["title"], album=album.title) for t in r["_tracks"]
        ]
        albums.append(album)
    return sorted(albums, key=lambda a: a.year or "9999")


def list_albums(artist: dict) -> list[Album]:
    """Official albums and EPs, oldest first. Skips live albums, compilations and remixes."""
    releases: list[dict] = []
    while True:
        page = _get(
            "release",
            artist=artist["id"],
            type="album|ep",
            status="official",
            inc="recordings+release-groups",
            limit="100",
            offset=str(len(releases)),
        )
        releases.extend(page.get("releases") or [])
        if not page.get("releases") or len(releases) >= page.get("release-count", 0):
            break
        time.sleep(1)
    return _albums_from_releases(artist["name"], releases)


if __name__ == "__main__":
    def rel(group: str, n: int, secondary: list[str] | None = None) -> dict:
        return {
            "title": group,
            "release-group": {"id": group, "title": group, "primary-type": "Album",
                              "secondary-types": secondary or [], "first-release-date": "2019-07-26"},
            "media": [{"tracks": [{"title": f"t{i}"} for i in range(n)]}],
        }

    albums = _albums_from_releases("NF", [rel("The Search", 20), rel("The Search", 19), rel("Live", 9, ["Live"])])
    assert [a.title for a in albums] == ["The Search"], albums
    assert len(albums[0].songs) == 19 and albums[0].songs[0] == Song("NF", "t0", album="The Search")
    print("ok")
