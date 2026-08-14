import os

import pytest

from spotify_nicotine.models import TrackStatus
from spotify_nicotine.organize import (
    album_folder_name,
    assign_album_folders,
    destination_folder,
    organize_files,
    local_filename,
    sanitize_component,
    target_basename,
    unique_target_path,
)
from spotify_nicotine.state import StateStore, merge_playlist, new_state

from tests.conftest import make_cfg, make_track

META = {"id": "pl1", "name": "Mix", "snapshot_id": "snap1"}


def build_state(tmp_path, downloads_dir, confidence=1.0, filename="05 - hc.mp3"):
    store = StateStore(str(tmp_path / "state"), "pl1")
    state = new_state(META)
    track = make_track(track_id="t1")
    merge_playlist(state, META, [track], [])
    record = state["tracks"]["t1"]
    record["status"] = TrackStatus.DOWNLOADED
    record["chosen_index"] = 0
    record["candidates"] = [
        {
            "username": "peer1",
            "virtual_path": "Music\\Eagles\\" + filename,
            "size": 1,
            "extension": "mp3",
            "confidence": confidence,
            "file_attributes": {},
        }
    ]
    record["local_folder"] = str(downloads_dir)
    record["local_path"] = str(downloads_dir / filename)
    return store, state, record


class TestSanitize:
    def test_illegal_characters_replaced(self):
        assert sanitize_component("AC/DC: Live?") == "AC-DC- Live-"

    def test_whitespace_collapsed_and_trimmed(self):
        assert sanitize_component("  Song   Name  ") == "Song Name"

    def test_leading_dot_stripped(self):
        assert sanitize_component(".hidden") == "hidden"

    def test_long_name_truncated(self):
        assert len(sanitize_component("x" * 300).encode()) <= 100

    def test_unicode_preserved(self):
        assert sanitize_component("Beyoncé — Déjà Vu") == "Beyoncé — Déjà Vu"


class TestTargetBasename:
    def test_basic_format(self):
        track = make_track(name="Hotel California", artists=["Eagles"])
        assert target_basename(track, "mp3") == "Eagles - Hotel California.mp3"

    def test_primary_artist_used_for_collaborations(self):
        track = make_track(name="Song", artists=["Artist One", "Artist Two"])
        assert target_basename(track, "flac") == "Artist One - Song.flac"

    def test_spotify_title_kept_verbatim(self):
        track = make_track(name="Airplanes (feat. Hayley Williams)", artists=["B.o.B"])
        assert (
            target_basename(track, "mp3")
            == "B.o.B - Airplanes (feat. Hayley Williams).mp3"
        )

    def test_missing_artist_falls_back_to_title(self):
        track = make_track(name="Untitled", artists=[])
        assert target_basename(track, "mp3") == "Untitled.mp3"

    def test_no_title_is_unusable(self):
        track = make_track(name="", artists=["Eagles"])
        assert target_basename(track, "mp3") is None


class TestUniqueTargetPath:
    def test_free_name_used(self, tmp_path):
        target = unique_target_path(str(tmp_path), "a.mp3", str(tmp_path / "src.mp3"))
        assert target == str(tmp_path / "a.mp3")

    def test_existing_other_file_gets_suffix(self, tmp_path):
        (tmp_path / "a.mp3").write_text("other")
        target = unique_target_path(str(tmp_path), "a.mp3", str(tmp_path / "src.mp3"))
        assert target == str(tmp_path / "a (2).mp3")

    def test_source_equals_target_is_kept(self, tmp_path):
        path = tmp_path / "a.mp3"
        path.write_text("me")
        assert unique_target_path(str(tmp_path), "a.mp3", str(path)) == str(path)


class TestLocalFilename:
    def test_windows_virtual_path(self):
        assert local_filename("Music\\Eagles\\05 - hc.mp3") == "05 - hc.mp3"


