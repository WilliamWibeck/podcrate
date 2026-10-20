"""Listening stats from Rockbox's playback log (.rockbox/playback.log).

Enable on the device: Settings > Playback Settings > Logging > Yes.
Each line is `timestamp:elapsed_ms:length_ms:path`; lines starting with `#` are comments.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

LOG_RELPATH = Path(".rockbox") / "playback.log"


@dataclass(frozen=True)
class Play:
    timestamp: int
    elapsed_ms: int
    length_ms: int
    path: str

    @property
    def track(self) -> str:
        return PurePosixPath(self.path).stem

    @property
    def artist(self) -> str:
        # Music/Artist/Album/Track.mp3 layout written by sync
        parts = PurePosixPath(self.path).parts
        return parts[-3] if len(parts) >= 3 else "Unknown"

    @property
    def completed(self) -> bool:
        # Last.fm rule: half the track counts as a listen
        return self.length_ms > 0 and self.elapsed_ms * 2 >= self.length_ms


def parse_log(text: str) -> list[Play]:
    plays: list[Play] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(":", 3)  # path may contain ':'
        if len(fields) != 4:
            continue
        try:
            ts, elapsed, length = (int(f) for f in fields[:3])
        except ValueError:
            continue
        plays.append(Play(ts, elapsed, length, fields[3]))
    return plays


def read_plays(ipod_root: Path) -> list[Play] | None:
    path = ipod_root / LOG_RELPATH
    if not path.is_file():
        return None
    return parse_log(path.read_text(errors="replace"))


@dataclass(frozen=True)
class Summary:
    plays: int
    skips: int
    listened_ms: int
    top_tracks: list[tuple[str, int]]
    top_artists: list[tuple[str, int]]


def summarize(plays: list[Play], top: int = 10) -> Summary:
    listens = [p for p in plays if p.completed]
    return Summary(
        plays=len(listens),
        skips=len(plays) - len(listens),
        listened_ms=sum(min(p.elapsed_ms, p.length_ms) for p in plays),
        top_tracks=Counter(p.track for p in listens).most_common(top),
        top_artists=Counter(p.artist for p in listens).most_common(top),
    )


if __name__ == "__main__":
    sample = """\
# Rockbox playback log
1700000000:200000:200000:/Music/Boards of Canada/Geogaddi/Boards of Canada - Dawn Chorus.mp3
1700000300:10000:240000:/Music/Air/Moon Safari/Air - La femme d'argent.mp3
1700000600:150000:200000:/Music/Boards of Canada/Geogaddi/Boards of Canada - Dawn Chorus.mp3
garbage line
1700000900:90000:180000:/Music/A:B/Album/A:B - Colon.mp3
"""
    s = summarize(parse_log(sample))
    assert (s.plays, s.skips) == (3, 1), s
    assert s.top_tracks[0] == ("Boards of Canada - Dawn Chorus", 2), s
    assert s.top_artists[0] == ("Boards of Canada", 2), s
    assert ("A:B", 1) in s.top_artists, s
    assert s.listened_ms == 200000 + 10000 + 150000 + 90000
    print("ok")
