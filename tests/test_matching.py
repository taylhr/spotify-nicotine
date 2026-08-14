"""Matching engine tests, including the weight-rationale regression scenarios
that pin down why the default gate is 0.65."""

import pytest

from spotify_nicotine.matching import (
    core_title,
    match_confidence,
    normalize,
    parse_attrs,
    quality_score,
    score_results,
    split_virtual_path,
    strip_track_number,
    version_tokens,
)
from spotify_nicotine.models import ParsedAttrs

from tests.conftest import make_cfg, make_item, make_track

GATE = 0.65  # default --min-confidence


def confidence_for(track, virtual_path, attrs=None, size=15_662_354):
    folder, basename = split_virtual_path(virtual_path)
    extension = virtual_path.rsplit(".", 1)[-1].lower()
    parsed = parse_attrs(attrs or {}, size, extension, track.duration_s)
    return match_confidence(track, folder, basename, parsed)


class TestNormalize:
    def test_diacritics_and_case(self):
        assert normalize("Beyoncé — Déjà Vu") == "beyonce deja vu"

    def test_punctuation_and_ampersand(self):
        assert normalize("AC/DC & Friends (Live!)") == "ac dc and friends live"

    def test_apostrophes_removed_not_spaced(self):
        assert normalize("Don't Stop Believin'") == "dont stop believin"


class TestStripTrackNumber:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("05 - Hotel California", "Hotel California"),
            ("1-05 Hotel California", "Hotel California"),
            ("cd2 03. Hotel California", "Hotel California"),
            ("05. Hotel California", "Hotel California"),
            ("Hotel California", "Hotel California"),
        ],
    )
    def test_variants(self, raw, expected):
        assert strip_track_number(raw) == expected


class TestCoreTitle:
    def test_noise_parenthetical_removed(self):
        assert core_title("Hotel California (2013 Remaster)") == "hotel california"

    def test_feat_clause_removed(self):
        assert core_title("Airplanes (feat. Hayley Williams)") == "airplanes"

    def test_inline_feat_removed(self):
        assert core_title("Airplanes feat Hayley Williams") == "airplanes"

    def test_version_parenthetical_kept(self):
        assert "live" in core_title("Hotel California (Live at Wembley)")

    def test_noise_phrase_removed(self):
        assert core_title("One More Time (Original Mix)") == "one more time"


class TestVersionTokens:
    def test_detects_live_and_remix_synonym(self):
        assert version_tokens("Song (Live) [Some RMX]") == {"live", "remix"}

    def test_plain_title_empty(self):
        assert version_tokens("Hotel California") == set()


class TestSplitVirtualPath:
    def test_windows_path(self):
        folder, base = split_virtual_path(
            "Music\\Eagles\\Hotel California (1976)\\05 - Hotel California.mp3"
        )
        assert folder.endswith("Hotel California (1976)")
        assert base == "05 - Hotel California"

    def test_no_folder(self):
        assert split_virtual_path("track.mp3") == ("", "track")


class TestParseAttrs:
    def test_full_attrs_string_keys(self):
        parsed = parse_attrs({"0": 320, "1": 391, "2": 0}, 15_662_354, "mp3", 391.0)
        assert parsed.bitrate_kbps == 320
        assert parsed.duration_s == 391
        assert parsed.vbr is False
        assert not parsed.bitrate_inferred

    def test_int_keys_accepted(self):
        parsed = parse_attrs({0: 320, 1: 391}, 15_662_354, "mp3", 391.0)
        assert parsed.bitrate_kbps == 320

    def test_duration_inferred_from_real_bitrate(self):
        parsed = parse_attrs({"0": 320}, 15_662_354, "mp3", 391.0)
        assert parsed.duration_s == pytest.approx(391.6, abs=1.0)
        assert parsed.duration_inferred

    def test_bitrate_inferred_from_size_and_spotify_duration(self):
        parsed = parse_attrs({}, 15_662_354, "mp3", 391.0)
        assert parsed.bitrate_kbps == pytest.approx(320, abs=5)
        assert parsed.bitrate_inferred

    def test_lossless_duration_not_inferred_from_bitrate(self):
        parsed = parse_attrs({"0": 900}, 30_000_000, "flac", 391.0)
        assert parsed.duration_s is None

    def test_empty_attrs_zero_size(self):
        parsed = parse_attrs({}, 0, "mp3", 391.0)
        assert parsed.bitrate_kbps is None
        assert parsed.duration_s is None


