import pytest

from spotify_nicotine import oauth
from spotify_nicotine.cli import (
    EXIT_ERROR,
    EXIT_FAILURES,
    EXIT_NEEDS_REVIEW,
    EXIT_OK,
    _build_spotify,
    _exit_code_from_state,
    build_parser,
    main,
)
from spotify_nicotine.models import TrackStatus
from spotify_nicotine.state import merge_playlist, new_state, set_track_status

from tests.conftest import make_cfg, make_track

META = {"id": "pl1", "name": "Mix", "snapshot_id": "snap1"}


def state_with_statuses(*statuses):
    state = new_state(META)
    tracks = [make_track(track_id="t%d" % i) for i in range(len(statuses))]
    merge_playlist(state, META, tracks, [])
    for i, status in enumerate(statuses):
        set_track_status(state["tracks"]["t%d" % i], status)
    return state


class TestParser:
    def test_download_flags(self):
        args = build_parser().parse_args(
            [
                "download",
                "37i9dQZF1DXcBWIGoYBM5M",
                "--formats",
                "flac,mp3",
                "--min-confidence",
                "0.7",
                "--dry-run",
                "--limit",
                "3",
            ]
        )
        assert args.command == "download"
        assert args.formats == "flac,mp3"
        assert args.min_confidence == 0.7
        assert args.dry_run is True
        assert args.limit == 3

    def test_status_has_no_download_flags(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["status", "id123", "--dry-run"])

    def test_command_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])


class TestExitCodes:
    def test_clean(self):
        state = state_with_statuses(TrackStatus.DOWNLOADED, TrackStatus.QUEUED)
        assert _exit_code_from_state(state) == EXIT_OK

    def test_needs_review(self):
        state = state_with_statuses(TrackStatus.DOWNLOADED, TrackStatus.NEEDS_REVIEW)
        assert _exit_code_from_state(state) == EXIT_NEEDS_REVIEW

    def test_failed_takes_precedence(self):
        state = state_with_statuses(TrackStatus.NEEDS_REVIEW, TrackStatus.FAILED)
        assert _exit_code_from_state(state) == EXIT_FAILURES


class TestMainErrors:
    def test_bad_playlist_ref(self, tmp_path, capsys):
        code = main(["status", "not a playlist!!", "--state-dir", str(tmp_path)])
        assert code == EXIT_ERROR
        assert "Unrecognized playlist" in capsys.readouterr().err

    def test_status_without_state(self, tmp_path, capsys):
        code = main(
            ["status", "37i9dQZF1DXcBWIGoYBM5M", "--state-dir", str(tmp_path)]
        )
        assert code == EXIT_ERROR
        assert "download" in capsys.readouterr().err

    def test_download_without_auth_instructs(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
        monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)
        monkeypatch.chdir(tmp_path)  # no ./.env, ./config.json, or token cache
        code = main(
            ["download", "37i9dQZF1DXcBWIGoYBM5M", "--state-dir", str(tmp_path)]
        )
        assert code == EXIT_ERROR
        assert "spotify-nicotine auth" in capsys.readouterr().err


class TestAuthCommand:
    def test_auth_parser_needs_no_playlist(self):
        args = build_parser().parse_args(
            ["auth", "--redirect-uri", "http://127.0.0.1:9/cb"]
        )
        assert args.command == "auth"
        assert args.spotify_redirect_uri == "http://127.0.0.1:9/cb"

    def test_auth_without_client_id_errors(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
        monkeypatch.chdir(tmp_path)
        code = main(["auth"])
        assert code == EXIT_ERROR
        assert "SPOTIFY_CLIENT_ID" in capsys.readouterr().err


class TestAuthVerification:
    def _setup(self, tmp_path, monkeypatch):
        import spotify_nicotine.cli as cli_module

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")

        def fake_browser_auth(cfg):
            oauth.save_tokens(
                cfg.spotify_token_cache,
                {"access_token": "at", "refresh_token": "rt", "expires_at": 10**12},
            )
            return {"scope": "playlist-read-private"}

        monkeypatch.setattr(cli_module, "_run_browser_auth", fake_browser_auth)
        return cli_module

    def test_auth_prints_authorized_identity(self, tmp_path, monkeypatch, capsys):
        cli_module = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(
            cli_module.SpotifyClient,
            "get_me",
            lambda self: {"display_name": "Taylor", "id": "rhyx"},
        )
        code = main(["auth"])
        out = capsys.readouterr().out
        assert code == EXIT_OK
        assert "Authorized as: Taylor (account id: rhyx)" in out

    def test_auth_detects_unregistered_user(self, tmp_path, monkeypatch, capsys):
        from spotify_nicotine.spotify import SpotifyForbiddenError, forbidden_message

        cli_module = self._setup(tmp_path, monkeypatch)

        def raise_forbidden(self):
            raise SpotifyForbiddenError(
                forbidden_message("User not registered in the Developer Dashboard")
            )

        monkeypatch.setattr(cli_module.SpotifyClient, "get_me", raise_forbidden)
        code = main(["auth"])
        out = capsys.readouterr().out
        assert code == EXIT_ERROR
        assert "REJECTS this user" in out
        assert "User Management" in out


class TestBuildSpotify:
    def _cfg(self, tmp_path, **overrides):
        defaults = {
            "spotify_client_id": "cid",
            "spotify_token_cache": str(tmp_path / "tokens.json"),
        }
        defaults.update(overrides)
        return make_cfg(**defaults)

    def test_auto_prefers_cached_user_tokens(self, tmp_path):
        cfg = self._cfg(tmp_path, spotify_client_secret="sec")
        oauth.save_tokens(
            cfg.spotify_token_cache, {"refresh_token": "rt", "access_token": "at"}
        )
        kind, _client = _build_spotify(cfg)
        assert kind == "user"

    def test_auto_without_cache_uses_client_credentials_if_secret(self, tmp_path):
        cfg = self._cfg(tmp_path, spotify_client_secret="sec")
        kind, _client = _build_spotify(cfg)
        assert kind == "client"

    def test_auto_without_secret_uses_user_flow(self, tmp_path):
        cfg = self._cfg(tmp_path, spotify_client_secret=None)
        kind, _client = _build_spotify(cfg)
        assert kind == "user"

    def test_forced_user_mode(self, tmp_path):
        cfg = self._cfg(
            tmp_path, spotify_client_secret="sec", spotify_auth_mode="user"
        )
        kind, _client = _build_spotify(cfg)
        assert kind == "user"
