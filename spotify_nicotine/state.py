"""Persistent per-playlist state with atomic writes.

The state file is the source of truth for stop/resume: it is saved after every
track state transition, so interrupting the run at any point is safe.
"""

import datetime
import json
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from spotify_nicotine.models import StatusReason, Track, TrackStatus

SCHEMA_VERSION = 1


def now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def seconds_since(iso_timestamp: Optional[str]) -> float:
    """Age of a stored ISO timestamp in seconds; very large when unparsable."""
    if not iso_timestamp:
        return float("inf")
    try:
        then = datetime.datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    delta = datetime.datetime.now(datetime.timezone.utc) - then
    return delta.total_seconds()


class StateStore:
    def __init__(self, state_dir: str, playlist_id: str):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, "%s.json" % playlist_id)

    def load(self) -> Optional[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            return None
        with open(self.path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        # A run interrupted mid-search leaves 'searching' behind; those tracks
        # simply search again next run.
        for record in state.get("tracks", {}).values():
            if record.get("status") == TrackStatus.SEARCHING:
                record["status"] = TrackStatus.PENDING
        return state

    def save(self, state: Dict[str, Any]) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=self.state_dir, prefix=".tmp-state-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def new_state(playlist_meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "playlist": dict(playlist_meta),
        "last_run": None,
        "skipped": [],
        "tracks": {},
    }


def new_track_record(track: Track) -> Dict[str, Any]:
    return {
        "spotify": track.to_dict(),
        "status": TrackStatus.PENDING,
        "status_reason": None,
        "queries_tried": [],
        "search_completed_at": None,
        "candidates": [],
        "chosen_index": None,
        "attempts": [],
        "updated_at": now_iso(),
    }


def set_track_status(
    record: Dict[str, Any], status: str, reason: Optional[str] = None
) -> None:
    record["status"] = status
    record["status_reason"] = reason
    record["updated_at"] = now_iso()


def merge_playlist(
    state: Dict[str, Any],
    playlist_meta: Dict[str, Any],
    tracks: List[Track],
    skipped: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """Fold a fresh playlist fetch into existing state.

    Existing per-track progress is always preserved; only membership changes.
    Returns a change summary with track ids per category.
    """
    state["playlist"] = dict(playlist_meta)
    state["skipped"] = list(skipped)
    records = state.setdefault("tracks", {})

    fetched_ids = set()
    summary: Dict[str, List[str]] = {"added": [], "removed": [], "restored": []}

    for track in tracks:
        fetched_ids.add(track.id)
        record = records.get(track.id)
        if record is None:
            records[track.id] = new_track_record(track)
            summary["added"].append(track.id)
        else:
            record["spotify"] = track.to_dict()
            if record.get("status") == TrackStatus.REMOVED:
                # Restore to the progress the track had when it left the
                # playlist. Restoring a queued track to 'pending' would
                # re-search and re-download it even though its original
                # download may have finished meanwhile; restored-to-queued
                # tracks get reconciled against the transfer list instead.
                prior = record.pop("status_before_removed", None)
                if prior in (TrackStatus.DOWNLOADING, TrackStatus.QUEUED):
                    prior = TrackStatus.QUEUED
                elif prior not in (
                    TrackStatus.NEEDS_REVIEW,
                    TrackStatus.FAILED,
                    TrackStatus.SKIPPED,
                ):
                    prior = TrackStatus.PENDING
                set_track_status(record, prior)
                summary["restored"].append(track.id)

    for track_id, record in records.items():
        if track_id in fetched_ids:
            continue
        status = record.get("status")
        if status in (TrackStatus.DOWNLOADED, TrackStatus.REMOVED):
            continue
        record["status_before_removed"] = status
        set_track_status(
            record, TrackStatus.REMOVED, StatusReason.REMOVED_FROM_PLAYLIST
        )
        summary["removed"].append(track_id)

    return summary


def tracks_with_status(state: Dict[str, Any], *statuses: str) -> List[Tuple[str, Dict[str, Any]]]:
    """(track_id, record) pairs matching any given status, in playlist order."""
    wanted = set(statuses)
    matching = [
        (track_id, record)
        for track_id, record in state.get("tracks", {}).items()
        if record.get("status") in wanted
    ]
    matching.sort(
        key=lambda pair: min(pair[1].get("spotify", {}).get("positions") or [10**9])
    )
    return matching
