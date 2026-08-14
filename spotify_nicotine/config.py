"""Configuration loading: CLI > environment (incl. .env) > config.json > defaults."""

import argparse
import json
import os
import re
from dataclasses import dataclass, fields
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG_PATH = "./config.json"
DEFAULT_ENV_PATH = "./.env"

DEFAULTS = {
    "api_url": "http://127.0.0.1:12339",
    "api_token": None,
    "spotify_client_id": None,
    "spotify_client_secret": None,
    "spotify_auth_mode": "auto",
    "spotify_redirect_uri": "http://127.0.0.1:8080/callback",
    "spotify_token_cache": "./.spotify-tokens.json",
    "min_confidence": 0.65,
    "formats": ["mp3", "m4a", "flac", "wav", "aiff"],
    "prefer_bitrate": 320,
    "min_bitrate": 192,
    "search_timeout": 20.0,
    "search_delay": 2.0,
    "search_concurrency": 6,
    "max_fallbacks": 3,
    "stall_retry_mins": 5.0,
    "max_retries": 3,
    "monitor_mins": 10.0,
    "limit": None,
    "dest_dir": None,
    "dry_run": False,
    "state_dir": "./state",
    "verbose": False,
}

# environment variable -> config key
ENV_MAP = {
    "SPOTIFY_CLIENT_ID": "spotify_client_id",
    "SPOTIFY_CLIENT_SECRET": "spotify_client_secret",
    "SPOTIFY_REDIRECT_URI": "spotify_redirect_uri",
    "NICOTINE_API_TOKEN": "api_token",
    "SPOTIFY_NICOTINE_API_URL": "api_url",
    "SPOTIFY_NICOTINE_STATE_DIR": "state_dir",
    "SPOTIFY_NICOTINE_DEST_DIR": "dest_dir",
    "SPOTIFY_NICOTINE_AUTH_MODE": "spotify_auth_mode",
    "SPOTIFY_NICOTINE_TOKEN_CACHE": "spotify_token_cache",
}

AUTH_MODES = ("auto", "user", "client")


class ConfigError(Exception):
    pass


@dataclass
class Config:
    api_url: str
    api_token: Optional[str]
    spotify_client_id: Optional[str]
    spotify_client_secret: Optional[str]
    spotify_auth_mode: str
    spotify_redirect_uri: str
    spotify_token_cache: str
    min_confidence: float
    formats: List[str]
    prefer_bitrate: int
    min_bitrate: int
    search_timeout: float
    search_delay: float
    search_concurrency: int
    max_fallbacks: int
    stall_retry_mins: float
    max_retries: int
    monitor_mins: float
    limit: Optional[int]
    dest_dir: Optional[str]
    dry_run: bool
    state_dir: str
    verbose: bool


def parse_formats(value: Any) -> List[str]:
    if isinstance(value, str):
        parts = [p.strip().lower().lstrip(".") for p in value.split(",")]
    elif isinstance(value, (list, tuple)):
        parts = [str(p).strip().lower().lstrip(".") for p in value]
    else:
        raise ConfigError("formats must be a comma-separated string or a list")
    parts = [p for p in parts if p]
    if not parts:
        raise ConfigError("formats must contain at least one extension")
    return parts


def parse_env_file(path: str) -> Dict[str, str]:
    """Minimal KEY=VALUE parser; '#' comments and blank lines ignored."""
    result: Dict[str, str] = {}
    if not os.path.isfile(path):
        return result
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if key:
                result[key] = value
    return result


_PLAYLIST_URL_RE = re.compile(
    r"(?:open\.spotify\.com/(?:[a-z-]+/)?playlist/|spotify:playlist:)([A-Za-z0-9]+)"
)
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9]{16,34}$")


def parse_playlist_ref(ref: str) -> str:
    """Accept a full open.spotify.com URL, a spotify: URI, or a bare playlist id."""
    ref = ref.strip()
    match = _PLAYLIST_URL_RE.search(ref)
    if match:
        return match.group(1)
    if _PLAYLIST_ID_RE.match(ref):
        return ref
    raise ConfigError(
        "Unrecognized playlist reference: %r (expected an open.spotify.com/playlist "
        "URL, spotify:playlist: URI, or a bare playlist id)" % ref
    )


def _load_config_file(path: str, explicit: bool) -> Dict[str, Any]:
    if not os.path.isfile(path):
        if explicit:
            raise ConfigError("Config file not found: %s" % path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except ValueError as exc:
        raise ConfigError("Invalid JSON in %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ConfigError("Config file %s must contain a JSON object" % path)
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ConfigError(
            "Unknown config key(s) in %s: %s" % (path, ", ".join(sorted(unknown)))
        )
    return data


def load_config(args: argparse.Namespace, environ: Optional[Dict[str, str]] = None) -> Config:
    environ = dict(os.environ if environ is None else environ)

    # .env fills in variables not already present in the real environment.
    for key, value in parse_env_file(getattr(args, "env_file", None) or DEFAULT_ENV_PATH).items():
        environ.setdefault(key, value)

    merged: Dict[str, Any] = dict(DEFAULTS)

    config_path = getattr(args, "config", None)
    merged.update(
        _load_config_file(config_path or DEFAULT_CONFIG_PATH, explicit=config_path is not None)
    )

    for env_key, cfg_key in ENV_MAP.items():
        if environ.get(env_key):
            merged[cfg_key] = environ[env_key]

    for field_def in fields(Config):
        cli_value = getattr(args, field_def.name, None)
        if cli_value is not None and cli_value is not False:
            merged[field_def.name] = cli_value

    if merged["spotify_auth_mode"] not in AUTH_MODES:
        raise ConfigError(
            "spotify_auth_mode must be one of: %s" % ", ".join(AUTH_MODES)
        )
    merged["formats"] = parse_formats(merged["formats"])
    for key in (
        "min_confidence",
        "search_timeout",
        "search_delay",
        "stall_retry_mins",
        "monitor_mins",
    ):
        merged[key] = float(merged[key])
    for key in (
        "prefer_bitrate",
        "min_bitrate",
        "max_fallbacks",
        "max_retries",
        "search_concurrency",
    ):
        merged[key] = int(merged[key])
    if merged["max_retries"] < 0:
        raise ConfigError("max_retries must be >= 0 (0 disables stall retries)")
    if merged["stall_retry_mins"] < 1:
        raise ConfigError(
            "stall_retry_mins must be at least 1 minute — nudging a peer's "
            "queue more often than that is abusive"
        )
    if not 1 <= merged["search_concurrency"] <= 15:
        raise ConfigError(
            "search_concurrency must be between 1 and 15 (the Nicotine+ plugin "
            "only caches ~20 searches; more concurrency risks losing results)"
        )
    if merged["search_delay"] < 0.5:
        raise ConfigError(
            "search_delay must be at least 0.5 seconds: dispatching searches "
            "faster risks a temporary ban from the Soulseek server"
        )
    if merged["limit"] is not None:
        merged["limit"] = int(merged["limit"])
    merged["dry_run"] = bool(merged["dry_run"])
    merged["verbose"] = bool(merged["verbose"])

    return Config(**{f.name: merged[f.name] for f in fields(Config)})