class TestMatchConfidenceScenarios:
    """The rationale scenarios that justify the weights (regression-pinned)."""

    def test_exact_match_with_attrs_scores_high(self):
        track = make_track()
        conf = confidence_for(
            track,
            "Music\\Eagles\\Hotel California (1976)\\05 - Hotel California.mp3",
            attrs={"0": 320, "1": 391},
        )
        assert conf >= 0.90

    def test_basename_only_unknown_duration_passes_gate(self):
        track = make_track()
        conf = confidence_for(track, "Eagles - Hotel California.mp3", size=0)
        assert conf >= GATE

    def test_artist_only_in_folder(self):
        track = make_track()
        conf = confidence_for(
            track,
            "shared\\Eagles - Hotel California (1976)\\05 - Hotel California.mp3",
            attrs={"0": 320, "1": 391},
        )
        assert conf >= 0.90

    def test_live_version_with_far_duration_rejected_hard(self):
        track = make_track()
        conf = confidence_for(
            track,
            "Eagles - Hotel California (Live).mp3",
            attrs={"0": 320, "1": 451},  # 60s longer
        )
        assert conf < 0.45

    def test_live_version_unknown_duration_below_gate(self):
        track = make_track()
        conf = confidence_for(track, "Eagles - Hotel California (Live).mp3", size=0)
        assert conf < GATE

    def test_live_target_matches_live_file(self):
        track = make_track(name="Hotel California - Live")
        conf = confidence_for(track, "Eagles - Hotel California (Live).mp3", size=0)
        assert conf >= GATE

    def test_wrong_song_same_artist_matching_duration_below_gate(self):
        track = make_track()
        conf = confidence_for(
            track,
            "Music\\Eagles\\05 - New Kid in Town.mp3",
            attrs={"0": 320, "1": 391},
        )
        assert conf < GATE

    def test_karaoke_trap_below_gate(self):
        track = make_track()
        conf = confidence_for(
            track,
            "Karaoke Hits\\Eagles - Hotel California (Karaoke Version).mp3",
            size=0,
        )
        assert conf < GATE

    def test_remaster_noise_does_not_penalize(self):
        track = make_track()
        conf = confidence_for(
            track,
            "Eagles - Hotel California (2013 Remaster).mp3",
            attrs={"0": 320, "1": 391},
        )
        assert conf >= 0.90

    def test_unicode_artist_and_title(self):
        track = make_track(
            name="Déjà Vu", artists=["Beyoncé"], album="B'Day", duration_ms=240_000
        )
        conf = confidence_for(
            track, "Beyonce\\BDay\\04 - Deja Vu.mp3", attrs={"0": 320, "1": 240}
        )
        assert conf >= 0.90

    def test_feat_clause_on_target_only(self):
        track = make_track(
            name="Airplanes (feat. Hayley Williams)",
            artists=["B.o.B"],
            album="B.o.B Presents",
            duration_ms=180_000,
        )
        conf = confidence_for(track, "B.o.B - Airplanes.mp3", attrs={"0": 320, "1": 181})
        assert conf >= GATE

    def test_title_starting_with_number(self):
        track = make_track(
            name="99 Problems", artists=["Jay-Z"], album="The Black Album",
            duration_ms=234_000,
        )
        conf = confidence_for(
            track, "Jay-Z\\The Black Album\\99 Problems.mp3", attrs={"0": 320, "1": 234}
        )
        assert conf >= 0.90

    def test_duration_within_3s_full_score(self):
        track = make_track()
        near = confidence_for(
            track, "Eagles - Hotel California.mp3", attrs={"0": 320, "1": 393}
        )
        exact = confidence_for(
            track, "Eagles - Hotel California.mp3", attrs={"0": 320, "1": 391}
        )
        assert near == pytest.approx(exact)

    def test_wrong_artist_same_title_below_gate(self):
        # The classic title-only-search trap: right title, wrong artist,
        # no duration info. Must not be auto-downloadable.
        track = make_track()
        conf = confidence_for(track, "Someone Else - Hotel California.mp3", size=0)
        assert conf < GATE

    def test_wrong_artist_even_with_matching_duration_below_gate(self):
        track = make_track()
        conf = confidence_for(
            track,
            "Shared\\Someone Else - Hotel California.mp3",
            attrs={"0": 320, "1": 391},
        )
        assert conf < GATE

    def test_artist_absent_but_album_folder_and_duration_pass(self):
        # No artist anywhere, but correct album folder + exact duration is
        # strong enough evidence to stay auto-queueable.
        track = make_track()
        conf = confidence_for(
            track,
            "Hotel California (1976)\\05 - Hotel California.mp3",
            attrs={"0": 320, "1": 391},
        )
        assert conf >= GATE

    def test_artist_absent_without_other_evidence_needs_review(self):
        track = make_track()
        conf = confidence_for(track, "Backups\\05 - Hotel California.mp3", size=0)
        assert conf < GATE


