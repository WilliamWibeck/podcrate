# podcrate

Fill a Rockbox iPod from the terminal. Describe a vibe and Gemini picks the
songs, or import your Spotify and YouTube playlists. podcrate downloads the
audio, tags it, and syncs folders and `.m3u` playlists to the device.

```
$ podcrate
? podcrate (↑/↓, enter)
» New AI playlist
  Import (Spotify / YouTube / file)
  Browse & manage playlists
  Combine playlists
  Sync everything to iPod
  Library stats
  Quit
```

## Requirements

- Linux, Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- `ffmpeg` on your `PATH`
- A JavaScript runtime (`deno` or `node`), which yt-dlp needs for YouTube
- An iPod running [Rockbox](https://www.rockbox.org/), mounted under `/run/media` or `/media`

## Install

```bash
git clone https://github.com/WilliamWibeck/podcrate.git
cd podcrate
uv sync
uv tool install --editable .   # puts `podcrate` on your PATH
cp .env.example .env
```

Keep the install editable. podcrate reads `.env` from the project folder.

## API keys

Put all keys in `.env`.

### Gemini (required for AI playlists)

1. Create a key at <https://aistudio.google.com/apikey>.
2. Set `GEMINI_API_KEY=...`.

### Spotify (optional, for importing playlists)

1. Go to <https://developer.spotify.com/dashboard> and click **Create app**.
2. Add the redirect URI `http://127.0.0.1:8765/callback`, then tick **Web API**.
3. Copy the **Client ID** into `PODCRATE_SPOTIFY_CLIENT_ID=...`. No secret is needed.
4. The first Spotify command opens a browser so you can log in. podcrate caches the token in `~/.cache/podcrate/`.

Spotify only lets apps in development mode read playlists you own or
collaborate on, plus Liked Songs. podcrate reads any other public playlist
from its public web page, which shows only the first 100 songs. For private
playlists you don't own, export a CSV with
[Exportify](https://exportify.app/) and run `podcrate fetch file.csv`.

### YouTube cookies (optional)

Private YouTube playlists need your browser's cookies, and so do downloads that
start failing with HTTP 403. Set one of these:

```bash
PODCRATE_YTDLP_COOKIES_FILE=~/cookies.txt        # Netscape-format export
PODCRATE_YTDLP_COOKIES_FROM_BROWSER=firefox      # or e.g. chromium+gnomekeyring
```

## Usage

Run `podcrate` with no arguments to open the menu above. Every menu action maps to one of these commands:

```bash
# AI playlists
podcrate run "similar to Loveless by My Bloody Valentine" -n 20   # generate, download, sync
podcrate playlist "90s trip hop" --name "Trip Hop"                 # generate + save only
podcrate fill "Trip Hop" --add 10                                  # add more songs like these

# Import
podcrate fetch --spotify                                  # pick from your Spotify playlists
podcrate fetch "https://open.spotify.com/playlist/..."
podcrate fetch "https://www.youtube.com/playlist?list=..."
podcrate fetch exports/                                   # .json/.csv file or folder of CSVs
podcrate artist "NF"                                      # pick albums/EPs to download, no playlist

# Device
podcrate sync                     # copy every tracked playlist to the iPod
podcrate sync --playlist "Trip Hop" --eject

# Manage
podcrate library
podcrate playlists list | show | add | remove | combine | rename | delete
```

Before anything downloads, generated playlists open in a checklist so you can
deselect songs. Use ↑/↓, space and Enter. Pass `--no-review` to skip it.

## How it works

- **Library:** downloads go to `~/Music/podcrate/<Artist>/<Album>/` as 192 kbps MP3s with tags and cover art. A song that is already in the library is never downloaded again, even when another playlist includes it.
- **Playlists:** each playlist is saved as `~/Music/podcrate/.playlists/<name>.json`. `sync` reads these files.
- **Sync:** podcrate copies files into `Music/` on the iPod and writes `Playlists/<name>.m3u`. A full sync also copies library tracks that aren't in any playlist, so Rockbox's database can browse them. It never deletes audio from the device. A full sync removes `.m3u` files for playlists you have deleted (pass `--no-prune` to keep them). If an iPod is plugged in when a download finishes, podcrate syncs that playlist straight away.

## Auto-sync on plug-in (optional)

A systemd user unit can sync everything whenever the iPod is mounted, and shows
a desktop notification when it's done:

```bash
cp contrib/systemd/podcrate-* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now podcrate-watch.path
```

Logs: `journalctl --user -u podcrate-autosync.service`.

## Configuration

These variables can be set in `.env` or in your shell.

| Variable | Default | |
|---|---|---|
| `GEMINI_API_KEY` | — | Required for AI playlists |
| `PODCRATE_SPOTIFY_CLIENT_ID` | — | Spotify import |
| `PODCRATE_SPOTIFY_REDIRECT_URI` | `http://127.0.0.1:8765/callback` | Must match the URI registered on your Spotify app |
| `PODCRATE_YTDLP_COOKIES_FILE` | — | YouTube cookies file |
| `PODCRATE_YTDLP_COOKIES_FROM_BROWSER` | — | Load YouTube cookies from a browser |
| `PODCRATE_LIBRARY_DIR` | `~/Music/podcrate` | Local library folder |
| `PODCRATE_MODEL` | `gemini-flash-latest` | Gemini model |
| `PODCRATE_IPOD_MUSIC_SUBDIR` | `Music` | Audio folder on the device |
| `PODCRATE_IPOD_PLAYLISTS_SUBDIR` | `Playlists` | `.m3u` folder on the device |

## Troubleshooting

- **YouTube 403s:** run `uv sync && uv tool install --editable . --force` to update yt-dlp, make sure `deno` or `node` is installed, then set up cookies as described above.
- **iPod mounted read-only:** the FAT32 volume wasn't unmounted cleanly. Run `fsck.vfat -a` on it and remount. podcrate prints the exact commands.
- **Spotify login fails with "Address already in use":** something else is using port 8765. Use another port in `PODCRATE_SPOTIFY_REDIRECT_URI` and register the same URI on your Spotify app.

## License

MIT