class TestApplyRenames:
    def test_renames_confident_download(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        (downloads / "05 - hc.mp3").write_text("audio")
        cfg = make_cfg(rename_files=True)
        store, state, record = build_state(tmp_path, downloads)

        assert organize_files(state, cfg, store) == 1
        assert (downloads / "Eagles - Hotel California.mp3").read_text() == "audio"
        assert not (downloads / "05 - hc.mp3").exists()
        assert record["local_path"].endswith("Eagles - Hotel California.mp3")
        # persisted, and the remote identity is untouched
        saved = store.load()["tracks"]["t1"]
        assert saved["local_path"] == record["local_path"]
        assert saved["candidates"][0]["virtual_path"] == "Music\\Eagles\\05 - hc.mp3"

    def test_disabled_by_default(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        (downloads / "05 - hc.mp3").write_text("audio")
        cfg = make_cfg()
        store, state, _record = build_state(tmp_path, downloads)

        assert organize_files(state, cfg, store) == 0
        assert (downloads / "05 - hc.mp3").exists()

    def test_below_threshold_not_renamed(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        (downloads / "05 - hc.mp3").write_text("audio")
        cfg = make_cfg(rename_files=True)
        store, state, _record = build_state(tmp_path, downloads, confidence=0.97)

        assert organize_files(state, cfg, store) == 0
        assert (downloads / "05 - hc.mp3").exists()

    def test_custom_threshold(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        (downloads / "05 - hc.mp3").write_text("audio")
        cfg = make_cfg(rename_files=True, rename_min_confidence=0.9)
        store, state, _record = build_state(tmp_path, downloads, confidence=0.97)

        assert organize_files(state, cfg, store) == 1

    def test_dry_run_never_renames(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        (downloads / "05 - hc.mp3").write_text("audio")
        cfg = make_cfg(rename_files=True, dry_run=True)
        store, state, _record = build_state(tmp_path, downloads)

        assert organize_files(state, cfg, store) == 0
        assert (downloads / "05 - hc.mp3").exists()

    def test_idempotent(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        (downloads / "05 - hc.mp3").write_text("audio")
        cfg = make_cfg(rename_files=True)
        store, state, _record = build_state(tmp_path, downloads)

        assert organize_files(state, cfg, store) == 1
        assert organize_files(state, cfg, store) == 0  # nothing left to do

    def test_missing_file_is_skipped_quietly(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()  # file never created (user moved it)
        cfg = make_cfg(rename_files=True)
        store, state, record = build_state(tmp_path, downloads)
        messages = []

        assert organize_files(state, cfg, store, log=messages.append) == 0
        assert "organized_at" not in record
        assert messages == []

    def test_collision_with_different_file_gets_suffix(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        (downloads / "05 - hc.mp3").write_text("mine")
        (downloads / "Eagles - Hotel California.mp3").write_text("someone else's")
        cfg = make_cfg(rename_files=True)
        store, state, record = build_state(tmp_path, downloads)

        assert organize_files(state, cfg, store) == 1
        # the pre-existing file is preserved, not clobbered
        assert (downloads / "Eagles - Hotel California.mp3").read_text() == (
            "someone else's"
        )
        assert (downloads / "Eagles - Hotel California (2).mp3").read_text() == "mine"

    def test_only_downloaded_tracks_touched(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        (downloads / "05 - hc.mp3").write_text("audio")
        cfg = make_cfg(rename_files=True)
        store, state, record = build_state(tmp_path, downloads)
        record["status"] = TrackStatus.QUEUED  # still transferring

        assert organize_files(state, cfg, store) == 0
        assert (downloads / "05 - hc.mp3").exists()

    def test_falls_back_to_dest_dir_when_path_unknown(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        (downloads / "05 - hc.mp3").write_text("audio")
        cfg = make_cfg(rename_files=True, dest_dir=str(downloads))
        store, state, record = build_state(tmp_path, downloads)
        del record["local_path"]
        del record["local_folder"]

        assert organize_files(state, cfg, store) == 1
        assert (downloads / "Eagles - Hotel California.mp3").exists()

    def test_extension_taken_from_actual_file(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        (downloads / "05 - hc.flac").write_text("audio")
        cfg = make_cfg(rename_files=True)
        store, state, _record = build_state(
            tmp_path, downloads, filename="05 - hc.flac"
        )

        assert organize_files(state, cfg, store) == 1
        assert (downloads / "Eagles - Hotel California.flac").exists()


def album_state(tmp_path, specs):
    """specs: list of (track_id, artist, album). Returns (store, state)."""
    store = StateStore(str(tmp_path / "state"), "pl1")
    state = new_state(META)
    tracks = []
    for index, (track_id, artist, album) in enumerate(specs):
        track = make_track(
            track_id=track_id,
            name="Song %s" % track_id,
            artists=[artist],
            album=album,
        )
        track.positions = [index]
        tracks.append(track)
    merge_playlist(state, META, tracks, [])
    return store, state


class TestAlbumFolderName:
    def test_format(self):
        assert album_folder_name("Eagles", "Hotel California") == (
            "Eagles - Hotel California"
        )

    def test_illegal_characters_sanitized(self):
        assert album_folder_name("AC/DC", "Back: In Black") == "AC-DC - Back- In Black"

    def test_missing_part_is_unusable(self):
        assert album_folder_name("", "Album") is None
        assert album_folder_name("Artist", "") is None


class TestAssignAlbumFolders:
    def test_two_or_more_tracks_get_a_folder(self, tmp_path):
        store, state = album_state(
            tmp_path,
            [
                ("a", "Eagles", "Hotel California"),
                ("b", "Eagles", "Hotel California"),
                ("c", "Moby", "Play"),  # lone track: no folder
            ],
        )
        assigned = assign_album_folders(state)

        assert assigned == {"Eagles - Hotel California": 2}
        assert state["tracks"]["a"]["album_folder"] == "Eagles - Hotel California"
        assert state["tracks"]["b"]["album_folder"] == "Eagles - Hotel California"
        assert "album_folder" not in state["tracks"]["c"]

    def test_same_album_different_artists_not_grouped(self, tmp_path):
        store, state = album_state(
            tmp_path,
            [("a", "Artist One", "Now 50"), ("b", "Artist Two", "Now 50")],
        )
        assert assign_album_folders(state) == {}

    def test_case_and_spacing_insensitive_grouping(self, tmp_path):
        store, state = album_state(
            tmp_path,
            [("a", "Eagles", "Hotel California"), ("b", "EAGLES", "hotel california")],
        )
        assert len(assign_album_folders(state)) == 1
        assert state["tracks"]["a"]["album_folder"] == state["tracks"]["b"]["album_folder"]

    def test_shrinking_group_clears_the_tag(self, tmp_path):
        store, state = album_state(
            tmp_path,
            [("a", "Eagles", "Hotel California"), ("b", "Eagles", "Hotel California")],
        )
        assign_album_folders(state)
        assert "album_folder" in state["tracks"]["a"]
        del state["tracks"]["b"]  # track removed from the playlist
        assign_album_folders(state)
        assert "album_folder" not in state["tracks"]["a"]

    def test_missing_metadata_ignored(self, tmp_path):
        store, state = album_state(
            tmp_path, [("a", "Eagles", ""), ("b", "Eagles", "")]
        )
        assert assign_album_folders(state) == {}


class TestDestinationFolder:
    def test_no_dest_dir_means_nicotine_default(self):
        cfg = make_cfg(album_folders=True)
        assert destination_folder({"album_folder": "A - B"}, cfg) is None

    def test_album_folder_appended_to_dest_dir(self):
        cfg = make_cfg(album_folders=True, dest_dir="/music")
        assert destination_folder({"album_folder": "A - B"}, cfg) == "/music/A - B"

    def test_plain_dest_dir_without_album(self):
        cfg = make_cfg(album_folders=True, dest_dir="/music")
        assert destination_folder({}, cfg) == "/music"

    def test_album_folders_disabled(self):
        cfg = make_cfg(dest_dir="/music")
        assert destination_folder({"album_folder": "A - B"}, cfg) == "/music"


class TestOrganizeAlbums:
    def _downloaded(self, state, track_id, downloads, filename, confidence=1.0):
        record = state["tracks"][track_id]
        record["status"] = TrackStatus.DOWNLOADED
        record["chosen_index"] = 0
        record["candidates"] = [
            {
                "username": "peer1",
                "virtual_path": "Music\\x\\" + filename,
                "size": 1,
                "extension": "mp3",
                "confidence": confidence,
                "file_attributes": {},
            }
        ]
        record["local_folder"] = str(downloads)
        record["local_path"] = str(downloads / filename)
        (downloads / filename).write_text("audio-" + track_id)
        return record

    def test_moves_album_tracks_into_folder(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        store, state = album_state(
            tmp_path,
            [
                ("a", "Eagles", "Hotel California"),
                ("b", "Eagles", "Hotel California"),
            ],
        )
        self._downloaded(state, "a", downloads, "one.mp3")
        self._downloaded(state, "b", downloads, "two.mp3")
        cfg = make_cfg(album_folders=True, dest_dir=str(downloads))

        assert organize_files(state, cfg, store) == 2
        folder = downloads / "Eagles - Hotel California"
        assert (folder / "one.mp3").read_text() == "audio-a"
        assert (folder / "two.mp3").read_text() == "audio-b"
        assert state["tracks"]["a"]["local_path"] == str(folder / "one.mp3")

    def test_single_track_album_left_alone(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        store, state = album_state(tmp_path, [("a", "Moby", "Play")])
        self._downloaded(state, "a", downloads, "one.mp3")
        cfg = make_cfg(album_folders=True, dest_dir=str(downloads))

        assert organize_files(state, cfg, store) == 0
        assert (downloads / "one.mp3").exists()

    def test_idempotent_without_dest_dir(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        store, state = album_state(
            tmp_path,
            [
                ("a", "Eagles", "Hotel California"),
                ("b", "Eagles", "Hotel California"),
            ],
        )
        self._downloaded(state, "a", downloads, "one.mp3")
        self._downloaded(state, "b", downloads, "two.mp3")
        cfg = make_cfg(album_folders=True)  # no dest_dir: folder beside files

        assert organize_files(state, cfg, store) == 2
        folder = downloads / "Eagles - Hotel California"
        assert (folder / "one.mp3").exists()
        # second pass must not nest another album folder inside the first
        assert organize_files(state, cfg, store) == 0
        assert not (folder / "Eagles - Hotel California").exists()

    def test_rename_and_album_folder_in_one_move(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        store, state = album_state(
            tmp_path,
            [
                ("a", "Eagles", "Hotel California"),
                ("b", "Eagles", "Hotel California"),
            ],
        )
        self._downloaded(state, "a", downloads, "one.mp3")
        self._downloaded(state, "b", downloads, "two.mp3")
        cfg = make_cfg(
            album_folders=True, rename_files=True, dest_dir=str(downloads)
        )

        assert organize_files(state, cfg, store) == 2
        folder = downloads / "Eagles - Hotel California"
        assert (folder / "Eagles - Song a.mp3").read_text() == "audio-a"
        assert (folder / "Eagles - Song b.mp3").read_text() == "audio-b"

    def test_album_move_happens_even_below_rename_confidence(self, tmp_path):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        store, state = album_state(
            tmp_path,
            [
                ("a", "Eagles", "Hotel California"),
                ("b", "Eagles", "Hotel California"),
            ],
        )
        self._downloaded(state, "a", downloads, "one.mp3", confidence=0.7)
        self._downloaded(state, "b", downloads, "two.mp3", confidence=0.7)
        cfg = make_cfg(
            album_folders=True, rename_files=True, dest_dir=str(downloads)
        )

        assert organize_files(state, cfg, store) == 2
        folder = downloads / "Eagles - Hotel California"
        assert (folder / "one.mp3").exists()  # filed, but original name kept
