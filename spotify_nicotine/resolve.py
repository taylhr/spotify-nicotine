"""Interactive picker for needs_review tracks.

Runs after (or separately from) the main download pass, so low-confidence
matches never hold up the run itself.
"""

from typing import Any, Callable, Dict

from spotify_nicotine.config import Config
from spotify_nicotine.models import StatusReason, Track, TrackStatus
from spotify_nicotine.orchestrator import enqueue_candidate, search_track
from spotify_nicotine.slsk_api import SlskClient
from spotify_nicotine.state import (
    StateStore,
    now_iso,
    set_track_status,
    tracks_with_status,
)
from spotify_nicotine import ui

HELP_TEXT = (
    "  <number> = queue that candidate   s = skip this track\n"
    "  r = re-search with a custom query q = quit (progress is saved)"
)


def run_resolve(
    cfg: Config,
    slsk: SlskClient,
    store: StateStore,
    state: Dict[str, Any],
    input_fn: Callable[[str], str] = input,
    log: Callable[[str], None] = print,
) -> None:
    review = tracks_with_status(state, TrackStatus.NEEDS_REVIEW)
    if not review:
        log("Nothing to review.")
        return

    log("%d track(s) to review." % len(review))
    total = len(review)

    for position, (_track_id, record) in enumerate(review, start=1):
        log("")
        log(ui.track_header(record, position, total))
        if ui.is_stale(record.get("search_completed_at")):
            log(
                "  (search results are over an hour old; slots/queue info may be "
                "stale — 'r' re-searches)"
            )
        for line in ui.candidate_table(record):
            log(line)

        while True:
            try:
                choice = input_fn(
                    "Choice [number=queue, s=skip, r=re-search, q=quit, ?=help]: "
                ).strip().lower()
            except EOFError:
                choice = "q"

            if choice == "q":
                store.save(state)
                log("Stopped; progress saved. Run 'resolve' again to continue.")
                return

            if choice == "s":
                set_track_status(record, TrackStatus.SKIPPED, StatusReason.USER_SKIPPED)
                store.save(state)
                log("  skipped.")
                break

            if choice == "r":
                query = input_fn("New search query: ").strip()
                if not query:
                    log("  empty query, unchanged.")
                    continue
                track = Track.from_dict(record["spotify"])
                candidates, tried = search_track(
                    slsk, track, cfg, query_override=query, log=log
                )
                for tried_query in tried:
                    if tried_query not in record["queries_tried"]:
                        record["queries_tried"].append(tried_query)
                record["candidates"] = [c.to_dict() for c in candidates]
                record["chosen_index"] = None
                record["search_completed_at"] = now_iso()
                store.save(state)
                for line in ui.candidate_table(record):
                    log(line)
                continue

            if choice.isdigit():
                index = int(choice) - 1
                candidates = record.get("candidates") or []
                if 0 <= index < len(candidates):
                    response = enqueue_candidate(slsk, cfg, record, index)
                    store.save(state)
                    note = (
                        " (was already in the Nicotine+ transfer list)"
                        if response.get("duplicate")
                        else ""
                    )
                    log("  queued from %s%s" % (candidates[index]["username"], note))
                    break
                log("  no candidate #%s." % choice)
                continue

            log(HELP_TEXT)

    log("")
    log("Review pass complete.")
