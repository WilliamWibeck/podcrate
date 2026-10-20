from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import questionary
import typer
from mutagen.mp3 import EasyMP3
from rich.console import Console

from . import fetch as fetch_mod
from . import musicbrainz
from . import stats as stats_mod
from . import transfer as transfer_mod
from .config import Config
from .interactive import pick, render_songs, review_songs, _parse_selection
from .library import (
    LibraryIndex,
    PlaylistDuplicate,
    add_songs_to_playlist,
    apply_playlist_renames,
    combine_playlists,
    delete_playlist_record,
    rename_playlist,
    find_all_playlist_duplicates,
    find_playlist_duplicates,
    get_playlist_record,
    list_playlist_records,
    plan_playlist_renames,
    remove_songs_from_playlist,
    resolve_playlist_record,
    read_folder_songs,
    resolve_playlist_name,
    sanitize_filename,
    save_playlist_record,
    song_key,
)
from .spotify import EMBED_LIMIT, SpotifyError, is_spotify_url, list_my_playlists, load_spotify_playlist
from .playlist import (
    Playlist,
    Song,
    generate_playlist,
    generate_similar_songs,
    is_youtube_url,
    load_playlist_file,
    resolve_playlist_inputs,
    save_playlist,
)

app = typer.Typer(
    help="Generate AI playlists, fetch the audio, and sync them to a Rockbox iPod. "
    "Run with no command for an interactive menu.",
)
playlists_app = typer.Typer(
    help="Manage tracked playlists: list, add songs, remove songs, combine.",
    no_args_is_help=True,
)
app.add_typer(playlists_app, name="playlists")
console = Console()


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context):
    if ctx.invoked_subcommand is not None:
        return
    if not sys.stdin.isatty():
        console.print(ctx.get_help())
        raise typer.Exit()
    from .menu import run_menu

    run_menu(app)

_STATE_FILE = (
    Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "podcrate-autosync-state"
)


def _guard_writable(root: Path) -> None:
    try:
        transfer_mod.assert_ipod_writable(root)
    except transfer_mod.IpodReadOnlyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


def _resolve_ipod(ipod: Optional[str]) -> Path:
    if ipod:
        root = Path(ipod).expanduser()
        if not root.is_dir():
            console.print(f"[red]{root} is not a directory.[/red]")
            raise typer.Exit(1)
        return root

    mounts = transfer_mod.find_ipod_mounts()
    if not mounts:
        console.print(
            "[red]No Rockbox/iPod device found under /run/media or /media.[/red]\n"
            "Plug it in and make sure it's mounted, or pass --ipod /path/to/mount."
        )
        raise typer.Exit(1)
    if len(mounts) > 1:
        console.print(f"[yellow]Multiple candidate devices found, using {mounts[0]}[/yellow]")
    return mounts[0]


def _warn_playlist_duplicates(duplicates: list[PlaylistDuplicate]) -> None:
    for dup in duplicates:
        if dup.kind == "name_collision":
            console.print(
                f"[yellow]Duplicate playlist name:[/yellow] '{dup.playlist}' "
                f"collides with '{dup.other}' ({dup.detail})"
            )
        else:
            console.print(
                f"[yellow]Duplicate playlist:[/yellow] '{dup.playlist}' has the same "
                f"tracks as '{dup.other}' ({dup.detail})"
            )


def _save_playlist_record(cfg: Config, playlist: Playlist) -> Path:
    duplicates = find_playlist_duplicates(cfg.library_dir, playlist)
    if duplicates:
        _warn_playlist_duplicates(duplicates)
    return save_playlist_record(cfg.library_dir, playlist)


def _print_sync_result(result: transfer_mod.SyncResult, ipod_root: Path) -> None:
    playlist_rel = result.playlist_file.relative_to(ipod_root)
    console.print(
        f"[bold]{result.playlist_name}[/bold]: "
        f"[green]{len(result.tracks_copied)} tracks copied[/green], "
        f"{len(result.tracks_skipped)} already on device, "
        f"playlist -> {playlist_rel}"
    )
    for rel in result.tracks_copied:
        console.print(f"  + {rel}")
    if result.missing:
        console.print(
            f"  [yellow]{len(result.missing)} not fetched yet:[/yellow] "
            + ", ".join(s.display() for s in result.missing)
        )


