from typing import Any, Dict, List, Optional

import pytest

from spotify_nicotine.models import StatusReason, TrackStatus
from spotify_nicotine.orchestrator import (
    build_queries,
    monitor_until_settled,
    reconcile,
    run_download,
    search_track,
)
from spotify_nicotine.state import StateStore, merge_playlist, new_state

from tests.conftest import make_cfg, make_item, make_track
from tests.fakes import FakeSlsk, TickClock

META = {"id": "pl1", "name": "Mix", "snapshot_id": "snap1"}


def good_items(count=5):
    return [
        make_item(
            "Music%d\\Eagles\\05 - Hotel California.mp3" % i,
            username="user%d" % i,
            file_attributes={"0": 320, "1": 391},
        )
        for i in range(count)
    ]


def setup_state(tmp_path, *tracks):
    store = StateStore(str(tmp_path), "pl1")
    state = new_state(META)
    merge_playlist(state, META, list(tracks), [])
    return store, state


def no_sleep(_seconds):
    pass


def run_scheduler(cfg, state, slsk, store, log=None):
    """Drive run_download under a fake clock; returns the TickClock."""
    tick = TickClock()
    slsk.clock = tick.clock
    run_download(
        cfg,
        state,
        slsk,
        store,
        log=log if log is not None else (lambda msg: None),
        sleep=tick.sleep,
        clock=tick.clock,
    )
    return tick


def make_tracks(count):
    tracks = []
    for i in range(count):
        track = make_track(track_id="t%d" % i)
        track.positions = [i]
        tracks.append(track)
    return tracks


class TestBuildQueries:
    def test_ladder_shape(self):
        track = make_track(name="Hotel California (2013 Remaster)")
        queries = build_queries(track)
        assert queries[0] == "eagles hotel california"
        assert "hotel california" in queries

    def test_short_title_no_bare_query(self):
        track = make_track(name="Go", artists=["Moby"])
        assert build_queries(track) == ["moby go"]


class TestSearchTrack:
    def test_stops_ladder_when_eligible_match_found(self, cfg):
        # A single eligible match is enough: broader fallback queries would
        # only pollute with same-title/wrong-artist files.
        slsk = FakeSlsk({"default": good_items(1)})
        candidates, tried = search_track(slsk, make_track(), cfg, sleep=no_sleep)
        assert len(tried) == 1
        assert len(candidates) == 1

    def test_ladder_continues_when_nothing_eligible(self, cfg):
        weak = [
            make_item(
                "Eagles - Hotel California (Live).mp3", username="liveuser", size=0
            )
        ]
        slsk = FakeSlsk({"default": weak})
        candidates, tried = search_track(slsk, make_track(), cfg, sleep=no_sleep)
        assert len(tried) >= 2
        assert len(candidates) == 1  # weak candidate kept for review

    def test_query_override_used_verbatim(self, cfg):
        slsk = FakeSlsk({"my custom query": good_items(6)})
        candidates, tried = search_track(
            slsk, make_track(), cfg, query_override="my custom query", sleep=no_sleep
        )
        assert tried == ["my custom query"]
        assert slsk.searches == ["my custom query"]
        assert candidates

    def test_politeness_delay_applied(self, cfg):
        tick = TickClock()
        slsk = FakeSlsk({"default": good_items(6)})
        search_track(slsk, make_track(), cfg, sleep=tick.sleep)
        assert tick.sleeps[0] == cfg.search_delay


