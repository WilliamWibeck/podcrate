from __future__ import annotations

import re
import sys

import questionary

from rich.console import Console
from rich.table import Table

from .playlist import Song

console = Console()

_RANGE_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def _parse_selection(raw: str, n: int) -> set[int] | None:
    """Parse '2,5-7' into a 0-indexed set of toggle targets. None if unparsable."""
    indices: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        m = _RANGE_RE.match(part)
        if not m:
            return None
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        for i in range(lo, hi + 1):
            if 1 <= i <= n:
                indices.add(i - 1)
    return indices


def render_songs(songs: list[Song], selected: list[bool], title: str) -> None:
    table = Table(title=title)
    table.add_column("", width=1)
    table.add_column("#", justify="right")
    table.add_column("Artist")
    table.add_column("Title")
    table.add_column("Why")
    for i, (song, on) in enumerate(zip(songs, selected), 1):
        mark = "[green]✓[/green]" if on else "[dim]✗[/dim]"
        table.add_row(
            mark, str(i), song.artist, song.title, song.reason,
            style=None if on else "dim",
        )
    console.print(table)


def pick(message: str, labels: list[str], *, checked: bool) -> list[int]:
    """Arrow-key checkbox list; returns the 0-indexed picks. Ctrl-C raises KeyboardInterrupt."""
    choices = [questionary.Choice(label, value=i, checked=checked) for i, label in enumerate(labels)]
    return questionary.checkbox(
        message,
        choices=choices,
        instruction="(↑/↓ move · space toggle · a all · i invert · enter confirm)",
    ).unsafe_ask()


def review_songs(songs: list[Song], title: str = "Playlist") -> list[Song]:
    """Let the user toggle songs on/off with arrow keys; returns the kept subset.

    No-ops (keeps everything) when stdin isn't a real terminal, so scripted
    usage isn't left waiting on input that will never come.
    """
    if not songs or not sys.stdin.isatty():
        return songs

    labels = [
        f"{s.display()}  — {s.reason}" if s.reason else s.display() for s in songs
    ]
    picks = set(pick(title, labels, checked=True))
    kept = [s for i, s in enumerate(songs) if i in picks]
    console.print(f"[green]{len(kept)}/{len(songs)} songs kept.[/green]")
    return kept
