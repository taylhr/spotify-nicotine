"""Tidy finished downloads: rename to "Artist - Title.ext" and group albums.

Both are optional (``--rename-files``, ``--album-folders``) and both are safe
for the tool's bookkeeping: downloads are tracked by the *remote*
(username, virtual_path) pair, never by the local filename or folder.
"""

import os
import re
import shutil
from typing import Any, Callable, Dict, Optional, Tuple

from spotify_nicotine.config import Config
from spotify_nicotine.models import Track, TrackStatus
from spotify_nicotine.state import StateStore, now_iso

# Characters that are illegal or troublesome in macOS/Windows filenames.
_ILLEGAL_RE = re.compile(r'[/\\:*?"<>|]')
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
# Keep well under the 255-byte per-component limit while leaving room for a
# collision suffix and the extension.
MAX_COMPONENT_BYTES = 100

# An album needs at least this many tracks in the playlist to get its own
# folder; a lone track from an album is just a single.
MIN_ALBUM_TRACKS = 2


def sanitize_component(text: str) -> str:
    """Make one filename component safe without mangling readable text."""
    text = _CONTROL_RE.sub("", text)
    text = _ILLEGAL_RE.sub("-", text)
    text = _WS_RE.sub(" ", text).strip()
    text = text.strip(". ")  # leading dots hide files; trailing dots break Windows
    return _truncate_bytes(text, MAX_COMPONENT_BYTES)