class TestRunDownload:
    def test_happy_path_enqueues_best(self, tmp_path, cfg):
        store, state = setup_state(tmp_path, make_track(track_id="t1"))
        slsk = FakeSlsk({"default": good_items(6)})
        run_scheduler(cfg, state, slsk, store)

        record = state["tracks"]["t1"]
        assert record["status"] == TrackStatus.QUEUED
        assert record["chosen_index"] == 0
        assert len(record["attempts"]) == 1
        assert record["candidates"]
        assert len(slsk.enqueues) == 1
        assert slsk.enqueues[0]["file_attributes"] == {"0": 320, "1": 391}
        # persisted
        assert store.load()["tracks"]["t1"]["status"] == TrackStatus.QUEUED

    def test_below_threshold_needs_review_never_blocks(self, tmp_path, cfg):
        weak = [
            make_item(
                "Eagles - Hotel California (Live).mp3",
                username="liveuser",
                size=0,
            )
        ]
        store, state = setup_state(
            tmp_path, make_track(track_id="t1"), make_track(track_id="t2")
        )
        slsk = FakeSlsk(
            {
                "eagles hotel california": weak + good_items(6),
                "default": weak,
            }
        )
        # t1 and t2 are the same song here; both find results, first gets queued
        run_scheduler(cfg, state, slsk, store)
        assert state["tracks"]["t1"]["status"] == TrackStatus.QUEUED
        assert state["tracks"]["t2"]["status"] == TrackStatus.QUEUED

    def test_weak_only_results_marked_for_review(self, tmp_path, cfg):
        weak = [
            make_item(
                "Eagles - Hotel California (Live).mp3", username="liveuser", size=0
            )
        ]
        store, state = setup_state(tmp_path, make_track(track_id="t1"))
        slsk = FakeSlsk({"default": weak})
        run_scheduler(cfg, state, slsk, store)

        record = state["tracks"]["t1"]
        assert record["status"] == TrackStatus.NEEDS_REVIEW
        assert record["status_reason"] == StatusReason.BELOW_THRESHOLD
        assert record["candidates"]  # kept for the resolve picker
        assert slsk.enqueues == []

    def test_only_below_floor_match_notifies(self, tmp_path, cfg):
        # A perfect match exists but only at 128kbps (< default 192 floor):
        # never auto-downloaded, flagged for review, user notified.
        lofi = [
            make_item(
                "Music\\Eagles\\05 - Hotel California.mp3",
                username="lofi",
                file_attributes={"0": 128, "1": 391},
            )
        ]
        store, state = setup_state(tmp_path, make_track(track_id="t1"))
        slsk = FakeSlsk({"default": lofi})
        messages = []
        run_scheduler(cfg, state, slsk, store, log=messages.append)

        record = state["tracks"]["t1"]
        assert record["status"] == TrackStatus.NEEDS_REVIEW
        assert record["status_reason"] == StatusReason.BELOW_MIN_BITRATE
        assert record["candidates"][0]["meets_min_bitrate"] is False
        assert slsk.enqueues == []
        assert any("below the minimum bitrate" in m for m in messages)

    def test_no_results_marked_for_review(self, tmp_path, cfg):
        store, state = setup_state(tmp_path, make_track(track_id="t1"))
        slsk = FakeSlsk({})
        run_scheduler(cfg, state, slsk, store)

        record = state["tracks"]["t1"]
        assert record["status"] == TrackStatus.NEEDS_REVIEW
        assert record["status_reason"] == StatusReason.NO_RESULTS
        assert len(slsk.searches) >= 2  # ladder exhausted

    def test_dry_run_no_enqueue_stays_pending(self, tmp_path):
        cfg = make_cfg(dry_run=True)
        store, state = setup_state(tmp_path, make_track(track_id="t1"))
        slsk = FakeSlsk({"default": good_items(6)})
        run_scheduler(cfg, state, slsk, store)

        record = state["tracks"]["t1"]
        assert record["status"] == TrackStatus.PENDING
        assert record["candidates"]  # scores were recorded for inspection
        assert slsk.enqueues == []

    def test_limit_processes_first_n(self, tmp_path):
        cfg = make_cfg(limit=1)
        t1 = make_track(track_id="t1")
        t1.positions = [0]
        t2 = make_track(track_id="t2")
        t2.positions = [1]
        store, state = setup_state(tmp_path, t1, t2)
        slsk = FakeSlsk({"default": good_items(6)})
        run_scheduler(cfg, state, slsk, store)

        assert state["tracks"]["t1"]["status"] == TrackStatus.QUEUED
        assert state["tracks"]["t2"]["status"] == TrackStatus.PENDING

    def test_dest_dir_passed_through(self, tmp_path):
        cfg = make_cfg(dest_dir="/music/playlist")
        store, state = setup_state(tmp_path, make_track(track_id="t1"))
        slsk = FakeSlsk({"default": good_items(6)})
        run_scheduler(cfg, state, slsk, store)
        assert slsk.enqueues[0]["folder_path"] == "/music/playlist"