@app.command()
def playlist(
    query: str = typer.Argument(..., help="e.g. 'shoegaze' or 'similar to Loveless by MBV'"),
    count: int = typer.Option(15, "--count", "-n", help="Number of songs to generate"),
    name: Optional[str] = typer.Option(
        None, "--name", help="Playlist name (used as the folder name on the iPod); defaults to the query"
    ),
    save: Optional[Path] = typer.Option(None, "--save", help="Also save playlist JSON to this path"),
    review: bool = typer.Option(
        True, "--review/--no-review", help="Interactively toggle songs before saving"
    ),
):
    """Ask Gemini to curate a playlist, then let you toggle songs before saving."""
    cfg = Config.load()
    with console.status(f"Asking Gemini for {count} songs matching '{query}'..."):
        songs, suggested_name = generate_playlist(query, count, cfg)

    title = f"Playlist: {query}"
    if review:
        songs = review_songs(songs, title)
    else:
        render_songs(songs, [True] * len(songs), title)

    if not songs:
        console.print("[yellow]No songs selected — nothing to save.[/yellow]")
        raise typer.Exit(0)

    pl = Playlist(
        name=resolve_playlist_name(explicit=name, suggested=suggested_name, fallback=query),
        query=query,
        songs=songs,
    )
    record_path = _save_playlist_record(cfg, pl)
    console.print(f"[green]Saved playlist '{pl.name}' ({len(songs)} songs) -> {record_path}[/green]")

    if save:
        save_playlist(pl, save)
        console.print(f"[green]Also saved to {save}[/green]")


def _fetch_playlist(
    pl: Playlist, cfg: Config, index: LibraryIndex, *, force: bool
) -> tuple[int, int]:
    ok, failed = 0, 0
    for song in pl.songs:
        with console.status(f"Fetching {song.display()}..."):
            try:
                path = fetch_mod.fetch_song(song, cfg, index, force=force)
            except fetch_mod.FetchError as exc:
                console.print(f"[red]FAILED[/red] {song.display()}: {exc}")
                failed += 1
                continue
        console.print(f"[green]ok[/green] {song.display()} -> {path}")
        ok += 1

    _save_playlist_record(cfg, pl)
    _sync_if_plugged_in(cfg, pl, index)
    return ok, failed


def _sync_if_plugged_in(cfg: Config, pl: Playlist, index: LibraryIndex) -> None:
    """Push a freshly fetched playlist to the iPod if one is mounted; silent otherwise.

    The autosync watcher only fires once per plug-in, so downloads made while
    the device is already connected would otherwise wait for a replug.
    """
    mounts = transfer_mod.find_ipod_mounts()
    if not mounts:
        return
    root = mounts[0]
    try:
        transfer_mod.assert_ipod_writable(root)
    except transfer_mod.IpodReadOnlyError as exc:
        console.print(f"[yellow]Not syncing '{pl.name}': {exc}[/yellow]")
        return
    with console.status(f"Syncing '{pl.name}' to {root}..."):
        result = transfer_mod.sync_playlist(cfg, root, pl, index)
    _print_sync_result(result, root)


def _fetch_from_spotify_account(cfg: Config, index: LibraryIndex, *, force: bool) -> None:
    with console.status("Listing your Spotify playlists..."):
        try:
            mine = list_my_playlists(cfg)
        except SpotifyError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
    if not mine:
        console.print("No playlists in your Spotify library.")
        return

    if not sys.stdin.isatty():
        console.print("[red]--spotify needs an interactive terminal; pass a playlist URL instead.[/red]")
        raise typer.Exit(1)
    def label(p: dict) -> str:
        # Followed playlists come from the public embed page, capped at EMBED_LIMIT.
        capped = not p["readable"] and p["total"] > EMBED_LIMIT
        note = f", first {EMBED_LIMIT} only" if capped else ""
        return f"{p['name']}  ({p['total']} songs, {p['owner']}{note})"

    labels = [label(p) for p in mine]
    several = "Select several…"
    choice = questionary.select(
        "Download which Spotify playlist?",
        choices=[several, *[questionary.Choice(label, value=i) for i, label in enumerate(labels)]],
        use_search_filter=True,
        use_jk_keys=False,
        instruction="(↑/↓, type to filter, enter downloads)",
    ).unsafe_ask()
    picks = pick("Download which Spotify playlists?", labels, checked=False) if choice == several else [choice]
    if not picks:
        console.print("Nothing selected.")
        return

    total_ok, total_failed = 0, 0
    for i in picks:
        try:
            pl = load_spotify_playlist(mine[i]["id"], cfg)
        except SpotifyError as exc:
            console.print(f"[red]{mine[i]['name']}: {exc}[/red]")
            continue
        console.print(f"\n[bold]Playlist: {pl.name}[/bold] ({len(pl.songs)} songs from Spotify)")
        ok, failed = _fetch_playlist(pl, cfg, index, force=force)
        total_ok += ok
        total_failed += failed
        console.print(f"[green]{ok} ready[/green], {failed} failed for '{pl.name}'")
    console.print(f"\n{total_ok} tracks ready, {total_failed} failed. Library: {cfg.library_dir}")


