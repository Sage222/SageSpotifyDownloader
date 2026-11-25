#!/usr/bin/env python3
"""
YT Music JSON / URL / Spotify -> Audio Downloader (PyQt6 + yt-dlp)
--------------------------------------------------------------------
Sources supported:
1. Library file - auto-detected as one of:
   a) YT Music JSON schema (songs/playlists with videoId)
   b) Raw Spotify Web API JSON playlist dump (items/track objects)
   c) Spotify export CSV (e.g. from Exportify - no API keys needed)
2. A single pasted URL:
   - YouTube video (youtube.com/watch, youtu.be, YouTube Shorts, music.youtube.com)
   - YouTube playlist (any of the above with a list= parameter)
   - Spotify playlist
   - Spotify single track
3. Spotify playlist/track via URL + your own Spotify API credentials (optional):
   - If Spotify API credentials are supplied, playlists and single tracks are
     resolved with full metadata (title, artist, album, year, genre, cover art)
     straight from the Spotify Web API.
   - If no credentials are supplied, single Spotify track links still work via
     Spotify's public, key-free oEmbed endpoint (open.spotify.com/oembed),
     which returns the track title and cover art (but not the artist name).
     The app falls back to searching YouTube on the title alone in that case,
     so matches may be less precise -- add API credentials for best accuracy.
     Spotify *playlist* links still require API credentials (oEmbed doesn't
     return the individual track list, only playlist-level metadata).

Quality handling:
- Before transcoding, the app probes the actual source stream's bitrate/codec.
- If the requested output format matches the source codec (e.g. opus -> opus,
  or m4a -> aac), ffmpeg stream-copies instead of re-encoding: zero additional
  quality loss, identical to the source.
- Otherwise, the requested bitrate is capped to the source's real bitrate so
  you never "upscale" a lossy YouTube stream into a bigger, no-better-quality
  file. FLAC (lossless) is left untouched since it can't be capped this way.

Search resolution:
- Spotify/Exportify-sourced tracks have no YouTube ID, so a search is run
  (explicitly prefixed "ytsearch1:" - relying on the 'default_search' option
  alone does NOT reliably work via the Python API, only via the CLI) and the
  result's id is validated against the real YouTube ID shape (11 chars,
  alnum/-/_ ) before being accepted, so a bad match fails loudly instead of
  silently building a broken download URL.

Tags written per file (via mutagen): title, artist, album, album artist,
genre, year, track number, cover art.

Requirements:
pip install PyQt6 yt-dlp mutagen spotipy
ffmpeg must be installed and on PATH.

Run:
python ytmusic_downloader.py
"""

import sys
import os
import re
import json
import csv
import traceback
import urllib.request
import urllib.parse
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional

from PyQt6.QtCore import (Qt, QThreadPool, QRunnable, QObject, pyqtSignal,
                           QMutex, QMutexLocker, QSettings)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QLabel, QFileDialog, QTableWidget, QTableWidgetItem,
    QProgressBar, QPlainTextEdit, QComboBox, QSpinBox, QCheckBox, QLineEdit,
    QHeaderView, QMessageBox, QSplitter, QAbstractItemView, QGroupBox, QTabWidget
)

import yt_dlp

try:
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, TCON, TDRC, TRCK, APIC, ID3NoHeaderError
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    from mutagen.flac import FLAC, Picture
    MUTAGEN_OK = True
except ImportError:
    MUTAGEN_OK = False

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    SPOTIPY_OK = True
except ImportError:
    SPOTIPY_OK = False

INVALID_CHARS = r'[<>:"/\\|?*\x00-\x1f]'

STATUS_QUEUED = "Queued"
STATUS_RESOLVING = "Resolving (search)"
STATUS_PROBING = "Checking source quality"
STATUS_DOWNLOADING = "Downloading"
STATUS_TAGGING = "Tagging"
STATUS_DONE = "Done"
STATUS_SKIPPED = "Skipped (exists)"
STATUS_FAILED = "Failed"
STATUS_CANCELLED = "Cancelled"


def sanitize(name: str, max_len: int = 150) -> str:
    if not name:
        name = "Unknown"
    name = re.sub(INVALID_CHARS, "_", name).strip().rstrip(".")
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "Unknown"


@dataclass
class Track:
    title: str
    artist: str
    album: str
    playlist: str
    video_id: Optional[str] = None
    duration_seconds: int = 0
    genre: str = ""
    year: str = ""
    track_number: int = 0
    album_artist: str = ""
    thumbnail_url: str = ""
    search_query: str = ""
    source: str = "json"
    row: int = -1
    status: str = STATUS_QUEUED
    error: str = ""

    def needs_resolution(self) -> bool:
        return not self.video_id


# ---------------- YT Music JSON schema ----------------

def parse_ytmusic_json(data) -> List[Track]:
    tracks: List[Track] = []

    def artist_str(entry):
        names = entry.get("artistName") or []
        if isinstance(names, str):
            return names
        return ", ".join(names) if names else "Unknown Artist"

    def add_songs(songs, playlist_name):
        for s in songs:
            vid = s.get("videoId")
            if not vid:
                continue
            tracks.append(Track(
                video_id=vid,
                title=s.get("title") or "Unknown Title",
                artist=artist_str(s),
                album=s.get("albumName") or "Unknown Album",
                duration_seconds=int(s.get("durationSeconds") or 0),
                playlist=playlist_name or "Default",
                thumbnail_url=s.get("thumbnails") or "",
                source="json",
            ))

    if isinstance(data, dict) and "playlists" in data:
        for pl in data["playlists"]:
            name = pl.get("name") or pl.get("playlistName") or "Playlist"
            add_songs(pl.get("songs", []), name)
    elif isinstance(data, dict) and "songs" in data:
        add_songs(data["songs"], data.get("name") or data.get("playlistName") or "Library")
    elif isinstance(data, list):
        add_songs(data, "Library")
    else:
        raise ValueError("Not a recognised YT Music schema.")

    return tracks


