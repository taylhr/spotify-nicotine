"""Per-track pipeline: search -> score -> enqueue -> monitor, with resume.

Searches run through a single-threaded rolling-window scheduler: up to
``search_concurrency`` searches are in flight at once while a global rate
limit (``search_delay`` between dispatches) respects the Soulseek server's
search throttling. Fallback ("ladder") queries re-enter the same dispatch
queue, so thin first passes don't serialize the run.
"""

import collections
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from spotify_nicotine.config import Config
from spotify_nicotine.matching import (
    EXCELLENT_CONFIDENCE,
    core_title,
    normalize,
    score_results,
)
from spotify_nicotine.models import (
    Candidate,
    FINISHED_STATUS,
    QUEUED_TRANSFER_STATUS,
    StatusReason,
    TERMINAL_FAILURE_STATUSES,
    Track,
    TRANSFERRING_STATUSES,
    TRANSIENT_FAILURE_STATUSES,
    TrackStatus,
)
from spotify_nicotine.organize import (
    destination_folder,
    local_filename,
    organize_files,
)
from spotify_nicotine.slsk_api import SearchWatch, SlskApiError, SlskClient
from spotify_nicotine.state import (
    StateStore,
    now_iso,
    seconds_since,
    set_track_status,
    tracks_with_status,
)

Log = Callable[[str], None]

SCHEDULER_TICK_S = 1.0  # poll cadence of the search scheduler
TRANSFER_SWEEP_EVERY_S = 10.0
# Once a search is this old, probe its results once; if an excellent eligible
# match is already in, conclude immediately instead of waiting out the stream.
# The bar equals the "excellent" ranking tier: only matches that could never
# be outranked on confidence justify cutting the search short.
EARLY_CHECK_AFTER_S = 4.0
EARLY_STOP_CONFIDENCE = EXCELLENT_CONFIDENCE  # 0.9


def _has_eligible(candidates: List[Candidate], cfg: Config) -> bool:
    return any(
        c.confidence >= cfg.min_confidence and c.meets_min_bitrate
        for c in candidates
    )


def build_queries(track: Track) -> List[str]:
    """Query ladder: artist+title, then title-only, then parenthetical-free."""
    title_core = core_title(track.name)
    primary_artist = normalize(track.artists[0]) if track.artists else ""

    queries: List[str] = []
    if primary_artist:
        queries.append("%s %s" % (primary_artist, title_core))
    else:
        queries.append(title_core)

    if len(title_core.split()) >= 2 or len(title_core) >= 8:
        queries.append(title_core)

    if "(" in track.name or "[" in track.name:
        bare = core_title(track.name.split("(")[0].split("[")[0])
        if bare and primary_artist:
            queries.append("%s %s" % (primary_artist, bare))

    deduped: List[str] = []
    for query in queries:
        query = query.strip()
        if query and query not in deduped:
            deduped.append(query)
    return deduped


def search_track(
    slsk: SlskClient,
    track: Track,
    cfg: Config,
    query_override: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
    log: Log = lambda msg: None,
) -> Tuple[List[Candidate], List[str]]:
    """Run the query ladder; returns (ranked candidates, queries tried)."""
    queries = [query_override] if query_override else build_queries(track)
    collected_items: List[Dict[str, Any]] = []
    tried: List[str] = []
    candidates: List[Candidate] = []

    for query in queries:
        sleep(cfg.search_delay)  # politeness: soulseek servers ban fast searchers
        token = slsk.start_search(query)
        tried.append(query)
        items = slsk.collect_results(token, timeout_s=cfg.search_timeout)
        log("    query %r: %d results" % (query, len(items)))
        collected_items.extend(items)
        candidates = score_results(track, collected_items, cfg)
        # Broader fallback queries (title-only) surface same-title wrong-artist
        # files; never run them once an auto-queueable match exists.
        if _has_eligible(candidates, cfg):
            break

    return candidates, tried


# --- transfer bookkeeping -------------------------------------------------


def transfer_index(slsk: SlskClient) -> Dict[Tuple[str, str], Dict[str, Any]]:
    return {
        (t.get("username", ""), t.get("virtual_path", "")): t
        for t in slsk.downloads()
    }