class TestReconcile:
    def _queued_state(self, tmp_path, cfg, candidate_confs=(0.9, 0.8, 0.5)):
        """State with one track already queued on candidate 0."""
        store, state = setup_state(tmp_path, make_track(track_id="t1"))
        slsk = FakeSlsk({"default": good_items(6)})
        run_scheduler(cfg, state, slsk, store)
        record = state["tracks"]["t1"]
        # rewrite candidates to controlled confidences
        for index, conf in enumerate(candidate_confs):
            record["candidates"][index]["confidence"] = conf
        del record["candidates"][len(candidate_confs):]
        return store, state, slsk, record

    def test_finished_becomes_downloaded(self, tmp_path, cfg):
        store, state, slsk, record = self._queued_state(tmp_path, cfg)
        chosen = record["candidates"][record["chosen_index"]]
        slsk.set_status(chosen["username"], chosen["virtual_path"], "Finished")

        reconcile(state, slsk, cfg, store)
        assert record["status"] == TrackStatus.DOWNLOADED
        assert record["attempts"][-1]["outcome"] == "Finished"

    def test_terminal_failure_falls_back_to_next_gated(self, tmp_path, cfg):
        store, state, slsk, record = self._queued_state(tmp_path, cfg)
        chosen = record["candidates"][0]
        slsk.set_status(chosen["username"], chosen["virtual_path"], "Connection timeout")

        reconcile(state, slsk, cfg, store)
        assert record["status"] == TrackStatus.QUEUED
        assert record["chosen_index"] == 1
        assert record["attempts"][0]["outcome"] == "Connection timeout"
        assert len(slsk.enqueues) == 2

    def test_fallback_skips_below_gate_candidates(self, tmp_path, cfg):
        # candidate 1 below gate: after 0 fails, only... none left -> failed
        store, state, slsk, record = self._queued_state(
            tmp_path, cfg, candidate_confs=(0.9, 0.5)
        )
        chosen = record["candidates"][0]
        slsk.set_status(chosen["username"], chosen["virtual_path"], "Cancelled")

        reconcile(state, slsk, cfg, store)
        assert record["status"] == TrackStatus.FAILED
        assert record["status_reason"] == StatusReason.EXHAUSTED_CANDIDATES
        assert len(slsk.enqueues) == 1  # no second enqueue

    def test_fallback_skips_below_floor_candidates(self, tmp_path, cfg):
        store, state, slsk, record = self._queued_state(
            tmp_path, cfg, candidate_confs=(0.9, 0.8, 0.7)
        )
        record["candidates"][1]["meets_min_bitrate"] = False
        chosen = record["candidates"][0]
        slsk.set_status(chosen["username"], chosen["virtual_path"], "Cancelled")

        reconcile(state, slsk, cfg, store)
        assert record["status"] == TrackStatus.QUEUED
        assert record["chosen_index"] == 2  # skipped the flagged #1

    def test_fallback_budget_exhausted(self, tmp_path):
        cfg = make_cfg(max_fallbacks=1)
        store, state, slsk, record = self._queued_state(
            tmp_path, cfg, candidate_confs=(0.9, 0.8, 0.7)
        )
        first = record["candidates"][0]
        slsk.set_status(first["username"], first["virtual_path"], "Cancelled")
        reconcile(state, slsk, cfg, store)
        assert record["chosen_index"] == 1

        second = record["candidates"][1]
        slsk.set_status(second["username"], second["virtual_path"], "Cancelled")
        reconcile(state, slsk, cfg, store)
        # budget = 1 + max_fallbacks = 2 distinct candidates tried
        assert record["status"] == TrackStatus.FAILED

    def test_absent_transfer_reenqueued(self, tmp_path, cfg):
        store, state, slsk, record = self._queued_state(tmp_path, cfg)
        chosen = record["candidates"][0]
        slsk.drop(chosen["username"], chosen["virtual_path"])

        reconcile(state, slsk, cfg, store)
        assert record["status"] == TrackStatus.QUEUED
        assert record["chosen_index"] == 0
        assert len(slsk.enqueues) == 2
        assert slsk.enqueues[-1]["virtual_path"] == chosen["virtual_path"]

    def test_transferring_marked_downloading(self, tmp_path, cfg):
        store, state, slsk, record = self._queued_state(tmp_path, cfg)
        chosen = record["candidates"][0]
        slsk.set_status(chosen["username"], chosen["virtual_path"], "Transferring")

        reconcile(state, slsk, cfg, store)
        assert record["status"] == TrackStatus.DOWNLOADING