@app.command()
def fetch(
    source: Optional[str] = typer.Argument(
        None,
        help="Playlist file (.json/.csv), a folder of Spotify .csv exports, a YouTube URL, or a Spotify playlist URL",
    ),
    name: Optional[str] = typer.Option(
        None, "--name", help="Playlist name (single file or URL; folders use each CSV filename)"
    ),
    spotify: bool = typer.Option(
        False, "--spotify", help="Pick playlists from your Spotify account instead of passing SOURCE"
    ),
    force: bool = typer.Option(False, "--force", help="Re-download even if already in library"),
):
    """Download + tag every song in a playlist file, folder of CSVs, YouTube or Spotify playlist."""
    cfg = Config.load()
    index = LibraryIndex(cfg.library_dir)
    total_ok, total_failed, playlists = 0, 0, 0

    if spotify:
        if source:
            console.print("[red]Pass either SOURCE or --spotify, not both.[/red]")
            raise typer.Exit(1)
        _fetch_from_spotify_account(cfg, index, force=force)
        return
    if not source:
        console.print("[red]Missing SOURCE (or pass --spotify).[/red]")
        raise typer.Exit(1)

    if is_spotify_url(source):
        with console.status("Reading Spotify playlist..."):
            try:
                pl = load_spotify_playlist(source, cfg, name=name)
            except SpotifyError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1)
        console.print(
            f"\n[bold]Playlist: {pl.name}[/bold] ({len(pl.songs)} songs from Spotify)"
        )
        ok, failed = _fetch_playlist(pl, cfg, index, force=force)
        console.print(f"\n{ok} tracks ready, {failed} failed. Library: {cfg.library_dir}")
        return

    if is_youtube_url(source):
        with console.status("Reading YouTube playlist..."):
            try:
                pl = fetch_mod.load_youtube_playlist(source, cfg, name=name)
            except fetch_mod.FetchError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1)
        console.print(
            f"\n[bold]Playlist: {pl.name}[/bold] ({len(pl.songs)} songs from YouTube)"
        )
        ok, failed = _fetch_playlist(pl, cfg, index, force=force)
        console.print(f"[green]{ok} ready[/green], {failed} failed for '{pl.name}'")
        console.print(f"\n{ok} tracks ready, {failed} failed. Library: {cfg.library_dir}")
        return

    playlist_path = Path(source)
    try:
        inputs = resolve_playlist_inputs(playlist_path)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if len(inputs) > 1 and name:
        console.print("[red]--name can only be used with a single playlist file.[/red]")
        raise typer.Exit(1)

    for path in inputs:
        try:
            pl = load_playlist_file(path, name=name)
        except ValueError as exc:
            console.print(f"[red]{path.name}: {exc}[/red]")
            raise typer.Exit(1)

        console.print(f"\n[bold]Playlist: {pl.name}[/bold] ({len(pl.songs)} songs from {path.name})")
        ok, failed = _fetch_playlist(pl, cfg, index, force=force)
        total_ok += ok
        total_failed += failed
        playlists += 1
        console.print(
            f"[green]{ok} ready[/green], {failed} failed for '{pl.name}'"
        )

    summary = f"\n{total_ok} tracks ready, {total_failed} failed"
    if playlists > 1:
        summary += f" across {playlists} playlists"
    summary += f". Library: {cfg.library_dir}"
    console.print(summary)


@app.command()
def sync(
    playlist_name: Optional[str] = typer.Option(
        None, "--playlist", help="Only sync this playlist (by name); default is all tracked playlists"
    ),
    ipod: Optional[str] = typer.Option(None, "--ipod", help="Mount path (auto-detected if omitted)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be copied"),
    prune: bool = typer.Option(
        True,
        "--prune/--no-prune",
        help="Remove .m3u files on the iPod for playlists no longer tracked (default on full sync)",
    ),
    eject: bool = typer.Option(False, "--eject", help="Safely unmount after syncing"),
):
    """Copy library tracks to Music/Artist/Album/ and write .m3u files under Playlists/."""
    cfg = Config.load()
    root = _resolve_ipod(ipod)
    if not dry_run:
        _guard_writable(root)
    index = LibraryIndex(cfg.library_dir)

    if playlist_name:
        pl = get_playlist_record(cfg.library_dir, playlist_name)
        if pl is None:
            console.print(f"[red]No tracked playlist named '{playlist_name}'.[/red]")
            raise typer.Exit(1)
        playlists = [pl]
        do_prune = prune
    else:
        playlists = list_playlist_records(cfg.library_dir)
        do_prune = prune

    with console.status(f"Syncing {len(playlists)} playlist(s) to {root}..."):
        results = [
            transfer_mod.sync_playlist(cfg, root, pl, index, dry_run=dry_run)
            for pl in playlists
        ]
        removed = (
            transfer_mod.cleanup_stale_playlists(cfg, root, dry_run=dry_run)
            if do_prune
            else []
        )
        # Tracks outside any playlist (e.g. `podcrate artist`) still belong on the device.
        extra, _ = (
            ([], []) if playlist_name else transfer_mod.sync_library(cfg, root, dry_run=dry_run)
        )

    for result in results:
        _print_sync_result(result, root)
    if extra:
        console.print(f"[bold]Library[/bold]: [green]{len(extra)} more tracks copied[/green]")
        for rel in extra:
            console.print(f"  + {rel}")

    if removed:
        verb = "Would remove" if dry_run else "Removed"
        console.print(f"\n[yellow]{verb} {len(removed)} stale playlist(s) on device:[/yellow]")
        for path in removed:
            console.print(f"  - {path.relative_to(root)}")

    if eject and not dry_run:
        console.print(transfer_mod.eject(root))


