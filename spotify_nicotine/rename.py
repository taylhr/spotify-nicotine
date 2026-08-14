"""Rename finished downloads to "Artist - Title.ext" using Spotify metadata.

Only files whose match confidence clears ``--rename-min-confidence`` are
touched, so a renamed file is also an assertion that the tool is certain what
the file is.

Renaming is safe for the tool's bookkeeping: downloads are tracked by the
*remote* (username, virtual_path) pair, never by the local filename.
"""

import os
import re
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


def rename_one(
    record: Dict[str, Any],
    track: Track,
    candidate: Dict[str, Any],
    cfg: Config,
) -> Tuple[Optional[str], Optional[str]]:
    """Rename one finished download. Returns (new_path, skip_reason)."""
    source = resolve_local_path(record, candidate, cfg)
    if not source:
        return None, "download folder unknown"
    if not os.path.isfile(source):
        # Not an error: the user may have moved or tidied the file already.
        return None, None

    extension = os.path.splitext(source)[1].lstrip(".") or candidate.get("extension", "")
    basename = target_basename(track, extension)
    if not basename:
        return None, "no usable title in the Spotify metadata"

    directory = os.path.dirname(source)
    target = unique_target_path(directory, basename, source)
    if os.path.abspath(target) == os.path.abspath(source):
        return target, None  # already correctly named
    try:
        os.rename(source, target)
    except OSError as exc:
        return None, "rename failed (%s)" % exc
    return target, None


def apply_renames(
    state: Dict[str, Any],
    cfg: Config,
    store: StateStore,
    log: Callable[[str], None] = lambda msg: None,
) -> int:
    """Rename every finished, confident, not-yet-renamed download."""
    if not cfg.rename_files or cfg.dry_run:
        return 0

    renamed = 0
    changed = False
    for record in state.get("tracks", {}).values():
        if record.get("status") != TrackStatus.DOWNLOADED or record.get("renamed_to"):
            continue
        index = record.get("chosen_index")
        candidates = record.get("candidates") or []
        if index is None or index >= len(candidates):
            continue
        candidate = candidates[index]
        if float(candidate.get("confidence", 0.0)) < cfg.rename_min_confidence:
            continue

        track = Track.from_dict(record["spotify"])
        new_path, skip_reason = rename_one(record, track, candidate, cfg)
        if skip_reason:
            log("  could not rename %s: %s" % (track.display, skip_reason))
            continue
        if not new_path:
            continue
        record["renamed_to"] = new_path
        record["local_path"] = new_path
        record["renamed_at"] = now_iso()
        changed = True
        renamed += 1
        log("  renamed: %s" % os.path.basename(new_path))

    if changed:
        store.save(state)
    return renamed