class TestMonitor:
    def test_monitor_waits_for_transfer_then_settles(self, tmp_path, cfg):
        store, state = setup_state(tmp_path, make_track(track_id="t1"))
        slsk = FakeSlsk({"default": good_items(6)})
        run_scheduler(cfg, state, slsk, store)
        record = state["tracks"]["t1"]
        chosen = record["candidates"][record["chosen_index"]]
        slsk.set_status(chosen["username"], chosen["virtual_path"], "Transferring")

        tick = TickClock()
        polls = {"count": 0}
        original_downloads = slsk.downloads

        def downloads_with_progress():
            polls["count"] += 1
            if polls["count"] >= 3:
                slsk.set_status(chosen["username"], chosen["virtual_path"], "Finished")
            return original_downloads()

        slsk.downloads = downloads_with_progress
        monitor_until_settled(
            state, slsk, cfg, store, sleep=tick.sleep, clock=tick.clock
        )
        assert record["status"] == TrackStatus.DOWNLOADED

    def test_monitor_respects_deadline_with_stuck_queue(self, tmp_path):
        cfg = make_cfg(monitor_mins=0.5)  # 30 seconds
        store, state = setup_state(tmp_path, make_track(track_id="t1"))
        slsk = FakeSlsk({"default": good_items(6)})
        run_scheduler(cfg, state, slsk, store)
        record = state["tracks"]["t1"]
        chosen = record["candidates"][record["chosen_index"]]
        slsk.set_status(chosen["username"], chosen["virtual_path"], "Transferring")

        tick = TickClock()
        monitor_until_settled(
            state, slsk, cfg, store, sleep=tick.sleep, clock=tick.clock
        )
        # never finished; loop must have given up at the deadline
        assert tick.now >= 30
        assert record["status"] == TrackStatus.DOWNLOADING

    def test_monitor_disabled(self, tmp_path):
        cfg = make_cfg(monitor_mins=0)
        store, state = setup_state(tmp_path, make_track(track_id="t1"))
        slsk = FakeSlsk({"default": good_items(6)})
        run_scheduler(cfg, state, slsk, store)
        tick = TickClock()
        monitor_until_settled(state, slsk, cfg, store, sleep=tick.sleep, clock=tick.clock)
        assert tick.sleeps == []