def _truncate_bytes(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore").strip()


def target_basename(track: Track, extension: str) -> Optional[str]:
    """"Artist - Title.ext" from Spotify metadata; None if unusable."""
    artist = sanitize_component(track.artists[0] if track.artists else "")
    title = sanitize_component(track.name or "")
    if not title:
        return None
    extension = extension.lstrip(".")
    stem = "%s - %s" % (artist, title) if artist else title
    return "%s.%s" % (stem, extension) if extension else stem


def album_folder_name(artist: str, album: str) -> Optional[str]:
    """"Artist - Album" folder name; None if either part is unusable."""
    artist = sanitize_component(artist or "")
    album = sanitize_component(album or "")
    if not artist or not album:
        return None
    return "%s - %s" % (artist, album)


def assign_album_folders(
    state: Dict[str, Any], min_tracks: int = MIN_ALBUM_TRACKS
) -> Dict[str, int]:
    """Tag every track that shares an album with enough playlist siblings.

    Runs over the whole playlist (not just this run's tracks) so that albums
    spanning several runs still group together. Returns {folder: count}.
    """
    groups: Dict[Tuple[str, str], list] = {}
    for record in state.get("tracks", {}).values():
        spotify = record.get("spotify", {})
        artists = spotify.get("artists") or []
        album = spotify.get("album") or ""
        if not artists or not album:
            continue
        key = (artists[0].casefold(), album.casefold())
        groups.setdefault(key, []).append(record)

    assigned: Dict[str, int] = {}
    for records in groups.values():
        spotify = records[0].get("spotify", {})
        folder = album_folder_name(spotify["artists"][0], spotify["album"])
        qualifies = folder is not None and len(records) >= min_tracks
        for record in records:
            if qualifies:
                record["album_folder"] = folder
            else:
                record.pop("album_folder", None)
        if qualifies:
            assigned[folder] = len(records)
    return assigned


def destination_folder(record: Dict[str, Any], cfg: Config) -> Optional[str]:
    """Local folder to hand Nicotine+ at enqueue time, if we can name one."""
    if not cfg.dest_dir:
        return None
    if cfg.album_folders and record.get("album_folder"):
        return os.path.join(cfg.dest_dir, record["album_folder"])
    return cfg.dest_dir


def unique_target_path(directory: str, basename: str, source_path: str) -> str:
    """Path in `directory` for `basename`, avoiding clobbering a different file."""
    candidate = os.path.join(directory, basename)
    if not os.path.exists(candidate) or os.path.abspath(candidate) == os.path.abspath(
        source_path
    ):
        return candidate
    stem, extension = os.path.splitext(basename)
    for suffix in range(2, 100):
        candidate = os.path.join(directory, "%s (%d)%s" % (stem, suffix, extension))
        if not os.path.exists(candidate):
            return candidate
    return os.path.join(directory, "%s (dup)%s" % (stem, extension))


def local_filename(virtual_path: str) -> str:
    return virtual_path.replace("\\", "/").rsplit("/", 1)[-1]


def resolve_local_path(
    record: Dict[str, Any], candidate: Dict[str, Any], cfg: Config
) -> Optional[str]:
    """Best guess at where Nicotine+ put the finished file."""
    stored = record.get("local_path")
    if stored:
        return stored
    folder = record.get("local_folder") or cfg.dest_dir
    if folder:
        return os.path.join(folder, local_filename(candidate.get("virtual_path", "")))
    return None


def _desired_directory(
    record: Dict[str, Any], cfg: Config, current_dir: str
) -> str:
    """Where this file should live: its album folder, or where it already is."""
    folder = record.get("album_folder")
    if not cfg.album_folders or not folder:
        return current_dir
    if os.path.basename(current_dir) == folder:
        return current_dir  # already filed under the album
    # Without an explicit destination, put the album folder inside whatever
    # folder Nicotine+ downloaded to.
    base = cfg.dest_dir or current_dir
    return os.path.join(base, folder)


def organize_one(
    record: Dict[str, Any],
    track: Track,
    candidate: Dict[str, Any],
    cfg: Config,
) -> Tuple[Optional[str], Optional[str]]:
    """Move/rename one finished download. Returns (new_path, skip_reason).

    Renaming and album filing are decided together so the file moves once.
    """
    source = resolve_local_path(record, candidate, cfg)
    if not source:
        return None, "download folder unknown"
    if not os.path.isfile(source):
        # Not an error: the user may have moved or tidied the file already.
        return None, None

    current_dir = os.path.dirname(source)
    target_dir = _desired_directory(record, cfg, current_dir)

    basename = os.path.basename(source)
    confident = float(candidate.get("confidence", 0.0)) >= cfg.rename_min_confidence
    if cfg.rename_files and confident:
        extension = os.path.splitext(source)[1].lstrip(".") or candidate.get(
            "extension", ""
        )
        renamed = target_basename(track, extension)
        if renamed:
            basename = renamed
        elif not cfg.album_folders:
            return None, "no usable title in the Spotify metadata"

    if os.path.abspath(os.path.join(target_dir, basename)) == os.path.abspath(source):
        return source, None  # already where it belongs, correctly named

    try:
        os.makedirs(target_dir, exist_ok=True)
        target = unique_target_path(target_dir, basename, source)
        shutil.move(source, target)
    except OSError as exc:
        return None, "could not move the file (%s)" % exc
    return target, None


def organize_files(
    state: Dict[str, Any],
    cfg: Config,
    store: StateStore,
    log: Callable[[str], None] = lambda msg: None,
) -> int:
    """Rename and/or file every finished download that still needs it."""
    if cfg.dry_run or not (cfg.rename_files or cfg.album_folders):
        return 0
    if cfg.album_folders:
        assign_album_folders(state)

    organized = 0
    changed = False
    for record in state.get("tracks", {}).values():
        if record.get("status") != TrackStatus.DOWNLOADED:
            continue
        index = record.get("chosen_index")
        candidates = record.get("candidates") or []
        if index is None or index >= len(candidates):
            continue
        candidate = candidates[index]

        track = Track.from_dict(record["spotify"])
        before = record.get("local_path")
        new_path, skip_reason = organize_one(record, track, candidate, cfg)
        if skip_reason:
            log("  could not tidy %s: %s" % (track.display, skip_reason))
            continue
        if not new_path or new_path == before:
            continue
        record["local_path"] = new_path
        record["local_folder"] = os.path.dirname(new_path)
        record["organized_at"] = now_iso()
        changed = True
        organized += 1
        parent = os.path.basename(os.path.dirname(new_path))
        log("  tidied: %s/%s" % (parent, os.path.basename(new_path)))

    if changed:
        store.save(state)
    return organized