@app.command()
def autosync():
    """Sync all playlists once per plug-in (run by the systemd watcher, not by hand).

    Debounces via a state file so the same plugged-in device isn't re-synced
    on every fs event, and resets once the device is unplugged.
    """
    cfg = Config.load()
    mounts = transfer_mod.find_ipod_mounts()

    if not mounts:
        _STATE_FILE.unlink(missing_ok=True)
        return

    root = mounts[0]
    _guard_writable(root)
    if _STATE_FILE.exists() and _STATE_FILE.read_text().strip() == str(root):
        return  # already handled this plug-in session

    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(str(root))

    results, _removed = transfer_mod.sync_all_playlists(cfg, root)
    extra, _ = transfer_mod.sync_library(cfg, root)
    total_copied = sum(len(r.tracks_copied) for r in results) + len(extra)
    if total_copied:
        transfer_mod.notify(
            "iPod synced", f"{total_copied} new track(s) copied to {root.name}"
        )
    else:
        transfer_mod.notify("iPod connected", f"{root.name} already up to date")


@app.command()
def run(
    query: str = typer.Argument(..., help="e.g. 'shoegaze' or 'similar to Loveless by MBV'"),
    count: int = typer.Option(15, "--count", "-n"),
    name: Optional[str] = typer.Option(
        None, "--name", help="Playlist name (used as the folder name on the iPod); defaults to the query"
    ),
    ipod: Optional[str] = typer.Option(None, "--ipod"),
    eject: bool = typer.Option(False, "--eject"),
    review: bool = typer.Option(
        True, "--review/--no-review", help="Interactively toggle songs before fetching"
    ),
):
    """One-shot: generate a playlist, fetch every song, and sync tracks + .m3u to the iPod."""
    cfg = Config.load()
    root = _resolve_ipod(ipod)  # fail fast before spending API/download time
    _guard_writable(root)

    with console.status(f"Asking Gemini for {count} songs matching '{query}'..."):
        songs, suggested_name = generate_playlist(query, count, cfg)

    if review:
        songs = review_songs(songs, f"Playlist: {query}")
    else:
        render_songs(songs, [True] * len(songs), f"Playlist: {query}")

    if not songs:
        console.print("[yellow]No songs selected — nothing to fetch.[/yellow]")
        raise typer.Exit(0)

    pl = Playlist(
        name=resolve_playlist_name(explicit=name, suggested=suggested_name, fallback=query),
        query=query,
        songs=songs,
    )

    index = LibraryIndex(cfg.library_dir)
    ok, failed = 0, 0
    for song in pl.songs:
        with console.status(f"Fetching {song.display()}..."):
            try:
                fetch_mod.fetch_song(song, cfg, index)
                ok += 1
            except fetch_mod.FetchError as exc:
                console.print(f"[red]FAILED[/red] {song.display()}: {exc}")
                failed += 1
    console.print(f"[green]{ok} fetched[/green], {failed} failed.")

    _save_playlist_record(cfg, pl)

    with console.status(f"Syncing '{pl.name}' to {root}..."):
        result = transfer_mod.sync_playlist(cfg, root, pl, index)
    _print_sync_result(result, root)

    if eject:
        console.print(transfer_mod.eject(root))


def _resolve_fill_target(
    folder: Path, cfg: Config
) -> tuple[list[Song], Optional[Playlist]]:
    """Return existing songs and optional tracked playlist for fill."""
    pl = get_playlist_record(cfg.library_dir, str(folder))
    if pl is not None:
        return pl.songs, pl

    if folder.is_dir():
        return read_folder_songs(folder), None

    console.print(
        f"[red]No tracked playlist named '{folder}', and {folder} is not a local folder.[/red]"
    )
    raise typer.Exit(1)