class TestQualityScore:
    def test_prefer_bitrate_exact_beats_low(self, cfg):
        item = make_item("a.mp3", extension="mp3")
        q320 = quality_score(
            ParsedAttrs(bitrate_kbps=320), item, cfg
        )
        q128 = quality_score(
            ParsedAttrs(bitrate_kbps=128), item, cfg
        )
        assert q320 > q128

    def test_lossless_full_bitrate_score(self):
        cfg = make_cfg(formats=["flac"])
        item = make_item("a.flac", extension="flac")
        q = quality_score(ParsedAttrs(bitrate_kbps=900), item, cfg)
        # full bitrate + free slots + queue position 0
        assert q == pytest.approx(0.55 + 0.25 + 0.08, abs=0.01)

    def test_tiny_flac_transcode_suspect(self):
        cfg = make_cfg(formats=["flac"])
        item = make_item("a.flac", extension="flac")
        q = quality_score(ParsedAttrs(bitrate_kbps=61, bitrate_inferred=True), item, cfg)
        assert q < 0.45

    def test_inferred_bitrate_discounted(self, cfg):
        item = make_item("a.mp3", extension="mp3")
        real = quality_score(ParsedAttrs(bitrate_kbps=320), item, cfg)
        inferred = quality_score(
            ParsedAttrs(bitrate_kbps=320, bitrate_inferred=True), item, cfg
        )
        assert inferred < real

    def test_free_slots_matter(self, cfg):
        with_slots = make_item("a.mp3", extension="mp3", free_upload_slots=True)
        without = make_item("a.mp3", extension="mp3", free_upload_slots=False)
        attrs = ParsedAttrs(bitrate_kbps=320)
        assert quality_score(attrs, with_slots, cfg) > quality_score(attrs, without, cfg)