def is_ytmusic_schema(data) -> bool:
    if isinstance(data, dict) and ("songs" in data or "playlists" in data):
        return True
    if isinstance(data, list) and data and isinstance(data[0], dict) and "videoId" in data[0]:
        return True
    return False


# ---------------- Spotify schemas: raw API JSON dump + export CSV ----------------

def _normalize_key(k: str) -> str:
    return re.sub(r"[^a-z0-9]", "", k.lower())


def _find_field(norm_map: dict, candidates):
    for c in candidates:
        if c in norm_map:
            return norm_map[c]
    return None


def _spotify_api_track_to_track(tr: dict, playlist_name: str, genre: str = "") -> Optional[Track]:
    if not tr:
        return None
    title = tr.get("name") or "Unknown Title"
    artists = [a.get("name") for a in tr.get("artists", []) if a.get("name")]
    artist_join = ", ".join(artists) if artists else "Unknown Artist"
    album_obj = tr.get("album") or {}
    album = album_obj.get("name") or "Unknown Album"
    release_date = album_obj.get("release_date") or ""
    year = release_date[:4] if release_date else ""
    track_number = tr.get("track_number") or 0
    duration_seconds = int((tr.get("duration_ms") or 0) / 1000)
    images = album_obj.get("images") or []
    thumb = images[0]["url"] if images else ""
    return Track(
        title=title, artist=artist_join, album=album, playlist=playlist_name,
        video_id=None, duration_seconds=duration_seconds, genre=genre, year=year,
        track_number=track_number, album_artist=artists[0] if artists else "Unknown Artist",
        thumbnail_url=thumb, search_query=f"{artists[0] if artists else ''} - {title} audio",
        source="spotify",
    )


def is_spotify_raw_api_schema(data) -> bool:
    if isinstance(data, dict) and "items" in data and isinstance(data["items"], list) and data["items"]:
        first = data["items"][0]
        return isinstance(first, dict) and "track" in first
    if isinstance(data, dict) and "tracks" in data and isinstance(data["tracks"], dict):
        return "items" in data["tracks"]
    return False


def parse_spotify_raw_api_json(data, playlist_name: str = "Imported Playlist") -> List[Track]:
    if isinstance(data, dict) and "tracks" in data and isinstance(data["tracks"], dict):
        items = data["tracks"].get("items", [])
        playlist_name = data.get("name") or playlist_name
    else:
        items = data.get("items", [])
    tracks = []
    for item in items:
        tr = item.get("track") if isinstance(item, dict) else None
        t = _spotify_api_track_to_track(tr, playlist_name)
        if t:
            tracks.append(t)
    if not tracks:
        raise ValueError("No tracks found in Spotify API-style JSON.")
    return tracks