@app.command()
def fill(
    folder: Path = typer.Argument(
        ...,
        help="Tracked playlist name, a library folder, or a path under the iPod Music/ tree",
    ),
    add: int = typer.Option(..., "--add", "-n", help="Number of similar songs to add"),
    query: Optional[str] = typer.Option(
        None, "--query", help="Extra guidance to steer the similar songs (optional)"
    ),
    ipod: Optional[str] = typer.Option(
        None, "--ipod", help="Mount path (auto-detected if omitted); only used when FOLDER is a name, not a path"
    ),
    review: bool = typer.Option(
        True, "--review/--no-review", help="Interactively toggle songs before fetching"
    ),
):
    """Analyze songs in a playlist or folder and fetch similar tracks into it."""
    cfg = Config.load()
    existing, playlist = _resolve_fill_target(folder, cfg)

    if not existing:
        console.print("[red]No songs found to analyze.[/red]")
        raise typer.Exit(1)
    label = playlist.name if playlist else str(folder)
    console.print(f"Found {len(existing)} existing song(s) for '{label}'.")
    if playlist:
        _guard_writable(_resolve_ipod(ipod))

    with console.status(f"Analyzing and asking Gemini for {add} similar songs..."):
        suggested = generate_similar_songs(existing, add, cfg, query)

    seen_keys = {song_key(s.artist, s.title) for s in existing}
    new_songs: list[Song] = []
    for s in suggested:
        key = song_key(s.artist, s.title)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        new_songs.append(s)

    if not new_songs:
        console.print("[yellow]Gemini didn't suggest any new songs.[/yellow]")
        raise typer.Exit(0)

    title = f"Adding to {label}"
    if review:
        new_songs = review_songs(new_songs, title)
    else:
        render_songs(new_songs, [True] * len(new_songs), title)

    if not new_songs:
        console.print("[yellow]No songs selected — nothing to fetch.[/yellow]")
        raise typer.Exit(0)

    index = LibraryIndex(cfg.library_dir)
    ok, failed = 0, 0
    added_songs: list[Song] = []
    for song in new_songs:
        with console.status(f"Fetching {song.display()}..."):
            try:
                fetch_mod.fetch_song(song, cfg, index)
            except fetch_mod.FetchError as exc:
                console.print(f"[red]FAILED[/red] {song.display()}: {exc}")
                failed += 1
                continue
        console.print(f"[green]ok[/green] {song.display()}")
        added_songs.append(song)
        ok += 1

    if playlist and added_songs:
        updated = Playlist(
            name=playlist.name,
            query=playlist.query,
            songs=[*playlist.songs, *added_songs],
        )
        _save_playlist_record(cfg, updated)
        root = _resolve_ipod(ipod)
        try:
            result = transfer_mod.sync_playlist(cfg, root, updated, index)
        except transfer_mod.IpodReadOnlyError as exc:
            console.print(f"[red]{exc}[/red]")
            console.print(
                "[yellow]Tracks are in the library. After repairing the volume, "
                f'run `podcrate sync --playlist "{updated.name}"`.[/yellow]'
            )
            raise typer.Exit(1)
        _print_sync_result(result, root)
    elif added_songs:
        console.print(
            "[yellow]Tracks saved to library. Run `podcrate sync` to copy them to the iPod.[/yellow]"
        )

    console.print(f"\n{ok} added, {failed} failed.")


@app.command()
def artist(
    name: str = typer.Argument(..., help="Artist name, e.g. 'NF'"),
    all_albums: bool = typer.Option(False, "--all", help="Download every album without asking"),
    force: bool = typer.Option(False, "--force", help="Re-download even if already in library"),
):
    """Download an artist's albums and EPs into the library (no playlist).

    Rockbox's database lists them under Artist/Album once synced.
    """
    cfg = Config.load()
    index = LibraryIndex(cfg.library_dir)
    with console.status(f"Looking up {name} on MusicBrainz..."):
        try:
            found = musicbrainz.find_artist(name)
            albums = musicbrainz.list_albums(found)
        except musicbrainz.MusicBrainzError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
    note = f" ({found['disambiguation']})" if found.get("disambiguation") else ""
    if not albums:
        console.print(f"[yellow]No official albums or EPs for {found['name']}{note}.[/yellow]")
        raise typer.Exit(0)
    console.print(f"[bold]{found['name']}[/bold]{note}: {len(albums)} albums/EPs")

    if not all_albums and sys.stdin.isatty():
        picks = set(pick("Download which?", [a.label() for a in albums], checked=True))
        albums = [a for i, a in enumerate(albums) if i in picks]

    ok, failed, fetched = 0, 0, []
    for album in albums:
        console.print(f"\n[bold]{album.title}[/bold] ({album.year})")
        for number, song in enumerate(album.songs, 1):
            already = None if force else index.get(song.artist, song.title)
            with console.status(f"Fetching {song.display()}..."):
                try:
                    path = fetch_mod.fetch_song(song, cfg, index, force=force)
                except fetch_mod.FetchError as exc:
                    console.print(f"[red]FAILED[/red] {song.display()}: {exc}")
                    failed += 1
                    continue
            if already is None:
                # Track numbers keep Rockbox's album view in order instead of A-Z.
                audio = EasyMP3(path)
                audio["tracknumber"] = str(number)
                audio.save()
            console.print(f"[green]ok[/green] {song.display()} -> {path}")
            fetched.append(path)
            ok += 1

    mounts = transfer_mod.find_ipod_mounts()
    if fetched and mounts:
        try:
            with console.status(f"Copying to {mounts[0]}..."):
                copied = [transfer_mod.sync_track(cfg, mounts[0], p)[1] for p in fetched]
            console.print(f"[green]{sum(copied)} tracks copied to {mounts[0]}[/green]")
        except transfer_mod.IpodReadOnlyError as exc:
            console.print(f"[yellow]Not syncing: {exc}[/yellow]")
    console.print(f"\n{ok} tracks ready, {failed} failed. Library: {cfg.library_dir}")