class TestScheduler:
    def test_dispatch_rate_limited(self, tmp_path):
        cfg = make_cfg(search_delay=2.0)
        store, state = setup_state(tmp_path, *make_tracks(3))
        slsk = FakeSlsk({"default": good_items(6)})
        run_scheduler(cfg, state, slsk, store)

        assert len(slsk.search_times) == 3
        gaps = [b - a for a, b in zip(slsk.search_times, slsk.search_times[1:])]
        assert all(gap >= cfg.search_delay for gap in gaps)

    def test_searches_overlap_and_cut_wall_clock(self, tmp_path):
        cfg = make_cfg(search_delay=1.0, search_concurrency=6)
        store, state = setup_state(tmp_path, *make_tracks(6))
        slsk = FakeSlsk({"default": good_items(6)})
        tick = run_scheduler(cfg, state, slsk, store)

        # every track ended up queued...
        for i in range(6):
            assert state["tracks"]["t%d" % i]["status"] == TrackStatus.QUEUED
        # ...and all six searches were dispatched within the first few ticks,
        # even though each needs >=4s to mature: they overlapped.
        assert len(slsk.search_times) == 6
        assert max(slsk.search_times) <= 8
        # sequential would need >= 6 * (delay + ~5s maturity) = ~36 ticks
        assert tick.now < 25

    def test_window_limits_in_flight(self, tmp_path):
        cfg = make_cfg(search_concurrency=2, search_delay=1.0)
        store, state = setup_state(tmp_path, *make_tracks(4))
        slsk = FakeSlsk({})  # zero results: each search matures at ~8s
        run_scheduler(cfg, state, slsk, store)

        # first two dispatch immediately; the third has to wait for a free
        # slot, which only opens when a zero-result search concludes (~8s)
        assert slsk.search_times[0] <= 1
        assert slsk.search_times[1] <= 2
        assert slsk.search_times[2] >= 8

    def test_ladder_requeues_through_scheduler(self, tmp_path):
        cfg = make_cfg(search_delay=1.0)
        weak = [
            make_item(
                "Eagles - Hotel California (Live).mp3", username="liveuser", size=0
            )
        ]
        store, state = setup_state(tmp_path, *make_tracks(2))
        slsk = FakeSlsk(
            {
                "eagles hotel california": weak,  # nothing eligible on pass 1
                "hotel california": good_items(6),
            }
        )
        run_scheduler(cfg, state, slsk, store)

        assert state["tracks"]["t0"]["status"] == TrackStatus.QUEUED
        assert state["tracks"]["t1"]["status"] == TrackStatus.QUEUED
        # first pass (q1) for both tracks ran before either second pass:
        # non-eligible first queries re-enter the back of the dispatch queue
        assert slsk.searches == [
            "eagles hotel california",
            "eagles hotel california",
            "hotel california",
            "hotel california",
        ]

    def test_ladder_stops_once_eligible_exists(self, tmp_path, cfg):
        store, state = setup_state(tmp_path, make_track(track_id="t1"))
        slsk = FakeSlsk({"eagles hotel california": good_items(1)})
        run_scheduler(cfg, state, slsk, store)

        assert state["tracks"]["t1"]["status"] == TrackStatus.QUEUED
        assert slsk.searches == ["eagles hotel california"]  # no title-only pass

    def test_early_stop_on_growing_popular_search(self, tmp_path):
        # Popular searches never stabilize (results trickle in continuously);
        # the 4s probe must conclude them instead of the 20s timeout.
        cfg = make_cfg(monitor_mins=0)
        store, state = setup_state(tmp_path, make_track(track_id="t1"))
        slsk = FakeSlsk({"default": good_items(6)})
        slsk.grow_totals = True
        tick = run_scheduler(cfg, state, slsk, store)

        assert state["tracks"]["t1"]["status"] == TrackStatus.QUEUED
        assert tick.now <= 8, "expected early-stop at ~4-5s, got %.0fs" % tick.now

    def test_incident_regression_correct_flac_wins_without_fallback(self, tmp_path, cfg):
        """A confident FLAC + a sub-floor mp3 + a private file: the FLAC must
        be queued from the first query, with no polluting title-only pass."""
        first_query_results = [
            make_item(
                "Music\\Eagles\\Hotel California\\05 - Hotel California.flac",
                username="flac_user",
                size=30_000_000,
                file_attributes={"1": 391, "4": 44100, "5": 16},
            ),
            make_item(
                "Shares\\Eagles - Hotel California.mp3",
                username="lofi_user",
                file_attributes={"0": 128, "1": 391},
            ),
            make_item(
                "Private\\Eagles - Hotel California.mp3",
                username="locked_user",
                file_attributes={"0": 320, "1": 391},
                is_private=True,
            ),
        ]
        store, state = setup_state(tmp_path, make_track(track_id="t1"))
        slsk = FakeSlsk({"eagles hotel california": first_query_results})
        run_scheduler(cfg, state, slsk, store)

        record = state["tracks"]["t1"]
        assert record["status"] == TrackStatus.QUEUED
        assert slsk.searches == ["eagles hotel california"]
        assert len(slsk.enqueues) == 1
        assert slsk.enqueues[0]["username"] == "flac_user"

    def test_evicted_tokens_handled_gracefully(self, tmp_path, cfg):
        store, state = setup_state(tmp_path, *make_tracks(2))
        slsk = FakeSlsk({"default": good_items(6)})
        slsk.evict_tokens = True
        messages = []
        run_scheduler(cfg, state, slsk, store, log=messages.append)

        for track_id in ("t0", "t1"):
            record = state["tracks"][track_id]
            assert record["status"] == TrackStatus.NEEDS_REVIEW
            assert record["status_reason"] == StatusReason.NO_RESULTS
        assert any("evicted" in m for m in messages)
        # ladder still walked both queries per track without crashing
        assert len(slsk.searches) == 4

    def test_interrupted_search_resumes_as_pending(self, tmp_path, cfg):
        store, state = setup_state(tmp_path, make_track(track_id="t1"))
        record = state["tracks"]["t1"]
        record["status"] = TrackStatus.SEARCHING  # simulate a mid-flight kill
        store.save(state)
        assert store.load()["tracks"]["t1"]["status"] == TrackStatus.PENDING
