# Music Downloader (PyQt6 + yt-dlp)

A desktop GUI for building a local, tagged audio library from a YT Music library export, a Spotify playlist export, or a single pasted URL. Audio is always sourced from YouTube via `yt-dlp` and converted to your chosen audio-only format; metadata is written from the source data you provide, not scraped from YouTube's own (often messy) titles.

## Features

- **Three ways to add tracks:**
  - Load a YT Music library JSON export (`songs`/`playlists` schema with `videoId`).
  - Load a **Spotify playlist export CSV** (e.g. from [Exportify](https://exportify.net) — no API keys or developer account needed).
  - Paste a single **YouTube video URL**, **YouTube playlist URL**, or **Spotify playlist URL** directly.
- **Optional Spotify API path** — if you'd rather paste Spotify URLs than export CSVs, you can enter your own Spotify Developer Client ID/Secret (Client Credentials flow). Not required for the CSV import path.
- **Auto-detecting parser** — one "Load" button handles YT Music JSON, raw Spotify API JSON dumps, and Exportify-style CSV, matched by column/key shape, not file extension alone.
- **Configurable folder structure** — group downloads by Playlist, Album, or Artist → Album.
- **Multiple audio formats** — mp3, m4a, opus, flac, vorbis, with adjustable quality.
- **Full ID3/MP4/Vorbis tagging** — title, artist, album, album artist, genre, year, track number, and embedded cover art (from Spotify album art or, as a fallback, the matched YouTube thumbnail).
- **Concurrent downloads** with a configurable worker pool, so the GUI never freezes.
- **Skip-existing** support for safe re-runs on large libraries.
- **Live per-track status table** (Queued → Resolving → Downloading → Tagging → Done/Failed/Skipped) plus a scrolling log.
- **End-of-batch summary popup** and an **Export Report (CSV)** button to see exactly which tracks failed and why.

## Requirements

```bash
pip install PyQt6 yt-dlp mutagen spotipy
```

You also need **ffmpeg** installed and available on your system `PATH` — it's required for audio extraction/conversion and is not installed via pip.

- Debian/Ubuntu: `sudo apt install ffmpeg`
- Windows: download from [ffmpeg.org](https://ffmpeg.org/download.html) and add the `bin` folder to PATH.

`spotipy` is only needed if you plan to paste Spotify playlist URLs directly with your own API credentials. It's optional if you're only using CSV imports.

## Running

```bash
python ytmusic_downloader.py
```

## Usage

1. **Add tracks** using one of:
   - **Load Library JSON / Spotify Export CSV...** — pick a `.json` (YT Music export) or `.csv` (Exportify playlist export) file.
   - **Add from URL** — paste a YouTube video/playlist link, or a Spotify playlist link (requires entering Spotify API credentials in the fields below it).
2. Set your **output folder**, **folder grouping** (Playlist / Album / Artist → Album), **audio format**, and **quality**.
3. Optionally add a **cookies file** (exported from your browser) if you hit age-restricted, region-locked, or private videos.
4. Click **Start Download**. Progress and per-track status appear in the table; a log panel shows detailed messages.
5. When the batch finishes, a **summary popup** shows counts of Done/Skipped/Failed/Cancelled tracks.
6. Use **Export Report (CSV)** at any time to save a full status report — filter by `Status = Failed` to see exactly which tracks had no YouTube match or hit a download error, and why.

## Notes on Spotify Export CSVs (Exportify)

Exportify's real column set is:

```
Track URI, Track Name, Album Name, Artist Name(s), Release Date, Duration (ms),
Popularity, Explicit, Added By, Added At, Genres, Record Label, + audio-feature columns
```

Two quirks the parser accounts for automatically:

- **`Artist Name(s)`** is semicolon-delimited for multi-artist tracks (e.g. `Gorillaz;Del The Funky Homosapien`), not comma-delimited.
- **`Genres`** is comma-delimited within the field, and is often **empty** for many tracks — this reflects gaps in Spotify's own artist-level genre data, not a bug in the parser.
- There's no album-artist, track-number, or cover-art-URL column in a standard Exportify export. Album artist falls back to the primary listed artist, and cover art falls back to the matched YouTube video's thumbnail.

## Legal Note

Downloading audio from YouTube sits in a legal gray area under YouTube's Terms of Service. This tool is intended for personal archival of content you have the right to access, not for redistribution.

## Known Limitations

- Single Spotify **track** links (not playlists) aren't supported yet — use a playlist link or export via CSV.
- Spotify → YouTube matching uses a best-effort search (`"{artist} - {title} audio"`) and takes the top result, which can occasionally grab a lyric video, cover, or wrong version. Tags are always written from the source metadata (Spotify/YT Music), independent of what YouTube's search match is actually titled.
- High concurrency (>3–5 simultaneous downloads) risks YouTube rate-limiting or temporary throttling — the default of 3 is a safe starting point.