@app.command()
def library():
    """Show what's in the local library and which playlists are tracked."""
    cfg = Config.load()
    tracks = sorted(cfg.library_dir.rglob("*.mp3")) if cfg.library_dir.is_dir() else []
    console.print(f"{len(tracks)} tracks in {cfg.library_dir}")

    playlists = list_playlist_records(cfg.library_dir)
    console.print(f"\n{len(playlists)} tracked playlist(s):")
    for pl in playlists:
        console.print(f"  {pl.name} ({len(pl.songs)} songs)")

    duplicates = find_all_playlist_duplicates(cfg.library_dir)
    if duplicates:
        console.print(f"\n[yellow]{len(duplicates)} duplicate playlist issue(s):[/yellow]")
        _warn_playlist_duplicates(duplicates)


@app.command()
def stats(
    ipod: Optional[str] = typer.Option(None, "--ipod", help="Mount path (auto-detected if omitted)"),
    top: int = typer.Option(10, "--top", help="How many tracks/artists to list"),
):
    """Show listening stats from the iPod's Rockbox playback log."""
    root = _resolve_ipod(ipod)
    plays = stats_mod.read_plays(root)
    if plays is None:
        console.print(
            f"[red]No {stats_mod.LOG_RELPATH} on {root}.[/red]\n"
            "Enable it on the iPod: Settings > Playback Settings > Logging > Yes."
        )
        raise typer.Exit(1)

    s = stats_mod.summarize(plays, top=top)
    hours, rem = divmod(s.listened_ms // 60000, 60)
    console.print(f"[bold]{s.plays} plays[/bold], {s.skips} skips, {hours}h {rem}m listened")
    console.print("\n[bold]Top tracks[/bold]")
    for name, count in s.top_tracks:
        console.print(f"  {count:>4}  {name}")
    console.print("\n[bold]Top artists[/bold]")
    for name, count in s.top_artists:
        console.print(f"  {count:>4}  {name}")

def _maybe_sync_playlist(
    cfg: Config,
    playlist: Playlist,
    *,
    sync: bool,
    ipod: Optional[str],
    prune: bool = False,
) -> None:
    if not sync:
        return
    root = _resolve_ipod(ipod)
    _guard_writable(root)
    index = LibraryIndex(cfg.library_dir)
    with console.status(f"Syncing '{playlist.name}' to {root}..."):
        result = transfer_mod.sync_playlist(cfg, root, playlist, index)
        removed = (
            transfer_mod.cleanup_stale_playlists(cfg, root)
            if prune
            else []
        )
    _print_sync_result(result, root)
    if removed:
        console.print(
            f"[yellow]Removed {len(removed)} stale playlist(s) on device.[/yellow]"
        )
        for path in removed:
            console.print(f"  - {path.relative_to(root)}")


@app.command("rename-playlists")
def rename_playlists(
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--apply",
        help="Preview changes (default) or apply renames",
    ),
):
    """Normalize existing playlist names and remove duplicate track lists."""
    cfg = Config.load()
    plans = plan_playlist_renames(cfg.library_dir)

    if not plans:
        console.print("[green]All playlist names already look good.[/green]")
        raise typer.Exit(0)

    for plan in plans:
        if plan.action == "remove":
            console.print(
                f"[yellow]remove[/yellow] '{plan.old_name}' ({plan.detail})"
            )
        else:
            detail = f" ({plan.detail})" if plan.detail else ""
            console.print(
                f"[cyan]{plan.action}[/cyan] '{plan.old_name}' -> '{plan.new_name}'{detail}"
            )

    if dry_run:
        console.print(
            f"\n[yellow]{len(plans)} change(s) planned. "
            "Re-run with --apply to write them.[/yellow]"
        )
        return

    apply_playlist_renames(cfg.library_dir, plans, dry_run=False)
    console.print(
        f"\n[green]Applied {len(plans)} change(s).[/green] "
        "Run `podcrate sync` to refresh .m3u files on the iPod."
    )


