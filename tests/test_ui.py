from spotify_nicotine import ui
from spotify_nicotine.models import StatusReason, TrackStatus


def make_record(**candidate_overrides):
    candidate = {
        "username": "peer9",
        "virtual_path": "X\\Eagles - Hotel California.mp3",
        "size": 9_000_000,
        "extension": "mp3",
        "free_upload_slots": True,
        "queue_position": 0,
        "confidence": 0.9,
        "bitrate_kbps": 128,
        "bitrate_inferred": False,
        "duration_s": 391,
        "meets_min_bitrate": True,
    }
    candidate.update(candidate_overrides)
    return {
        "spotify": {
            "name": "Hotel California",
            "artists": ["Eagles"],
            "album": "Hotel California",
            "duration_ms": 391_000,
        },
        "status": TrackStatus.NEEDS_REVIEW,
        "candidates": [candidate],
    }


class TestCandidateTable:
    def test_below_floor_marked_with_bang_and_legend(self):
        lines = ui.candidate_table(make_record(meets_min_bitrate=False))
        assert any("128!" in line for line in lines)
        assert any("below the configured minimum bitrate" in line for line in lines)

    def test_meeting_floor_not_marked(self):
        lines = ui.candidate_table(make_record(meets_min_bitrate=True))
        assert not any("128!" in line for line in lines)
        assert not any("minimum bitrate" in line for line in lines)

    def test_inferred_bitrate_star(self):
        lines = ui.candidate_table(
            make_record(bitrate_kbps=320, bitrate_inferred=True)
        )
        assert any("320*" in line for line in lines)

    def test_empty_candidates_hint(self):
        record = make_record()
        record["candidates"] = []
        lines = ui.candidate_table(record)
        assert any("no stored candidates" in line for line in lines)


class TestHelpers:
    def test_fmt_duration(self):
        assert ui.fmt_duration(391) == "6:31"
        assert ui.fmt_duration(None) == "?"

    def test_summary_shows_reason(self):
        state = {
            "playlist": {"name": "Mix"},
            "tracks": {
                "t1": {
                    "spotify": {"name": "Song", "artists": ["A"]},
                    "status": TrackStatus.NEEDS_REVIEW,
                    "status_reason": StatusReason.BELOW_MIN_BITRATE,
                }
            },
        }
        text = "\n".join(ui.summary_lines(state))
        assert "needs_review" in text
        assert StatusReason.BELOW_MIN_BITRATE in text
