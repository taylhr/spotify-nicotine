"""Plain-text tables and summaries."""

import datetime
from typing import Any, Dict, List, Optional

from spotify_nicotine.models import TrackStatus


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    seconds = int(round(seconds))
    return "%d:%02d" % (seconds // 60, seconds % 60)


def fmt_size(size_bytes: int) -> str:
    return "%.1fMB" % (size_bytes / 1_000_000.0)


def track_header(record: Dict[str, Any], position: int, total: int) -> str:
    spotify = record.get("spotify", {})
    return "[%d/%d] %s — %s  [%s]  (album: %s)" % (
        position,
        total,
        ", ".join(spotify.get("artists", [])),
        spotify.get("name", ""),
        fmt_duration((spotify.get("duration_ms") or 0) / 1000.0),
        spotify.get("album", ""),
    )


def candidate_table(record: Dict[str, Any]) -> List[str]:
    candidates = record.get("candidates") or []
    if not candidates:
        return ["  (no stored candidates — use 'r' to search with a custom query)"]

    target_s = (record.get("spotify", {}).get("duration_ms") or 0) / 1000.0
    lines = [
        "  %3s  %-5s %-4s %-6s %-7s %-9s %-18s %-5s %s"
        % ("#", "conf", "fmt", "kbps", "dur", "size", "user", "slots", "queue")
    ]
    below_floor_present = False
    for index, cand in enumerate(candidates):
        bitrate = cand.get("bitrate_kbps")
        kbps = "-"
        if bitrate:
            kbps = "%d%s" % (round(bitrate), "*" if cand.get("bitrate_inferred") else "")
        if not cand.get("meets_min_bitrate", True):
            kbps += "!"
            below_floor_present = True
        duration = cand.get("duration_s")
        dur_text = fmt_duration(duration)
        if duration is not None and target_s:
            delta = duration - target_s
            if abs(delta) >= 1:
                dur_text += "%+d" % round(delta)
        lines.append(
            "  %3d  %-5.2f %-4s %-6s %-7s %-9s %-18s %-5s %d"
            % (
                index + 1,
                cand.get("confidence", 0.0),
                cand.get("extension", "?"),
                kbps,
                dur_text,
                fmt_size(int(cand.get("size") or 0)),
                cand.get("username", "?")[:18],
                "yes" if cand.get("free_upload_slots") else "no",
                cand.get("queue_position") or 0,
            )
        )
    if below_floor_present:
        lines.append(
            "  (! = below the configured minimum bitrate; never auto-downloaded, "
            "but you can pick it here)"
        )
    return lines


def is_stale(iso_timestamp: Optional[str], max_age_s: float = 3600) -> bool:
    if not iso_timestamp:
        return True
    try:
        then = datetime.datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return True
    age = datetime.datetime.now(datetime.timezone.utc) - then
    return age.total_seconds() > max_age_s


STATUS_ORDER = [
    TrackStatus.DOWNLOADED,
    TrackStatus.DOWNLOADING,
    TrackStatus.QUEUED,
    TrackStatus.NEEDS_REVIEW,
    TrackStatus.FAILED,
    TrackStatus.PENDING,
    TrackStatus.SKIPPED,
    TrackStatus.REMOVED,
]


def _display(record: Dict[str, Any]) -> str:
    spotify = record.get("spotify", {})
    return "%s — %s" % (", ".join(spotify.get("artists", [])), spotify.get("name", ""))


def summary_lines(state: Dict[str, Any], verbose: bool = False) -> List[str]:
    records = list(state.get("tracks", {}).values())
    by_status: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        by_status.setdefault(record.get("status", "?"), []).append(record)

    lines = ["", "Summary for playlist %r:" % state.get("playlist", {}).get("name", "")]
    for status in STATUS_ORDER:
        group = by_status.pop(status, [])
        if not group:
            continue
        lines.append("  %-13s %d" % (status, len(group)))
        if status in (TrackStatus.NEEDS_REVIEW, TrackStatus.FAILED) or verbose:
            for record in group:
                extra = ""
                if status == TrackStatus.QUEUED and record.get("transfer_status"):
                    extra = "  [%s since %s]" % (
                        record.get("transfer_status"),
                        record.get("updated_at", "?"),
                    )
                elif status == TrackStatus.DOWNLOADING:
                    extra = "  [%.0f%%]" % (record.get("transfer_progress_pct") or 0)
                elif record.get("status_reason"):
                    extra = "  (%s)" % record["status_reason"]
                lines.append("      - %s%s" % (_display(record), extra))
    for status, group in sorted(by_status.items()):
        lines.append("  %-13s %d" % (status, len(group)))

    skipped = state.get("skipped") or []
    if skipped:
        lines.append(
            "  (playlist items not searchable: %d — %s)"
            % (
                len(skipped),
                ", ".join(sorted({s.get("reason", "?") for s in skipped})),
            )
        )
    queued = [
        r
        for r in records
        if r.get("status") == TrackStatus.QUEUED and r.get("transfer_status") == "Queued"
    ]
    if queued:
        lines.append(
            "  Note: %d download(s) still waiting in remote queues; they continue "
            "inside Nicotine+ after this script exits. Re-run later to update "
            "their status." % len(queued)
        )
    return lines
