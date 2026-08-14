import argparse
import json

import pytest

from spotify_nicotine.config import (
    ConfigError,
    load_config,
    parse_env_file,
    parse_formats,
    parse_playlist_ref,
)


def make_args(**overrides):
    defaults = {
        "config": None,
        "env_file": None,
        "api_url": None,
        "api_token": None,
        "min_confidence": None,
        "formats": None,
        "prefer_bitrate": None,
        "min_bitrate": None,
        "search_timeout": None,
        "search_delay": None,
        "max_fallbacks": None,
        "monitor_mins": None,
        "limit": None,
        "dest_dir": None,
        "dry_run": False,
        "state_dir": None,
        "verbose": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestParsePlaylistRef:
    def test_full_url(self):
        ref = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc123"
        assert parse_playlist_ref(ref) == "37i9dQZF1DXcBWIGoYBM5M"

    def test_url_with_locale_segment(self):
        ref = "https://open.spotify.com/intl-fr/playlist/37i9dQZF1DXcBWIGoYBM5M"
        assert parse_playlist_ref(ref) == "37i9dQZF1DXcBWIGoYBM5M"

    def test_uri(self):
        assert parse_playlist_ref("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M") == "37i9dQZF1DXcBWIGoYBM5M"

    def test_bare_id(self):
        assert parse_playlist_ref("37i9dQZF1DXcBWIGoYBM5M") == "37i9dQZF1DXcBWIGoYBM5M"

    def test_garbage_rejected(self):
        with pytest.raises(ConfigError):
            parse_playlist_ref("not a playlist!")

    def test_track_url_rejected(self):
        with pytest.raises(ConfigError):
            parse_playlist_ref("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC")


class TestParseFormats:
    def test_comma_string(self):
        assert parse_formats("mp3, FLAC") == ["mp3", "flac"]

    def test_leading_dot_stripped(self):
        assert parse_formats(".mp3,.m4a") == ["mp3", "m4a"]

    def test_list(self):
        assert parse_formats(["Mp3"]) == ["mp3"]

    def test_empty_rejected(self):
        with pytest.raises(ConfigError):
            parse_formats(" , ")


class TestParseEnvFile:
    def test_parses_and_ignores_comments(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "# comment\n"
            "SPOTIFY_CLIENT_ID=abc\n"
            "QUOTED='hello world'\n"
            "DOUBLE=\"x=y\"\n"
            "\n"
            "NOEQUALS\n"
        )
        parsed = parse_env_file(str(env))
        assert parsed == {
            "SPOTIFY_CLIENT_ID": "abc",
            "QUOTED": "hello world",
            "DOUBLE": "x=y",
        }

    def test_missing_file_is_empty(self, tmp_path):
        assert parse_env_file(str(tmp_path / "nope")) == {}


class TestPrecedence:
    def test_defaults(self):
        cfg = load_config(make_args(), environ={})
        assert cfg.api_url == "http://127.0.0.1:12339"
        assert cfg.formats == ["mp3", "m4a", "flac", "wav", "aiff"]
        assert cfg.prefer_bitrate == 320
        assert cfg.min_bitrate == 192
        assert cfg.min_confidence == 0.65
        assert cfg.limit is None
        assert cfg.spotify_auth_mode == "auto"
        assert cfg.spotify_redirect_uri == "http://127.0.0.1:8080/callback"
        assert cfg.spotify_token_cache == "./.spotify-tokens.json"

    def test_invalid_auth_mode_rejected(self):
        with pytest.raises(ConfigError, match="spotify_auth_mode"):
            load_config(make_args(spotify_auth_mode="magic"), environ={})

    def test_search_concurrency_default(self):
        cfg = load_config(make_args(), environ={})
        assert cfg.search_concurrency == 6

    def test_search_concurrency_bounds(self):
        with pytest.raises(ConfigError, match="search_concurrency"):
            load_config(make_args(search_concurrency=0), environ={})
        with pytest.raises(ConfigError, match="search_concurrency"):
            load_config(make_args(search_concurrency=16), environ={})

    def test_search_delay_default_is_conservative(self):
        # The Soulseek server blocks fast searchers; 8s survived 300+ tracks.
        cfg = load_config(make_args(), environ={})
        assert cfg.search_delay == 8.0
        assert cfg.max_empty_streak == 6

    def test_search_delay_floor(self):
        with pytest.raises(ConfigError, match="search_delay"):
            load_config(make_args(search_delay=1.0), environ={})
        # the floor is honoured, not silently clamped
        assert load_config(make_args(search_delay=2.0), environ={}).search_delay == 2.0

    def test_stall_retry_defaults(self):
        cfg = load_config(make_args(), environ={})
        assert cfg.stall_retry_mins == 5.0
        assert cfg.max_retries == 3

    def test_stall_retry_validation(self):
        with pytest.raises(ConfigError, match="max_retries"):
            load_config(make_args(max_retries=-1), environ={})
        with pytest.raises(ConfigError, match="stall_retry_mins"):
            load_config(make_args(stall_retry_mins=0.5), environ={})

    def test_config_file_overrides_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"prefer_bitrate": 256, "formats": "flac,mp3"}))
        cfg = load_config(make_args(config=str(path)), environ={})
        assert cfg.prefer_bitrate == 256
        assert cfg.formats == ["flac", "mp3"]

    def test_env_overrides_config_file(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"api_url": "http://file:1"}))
        cfg = load_config(
            make_args(config=str(path)),
            environ={"SPOTIFY_NICOTINE_API_URL": "http://env:2"},
        )
        assert cfg.api_url == "http://env:2"

    def test_cli_overrides_env(self):
        cfg = load_config(
            make_args(api_url="http://cli:3"),
            environ={"SPOTIFY_NICOTINE_API_URL": "http://env:2"},
        )
        assert cfg.api_url == "http://cli:3"

    def test_env_file_fills_missing_only(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            "SPOTIFY_CLIENT_ID=from_dotenv\nNICOTINE_API_TOKEN=tok\n"
        )
        cfg = load_config(make_args(), environ={"SPOTIFY_CLIENT_ID": "from_real_env"})
        assert cfg.spotify_client_id == "from_real_env"
        assert cfg.api_token == "tok"

    def test_cli_formats_parsed(self):
        cfg = load_config(make_args(formats="flac,mp3"), environ={})
        assert cfg.formats == ["flac", "mp3"]

    def test_unknown_config_key_rejected(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"nope": 1}))
        with pytest.raises(ConfigError):
            load_config(make_args(config=str(path)), environ={})

    def test_explicit_missing_config_rejected(self):
        with pytest.raises(ConfigError):
            load_config(make_args(config="/nonexistent/config.json"), environ={})