def _count_m3u_tracks(path: Path) -> int:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    return sum(1 for line in lines if line.strip() and not line.lstrip().startswith("#"))


def _print_tracked_playlists(cfg: Config) -> int:
    records = list_playlist_records(cfg.library_dir)
    if not records:
        console.print("[yellow]No tracked playlists yet.[/yellow]")
        return 0
    for pl in records:
        console.print(f"  {pl.name} ({len(pl.songs)} songs)")
    return len(records)


def _print_device_playlists(cfg: Config, ipod: Optional[str]) -> int:
    root = _resolve_ipod(ipod)
    playlists_dir = root / cfg.ipod_playlists_subdir
    files = transfer_mod.list_device_playlist_files(cfg, root)
    if not files:
        console.print(f"[yellow]No playlist files in {playlists_dir}.[/yellow]")
        return 0

    tracked_stems = {
        sanitize_filename(pl.name).casefold() for pl in list_playlist_records(cfg.library_dir)
    }
    console.print(f"[bold]{playlists_dir}[/bold]")
    for path in files:
        stem = path.stem.casefold() if path.suffix.lower() == ".m3u" else path.name.casefold()
        mark = "" if stem in tracked_stems else " [dim](not tracked)[/dim]"
        console.print(f"  {path.name} ({_count_m3u_tracks(path)} tracks){mark}")
    return len(files)


@playlists_app.command("list")
def playlists_list(
    device: bool = typer.Option(
        False,
        "--device",
        "-d",
        help="List files in Playlists/ on the mounted iPod instead of tracked records",
    ),
    ipod: Optional[str] = typer.Option(None, "--ipod", help="Mount path (auto-detected if omitted)"),
):
    """List tracked playlists, or .m3u files under Playlists/ on the iPod."""
    cfg = Config.load()
    if device:
        _print_device_playlists(cfg, ipod)
        return
    _print_tracked_playlists(cfg)


@playlists_app.command("show")
def playlists_show(
    name: str = typer.Argument(..., help="Tracked playlist name"),
):
    """Show numbered songs in a tracked playlist."""
    cfg = Config.load()
    try:
        pl = resolve_playlist_record(cfg.library_dir, name)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    render_songs(pl.songs, [True] * len(pl.songs), f"{pl.name} ({len(pl.songs)} songs)")


@playlists_app.command("add")
def playlists_add(
    target: str = typer.Argument(..., help="Tracked playlist to add songs to"),
    sources: list[str] = typer.Argument(
        ..., help="Other tracked playlists, or .json/.csv files/folders"
    ),
    sync: bool = typer.Option(False, "--sync", help="Sync the updated playlist to the iPod"),
    ipod: Optional[str] = typer.Option(None, "--ipod", help="Mount path (auto-detected if omitted)"),
):
    """Add songs from other playlists or files into a tracked playlist."""
    cfg = Config.load()
    try:
        updated, added = add_songs_to_playlist(cfg.library_dir, target, sources)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if added == 0:
        console.print(f"[yellow]No new songs to add to '{updated.name}'.[/yellow]")
    else:
        console.print(
            f"[green]Added {added} song(s) to '{updated.name}' "
            f"({len(updated.songs)} total).[/green]"
        )
    _maybe_sync_playlist(cfg, updated, sync=sync, ipod=ipod)