def is_spotify_export_schema(data) -> bool:
    """Matches Exportify-style rows: e.g. Track Name, Artist Name(s), Album Name, Release Date, Genres."""
    rows = None
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("tracks", "items", "songs"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
    if not rows or not isinstance(rows[0], dict):
        return False
    norm_map = {_normalize_key(k): k for k in rows[0].keys()}
    has_title = _find_field(norm_map, ["trackname", "name", "title", "track"]) is not None
    has_artist = _find_field(norm_map, ["artistnames", "artistname", "artist", "artists"]) is not None
    return has_title and has_artist


def _split_multi(raw, sep_chars: str) -> List[str]:
    if isinstance(raw, list):
        return [str(v).strip() for v in raw if str(v).strip()]
    if not raw:
        return []
    pattern = "[" + re.escape(sep_chars) + "]"
    return [v.strip() for v in re.split(pattern, str(raw)) if v.strip()]


def parse_spotify_export_json(data, playlist_name: str = "Imported Playlist") -> List[Track]:
    """Parses Exportify-style rows (works whether they came from CSV or JSON).
    Real Exportify columns: Track URI, Track Name, Album Name, Artist Name(s),
    Release Date, Duration (ms), Popularity, Explicit, Added By, Added At,
    Genres, Record Label, + audio-feature columns (ignored).
    NOTE: Artist Name(s) is semicolon-delimited; Genres is comma-delimited.
    """
    rows = data
    if isinstance(data, dict):
        for key in ("tracks", "items", "songs"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
        playlist_name = data.get("name") or data.get("playlistName") or playlist_name

    if not isinstance(rows, list) or not rows:
        raise ValueError("Could not find a list of track rows in this file.")

    tracks = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        norm_map = {_normalize_key(k): k for k in row.keys()}

        def get(*cands, default=""):
            key = _find_field(norm_map, cands)
            return row.get(key, default) if key else default

        title = get("trackname", "name", "title", "track", default="Unknown Title")

        artist_raw = get("artistnames", "artistname", "artist", "artists", default="Unknown Artist")
        artist_list = _split_multi(artist_raw, ";")
        artist = ", ".join(artist_list) if artist_list else "Unknown Artist"

        album = get("albumname", "album", default="Unknown Album")

        album_artist_raw = get("albumartistnames", "albumartistname", "albumartist", default="")
        album_artist_list = _split_multi(album_artist_raw, ";")
        album_artist = ", ".join(album_artist_list) if album_artist_list else (artist_list[0] if artist_list else artist)

        release_date = get("albumreleasedate", "releasedate", "date", "year", default="")
        year = str(release_date)[:4] if release_date else ""

        track_number_raw = get("tracknumber", "trackno", default=0)
        try:
            track_number = int(track_number_raw) if track_number_raw else 0
        except (ValueError, TypeError):
            track_number = 0

        duration_ms = get("trackdurationms", "durationms", "duration", default=0)
        try:
            duration_seconds = int(float(duration_ms) / 1000) if duration_ms else 0
        except (ValueError, TypeError):
            duration_seconds = 0

        genres_raw = get("genres", "genre", default="")
        genre_list = _split_multi(genres_raw, ",")
        genre = ", ".join(genre_list) if genre_list else ""

        image = get("albumimageurl", "imageurl", "artworkurl", "albumart", default="")
        primary_artist = artist_list[0] if artist_list else artist

        tracks.append(Track(
            title=str(title), artist=artist, album=album, playlist=playlist_name,
            video_id=None, duration_seconds=duration_seconds, genre=genre, year=year,
            track_number=track_number, album_artist=album_artist,
            thumbnail_url=image, search_query=f"{primary_artist} - {title} audio",
            source="spotify_export",
        ))
    return tracks


def load_library_json(data) -> List[Track]:
    """Auto-detect and parse: YT Music schema, raw Spotify API dump, or Spotify export rows."""
    if is_ytmusic_schema(data):
        return parse_ytmusic_json(data)
    if is_spotify_raw_api_schema(data):
        return parse_spotify_raw_api_json(data)
    if is_spotify_export_schema(data):
        return parse_spotify_export_json(data)
    raise ValueError(
        "Unrecognised JSON format. Expected a YT Music library export (songs/videoId), "
        "a raw Spotify API playlist dump (items/track), or Spotify export rows "
        "(Track Name, Artist Name(s), Album Name, etc.)."
    )


def load_csv_rows(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [row for row in reader]


def load_library_file(path: str) -> List[Track]:
    """Load a .json or .csv library/export file, auto-detecting its schema."""
    ext = Path(path).suffix.lower()
    playlist_name = Path(path).stem
    if ext == ".csv":
        rows = load_csv_rows(path)
        if not rows:
            raise ValueError("CSV file is empty or has no data rows.")
        if not is_spotify_export_schema(rows):
            raise ValueError(
                "This CSV doesn't look like an Exportify-style export. Expected columns like "
                "'Track Name', 'Artist Name(s)', 'Album Name', 'Release Date', 'Genres'."
            )
        return parse_spotify_export_json(rows, playlist_name=playlist_name)
    else:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return load_library_json(data)


# ---------------- URL classification & loading ----------------

def classify_url(url: str) -> str:
    url = url.strip()
    if "open.spotify.com/playlist" in url or "spotify.link" in url and "playlist" in url:
        return "spotify_playlist"
    if "open.spotify.com/track" in url:
        return "spotify_track"
    if "spotify:track:" in url:
        return "spotify_track"
    if "spotify:playlist:" in url:
        return "spotify_playlist"
    if "youtube.com" in url or "youtu.be" in url:
        if "list=" in url:
            return "youtube_playlist"
        return "youtube_video"
    return "unknown"


def load_single_youtube_video(url: str) -> List[Track]:
    """Handles a single YouTube video URL: standard watch URLs, youtu.be short
    links, YouTube Shorts, and music.youtube.com links (any of which may also
    carry harmless tracking params like ?si=... without a list=, so they're
    still classified as a single video, not a playlist)."""
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    year = ""
    upload_date = info.get("upload_date")
    if upload_date:
        year = upload_date[:4]
    t = Track(
        title=info.get("title") or "Unknown Title",
        artist=info.get("artist") or info.get("uploader") or "Unknown Artist",
        album=info.get("album") or "Singles",
        playlist="Pasted URL",
        video_id=info.get("id"),
        duration_seconds=int(info.get("duration") or 0),
        year=year,
        thumbnail_url=info.get("thumbnail") or "",
        source="youtube_url",
    )
    return [t]


def load_youtube_playlist(url: str) -> List[Track]:
    tracks = []
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    playlist_name = info.get("title") or "YouTube Playlist"
    for entry in info.get("entries", []) or []:
        if not entry:
            continue
        tracks.append(Track(
            title=entry.get("title") or "Unknown Title",
            artist=entry.get("uploader") or entry.get("channel") or "Unknown Artist",
            album=playlist_name,
            playlist=playlist_name,
            video_id=entry.get("id"),
            duration_seconds=int(entry.get("duration") or 0),
            thumbnail_url="",
            source="youtube_url",
        ))
    return tracks


def _spotify_id_from_url(url: str, kind: str) -> Optional[str]:
    """Extract a Spotify entity ID from an open.spotify.com URL or a spotify:track:ID URI."""
    m = re.search(rf"spotify:{kind}:([a-zA-Z0-9]+)", url)
    if m:
        return m.group(1)
    m = re.search(rf"{kind}/([a-zA-Z0-9]+)", url)
    if m:
        return m.group(1)
    return None


def load_spotify_playlist_via_api(url: str, client_id: str, client_secret: str) -> List[Track]:
    """Optional path: uses your own Spotify Developer credentials (Client Credentials flow)."""
    if not SPOTIPY_OK:
        raise RuntimeError("spotipy is not installed. pip install spotipy")
    playlist_id = _spotify_id_from_url(url, "playlist")
    if not playlist_id:
        raise ValueError("Could not find a playlist ID in that Spotify URL.")

    auth = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
    sp = spotipy.Spotify(auth_manager=auth)

    pl_meta = sp.playlist(playlist_id, fields="name")
    playlist_name = pl_meta.get("name") or "Spotify Playlist"

    tracks: List[Track] = []
    genre_cache = {}

    results = sp.playlist_items(playlist_id, additional_types=["track"])
    items = results.get("items", [])
    while results.get("next"):
        results = sp.next(results)
        items.extend(results.get("items", []))

    for item in items:
        tr = item.get("track")
        if not tr:
            continue
        primary_artist = (tr.get("artists") or [{}])[0]
        artist_id = primary_artist.get("id")
        genre = ""
        if artist_id:
            if artist_id not in genre_cache:
                try:
                    a_info = sp.artist(artist_id)
                    genre_cache[artist_id] = ", ".join(a_info.get("genres", [])[:3])
                except Exception:
                    genre_cache[artist_id] = ""
            genre = genre_cache[artist_id]
        t = _spotify_api_track_to_track(tr, playlist_name, genre=genre)
        if t:
            tracks.append(t)

    return tracks


def load_single_spotify_track_via_api(url: str, client_id: str, client_secret: str) -> List[Track]:
    """Resolves a single Spotify track link with full metadata (title, artist,
    album, year, cover art, and up to 3 genre tags from the primary artist)
    using your own Spotify Developer credentials (Client Credentials flow)."""
    if not SPOTIPY_OK:
        raise RuntimeError("spotipy is not installed. pip install spotipy")
    track_id = _spotify_id_from_url(url, "track")
    if not track_id:
        raise ValueError("Could not find a track ID in that Spotify URL.")

    auth = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
    sp = spotipy.Spotify(auth_manager=auth)

    tr = sp.track(track_id)
    if not tr:
        raise ValueError("Spotify returned no data for that track.")

    genre = ""
    primary_artist = (tr.get("artists") or [{}])[0]
    artist_id = primary_artist.get("id")
    if artist_id:
        try:
            a_info = sp.artist(artist_id)
            genre = ", ".join(a_info.get("genres", [])[:3])
        except Exception:
            genre = ""

    t = _spotify_api_track_to_track(tr, "Pasted URL", genre=genre)
    if not t:
        raise ValueError("Could not parse that Spotify track's metadata.")
    return [t]


def fetch_spotify_oembed_title(url: str) -> Optional[dict]:
    """Key-free fallback: Spotify's public oEmbed endpoint. Works without any
    API credentials, but for tracks it only returns the track title and cover
    art -- no artist name -- so the resulting YouTube search is less precise
    than the full-API path."""
    endpoint = "https://open.spotify.com/oembed?url=" + urllib.parse.quote(url, safe="")
    req = urllib.request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data


def load_single_spotify_track_via_oembed(url: str) -> List[Track]:
    """Key-free single-track fallback used when no Spotify API credentials are
    configured. Only the track title (and cover art) are available, so the
    artist field is left as 'Unknown Artist' and the YouTube search query is
    built from the title alone -- expect occasional mismatches on generic
    titles; add Spotify API credentials for reliable artist-aware matching."""
    data = fetch_spotify_oembed_title(url)
    title = (data or {}).get("title") or "Unknown Title"
    thumb = (data or {}).get("thumbnail_url") or ""
    t = Track(
        title=title,
        artist="Unknown Artist",
        album="Singles",
        playlist="Pasted URL",
        video_id=None,
        thumbnail_url=thumb,
        search_query=f"{title} audio",
        source="spotify_oembed",
    )
    return [t]


# ---------------- output path / resolution / quality decision / tagging ----------------

def build_output_path(base_dir: Path, track: Track, grouping: str, ext: str) -> Path:
    if grouping == "Playlist":
        folder = base_dir / sanitize(track.playlist)
    elif grouping == "Album":
        folder = base_dir / sanitize(track.album)
    else:  # Artist -> Album
        folder = base_dir / sanitize(track.artist) / sanitize(track.album)
    folder.mkdir(parents=True, exist_ok=True)

    filename = sanitize(f"{track.artist} - {track.title}")
    candidate = folder / f"{filename}.{ext}"
    if candidate.exists():
        suffix = track.video_id or track.search_query[:8]
        candidate = folder / f"{filename} [{suffix}].{ext}"
    return candidate


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def resolve_video_id(track: Track):
    query = track.search_query or f"{track.artist} - {track.title} audio"
    # NOTE: relying on the 'default_search' option does NOT reliably auto-prefix
    # bare queries when calling extract_info() via the API (that auto-prefixing is
    # a CLI-only behavior) - explicitly prefixing "ytsearch1:" is the documented,
    # reliable way to force a search rather than risking a fallback extractor
    # echoing the raw query back as a bogus "id".
    search_term = f"ytsearch1:{query}"
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(search_term, download=False)
    if info and "entries" in info and info["entries"]:
        info = info["entries"][0]
    candidate_id = info.get("id") if info else None
    if not candidate_id or not VIDEO_ID_RE.match(str(candidate_id)):
        raise RuntimeError(f"No valid YouTube match found for '{query}' (got id={candidate_id!r})")
    track.video_id = candidate_id
    if not track.thumbnail_url:
        track.thumbnail_url = info.get("thumbnail") or ""


def probe_source_audio(video_id: str) -> dict:
    """Inspect the actual bestaudio stream yt-dlp would pick, without downloading it."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "format": "bestaudio/best"}
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {"abr": info.get("abr"), "acodec": info.get("acodec"), "ext": info.get("ext")}


def _normalize_codec(acodec: Optional[str]) -> str:
    if not acodec:
        return ""
    ac = acodec.lower()
    if ac.startswith("opus"):
        return "opus"
    if ac.startswith("mp4a") or "aac" in ac:
        return "aac"
    if ac.startswith("vorbis"):
        return "vorbis"
    if ac.startswith("flac"):
        return "flac"
    return ac


_FMT_TO_CODEC = {"opus": "opus", "m4a": "aac", "mp4": "aac", "vorbis": "vorbis",
                 "ogg": "vorbis", "flac": "flac", "mp3": "mp3"}


def decide_transcode_plan(source_acodec: Optional[str], source_abr: Optional[float],
                           target_fmt: str, requested_kbps: int):
    """Decide whether to stream-copy (no re-encode) or transcode, and at what bitrate.
    Returns (skip_reencode: bool, target_kbps: Optional[int], note: str).
    """
    src_codec = _normalize_codec(source_acodec)
    tgt_codec = _FMT_TO_CODEC.get(target_fmt, target_fmt)

    if tgt_codec == "flac":
        return False, None, (f"Lossless target (FLAC); source was already lossy "
                              f"({src_codec or 'unknown'} ~{round(source_abr) if source_abr else '?'}kbps), "
                              f"so the original compression ceiling still applies.")

    if src_codec and tgt_codec == src_codec:
        return True, None, (f"Output codec matches source ({src_codec}) - stream copy, "
                             f"no re-encode, quality identical to source.")

    if source_abr:
        capped = min(requested_kbps, round(source_abr))
        if capped < requested_kbps:
            note = (f"Transcoding {src_codec or 'source'} (~{round(source_abr)}kbps) -> {target_fmt}: "
                     f"capped target bitrate to {capped}kbps (requested {requested_kbps}kbps, "
                     f"which would exceed the source).")
        else:
            note = f"Transcoding {src_codec or 'source'} (~{round(source_abr)}kbps) -> {target_fmt} at {capped}kbps."
        return False, capped, note

    return False, requested_kbps, (f"Transcoding to {target_fmt} at {requested_kbps}kbps "
                                    f"(source bitrate unknown - using requested value as-is).")


def fetch_thumbnail_bytes(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read()
    except Exception:
        return None


def tag_file(path: Path, track: Track, ext: str, embed_art: bool):
    if not MUTAGEN_OK:
        return
    art_bytes = fetch_thumbnail_bytes(track.thumbnail_url) if embed_art else None
    try:
        if ext == "mp3":
            try:
                audio = ID3(path)
            except ID3NoHeaderError:
                audio = ID3()
            audio["TIT2"] = TIT2(encoding=3, text=track.title)
            audio["TPE1"] = TPE1(encoding=3, text=track.artist)
            audio["TALB"] = TALB(encoding=3, text=track.album)
            if track.album_artist:
                audio["TPE2"] = TPE2(encoding=3, text=track.album_artist)
            if track.genre:
                audio["TCON"] = TCON(encoding=3, text=track.genre)
            if track.year:
                audio["TDRC"] = TDRC(encoding=3, text=track.year)
            if track.track_number:
                audio["TRCK"] = TRCK(encoding=3, text=str(track.track_number))
            if art_bytes:
                audio["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=art_bytes)
            audio.save(path, v2_version=3)

        elif ext in ("m4a", "mp4"):
            audio = MP4(path)
            audio["\xa9nam"] = [track.title]
            audio["\xa9ART"] = [track.artist]
            audio["\xa9alb"] = [track.album]
            if track.album_artist:
                audio["aART"] = [track.album_artist]
            if track.genre:
                audio["\xa9gen"] = [track.genre]
            if track.year:
                audio["\xa9day"] = [track.year]
            if track.track_number:
                audio["trkn"] = [(track.track_number, 0)]
            if art_bytes:
                audio["covr"] = [MP4Cover(art_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()

        elif ext in ("opus", "ogg", "vorbis"):
            audio = OggOpus(path) if ext == "opus" else OggVorbis(path)
            audio["title"] = [track.title]
            audio["artist"] = [track.artist]
            audio["album"] = [track.album]
            if track.album_artist:
                audio["albumartist"] = [track.album_artist]
            if track.genre:
                audio["genre"] = [track.genre]
            if track.year:
                audio["date"] = [track.year]
            if track.track_number:
                audio["tracknumber"] = [str(track.track_number)]
            audio.save()

        elif ext == "flac":
            audio = FLAC(path)
            audio["title"] = track.title
            audio["artist"] = track.artist
            audio["album"] = track.album
            if track.album_artist:
                audio["albumartist"] = track.album_artist
            if track.genre:
                audio["genre"] = track.genre
            if track.year:
                audio["date"] = track.year
            if track.track_number:
                audio["tracknumber"] = str(track.track_number)
            if art_bytes:
                pic = Picture()
                pic.data = art_bytes
                pic.type = 3
                pic.mime = "image/jpeg"
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()
    except Exception:
        pass  # tagging must never fail the whole job


# ---------------- Qt worker plumbing ----------------

class SignalHub(QObject):
    row_status = pyqtSignal(int, str, str)
    log = pyqtSignal(str)
    overall_progress = pyqtSignal(int, int)


class DownloadJob(QRunnable):
    def __init__(self, track: Track, out_dir: Path, grouping: str, fmt: str,
                 requested_kbps: int, cookies_file: str, skip_existing: bool, embed_art: bool,
                 hub: SignalHub, cancel_flag, counter, counter_mutex, total):
        super().__init__()
        self.t = track
        self.out_dir = out_dir
        self.grouping = grouping
        self.fmt = fmt
        self.requested_kbps = requested_kbps
        self.cookies_file = cookies_file
        self.skip_existing = skip_existing
        self.embed_art = embed_art
        self.hub = hub
        self.cancel_flag = cancel_flag
        self.counter = counter
        self.counter_mutex = counter_mutex
        self.total = total

    def run(self):
        t = self.t
        if self.cancel_flag():
            self.hub.row_status.emit(t.row, STATUS_CANCELLED, "")
            self._bump()
            return

        try:
            if t.needs_resolution():
                self.hub.row_status.emit(t.row, STATUS_RESOLVING, "")
                resolve_video_id(t)
        except Exception as e:
            self.hub.row_status.emit(t.row, STATUS_FAILED, f"resolve: {e}")
            self.hub.log.emit(f"[FAIL] {t.title} -> resolve error: {e}")
            self._bump()
            return

        target = build_output_path(self.out_dir, t, self.grouping, self.fmt)

        if self.skip_existing and target.exists():
            self.hub.row_status.emit(t.row, STATUS_SKIPPED, "")
            self.hub.log.emit(f"[SKIP] {t.title} -> already exists")
            self._bump()
            return

        # Probe the actual source stream before deciding how to transcode.
        self.hub.row_status.emit(t.row, STATUS_PROBING, "")
        skip_reencode = False
        target_kbps = self.requested_kbps
        try:
            source_info = probe_source_audio(t.video_id)
            skip_reencode, target_kbps, note = decide_transcode_plan(
                source_info.get("acodec"), source_info.get("abr"), self.fmt, self.requested_kbps)
            self.hub.log.emit(f"[INFO] {t.title}: {note}")
        except Exception as e:
            self.hub.log.emit(f"[WARN] {t.title}: could not probe source quality ({e}); "
                               f"proceeding with requested {self.requested_kbps}kbps as-is.")

        self.hub.row_status.emit(t.row, STATUS_DOWNLOADING, "")
        tmp_template = str(target.with_suffix("")) + ".%(ext)s"

        postprocessor = {"key": "FFmpegExtractAudio", "preferredcodec": self.fmt}
        if not skip_reencode and target_kbps:
            postprocessor["preferredquality"] = str(target_kbps)

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": tmp_template,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "postprocessors": [postprocessor],
            "retries": 3,
            "fragment_retries": 3,
            "ignoreerrors": False,
        }

        if self.cookies_file:
            ydl_opts["cookiefile"] = self.cookies_file

        url = f"https://www.youtube.com/watch?v={t.video_id}"

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            final_path = target if target.exists() else target.with_suffix(f".{self.fmt}")
            if not final_path.exists():
                candidates = list(final_path.parent.glob(final_path.stem + ".*"))
                if candidates:
                    final_path = candidates[0]

            if final_path.exists():
                self.hub.row_status.emit(t.row, STATUS_TAGGING, "")
                tag_file(final_path, t, self.fmt, self.embed_art)
                self.hub.row_status.emit(t.row, STATUS_DONE, "")
                self.hub.log.emit(f"[OK] {t.artist} - {t.title}")
            else:
                self.hub.row_status.emit(t.row, STATUS_FAILED, "Output file missing")
                self.hub.log.emit(f"[FAIL] {t.title} -> output file missing")

        except Exception as e:
            self.hub.row_status.emit(t.row, STATUS_FAILED, str(e))
            self.hub.log.emit(f"[FAIL] {t.title} -> {e}")

        self._bump()

    def _bump(self):
        with QMutexLocker(self.counter_mutex):
            self.counter[0] += 1
            done = self.counter[0]
        self.hub.overall_progress.emit(done, self.total)


# ---------------- Main window ----------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Music Downloader - JSON / CSV / URL / Spotify")
        self.resize(1150, 800)
        self.settings = QSettings("YTMusicDownloader", "Config")

        self.tracks: List[Track] = []
        self.cancel_flag = {"v": False}
        self.pool = QThreadPool.globalInstance()
        self.hub = SignalHub()
        self.hub.row_status.connect(self.on_row_status)
        self.hub.log.connect(self.on_log)
        self.hub.overall_progress.connect(self.on_overall_progress)
        self.counter = [0]
        self.counter_mutex = QMutex()

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        # --- Source group ---
        src_box = QGroupBox("Add tracks")
        src_layout = QVBoxLayout()

        row_json = QHBoxLayout()
        btn_load_json = QPushButton("Load Library JSON / Spotify Export CSV...")
        btn_load_json.clicked.connect(self.load_json)
        row_json.addWidget(btn_load_json)
        row_json.addWidget(QLabel("(auto-detects YT Music JSON, Exportify CSV, or raw Spotify API JSON dump)"))
        src_layout.addLayout(row_json)

        row_url = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "Paste a single YouTube video URL, YouTube playlist URL, "
            "single Spotify track URL, or Spotify playlist URL..."
        )
        btn_add_url = QPushButton("Add from URL")
        btn_add_url.clicked.connect(self.add_from_url)
        row_url.addWidget(self.url_edit)
        row_url.addWidget(btn_add_url)
        src_layout.addLayout(row_url)

        row_spot = QHBoxLayout()
        self.spotify_id_edit = QLineEdit(self.settings.value("spotify_client_id", ""))
        self.spotify_id_edit.setPlaceholderText("Spotify Client ID (optional - improves single-track/playlist accuracy)")
        self.spotify_secret_edit = QLineEdit(self.settings.value("spotify_client_secret", ""))
        self.spotify_secret_edit.setPlaceholderText("Spotify Client Secret (optional)")
        self.spotify_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        row_spot.addWidget(QLabel("Spotify API creds (optional):"))
        row_spot.addWidget(self.spotify_id_edit)
        row_spot.addWidget(self.spotify_secret_edit)
        src_layout.addLayout(row_spot)
        note = QLabel(
            "Tip: Spotify playlist links always need API credentials above. Single Spotify "
            "track links work without credentials too (via Spotify's public oEmbed lookup), "
            "but matching is title-only then (no artist), so add credentials for best accuracy. "
            "To avoid the API entirely for playlists, export with a tool like Exportify (CSV) "
            "and load it via the button above instead."
        )
        note.setWordWrap(True)
        src_layout.addWidget(note)
        if not SPOTIPY_OK:
            src_layout.addWidget(QLabel(
                "spotipy not installed - pasted Spotify playlist URLs won't work, and single "
                "Spotify track links will fall back to title-only oEmbed matching "
                "(CSV/JSON loading still works fine). pip install spotipy"
            ))

        src_box.setLayout(src_layout)
        outer.addWidget(src_box)

        # --- Download settings ---
        form = QFormLayout()

        self.out_dir_edit = QLineEdit(str(Path.home() / "Music" / "Downloads"))
        btn_out_dir = QPushButton("Browse...")
        btn_out_dir.clicked.connect(self.pick_out_dir)
        row2 = QHBoxLayout()
        row2.addWidget(self.out_dir_edit)
        row2.addWidget(btn_out_dir)
        form.addRow("Output folder:", row2)

        self.grouping_combo = QComboBox()
        self.grouping_combo.addItems(["Playlist", "Album", "Artist -> Album"])
        form.addRow("Folder grouping:", self.grouping_combo)

        self.format_combo = QComboBox()
        self.format_combo.addItems(["opus", "m4a", "mp3", "vorbis", "flac"])
        form.addRow("Audio format:", self.format_combo)

        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["320", "256", "192", "160", "128", "96"])
        self.quality_combo.setCurrentText("192")
        form.addRow("Target bitrate (kbps):", self.quality_combo)
        quality_note = QLabel(
            "This is a ceiling, not a guarantee: if the output format matches the source codec "
            "(e.g. opus source -> opus output), the app stream-copies with zero re-encode loss "
            "and this setting is ignored. Otherwise the bitrate is automatically capped to the "
            "actual source stream's bitrate (typically ~128-160kbps from YouTube), so raising this "
            "above that won't upscale quality - just check the log for what was actually used per track."
        )
        quality_note.setWordWrap(True)
        form.addRow("", quality_note)

        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 10)
        self.concurrency_spin.setValue(3)
        form.addRow("Concurrent downloads:", self.concurrency_spin)

        self.cookies_edit = QLineEdit()
        btn_cookies = QPushButton("Browse...")
        btn_cookies.clicked.connect(self.pick_cookies)
        row3 = QHBoxLayout()
        row3.addWidget(self.cookies_edit)
        row3.addWidget(btn_cookies)
        form.addRow("Cookies file (optional):", row3)

        self.skip_existing_check = QCheckBox("Skip if file already exists")
        self.skip_existing_check.setChecked(True)
        form.addRow("", self.skip_existing_check)

        self.embed_art_check = QCheckBox("Embed cover art")
        self.embed_art_check.setChecked(True)
        form.addRow("", self.embed_art_check)

        outer.addLayout(form)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start Download")
        self.start_btn.clicked.connect(self.start_download)
        self.cancel_btn = QPushButton("Cancel Remaining")
        self.cancel_btn.clicked.connect(self.cancel_download)
        self.cancel_btn.setEnabled(False)
        self.clear_btn = QPushButton("Clear List")
        self.clear_btn.clicked.connect(self.clear_list)
        self.export_report_btn = QPushButton("Export Report (CSV)")
        self.export_report_btn.clicked.connect(lambda: self.export_report(failed_only=False))
        self.export_errors_btn = QPushButton("Export Errors Only (CSV)")
        self.export_errors_btn.clicked.connect(lambda: self.export_report(failed_only=True))
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.clear_btn)
        btn_row.addWidget(self.export_report_btn)
        btn_row.addWidget(self.export_errors_btn)
        outer.addLayout(btn_row)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Status", "Playlist", "Artist", "Title", "Album", "Genre", "Year", "Error"])
        for col in (2, 3, 4):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        splitter.addWidget(self.table)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        splitter.addWidget(self.log_view)
        splitter.setSizes([450, 220])
        outer.addWidget(splitter)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("%v / %m tracks")
        outer.addWidget(self.progress_bar)

        self.summary_label = QLabel("No tracks loaded.")
        outer.addWidget(self.summary_label)

    # ---------- source loading ----------

    def load_json(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select library JSON or Spotify export CSV", "",
            "JSON or CSV Files (*.json *.csv);;JSON Files (*.json);;CSV Files (*.csv)")
        if not path:
            return
        try:
            new_tracks = load_library_file(path)
        except Exception as e:
            QMessageBox.critical(self, "Failed to load file", f"{e}\n\n{traceback.format_exc()}")
            return
        self.append_tracks(new_tracks)

    def add_from_url(self):
        url = self.url_edit.text().strip()
        if not url:
            return
        kind = classify_url(url)
        try:
            if kind == "youtube_video":
                new_tracks = load_single_youtube_video(url)
            elif kind == "youtube_playlist":
                new_tracks = load_youtube_playlist(url)
            elif kind == "spotify_playlist":
                cid = self.spotify_id_edit.text().strip()
                secret = self.spotify_secret_edit.text().strip()
                if not cid or not secret:
                    QMessageBox.warning(self, "Spotify credentials required",
                        "Pasting a Spotify playlist URL directly requires Spotify API credentials.\n\n"
                        "To avoid using the API, export the playlist with a tool like Exportify "
                        "(CSV) instead and load it via 'Load Library JSON / Spotify Export CSV'.")
                    return
                self.settings.setValue("spotify_client_id", cid)
                self.settings.setValue("spotify_client_secret", secret)
                new_tracks = load_spotify_playlist_via_api(url, cid, secret)
            elif kind == "spotify_track":
                cid = self.spotify_id_edit.text().strip()
                secret = self.spotify_secret_edit.text().strip()
                if cid and secret:
                    self.settings.setValue("spotify_client_id", cid)
                    self.settings.setValue("spotify_client_secret", secret)
                    new_tracks = load_single_spotify_track_via_api(url, cid, secret)
                else:
                    try:
                        new_tracks = load_single_spotify_track_via_oembed(url)
                        self.log_view.appendPlainText(
                            "[INFO] No Spotify API credentials set - resolved this track by title "
                            "only via Spotify's oEmbed lookup. Add credentials above for accurate "
                            "artist-aware matching."
                        )
                    except Exception as e:
                        QMessageBox.critical(self, "Failed to resolve Spotify track",
                            f"Could not resolve this track without Spotify API credentials: {e}\n\n"
                            "Add Spotify Client ID/Secret above and try again.")
                        return
            else:
                QMessageBox.warning(self, "Unrecognised URL",
                    "Paste a single YouTube video/playlist URL or a single Spotify track/playlist URL.")
                return
        except Exception as e:
            QMessageBox.critical(self, "Failed to load URL", f"{e}\n\n{traceback.format_exc()}")
            return

        self.append_tracks(new_tracks)
        self.url_edit.clear()

    def append_tracks(self, new_tracks: List[Track]):
        start = len(self.tracks)
        self.tracks.extend(new_tracks)
        self.table.setRowCount(len(self.tracks))
        for i in range(start, len(self.tracks)):
            t = self.tracks[i]
            t.row = i
            self.set_table_row(i, t)
        self.summary_label.setText(f"Loaded {len(self.tracks)} tracks total.")
        self.progress_bar.setMaximum(len(self.tracks))

    def set_table_row(self, i: int, t: Track):
        self.table.setItem(i, 0, QTableWidgetItem(t.status))
        self.table.setItem(i, 1, QTableWidgetItem(t.playlist))
        self.table.setItem(i, 2, QTableWidgetItem(t.artist))
        self.table.setItem(i, 3, QTableWidgetItem(t.title))
        self.table.setItem(i, 4, QTableWidgetItem(t.album))
        self.table.setItem(i, 5, QTableWidgetItem(t.genre))
        self.table.setItem(i, 6, QTableWidgetItem(t.year))
        self.table.setItem(i, 7, QTableWidgetItem(""))

    def clear_list(self):
        self.tracks = []
        self.table.setRowCount(0)
        self.progress_bar.setMaximum(1)
        self.progress_bar.setValue(0)
        self.summary_label.setText("No tracks loaded.")

    def pick_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select output folder", self.out_dir_edit.text())
        if d:
            self.out_dir_edit.setText(d)

    def pick_cookies(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select cookies.txt", "", "Text Files (*.txt);;All Files (*)")
        if path:
            self.cookies_edit.setText(path)

    # ---------- downloading ----------

    def start_download(self):
        if not self.tracks:
            QMessageBox.warning(self, "No tracks", "Load a JSON/CSV file, URL, or Spotify playlist first.")
            return

        out_dir = Path(self.out_dir_edit.text())
        out_dir.mkdir(parents=True, exist_ok=True)

        grouping_map = {"Playlist": "Playlist", "Album": "Album", "Artist -> Album": "Artist"}
        grouping = grouping_map[self.grouping_combo.currentText()]
        fmt = self.format_combo.currentText()
        requested_kbps = int(self.quality_combo.currentText())
        cookies = self.cookies_edit.text().strip() or None
        skip_existing = self.skip_existing_check.isChecked()
        embed_art = self.embed_art_check.isChecked()

        self.pool.setMaxThreadCount(self.concurrency_spin.value())
        self.cancel_flag["v"] = False
        self.counter[0] = 0
        self.progress_bar.setMaximum(len(self.tracks))
        self.progress_bar.setValue(0)

        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.log_view.appendPlainText(f"Starting download of {len(self.tracks)} tracks -> {out_dir}")

        for t in self.tracks:
            t.status = STATUS_QUEUED
            t.error = ""
            self.table.setItem(t.row, 0, QTableWidgetItem(t.status))
            job = DownloadJob(
                track=t, out_dir=out_dir, grouping=grouping, fmt=fmt, requested_kbps=requested_kbps,
                cookies_file=cookies, skip_existing=skip_existing, embed_art=embed_art,
                hub=self.hub, cancel_flag=lambda: self.cancel_flag["v"], counter=self.counter,
                counter_mutex=self.counter_mutex, total=len(self.tracks),
            )
            self.pool.start(job)

    def cancel_download(self):
        self.cancel_flag["v"] = True
        self.log_view.appendPlainText("Cancel requested: no new downloads will start; in-flight ones will finish.")
        self.cancel_btn.setEnabled(False)

    def export_report(self, failed_only: bool = False):
        if not self.tracks:
            QMessageBox.information(self, "Nothing to export", "Load some tracks first.")
            return
        rows_to_write = [t for t in self.tracks if t.status == STATUS_FAILED] if failed_only else self.tracks
        if failed_only and not rows_to_write:
            QMessageBox.information(self, "No failures", "No tracks are currently marked Failed - nothing to export.")
            return
        default_name = "download_errors.csv" if failed_only else "download_report.csv"
        dialog_title = "Save failed-tracks report as CSV" if failed_only else "Save full report as CSV"
        path, _ = QFileDialog.getSaveFileName(self, dialog_title, default_name, "CSV Files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Status", "Playlist", "Artist", "Title", "Album", "Genre", "Year",
                                  "Source", "VideoID/SearchQuery", "Error"])
                for t in rows_to_write:
                    writer.writerow([t.status, t.playlist, t.artist, t.title, t.album, t.genre, t.year,
                                      t.source, t.video_id or t.search_query, t.error])
            QMessageBox.information(self, "Report saved", f"Saved {len(rows_to_write)} row(s) to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Failed to save report", str(e))

    # ---------- signal handlers ----------

    def on_row_status(self, row: int, status: str, error: str):
        self.table.setItem(row, 0, QTableWidgetItem(status))
        self.table.setItem(row, 7, QTableWidgetItem(error))
        if 0 <= row < len(self.tracks):
            self.tracks[row].status = status
            self.tracks[row].error = error

    def on_log(self, msg: str):
        self.log_view.appendPlainText(msg)

    def on_overall_progress(self, done: int, total: int):
        self.progress_bar.setValue(done)
        if done >= total:
            self.start_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
            self.log_view.appendPlainText("All jobs finished.")
            self.show_summary()

    def show_summary(self):
        counts = {}
        for t in self.tracks:
            counts[t.status] = counts.get(t.status, 0) + 1
        lines = [f"{status}: {count}" for status, count in sorted(counts.items())]
        failed = [t for t in self.tracks if t.status == STATUS_FAILED]
        msg = "Batch finished.\n\n" + "\n".join(lines)
        if failed:
            msg += f"\n\n{len(failed)} track(s) failed - use 'Export Errors Only (CSV)' to see exactly which ones and why."
        QMessageBox.information(self, "Download summary", msg)


def main():
    if not MUTAGEN_OK:
        print("WARNING: mutagen not installed -- tags will not be written. pip install mutagen")
    if not SPOTIPY_OK:
        print("NOTE: spotipy not installed -- pasted Spotify playlist URLs will not work, and "
              "single Spotify track links will fall back to title-only oEmbed matching. "
              "pip install spotipy")
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
