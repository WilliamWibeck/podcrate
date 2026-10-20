from __future__ import annotations

import errno
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .library import LibraryIndex, list_playlist_records, sanitize_filename
from .playlist import Playlist, Song


class IpodReadOnlyError(OSError):
    """The iPod mount is read-only (usually a dirty FAT remount)."""

_CANDIDATE_ROOTS = [
    Path("/run/media"),
    Path("/media"),
    Path(f"/media/{Path.home().name}"),
]

_IPOD_MARKERS = (".rockbox", "iPod_Control")


def find_ipod_mounts() -> list[Path]:
    """Look for mounted volumes that look like a Rockbox/iPod device."""
    found = []
    seen = set()
    for root in _CANDIDATE_ROOTS:
        if not root.is_dir():
            continue
        for entry in root.glob("*"):
            candidates = [entry] if not entry.is_dir() else [entry, *entry.glob("*")]
            for cand in candidates:
                if cand in seen or not cand.is_dir():
                    continue
                seen.add(cand)
                if any((cand / marker).exists() for marker in _IPOD_MARKERS):
                    found.append(cand)
    return found


@dataclass
class SyncResult:
    playlist_name: str
    playlist_file: Path
    tracks_copied: list[Path] = field(default_factory=list)
    tracks_skipped: list[Path] = field(default_factory=list)
    missing: list[Song] = field(default_factory=list)


