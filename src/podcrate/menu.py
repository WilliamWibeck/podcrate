"""Arrow-key home menu shown when `podcrate` runs with no subcommand.

Every action builds argv for an existing command and runs it through the
Typer app, so the menu never duplicates command logic.
"""
from __future__ import annotations

import time

import questionary
import typer
from rich.console import Console

from .config import Config
from .library import list_playlist_records, playlist_record_path

console = Console()


def _run(app: typer.Typer, *args: str) -> None:
    console.print(f"[dim]$ podcrate {' '.join(args)}[/dim]")
    try:
        app(list(args), prog_name="podcrate")
    except SystemExit:
        pass  # standalone mode exits after every command (errors already printed)


def _ask_count(message: str, default: int) -> str:
    return questionary.text(
        message, default=str(default), validate=lambda v: v.isdigit() and int(v) > 0
    ).unsafe_ask()


def _pick_playlist(message: str) -> str | None:
    names = [p.name for p in list_playlist_records(Config.load().library_dir)]
    if not names:
        console.print("[yellow]No tracked playlists yet.[/yellow]")
        return None
    return questionary.select(
        message,
        choices=[*names, "← Back"],
        use_search_filter=True,
        use_jk_keys=False,
        instruction="(↑/↓, type to filter)",
    ).unsafe_ask()


def _new_playlist(app: typer.Typer) -> None:
    query = questionary.text("Describe the playlist:").unsafe_ask().strip()
    if not query:
        return
    count = _ask_count("How many songs?", 15)
    name = questionary.text("Name (blank = suggested):").unsafe_ask().strip()
    started = time.time()
    _run(app, "playlist", query, "-n", count, *(["--name", name] if name else []))

    # ponytail: newest record written since we started = the one just saved
    cfg = Config.load()
    fresh = [
        p for p in list_playlist_records(cfg.library_dir)
        if playlist_record_path(cfg.library_dir, p.name).stat().st_mtime >= started
    ]
    if fresh and questionary.confirm(f"Download '{fresh[0].name}' now?").unsafe_ask():
        _run(app, "fetch", str(playlist_record_path(cfg.library_dir, fresh[0].name)))


def _import(app: typer.Typer) -> None:
    source = questionary.select(
        "Import from:",
        choices=["My Spotify playlists", "Spotify / YouTube URL", "File or folder (.json/.csv)", "← Back"],
    ).unsafe_ask()
    if source == "My Spotify playlists":
        _run(app, "fetch", "--spotify")
    elif source == "Spotify / YouTube URL":
        url = questionary.text("URL:").unsafe_ask().strip()
        if url:
            _run(app, "fetch", url)
    elif source.startswith("File"):
        path = questionary.path("Path:").unsafe_ask().strip()
        if path:
            _run(app, "fetch", path)


def _manage(app: typer.Typer) -> None:
    name = _pick_playlist("Which playlist?")
    if not name or name == "← Back":
        return
    while True:
        action = questionary.select(
            name,
            choices=[
                "Show songs",
                "Add more like this (AI)",
                "Add songs from another playlist / URL / file",
                "Remove songs",
                "Download missing songs",
                "Sync just this to iPod",
                "Rename",
                "Delete",
                "← Back",
            ],
        ).unsafe_ask()
        if action == "Show songs":
            _run(app, "playlists", "show", name)
        elif action.startswith("Add more"):
            _run(app, "fill", name, "--add", _ask_count("How many?", 10))
        elif action.startswith("Add songs"):
            source = questionary.text("Playlist name, URL or path:").unsafe_ask().strip()
            if source:
                _run(app, "playlists", "add", name, source)
        elif action == "Remove songs":
            _run(app, "playlists", "remove", name)
        elif action.startswith("Download"):
            _run(app, "fetch", str(playlist_record_path(Config.load().library_dir, name)))
        elif action.startswith("Sync"):
            _run(app, "sync", "--playlist", name)
        elif action == "Rename":
            new = questionary.text("New name:", default=name).unsafe_ask().strip()
            if new and new != name:
                _run(app, "playlists", "rename", name, new)
                name = new
        elif action == "Delete":
            _run(app, "playlists", "delete", name)  # command asks to confirm
            return
        else:
            return


def _combine(app: typer.Typer) -> None:
    names = [p.name for p in list_playlist_records(Config.load().library_dir)]
    if len(names) < 2:
        console.print("[yellow]Need at least two tracked playlists.[/yellow]")
        return
    picked = questionary.checkbox("Combine which?", choices=names).unsafe_ask()
    if len(picked) < 2:
        return
    new = questionary.text("Name for the combined playlist:").unsafe_ask().strip()
    if new:
        _run(app, "playlists", "combine", new, *picked)


def run_menu(app: typer.Typer) -> None:
    actions = {
        "New AI playlist": _new_playlist,
        "Import (Spotify / YouTube / file)": _import,
        "Browse & manage playlists": _manage,
        "Combine playlists": _combine,
        "Sync everything to iPod": lambda a: _run(a, "sync"),
        "Library stats": lambda a: _run(a, "library"),
        "Listening stats (iPod)": lambda a: _run(a, "stats"),
    }
    while True:
        try:
            choice = questionary.select(
                "podcrate", choices=[*actions, "Quit"], instruction="(↑/↓, enter)"
            ).unsafe_ask()
        except KeyboardInterrupt:
            return
        if choice == "Quit":
            return
        try:
            actions[choice](app)
        except KeyboardInterrupt:
            pass  # Ctrl-C inside an action goes back to the main menu
        console.print()
