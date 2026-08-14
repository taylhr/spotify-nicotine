"""Shared dataclasses and status constants."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class TrackStatus:
    PENDING = "pending"
    SEARCHING = "searching"
    NEEDS_REVIEW = "needs_review"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    FAILED = "failed"
    SKIPPED = "skipped"
    REMOVED = "removed"


class StatusReason:
    NO_RESULTS = "no_results"
    BELOW_THRESHOLD = "below_threshold"
    BELOW_MIN_BITRATE = "below_min_bitrate"
    EXHAUSTED_CANDIDATES = "exhausted_candidates"
    USER_SKIPPED = "user_skipped"
    REMOVED_FROM_PLAYLIST = "removed_from_playlist"


# Transfer statuses reported by the Nicotine+ API plugin (see POSTMAN_API.md 7.1).
#
# TERMINAL failures never self-resurrect, so falling back to another source is
# safe. TRANSIENT failures are connection blips that Nicotine+ itself retries
# when the peer returns — falling back on those would double-download (there is
# no cancel API), so they are nudged (re-enqueued) instead, with a retry cap.
TERMINAL_FAILURE_STATUSES = frozenset({
    "Cancelled",
    "Filtered",
    "Download folder error",
    "Local file error",
})
TRANSIENT_FAILURE_STATUSES = frozenset({
    "User logged off",
    "Connection closed",
    "Connection timeout",
})

FINISHED_STATUS = "Finished"
QUEUED_TRANSFER_STATUS = "Queued"
TRANSFERRING_STATUSES = frozenset({"Transferring", "Getting status"})

LOSSLESS_EXTENSIONS = frozenset({"flac", "ape", "alac", "wav", "aiff"})

# Soulseek file-attribute codes (JSON keys arrive as strings).
ATTR_BITRATE = "0"
ATTR_DURATION = "1"
ATTR_VBR = "2"
ATTR_SAMPLE_RATE = "4"
ATTR_BIT_DEPTH = "5"


@dataclass
class Track:
    id: str
    name: str
    artists: List[str]
    album: str
    duration_ms: int
    isrc: Optional[str] = None
    positions: List[int] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def display(self) -> str:
        return "%s — %s" % (", ".join(self.artists), self.name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "artists": list(self.artists),
            "album": self.album,
            "duration_ms": self.duration_ms,
            "isrc": self.isrc,
            "positions": list(self.positions),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Track":
        return cls(
            id=data["id"],
            name=data["name"],
            artists=list(data.get("artists", [])),
            album=data.get("album", ""),
            duration_ms=int(data.get("duration_ms", 0)),
            isrc=data.get("isrc"),
            positions=list(data.get("positions", [])),
        )


@dataclass
class ParsedAttrs:
    bitrate_kbps: Optional[float] = None
    bitrate_inferred: bool = False
    duration_s: Optional[float] = None
    duration_inferred: bool = False
    vbr: Optional[bool] = None


@dataclass
class Candidate:
    username: str
    virtual_path: str
    size: int
    extension: str
    file_attributes: Dict[str, Any]
    free_upload_slots: bool
    queue_position: int
    upload_speed: int
    confidence: float = 0.0
    quality: float = 0.0
    format_rank: int = 0
    bitrate_kbps: Optional[float] = None
    bitrate_inferred: bool = False
    duration_s: Optional[float] = None
    # False when the file's known bitrate is under --min-bitrate: kept for the
    # manual review picker, but never auto-queued.
    meets_min_bitrate: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "username": self.username,
            "virtual_path": self.virtual_path,
            "size": self.size,
            "extension": self.extension,
            "file_attributes": dict(self.file_attributes),
            "free_upload_slots": self.free_upload_slots,
            "queue_position": self.queue_position,
            "upload_speed": self.upload_speed,
            "confidence": round(self.confidence, 4),
            "quality": round(self.quality, 4),
            "format_rank": self.format_rank,
            "bitrate_kbps": self.bitrate_kbps,
            "bitrate_inferred": self.bitrate_inferred,
            "duration_s": self.duration_s,
            "meets_min_bitrate": self.meets_min_bitrate,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Candidate":
        return cls(
            username=data["username"],
            virtual_path=data["virtual_path"],
            size=int(data.get("size", 0)),
            extension=data.get("extension", ""),
            file_attributes=dict(data.get("file_attributes", {})),
            free_upload_slots=bool(data.get("free_upload_slots", False)),
            queue_position=int(data.get("queue_position", 0)),
            upload_speed=int(data.get("upload_speed", 0)),
            confidence=float(data.get("confidence", 0.0)),
            quality=float(data.get("quality", 0.0)),
            format_rank=int(data.get("format_rank", 0)),
            bitrate_kbps=data.get("bitrate_kbps"),
            bitrate_inferred=bool(data.get("bitrate_inferred", False)),
            duration_s=data.get("duration_s"),
            meets_min_bitrate=bool(data.get("meets_min_bitrate", True)),
        )