def _chosen_candidate(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    index = record.get("chosen_index")
    candidates = record.get("candidates") or []
    if index is None or index >= len(candidates):
        return None
    return candidates[index]


def _attempted_indices(record: Dict[str, Any]) -> List[int]:
    return sorted({a["candidate_index"] for a in record.get("attempts", [])})


def _next_untried_index(record: Dict[str, Any], min_confidence: float) -> Optional[int]:
    """Next fallback candidate: untried, above the confidence gate, and not
    below the bitrate floor.

    Stored candidates deliberately include below-gate and below-floor entries
    for the review picker; automatic fallback must never enqueue those.
    """
    attempted = set(_attempted_indices(record))
    for index, candidate in enumerate(record.get("candidates") or []):
        if index in attempted:
            continue
        if float(candidate.get("confidence", 0.0)) < min_confidence:
            continue
        if not candidate.get("meets_min_bitrate", True):
            continue
        return index
    return None


def enqueue_candidate(
    slsk: SlskClient,
    cfg: Config,
    record: Dict[str, Any],
    candidate_index: int,
) -> Dict[str, Any]:
    """Enqueue one stored candidate and record the attempt on the track."""
    candidate = record["candidates"][candidate_index]
    response = slsk.enqueue(
        candidate["username"],
        candidate["virtual_path"],
        int(candidate.get("size") or 0),
        file_attributes=candidate.get("file_attributes") or {},
        folder_path=destination_folder(record, cfg),
    )
    record["chosen_index"] = candidate_index
    record.setdefault("attempts", []).append(
        {
            "candidate_index": candidate_index,
            "enqueued_at": now_iso(),
            "enqueue_response": {
                "queued": response.get("queued"),
                "duplicate": response.get("duplicate"),
                "status": response.get("status"),
            },
            "outcome": None,
            "finished_at": None,
        }
    )
    set_track_status(record, TrackStatus.QUEUED)
    return response


def _fail_or_fallback(
    record: Dict[str, Any],
    transfer_status: str,
    slsk: SlskClient,
    cfg: Config,
    log: Log,
    display: str,
) -> None:
    """Current attempt hit a dead end: mark it, try the next candidate."""
    attempts = record.get("attempts", [])
    if attempts and attempts[-1]["outcome"] is None:
        attempts[-1]["outcome"] = transfer_status
        attempts[-1]["finished_at"] = now_iso()

    next_index = _next_untried_index(record, cfg.min_confidence)
    if next_index is None or len(_attempted_indices(record)) >= 1 + cfg.max_fallbacks:
        set_track_status(
            record, TrackStatus.FAILED, StatusReason.EXHAUSTED_CANDIDATES
        )
        log("  FAILED %s (%s; no candidates left)" % (display, transfer_status))
        return
    log(
        "  %s: %s -> falling back to candidate #%d"
        % (display, transfer_status, next_index + 1)
    )
    enqueue_candidate(slsk, cfg, record, next_index)


def _current_attempt(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    attempts = record.get("attempts", [])
    if not attempts:
        return None
    attempt = attempts[-1]
    if attempt.get("outcome") is not None:
        return None
    if attempt.get("candidate_index") != record.get("chosen_index"):
        return None
    return attempt


def _nudge_or_drop(
    record: Dict[str, Any],
    transfer: Dict[str, Any],
    status: str,
    slsk: SlskClient,
    cfg: Config,
    log: Log,
    display: str,
) -> bool:
    """Handle a stalled transfer (remote-queued or transient failure).

    Like clicking "Retry" in the Nicotine+ Downloads tab: re-enqueueing the
    same (username, virtual_path) makes Nicotine+ re-request the file. Each
    candidate gets cfg.max_retries nudges, spaced cfg.stall_retry_mins apart;
    a queue position that is still moving counts as progress and defers the
    clock. After the budget is spent the uploader is dropped and the next
    candidate takes over. Returns True when the record changed.
    """
    if cfg.max_retries <= 0:
        return False
    attempt = _current_attempt(record)
    if attempt is None:
        return False

    # A moving remote queue is progress, not a stall.
    if status == QUEUED_TRANSFER_STATUS:
        position = int(transfer.get("queue_position") or 0)
        last_position = attempt.get("last_queue_position")
        if last_position is None or position < int(last_position):
            attempt["last_queue_position"] = position
            attempt["last_progress_at"] = now_iso()
            return True

    stalled_for = min(
        seconds_since(attempt.get("enqueued_at")),
        seconds_since(attempt.get("last_retry_at")),
        seconds_since(attempt.get("last_progress_at")),
    )
    if stalled_for < cfg.stall_retry_mins * 60:
        return False

    retries = int(attempt.get("retries") or 0)
    if retries >= cfg.max_retries:
        log(
            "  %s: still %s after %d nudges — dropping this uploader. NOTE: "
            "the abandoned transfer stays in Nicotine+ and may still finish "
            "later; remove it from the Downloads tab if you don't want a "
            "second copy." % (display, status, retries)
        )
        _fail_or_fallback(
            record,
            "%s (dropped after %d stalled retries)" % (status, retries),
            slsk,
            cfg,
            log,
            display,
        )
        return True

    candidate = record["candidates"][record["chosen_index"]]
    slsk.enqueue(
        candidate["username"],
        candidate["virtual_path"],
        int(candidate.get("size") or 0),
        file_attributes=candidate.get("file_attributes") or {},
        folder_path=destination_folder(record, cfg),
    )
    attempt["retries"] = retries + 1
    attempt["last_retry_at"] = now_iso()
    log(
        "  %s: %s — nudged the download (retry %d/%d)"
        % (display, status, retries + 1, cfg.max_retries)
    )
    return True


def sync_transfers(
    state: Dict[str, Any],
    slsk: SlskClient,
    cfg: Config,
    store: StateStore,
    reenqueue_absent: bool = False,
    log: Log = lambda msg: None,
) -> None:
    """Fold current /downloads statuses into track records.

    With reenqueue_absent (start-of-run reconcile), tracks whose transfer
    vanished from Nicotine+ are re-enqueued; mid-run sweeps leave them alone.
    Never blindly re-enqueue an existing transfer: enqueueing an entry that is
    already in the list can reactivate a finished one.
    """
    active = tracks_with_status(state, TrackStatus.QUEUED, TrackStatus.DOWNLOADING)
    if not active:
        return
    transfers = transfer_index(slsk)
    changed = False

    for _track_id, record in active:
        candidate = _chosen_candidate(record)
        if candidate is None:
            continue
        display = "%s — %s" % (
            ", ".join(record["spotify"].get("artists", [])),
            record["spotify"].get("name", ""),
        )
        key = (candidate["username"], candidate["virtual_path"])
        transfer = transfers.get(key)

        if transfer is None:
            if reenqueue_absent:
                log("  %s: not in Nicotine+ transfers, re-enqueueing" % display)
                enqueue_candidate(slsk, cfg, record, record["chosen_index"])
                changed = True
            continue

        status = transfer.get("status", "")
        record["transfer_status"] = status
        record["transfer_progress_pct"] = transfer.get("progress_pct", 0)

        if status == FINISHED_STATUS:
            attempts = record.get("attempts", [])
            if attempts and attempts[-1]["outcome"] is None:
                attempts[-1]["outcome"] = FINISHED_STATUS
                attempts[-1]["finished_at"] = now_iso()
            # Remember where the file landed while the transfer entry still
            # says so; --rename-files needs it, possibly on a later run.
            if transfer.get("folder_path"):
                record["local_folder"] = transfer["folder_path"]
                record["local_path"] = os.path.join(
                    transfer["folder_path"], local_filename(candidate["virtual_path"])
                )
            set_track_status(record, TrackStatus.DOWNLOADED)
            log("  DONE %s" % display)
            changed = True
        elif status in TERMINAL_FAILURE_STATUSES:
            _fail_or_fallback(record, status, slsk, cfg, log, display)
            changed = True
        elif status in TRANSFERRING_STATUSES:
            if record["status"] != TrackStatus.DOWNLOADING:
                set_track_status(record, TrackStatus.DOWNLOADING)
                changed = True
        elif status in TRANSIENT_FAILURE_STATUSES or status == QUEUED_TRANSFER_STATUS:
            # Connection blips and remote queues: Nicotine+ retries these on
            # its own, and there is no cancel API — falling back immediately
            # would risk downloading the track twice. Nudge instead.
            if record["status"] != TrackStatus.QUEUED:
                set_track_status(record, TrackStatus.QUEUED)
                changed = True
            if _nudge_or_drop(record, transfer, status, slsk, cfg, log, display):
                changed = True
        else:
            # Paused / anything unknown: keep waiting, never nudge (a pause
            # is a deliberate user action in the GUI).
            if record["status"] != TrackStatus.QUEUED:
                set_track_status(record, TrackStatus.QUEUED)
                changed = True

    if changed:
        store.save(state)


def reconcile(
    state: Dict[str, Any],
    slsk: SlskClient,
    cfg: Config,
    store: StateStore,
    log: Log = lambda msg: None,
) -> None:
    sync_transfers(state, slsk, cfg, store, reenqueue_absent=True, log=log)


def monitor_until_settled(
    state: Dict[str, Any],
    slsk: SlskClient,
    cfg: Config,
    store: StateStore,
    log: Log = lambda msg: None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    poll_interval: float = 5.0,
) -> None:
    """Watch transfers until nothing is actively moving or the budget expires.

    Tracks stuck in a remote user's queue do not block completion; they stay
    'queued' in state and are reported by the summary.
    """
    if cfg.monitor_mins <= 0:
        return
    if not tracks_with_status(state, TrackStatus.QUEUED, TrackStatus.DOWNLOADING):
        return
    deadline = clock() + cfg.monitor_mins * 60

    while clock() < deadline:
        sleep(poll_interval)  # grace for fresh enqueues to start moving
        sync_transfers(state, slsk, cfg, store, log=log)
        actively_moving = [
            record
            for _tid, record in tracks_with_status(state, TrackStatus.DOWNLOADING)
        ]
        if not actively_moving:
            return


# --- concurrent search scheduler ------------------------------------------


@dataclass
class _SearchJob:
    """One track moving through the search query ladder."""

    position: int
    track_id: str
    record: Dict[str, Any]
    track: Track
    queries: List[str]
    query_index: int = 0
    items: List[Dict[str, Any]] = field(default_factory=list)
    token: Optional[int] = None
    watch: Optional[SearchWatch] = None
    early_checked: bool = False


def _conclude_track(
    cfg: Config,
    state: Dict[str, Any],
    slsk: SlskClient,
    store: StateStore,
    record: Dict[str, Any],
    track: Track,
    candidates: List[Candidate],
    log: Log,
    label: str,
) -> None:
    """Record search results and decide: enqueue, needs_review, or dry-run."""
    record["search_completed_at"] = now_iso()
    record["candidates"] = [c.to_dict() for c in candidates]
    record["chosen_index"] = None

    eligible = [
        (index, c)
        for index, c in enumerate(candidates)
        if c.confidence >= cfg.min_confidence and c.meets_min_bitrate
    ]
    confident_below_floor = [
        c
        for c in candidates
        if c.confidence >= cfg.min_confidence and not c.meets_min_bitrate
    ]

    log("%s %s" % (label, track.display))
    if eligible:
        best_index, best = eligible[0]
        if cfg.dry_run:
            log(
                "  would enqueue: %s [%s%s, conf %.2f] from %s"
                % (
                    best.virtual_path.rsplit("\\", 1)[-1],
                    best.extension,
                    " %dk" % best.bitrate_kbps if best.bitrate_kbps else "",
                    best.confidence,
                    best.username,
                )
            )
            set_track_status(record, TrackStatus.PENDING)
        else:
            response = enqueue_candidate(slsk, cfg, record, best_index)
            note = " (already in Nicotine+ transfer list)" if response.get("duplicate") else ""
            log(
                "  queued: %s [conf %.2f] from %s%s"
                % (
                    best.virtual_path.rsplit("\\", 1)[-1],
                    best.confidence,
                    best.username,
                    note,
                )
            )
    elif confident_below_floor:
        best = max(confident_below_floor, key=lambda c: c.confidence)
        set_track_status(
            record, TrackStatus.NEEDS_REVIEW, StatusReason.BELOW_MIN_BITRATE
        )
        log(
            "  NOTE: match found only below the minimum bitrate (%s %s, "
            "confidence %.2f) — not auto-downloaded. Accept it via "
            "'resolve' or lower --min-bitrate."
            % (
                "%dk" % round(best.bitrate_kbps) if best.bitrate_kbps else "?k",
                best.extension,
                best.confidence,
            )
        )
    elif candidates:
        set_track_status(
            record, TrackStatus.NEEDS_REVIEW, StatusReason.BELOW_THRESHOLD
        )
        log(
            "  needs review: best confidence %.2f < %.2f (%d candidates kept)"
            % (candidates[0].confidence, cfg.min_confidence, len(candidates))
        )
    else:
        set_track_status(record, TrackStatus.NEEDS_REVIEW, StatusReason.NO_RESULTS)
        log("  needs review: no usable results")

    store.save(state)


# A query with so many hits on Soulseek that an empty response means the
# server is not answering us, rather than that the music is obscure.
CANARY_QUERY = "the beatles"


def _server_answers_searches(
    slsk: SlskClient,
    cfg: Config,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> bool:
    """Decide whether an empty-result streak means throttling or obscurity.

    Unfindable tracks bunch together (their ladder retries queue behind every
    first pass), so a streak alone is not proof. One search for something
    universally shared settles it.
    """
    sleep(cfg.search_delay)
    try:
        token = slsk.start_search(CANARY_QUERY)
        watch = SearchWatch(clock(), cfg.search_timeout)
        while True:
            sleep(SCHEDULER_TICK_S)
            total = int(slsk.get_results(token, offset=0, limit=1).get("total", 0))
            if watch.record_poll(total, clock()):
                return total > 0
    except SlskApiError:
        return True  # inconclusive: never abort a run on a failed probe


def _abort_throttled(
    state: Dict[str, Any],
    store: StateStore,
    empty_streak: List[str],
    in_flight: List["_SearchJob"],
    dispatch: "Deque[_SearchJob]",
    log: Log,
) -> None:
    """The Soulseek server has stopped answering our searches.

    Every search now comes back empty, so the "no results" verdicts already
    recorded are false negatives. Undo them and park everything unfinished
    back at 'pending' so a later run re-searches instead of leaving the
    playlist permanently marked as unfindable.
    """
    for track_id in empty_streak:
        record = state.get("tracks", {}).get(track_id)
        if record is not None and record.get("status") == TrackStatus.NEEDS_REVIEW:
            set_track_status(record, TrackStatus.PENDING)
    for job in list(in_flight) + list(dispatch):
        set_track_status(job.record, TrackStatus.PENDING)

    if isinstance(state.get("last_run"), dict):
        state["last_run"]["throttled_at"] = now_iso()
    store.save(state)

    log("")
    log(
        "STOPPING: %d searches in a row came back completely empty. That is "
        "what the Soulseek server's search rate limit looks like — it has "
        "stopped answering this client." % len(empty_streak)
    )
    log(
        "  No tracks were marked unfindable; the affected ones are back to "
        "'pending'. Wait ~15-30 minutes, then re-run the same command to "
        "resume (raise --search-delay if it keeps happening). Downloads "
        "already queued in Nicotine+ are unaffected and keep going."
    )


def _run_search_scheduler(
    cfg: Config,
    state: Dict[str, Any],
    slsk: SlskClient,
    store: StateStore,
    pending: List[Tuple[str, Dict[str, Any]]],
    log: Log,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Rolling-window scheduler: overlap search waiting across tracks.

    One thread, one loop. Each tick it (1) dispatches a new search when the
    in-flight window has room AND the global rate budget allows, (2) polls
    every in-flight search and retires matured ones (scoring, enqueueing, or
    re-queueing the track for its next ladder query), (3) periodically sweeps
    transfer statuses. All progress is saved at each transition, so Ctrl+C
    keeps its resume guarantees; interrupted searches simply rerun next time.
    """
    dispatch: Deque[_SearchJob] = collections.deque()
    for position, (track_id, record) in enumerate(pending, start=1):
        track = Track.from_dict(record["spotify"])
        dispatch.append(
            _SearchJob(position, track_id, record, track, build_queries(track))
        )

    total = len(dispatch)
    in_flight: List[_SearchJob] = []
    empty_streak: List[str] = []  # consecutively concluded with zero results
    done = 0
    next_dispatch_at = clock()
    last_sweep = clock()

    while dispatch or in_flight:
        now = clock()

        # 1. dispatch one search when the window and rate budget allow
        if (
            dispatch
            and len(in_flight) < cfg.search_concurrency
            and now >= next_dispatch_at
        ):
            job = dispatch.popleft()
            query = job.queries[job.query_index]
            if job.query_index == 0:
                set_track_status(job.record, TrackStatus.SEARCHING)
                store.save(state)
            job.token = slsk.start_search(query)
            if query not in job.record["queries_tried"]:
                job.record["queries_tried"].append(query)
            job.watch = SearchWatch(now, cfg.search_timeout)
            job.early_checked = False
            in_flight.append(job)
            next_dispatch_at = now + cfg.search_delay
            retry = " (query %d)" % (job.query_index + 1) if job.query_index else ""
            log("[%d/%d] searching%s: %s" % (job.position, total, retry, query))

        # 2. poll in-flight searches; retire the matured ones
        for job in list(in_flight):
            evicted = False
            matured = False
            fetched: Optional[List[Dict[str, Any]]] = None
            try:
                page = slsk.get_results(job.token, offset=0, limit=1)
            except SlskApiError as exc:
                if exc.status != 400:
                    raise
                # The plugin's LRU cache dropped this token (only ~20 kept).
                evicted = True
                matured = True
            else:
                poll_now = clock()
                total_now = int(page.get("total", 0))
                matured = job.watch.record_poll(total_now, poll_now)
                if (
                    not matured
                    and not job.early_checked
                    and total_now > 0
                    and poll_now - job.watch.started_at >= EARLY_CHECK_AFTER_S
                ):
                    # Popular searches stream results for the whole window;
                    # probe once and stop as soon as an excellent match is in.
                    job.early_checked = True
                    try:
                        probe = slsk.fetch_results(
                            job.token, min(total_now, job.watch.max_results)
                        )
                    except SlskApiError as exc:
                        if exc.status != 400:
                            raise
                        evicted = True
                        matured = True
                        probe = []
                    if not evicted:
                        probe_candidates = score_results(
                            job.track, job.items + probe, cfg
                        )
                        bar = max(EARLY_STOP_CONFIDENCE, cfg.min_confidence)
                        if any(
                            c.meets_min_bitrate and c.confidence >= bar
                            for c in probe_candidates
                        ):
                            matured = True
                            fetched = probe
            if not matured:
                continue

            in_flight.remove(job)
            if fetched is None:
                fetched = []
                if not evicted and job.watch.last_total > 0:
                    try:
                        fetched = slsk.fetch_results(
                            job.token,
                            min(job.watch.last_total, job.watch.max_results),
                        )
                    except SlskApiError as exc:
                        if exc.status != 400:
                            raise
                        evicted = True
            if evicted:
                log(
                    "  (results for %s were evicted from the plugin cache; "
                    "using what was already collected)" % job.track.display
                )
            job.items.extend(fetched)

            candidates = score_results(job.track, job.items, cfg)
            job.query_index += 1
            # Broader fallback queries pollute (same title, wrong artist);
            # only ladder further while nothing auto-queueable exists.
            if not _has_eligible(candidates, cfg) and job.query_index < len(job.queries):
                dispatch.append(job)  # ladder: try the next query later
            else:
                done += 1
                _conclude_track(
                    cfg,
                    state,
                    slsk,
                    store,
                    job.record,
                    job.track,
                    candidates,
                    log,
                    "[%d/%d done]" % (done, total),
                )
                # A run of tracks where every query returned literally nothing
                # means the server stopped answering, not that the music does
                # not exist.
                if job.items:
                    empty_streak.clear()
                else:
                    empty_streak.append(job.track_id)
                    if (
                        cfg.max_empty_streak
                        and len(empty_streak) >= cfg.max_empty_streak
                    ):
                        if _server_answers_searches(slsk, cfg, sleep, clock):
                            log(
                                "  (%d tracks in a row found nothing, but the "
                                "server is still answering searches — these "
                                "really are hard to find)" % len(empty_streak)
                            )
                            empty_streak.clear()
                        else:
                            _abort_throttled(
                                state, store, empty_streak, in_flight, dispatch, log
                            )
                            return

        # 3. periodic transfer sweep so early failures fall back promptly
        if not cfg.dry_run and now - last_sweep >= TRANSFER_SWEEP_EVERY_S:
            sync_transfers(state, slsk, cfg, store, log=log)
            last_sweep = now

        if dispatch or in_flight:
            sleep(SCHEDULER_TICK_S)


# --- main entry -----------------------------------------------------------


def run_download(
    cfg: Config,
    state: Dict[str, Any],
    slsk: SlskClient,
    store: StateStore,
    log: Log = lambda msg: None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Search-and-enqueue pass over pending tracks (playlist already merged)."""
    state["last_run"] = {
        "at": now_iso(),
        "min_confidence": cfg.min_confidence,
        "formats": list(cfg.formats),
        "prefer_bitrate": cfg.prefer_bitrate,
        "dry_run": cfg.dry_run,
    }

    reconcile(state, slsk, cfg, store, log=log)

    pending = tracks_with_status(state, TrackStatus.PENDING)
    if cfg.limit is not None:
        pending = pending[: cfg.limit]

    if pending:
        log(
            "Searching %d track(s): up to %d at once, at most one search "
            "dispatch per %.1fs..."
            % (len(pending), cfg.search_concurrency, cfg.search_delay)
        )
        _run_search_scheduler(cfg, state, slsk, store, pending, log, sleep, clock)

    if not cfg.dry_run:
        monitor_until_settled(
            state, slsk, cfg, store, log=log, sleep=sleep, clock=clock
        )
        organize_files(state, cfg, store, log=log)