def ipod_block_device(ipod_root: Path) -> str:
    """Resolve the block device backing an iPod mount, or '' if unknown."""
    try:
        return subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", "--target", str(ipod_root)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def mount_is_readonly(ipod_root: Path) -> bool:
    try:
        opts = subprocess.run(
            ["findmnt", "-n", "-o", "OPTIONS", "--target", str(ipod_root)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return "ro" in opts.split(",")


def assert_ipod_writable(ipod_root: Path) -> None:
    if not mount_is_readonly(ipod_root):
        return
    device = ipod_block_device(ipod_root) or "/dev/sdX1"
    raise IpodReadOnlyError(
        f"{ipod_root} is mounted read-only. The FAT32 volume was not "
        "unmounted cleanly and the kernel remounted it after a filesystem error.\n\n"
        "Repair, remount, then sync:\n"
        f"  udisksctl unmount -b {device}\n"
        f"  sudo fsck.vfat -a {device}\n"
        f"  udisksctl mount -b {device}\n"
        "  podcrate sync"
    )


def _music_root(cfg: Config, ipod_root: Path) -> Path:
    return ipod_root / cfg.ipod_music_subdir


def _playlists_dir(cfg: Config, ipod_root: Path) -> Path:
    return ipod_root / cfg.ipod_playlists_subdir


def _track_dest(cfg: Config, ipod_root: Path, library_rel: Path) -> Path:
    return _music_root(cfg, ipod_root) / library_rel


def _m3u_entry(playlists_dir: Path, track_path: Path) -> str:
    rel = os.path.relpath(track_path, playlists_dir)
    return rel.replace("\\", "/")


def _write_m3u(
    path: Path,
    entries: list[tuple[str, str]],
    *,
    dry_run: bool,
) -> None:
    lines = ["#EXTM3U"]
    for display, rel in entries:
        lines.append(f"#EXTINF:-1,{display}")
        lines.append(rel)
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def sync_track(
    cfg: Config,
    ipod_root: Path,
    src: Path,
    *,
    dry_run: bool = False,
) -> tuple[Path, bool]:
    """Copy one library track to the iPod Artist/Album tree. Returns (dest, copied)."""
    library_rel = src.relative_to(cfg.library_dir)
    dest = _track_dest(cfg, ipod_root, library_rel)
    rel = dest.relative_to(ipod_root)
    if dest.exists() and dest.stat().st_size == src.stat().st_size:
        return rel, False
    if not dry_run:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        except OSError as exc:
            if exc.errno == errno.EROFS:
                assert_ipod_writable(ipod_root)
                raise IpodReadOnlyError(str(exc)) from exc
            raise
    return rel, True


def sync_playlist(
    cfg: Config,
    ipod_root: Path,
    playlist: Playlist,
    index: LibraryIndex,
    *,
    dry_run: bool = False,
) -> SyncResult:
    """Copy playlist tracks into Music/Artist/Album/ and write a top-level .m3u."""
    if not dry_run:
        assert_ipod_writable(ipod_root)
    playlists_dir = _playlists_dir(cfg, ipod_root)
    playlist_file = playlists_dir / f"{sanitize_filename(playlist.name)}.m3u"
    result = SyncResult(playlist_name=playlist.name, playlist_file=playlist_file)
    m3u_entries: list[tuple[str, str]] = []

    for song in playlist.songs:
        src = index.get(song.artist, song.title)
        if src is None:
            result.missing.append(song)
            continue

        rel, copied = sync_track(cfg, ipod_root, src, dry_run=dry_run)
        track_path = ipod_root / rel
        if copied:
            result.tracks_copied.append(rel)
        else:
            result.tracks_skipped.append(rel)

        m3u_entries.append((song.display(), _m3u_entry(playlists_dir, track_path)))

    if m3u_entries:
        _write_m3u(playlist_file, m3u_entries, dry_run=dry_run)

    return result


def sync_library(
    cfg: Config, ipod_root: Path, *, dry_run: bool = False
) -> tuple[list[Path], list[Path]]:
    """Copy every library track, playlist or not, so Rockbox's database can browse it.

    Returns (copied, skipped) paths relative to the iPod root.
    """
    if not dry_run:
        assert_ipod_writable(ipod_root)
    copied: list[Path] = []
    skipped: list[Path] = []
    for src in sorted(cfg.library_dir.rglob("*.mp3")):
        # is_file: artist folders can end in ".mp3" too (e.g. "daniel.mp3/")
        if not src.is_file() or any(
            part.startswith(".") for part in src.relative_to(cfg.library_dir).parts
        ):
            continue
        rel, did_copy = sync_track(cfg, ipod_root, src, dry_run=dry_run)
        (copied if did_copy else skipped).append(rel)
    return copied, skipped


def list_device_playlist_files(cfg: Config, ipod_root: Path) -> list[Path]:
    """Return playlist files under Playlists/ on the device."""
    playlists_dir = _playlists_dir(cfg, ipod_root)
    if not playlists_dir.is_dir():
        return []
    return sorted(
        path
        for path in playlists_dir.iterdir()
        if path.is_file() and not path.name.startswith(".")
    )


def expected_playlist_m3u_paths(cfg: Config, ipod_root: Path) -> set[Path]:
    playlists_dir = _playlists_dir(cfg, ipod_root)
    return {
        playlists_dir / f"{sanitize_filename(playlist.name)}.m3u"
        for playlist in list_playlist_records(cfg.library_dir)
    }


def cleanup_stale_playlists(
    cfg: Config, ipod_root: Path, *, dry_run: bool = False
) -> list[Path]:
    """Remove .m3u files on the iPod that no longer have a tracked playlist."""
    playlists_dir = _playlists_dir(cfg, ipod_root)
    if not playlists_dir.is_dir():
        return []

    expected = expected_playlist_m3u_paths(cfg, ipod_root)
    removed: list[Path] = []
    for path in sorted(playlists_dir.glob("*.m3u")):
        if path in expected:
            continue
        if not dry_run:
            path.unlink()
        removed.append(path)
    return removed


def sync_all_playlists(
    cfg: Config, ipod_root: Path, *, dry_run: bool = False, prune: bool = True
) -> tuple[list[SyncResult], list[Path]]:
    """Sync every tracked playlist: copy tracks + refresh .m3u files."""
    index = LibraryIndex(cfg.library_dir)
    playlists = list_playlist_records(cfg.library_dir)
    results = [
        sync_playlist(cfg, ipod_root, playlist, index, dry_run=dry_run)
        for playlist in playlists
    ]
    removed = cleanup_stale_playlists(cfg, ipod_root, dry_run=dry_run) if prune else []
    return results, removed


def notify(title: str, body: str) -> None:
    """Best-effort desktop notification; silently no-ops if unavailable."""
    try:
        subprocess.run(
            ["notify-send", "--app-name=podcrate", title, body],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        pass


def eject(ipod_root: Path) -> str:
    """Unmount and power off the device via udisksctl. Returns a status message."""
    try:
        device = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", "--target", str(ipod_root)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        return f"could not resolve block device for {ipod_root}: {exc}"

    if not device:
        return f"could not resolve block device for {ipod_root}"

    try:
        subprocess.run(
            ["udisksctl", "unmount", "-b", device], check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["udisksctl", "power-off", "-b", device], check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as exc:
        return f"eject failed: {exc.stderr.strip() if exc.stderr else exc}"

    return f"{device} safely ejected"
