from typing import List

from spotify_nicotine.models import StatusReason, TrackStatus
from spotify_nicotine.resolve import run_resolve
from spotify_nicotine.state import (
    StateStore,
    merge_playlist,
    new_state,
    set_track_status,
)

from tests.conftest import make_cfg, make_item, make_track
from tests.fakes import FakeSlsk

META = {"id": "pl1", "name": "Mix", "snapshot_id": "snap1"}


def make_review_state(tmp_path, with_candidates=True, track_ids=("t1",)):
    store = StateStore(str(tmp_path), "pl1")
    state = new_state(META)
    tracks = []
    for i, track_id in enumerate(track_ids):
        t = make_track(track_id=track_id)
        t.positions = [i]
        tracks.append(t)
    merge_playlist(state, META, tracks, [])
    for track_id in track_ids:
        record = state["tracks"][track_id]
        set_track_status(
            record,
            TrackStatus.NEEDS_REVIEW,
            StatusReason.BELOW_THRESHOLD if with_candidates else StatusReason.NO_RESULTS,
        )
        if with_candidates:
            record["candidates"] = [
                {
                    "username": "peer9",
                    "virtual_path": "X\\Eagles - Hotel California (Live).mp3",
                    "size": 9_000_000,
                    "extension": "mp3",
                    "file_attributes": {"0": 320},
                    "free_upload_slots": True,
                    "queue_position": 0,
                    "upload_speed": 100,
                    "confidence": 0.55,
                    "quality": 0.8,
                    "format_rank": 0,
                    "bitrate_kbps": 320,
                    "bitrate_inferred": False,
                    "duration_s": 451,
                },
                {
                    "username": "peer3",
                    "virtual_path": "Y\\hotel_california.mp3",
                    "size": 7_000_000,
                    "extension": "mp3",
                    "file_attributes": {},
                    "free_upload_slots": False,
                    "queue_position": 14,
                    "upload_speed": 0,
                    "confidence": 0.5,
                    "quality": 0.4,
                    "format_rank": 0,
                    "bitrate_kbps": None,
                    "bitrate_inferred": False,
                    "duration_s": None,
                },
            ]
    store.save(state)
    return store, state


class ScriptedInput:
    def __init__(self, answers: List[str]):
        self.answers = list(answers)
        self.prompts: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


def quiet(_msg: str) -> None:
    pass


class TestRunResolve:
    def test_pick_number_enqueues(self, tmp_path, cfg):
        store, state = make_review_state(tmp_path)
        slsk = FakeSlsk()
        run_resolve(cfg, slsk, store, state, input_fn=ScriptedInput(["2"]), log=quiet)

        record = state["tracks"]["t1"]
        assert record["status"] == TrackStatus.QUEUED
        assert record["chosen_index"] == 1
        assert slsk.enqueues[0]["username"] == "peer3"
        assert store.load()["tracks"]["t1"]["status"] == TrackStatus.QUEUED

    def test_skip(self, tmp_path, cfg):
        store, state = make_review_state(tmp_path)
        slsk = FakeSlsk()
        run_resolve(cfg, slsk, store, state, input_fn=ScriptedInput(["s"]), log=quiet)

        record = state["tracks"]["t1"]
        assert record["status"] == TrackStatus.SKIPPED
        assert record["status_reason"] == StatusReason.USER_SKIPPED
        assert slsk.enqueues == []

    def test_research_then_pick(self, tmp_path, cfg):
        store, state = make_review_state(tmp_path)
        fresh = [
            make_item(
                "Music\\Eagles\\05 - Hotel California.mp3",
                username="fresh_user",
                file_attributes={"0": 320, "1": 391},
            )
        ]
        slsk = FakeSlsk({"eagles hotel california 1976": fresh})
        run_resolve(
            cfg,
            slsk,
            store,
            state,
            input_fn=ScriptedInput(["r", "eagles hotel california 1976", "1"]),
            log=quiet,
        )

        record = state["tracks"]["t1"]
        assert "eagles hotel california 1976" in record["queries_tried"]
        assert record["status"] == TrackStatus.QUEUED
        assert slsk.enqueues[0]["username"] == "fresh_user"

    def test_quit_saves_and_stops(self, tmp_path, cfg):
        store, state = make_review_state(tmp_path, track_ids=("t1", "t2"))
        slsk = FakeSlsk()
        run_resolve(cfg, slsk, store, state, input_fn=ScriptedInput(["q"]), log=quiet)

        assert state["tracks"]["t1"]["status"] == TrackStatus.NEEDS_REVIEW
        assert state["tracks"]["t2"]["status"] == TrackStatus.NEEDS_REVIEW
        assert slsk.enqueues == []

    def test_second_track_reached_after_first_resolved(self, tmp_path, cfg):
        store, state = make_review_state(tmp_path, track_ids=("t1", "t2"))
        slsk = FakeSlsk()
        run_resolve(
            cfg, slsk, store, state, input_fn=ScriptedInput(["1", "s"]), log=quiet
        )
        assert state["tracks"]["t1"]["status"] == TrackStatus.QUEUED
        assert state["tracks"]["t2"]["status"] == TrackStatus.SKIPPED

    def test_invalid_then_valid_input(self, tmp_path, cfg):
        store, state = make_review_state(tmp_path)
        slsk = FakeSlsk()
        messages = []
        run_resolve(
            cfg,
            slsk,
            store,
            state,
            input_fn=ScriptedInput(["x", "99", "1"]),
            log=messages.append,
        )
        assert state["tracks"]["t1"]["status"] == TrackStatus.QUEUED
        assert any("no candidate #99" in m for m in messages)

    def test_no_candidates_number_rejected(self, tmp_path, cfg):
        store, state = make_review_state(tmp_path, with_candidates=False)
        slsk = FakeSlsk()
        run_resolve(
            cfg, slsk, store, state, input_fn=ScriptedInput(["1", "s"]), log=quiet
        )
        assert state["tracks"]["t1"]["status"] == TrackStatus.SKIPPED

    def test_eof_quits_gracefully(self, tmp_path, cfg):
        store, state = make_review_state(tmp_path)
        slsk = FakeSlsk()
        run_resolve(cfg, slsk, store, state, input_fn=ScriptedInput([]), log=quiet)
        assert state["tracks"]["t1"]["status"] == TrackStatus.NEEDS_REVIEW