@playlists_app.command("remove")
def playlists_remove(
    target: str = typer.Argument(..., help="Tracked playlist to remove songs from"),
    selection: list[str] = typer.Argument(
        None, help="Song numbers or ranges to remove, e.g. 2 5-7"
    ),
    song: list[str] = typer.Option(
        None, "--song", help="Remove by 'Artist - Title' (repeatable)"
    ),
    sync: bool = typer.Option(False, "--sync", help="Sync the updated playlist to the iPod"),
    ipod: Optional[str] = typer.Option(None, "--ipod", help="Mount path (auto-detected if omitted)"),
    review: bool = typer.Option(
        True,
        "--review/--no-review",
        help="Interactively pick songs to remove when no selection is given",
    ),
):
    """Remove songs from a tracked playlist by number, name, or interactively."""
    cfg = Config.load()
    try:
        pl = resolve_playlist_record(cfg.library_dir, target)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    indices: set[int] = set()
    if selection:
        for part in selection:
            parsed = _parse_selection(part.replace(" ", ","), len(pl.songs))
            if parsed is None:
                console.print(f"[red]Couldn't parse selection {part!r}.[/red]")
                raise typer.Exit(1)
            indices.update(parsed)

    if not indices and not song and review:
        kept = review_songs(pl.songs, f"Remove from {pl.name}")
        kept_keys = {song_key(s.artist, s.title) for s in kept}
        remove_specs = [
            s.display()
            for s in pl.songs
            if song_key(s.artist, s.title) not in kept_keys
        ]
        if not remove_specs:
            console.print("[yellow]No songs selected for removal.[/yellow]")
            raise typer.Exit(0)
        song = remove_specs

    try:
        updated, removed = remove_songs_from_playlist(
            cfg.library_dir,
            pl.name,
            indices=indices or None,
            song_specs=song,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if removed == 0:
        console.print(f"[yellow]No matching songs to remove from '{updated.name}'.[/yellow]")
    else:
        console.print(
            f"[green]Removed {removed} song(s) from '{updated.name}' "
            f"({len(updated.songs)} remaining).[/green]"
        )
    _maybe_sync_playlist(cfg, updated, sync=sync, ipod=ipod)


@playlists_app.command("combine")
def playlists_combine(
    name: Optional[str] = typer.Argument(None, help="Name for the combined playlist"),
    sources: Optional[list[str]] = typer.Argument(
        None, help="Tracked playlists, or .json/.csv files/folders to merge"
    ),
    list_playlists: bool = typer.Option(
        False,
        "--list",
        "-l",
        help="List playlists you can combine (add --device for Playlists/ on the iPod)",
    ),
    device: bool = typer.Option(
        False,
        "--device",
        "-d",
        help="With --list, show Playlists/ on the mounted iPod",
    ),
    remove_sources: bool = typer.Option(
        True,
        "--remove-sources/--keep-sources",
        help="Delete source playlist records after combining (default)",
    ),
    sync: bool = typer.Option(False, "--sync", help="Sync the combined playlist to the iPod"),
    ipod: Optional[str] = typer.Option(None, "--ipod", help="Mount path (auto-detected if omitted)"),
):
    """Merge multiple playlists or files into one, deduping by artist/title.

    Source playlist records are removed by default; pass --keep-sources to keep them.
    """
    cfg = Config.load()
    if list_playlists:
        if device:
            _print_device_playlists(cfg, ipod)
        else:
            _print_tracked_playlists(cfg)
        return
    if not name or not sources:
        console.print(
            "[red]Need a name and at least one source playlist.[/red] "
            "Pass --list to see available playlists."
        )
        raise typer.Exit(1)
    try:
        combined, deleted = combine_playlists(
            cfg.library_dir,
            name,
            sources,
            remove_sources=remove_sources,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    console.print(
        f"[green]Combined into '{combined.name}' ({len(combined.songs)} songs).[/green]"
    )
    if deleted:
        console.print(
            f"[dim]Removed source playlist record(s): {', '.join(deleted)}[/dim]"
        )
    _maybe_sync_playlist(
        cfg, combined, sync=sync, ipod=ipod, prune=remove_sources
    )


@playlists_app.command("rename")
def playlists_rename(
    current: str = typer.Argument(..., help="Current playlist name"),
    new_name: str = typer.Argument(..., help="New playlist name"),
    sync: bool = typer.Option(
        False, "--sync", help="Write the new .m3u on the iPod and remove the old one"
    ),
    ipod: Optional[str] = typer.Option(None, "--ipod", help="Mount path (auto-detected if omitted)"),
):
    """Rename a tracked playlist. Use --sync to update Playlists/ on the iPod."""
    cfg = Config.load()
    try:
        updated, previous = rename_playlist(cfg.library_dir, current, new_name)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if updated.name == previous:
        console.print(f"[yellow]Already named '{updated.name}'.[/yellow]")
        return

    console.print(f"[green]Renamed '{previous}' -> '{updated.name}'.[/green]")
    if not sync:
        console.print(
            "[dim]Run with --sync (once the iPod is writable) to rename the .m3u.[/dim]"
        )
        return

    root = _resolve_ipod(ipod)
    _guard_writable(root)
    index = LibraryIndex(cfg.library_dir)
    result = transfer_mod.sync_playlist(cfg, root, updated, index)
    _print_sync_result(result, root)

    old_m3u = (
        root / cfg.ipod_playlists_subdir / f"{sanitize_filename(previous)}.m3u"
    )
    if old_m3u.exists() and old_m3u != result.playlist_file:
        old_m3u.unlink()
        console.print(f"[dim]Removed old playlist file {old_m3u.relative_to(root)}[/dim]")


@playlists_app.command("delete")
def playlists_delete(
    name: str = typer.Argument(..., help="Tracked playlist to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete a tracked playlist record (does not remove downloaded MP3s)."""
    cfg = Config.load()
    try:
        pl = resolve_playlist_record(cfg.library_dir, name)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if not yes:
        confirm = typer.confirm(
            f"Delete tracked playlist '{pl.name}' ({len(pl.songs)} songs)?"
        )
        if not confirm:
            raise typer.Exit(0)

    delete_playlist_record(cfg.library_dir, pl.name)
    console.print(f"[green]Deleted playlist '{pl.name}'.[/green]")


if __name__ == "__main__":
    app()
