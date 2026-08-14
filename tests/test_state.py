import json
import os

import pytest

from spotify_nicotine.models import StatusReason, TrackStatus
from spotify_nicotine.state import (
    StateStore,
    merge_playlist,
    new_state,
    new_track_record,
    set_track_status,
    tracks_with_status,
)

from tests.conftest import make_track

META = {"id": "pl1", "name": "Mix", "snapshot_id": "snap1"}


def make_state_with(*tracks):
    state = new_state(META)
    merge_playlist(state, META, list(tracks), [])
    return state


class TestStateStore:
    def test_roundtrip(self, tmp_path):
        store = StateStore(str(tmp_path), "pl1")
        state = make_state_with(make_track())
        store.save(state)
        loaded = store.load()
        assert loaded == state

    def test_load_missing_returns_none(self, tmp_path):
        assert StateStore(str(tmp_path), "nope").load() is None

    def test_searching_reset_to_pending_on_load(self, tmp_path):
        store = StateStore(str(tmp_path), "pl1")
        state = make_state_with(make_track())
        track_id = next(iter(state["tracks"]))
        state["tracks"][track_id]["status"] = TrackStatus.SEARCHING
        store.save(state)
        loaded = store.load()
        assert loaded["tracks"][track_id]["status"] == TrackStatus.PENDING

    def test_atomic_write_keeps_original_on_failure(self, tmp_path):
        store = StateStore(str(tmp_path), "pl1")
        good = make_state_with(make_track())
        store.save(good)
        bad = dict(good)
        bad["unserializable"] = object()
        with pytest.raises(TypeError):
            store.save(bad)
        # original intact, no tmp litter
        assert store.load() == good
        leftovers = [p for p in os.listdir(str(tmp_path)) if p.startswith(".tmp")]
        assert leftovers == []

    def test_creates_state_dir(self, tmp_path):
        store = StateStore(str(tmp_path / "deep" / "state"), "pl1")
        store.save(make_state_with(make_track()))
        assert store.load() is not None


class TestMergePlaylist:
    def test_new_tracks_added_as_pending(self):
        state = new_state(META)
        summary = merge_playlist(state, META, [make_track(track_id="t1")], [])
        assert summary["added"] == ["t1"]
        assert state["tracks"]["t1"]["status"] == TrackStatus.PENDING

    def test_existing_progress_preserved(self):
        state = make_state_with(make_track(track_id="t1"))
        set_track_status(state["tracks"]["t1"], TrackStatus.QUEUED)
        state["tracks"]["t1"]["chosen_index"] = 0
        summary = merge_playlist(state, META, [make_track(track_id="t1")], [])
        assert summary["added"] == []
        assert state["tracks"]["t1"]["status"] == TrackStatus.QUEUED
        assert state["tracks"]["t1"]["chosen_index"] == 0

    def test_gone_tracks_marked_removed_unless_downloaded(self):
        state = make_state_with(
            make_track(track_id="t1"), make_track(track_id="t2")
        )
        set_track_status(state["tracks"]["t2"], TrackStatus.DOWNLOADED)
        summary = merge_playlist(state, META, [], [])
        assert summary["removed"] == ["t1"]
        assert state["tracks"]["t1"]["status"] == TrackStatus.REMOVED
        assert (
            state["tracks"]["t1"]["status_reason"]
            == StatusReason.REMOVED_FROM_PLAYLIST
        )
        assert state["tracks"]["t2"]["status"] == TrackStatus.DOWNLOADED

    def test_removed_track_restored_when_readded(self):
        state = make_state_with(make_track(track_id="t1"))
        merge_playlist(state, META, [], [])
        assert state["tracks"]["t1"]["status"] == TrackStatus.REMOVED
        summary = merge_playlist(state, META, [make_track(track_id="t1")], [])
        assert summary["restored"] == ["t1"]
        assert state["tracks"]["t1"]["status"] == TrackStatus.PENDING

    def test_removed_while_queued_restores_to_queued(self):
        # Restoring to pending would re-search and re-download a track whose
        # original download may have completed meanwhile; reconciliation
        # against the transfer list must get the chance to decide.
        state = make_state_with(make_track(track_id="t1"))
        record = state["tracks"]["t1"]
        set_track_status(record, TrackStatus.QUEUED)
        record["chosen_index"] = 0
        merge_playlist(state, META, [], [])
        assert record["status"] == TrackStatus.REMOVED
        assert record["status_before_removed"] == TrackStatus.QUEUED

        merge_playlist(state, META, [make_track(track_id="t1")], [])
        assert record["status"] == TrackStatus.QUEUED
        assert record["chosen_index"] == 0  # candidate choice preserved
        assert "status_before_removed" not in record

    def test_removed_while_downloading_restores_to_queued(self):
        state = make_state_with(make_track(track_id="t1"))
        set_track_status(state["tracks"]["t1"], TrackStatus.DOWNLOADING)
        merge_playlist(state, META, [], [])
        merge_playlist(state, META, [make_track(track_id="t1")], [])
        assert state["tracks"]["t1"]["status"] == TrackStatus.QUEUED

    def test_removed_while_needs_review_restores_to_needs_review(self):
        state = make_state_with(make_track(track_id="t1"))
        set_track_status(state["tracks"]["t1"], TrackStatus.NEEDS_REVIEW)
        merge_playlist(state, META, [], [])
        merge_playlist(state, META, [make_track(track_id="t1")], [])
        assert state["tracks"]["t1"]["status"] == TrackStatus.NEEDS_REVIEW

    def test_skipped_list_refreshed(self):
        state = new_state(META)
        merge_playlist(state, META, [], [{"position": 3, "reason": "local_track"}])
        assert state["skipped"] == [{"position": 3, "reason": "local_track"}]

    def test_spotify_meta_refreshed(self):
        state = make_state_with(make_track(track_id="t1", name="Old Name"))
        merge_playlist(
            state, META, [make_track(track_id="t1", name="New Name")], []
        )
        assert state["tracks"]["t1"]["spotify"]["name"] == "New Name"


class TestTracksWithStatus:
    def test_filters_and_orders_by_position(self):
        state = new_state(META)
        t_a = make_track(track_id="a")
        t_a.positions = [5]
        t_b = make_track(track_id="b")
        t_b.positions = [1]
        t_c = make_track(track_id="c")
        t_c.positions = [3]
        merge_playlist(state, META, [t_a, t_b, t_c], [])
        set_track_status(state["tracks"]["c"], TrackStatus.QUEUED)
        pending = tracks_with_status(state, TrackStatus.PENDING)
        assert [tid for tid, _ in pending] == ["b", "a"]

    def test_state_json_serializable(self):
        state = make_state_with(make_track())
        json.dumps(state)