class TestScoreResults:
    def test_end_to_end_ranking(self):
        cfg = make_cfg(formats=["mp3", "flac"])
        track = make_track()
        items = [
            make_item(
                "Music\\Eagles\\05 - Hotel California.flac",
                username="flac_user",
                size=30_000_000,
                file_attributes={"1": 391},
            ),
            make_item(
                "Music\\Eagles\\05 - Hotel California.mp3",
                username="good_mp3",
                file_attributes={"0": 320, "1": 391},
            ),
            make_item(
                "Music\\Eagles\\05 - Hotel California.mp3",
                username="slow_mp3",
                file_attributes={"0": 128, "1": 391},
                size=6_000_000,
                free_upload_slots=False,
            ),
        ]
        candidates = score_results(track, items, cfg)
        # 320 mp3 and flac share the top bitrate band -> format order decides;
        # the 128kbps file sinks regardless of format.
        assert [c.username for c in candidates] == ["good_mp3", "flac_user", "slow_mp3"]
        # 128 < default min_bitrate 192: kept for review, flagged
        assert [c.meets_min_bitrate for c in candidates] == [True, True, False]

    def test_bitrate_band_beats_format_order(self):
        # "prefer up to 320kbps before filetype": a 320 m4a outranks a 256 mp3
        cfg = make_cfg()
        track = make_track()
        items = [
            make_item(
                "A\\Eagles - Hotel California.mp3",
                username="mp3_256",
                file_attributes={"0": 256, "1": 391},
            ),
            make_item(
                "B\\Eagles - Hotel California.m4a",
                username="m4a_320",
                file_attributes={"0": 320, "1": 391},
            ),
        ]
        candidates = score_results(track, items, cfg)
        assert [c.username for c in candidates] == ["m4a_320", "mp3_256"]

    def test_vbr_near_target_ties_with_cbr_format_decides(self):
        # ~317kbps VBR m4a lands in the same band as 320 CBR mp3 -> mp3 wins
        cfg = make_cfg()
        track = make_track()
        items = [
            make_item(
                "A\\Eagles - Hotel California.m4a",
                username="m4a_317",
                file_attributes={"0": 317, "1": 391, "2": 1},
            ),
            make_item(
                "B\\Eagles - Hotel California.mp3",
                username="mp3_320",
                file_attributes={"0": 320, "1": 391},
            ),
        ]
        candidates = score_results(track, items, cfg)
        assert candidates[0].username == "mp3_320"

    def test_lossless_beats_lower_bitrate_lossy(self):
        cfg = make_cfg()
        track = make_track()
        items = [
            make_item(
                "A\\Eagles - Hotel California.mp3",
                username="mp3_256",
                file_attributes={"0": 256, "1": 391},
            ),
            make_item(
                "B\\Eagles - Hotel California.flac",
                username="flac_user",
                size=30_000_000,
                file_attributes={"1": 391},
            ),
        ]
        candidates = score_results(track, items, cfg)
        assert candidates[0].username == "flac_user"

    def test_filters_private_and_wrong_format_keep_flagged_low_bitrate(self):
        cfg = make_cfg(formats=["mp3"], min_bitrate=256)
        track = make_track()
        items = [
            make_item("Eagles - Hotel California.ogg", username="ogg_user"),
            make_item(
                "Eagles - Hotel California.mp3",
                username="private_user",
                is_private=True,
            ),
            make_item(
                "Eagles - Hotel California.mp3",
                username="low_bitrate",
                file_attributes={"0": 128, "1": 391},
            ),
            make_item(
                "Eagles - Hotel California.mp3",
                username="keeper",
                file_attributes={"0": 320, "1": 391},
            ),
        ]
        candidates = score_results(track, items, cfg)
        assert [c.username for c in candidates] == ["keeper", "low_bitrate"]
        assert candidates[0].meets_min_bitrate is True
        assert candidates[1].meets_min_bitrate is False

    def test_excellent_confidence_beats_preferred_format(self):
        # A ~0.9+ match must not lose to a merely-eligible (~0.7-0.9) match
        # just because the weaker one is in a preferred format at 320kbps.
        cfg = make_cfg()
        track = make_track()
        items = [
            make_item(
                "Music\\Eagles\\05 - Hotel California.flac",
                username="right_flac",
                size=30_000_000,
                file_attributes={"1": 391},
            ),
            make_item(
                # right artist/title but duration 11s off: eligible, not excellent
                "Eagles - Hotel California.mp3",
                username="iffy_mp3",
                file_attributes={"0": 320, "1": 402},
            ),
        ]
        candidates = score_results(track, items, cfg)
        assert candidates[0].username == "right_flac"
        assert candidates[0].confidence >= 0.9
        assert 0.65 <= candidates[1].confidence < 0.9

    def test_below_floor_witness_survives_truncation(self):
        # 10 weak above-floor candidates + 1 confident below-floor: the
        # witness must survive top-K truncation so the notifier can fire.
        cfg = make_cfg(min_confidence=0.97)
        track = make_track()
        items = [
            make_item(
                "W%d\\Eagles - Hotel California (Live).mp3" % i,
                username="weak%d" % i,
                file_attributes={"0": 192, "1": 399},
            )
            for i in range(10)
        ]
        items.append(
            make_item(
                "Music\\Eagles\\05 - Hotel California.mp3",
                username="witness",
                file_attributes={"0": 128, "1": 391},
            )
        )
        candidates = score_results(track, items, cfg)
        assert len(candidates) == 10
        witness = [c for c in candidates if c.username == "witness"]
        assert len(witness) == 1
        assert witness[0].meets_min_bitrate is False
        assert witness[0].confidence >= 0.97

    def test_junk_floor_drops_unrelated(self, cfg):
        track = make_track()
        items = [
            make_item("Random\\Completely Different Song.mp3", username="junk"),
            make_item(
                "Eagles - Hotel California.mp3",
                username="keeper",
                file_attributes={"0": 320, "1": 391},
            ),
        ]
        candidates = score_results(track, items, cfg)
        assert [c.username for c in candidates] == ["keeper"]

    def test_dedupe_and_top_k(self, cfg):
        track = make_track()
        items = []
        for i in range(15):
            items.append(
                make_item(
                    "Music%d\\Eagles - Hotel California.mp3" % i,
                    username="user%d" % i,
                    file_attributes={"0": 320, "1": 391},
                )
            )
        items.append(dict(items[0]))  # exact duplicate (username, path)
        candidates = score_results(track, items, cfg)
        assert len(candidates) == 10

    def test_extension_from_path_when_field_empty(self, cfg):
        track = make_track()
        items = [
            make_item(
                "Eagles - Hotel California.MP3",
                username="upper_ext",
                extension="",
                file_attributes={"0": 320, "1": 391},
            )
        ]
        candidates = score_results(track, items, cfg)
        assert len(candidates) == 1
        assert candidates[0].extension == "mp3"
