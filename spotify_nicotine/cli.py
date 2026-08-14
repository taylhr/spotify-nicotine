"""Command-line interface: download / resolve / status."""

import argparse
import sys
import warnings
from typing import Any, Dict, List, Optional, Tuple

# macOS system Python links LibreSSL; urllib3 warns about it on every import.
# Harmless here, and noise for the user, so silence it before requests loads.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

import requests

from spotify_nicotine import __version__, oauth, ui
from spotify_nicotine.config import Config, ConfigError, load_config, parse_playlist_ref
from spotify_nicotine.models import StatusReason, TrackStatus
from spotify_nicotine.orchestrator import reconcile, run_download
from spotify_nicotine.rename import apply_renames
from spotify_nicotine.resolve import run_resolve
from spotify_nicotine.slsk_api import SlskApiError, SlskClient
from spotify_nicotine.spotify import (
    ClientCredentialsProvider,
    SpotifyClient,
    SpotifyError,
    SpotifyForbiddenError,
    UserTokenProvider,
    fetch_playlist,
)
from spotify_nicotine.state import (
    StateStore,
    merge_playlist,
    new_state,
    set_track_status,
)

EXIT_OK = 0
EXIT_NEEDS_REVIEW = 2
EXIT_FAILURES = 3
EXIT_ERROR = 4
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spotify-nicotine",
        description="Download a Spotify playlist from Soulseek via the "
        "Nicotine+ API plugin.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("playlist", help="Spotify playlist URL, URI, or id")
        sub.add_argument("--config", help="path to config.json")
        sub.add_argument("--env-file", help="path to .env file (default ./.env)")
        sub.add_argument("--api-url", help="Nicotine+ API base URL")
        sub.add_argument("--api-token", help="Nicotine+ API token, if configured")
        sub.add_argument("--state-dir", help="state directory (default ./state)")
        sub.add_argument(
            "--auth-mode",
            dest="spotify_auth_mode",
            choices=["auto", "user", "client"],
            help="Spotify auth: browser user tokens, app-only tokens, or auto",
        )
        sub.add_argument(
            "--token-cache",
            dest="spotify_token_cache",
            help="path of the Spotify user-token cache (default ./.spotify-tokens.json)",
        )
        sub.add_argument("-v", "--verbose", action="store_true", default=False)

    def add_download_opts(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--min-confidence", type=float, dest="min_confidence")
        sub.add_argument(
            "--formats", help="preferred formats in order, e.g. 'mp3,flac'"
        )
        sub.add_argument("--prefer-bitrate", type=int, dest="prefer_bitrate")
        sub.add_argument("--min-bitrate", type=int, dest="min_bitrate")
        sub.add_argument("--search-timeout", type=float, dest="search_timeout")
        sub.add_argument(
            "--search-delay",
            type=float,
            dest="search_delay",
            help="minimum seconds between search dispatches (default 2.0)",
        )
        sub.add_argument(
            "--search-concurrency",
            type=int,
            dest="search_concurrency",
            help="how many track searches run at once (default 6)",
        )
        sub.add_argument(
            "--max-empty-streak",
            type=int,
            dest="max_empty_streak",
            help="empty-result tracks in a row before checking whether the "
            "Soulseek server has stopped answering (default 6, 0 = off)",
        )
        sub.add_argument("--max-fallbacks", type=int, dest="max_fallbacks")
        sub.add_argument(
            "--stall-retry-mins",
            type=float,
            dest="stall_retry_mins",
            help="minutes a download may stall before it is nudged (default 5)",
        )
        sub.add_argument(
            "--max-retries",
            type=int,
            dest="max_retries",
            help="stalled-download nudges before dropping the uploader "
            "(default 3, 0 = never nudge or drop)",
        )
        sub.add_argument("--monitor-mins", type=float, dest="monitor_mins")
        sub.add_argument("--limit", type=int, help="only process first N tracks")
        sub.add_argument(
            "--dest-dir",
            dest="dest_dir",
            help="local download folder passed to Nicotine+ (default: its own)",
        )
        sub.add_argument("--dry-run", action="store_true", default=False)
        sub.add_argument(
            "--rename-files",
            action="store_true",
            default=False,
            dest="rename_files",
            help="rename finished downloads to 'Artist - Title.ext' using the "
            "Spotify metadata (only for matches at or above "
            "--rename-min-confidence)",
        )
        sub.add_argument(
            "--rename-min-confidence",
            type=float,
            dest="rename_min_confidence",
            help="confidence required before a file is renamed (default 1.0, "
            "i.e. only perfect matches)",
        )
        sub.add_argument(
            "--retry-no-results",
            action="store_true",
            default=False,
            dest="retry_no_results",
            help="re-search tracks previously recorded as having no results "
            "(use after a run was cut short by the Soulseek rate limit)",
        )

    download = subparsers.add_parser(
        "download", help="search and queue the whole playlist"
    )
    add_common(download)
    add_download_opts(download)

    resolve = subparsers.add_parser(
        "resolve", help="interactively resolve low-confidence tracks"
    )
    add_common(resolve)
    add_download_opts(resolve)

    status = subparsers.add_parser("status", help="show playlist progress")
    add_common(status)

    auth = subparsers.add_parser(
        "auth", help="one-time browser authorization with Spotify"
    )
    auth.add_argument("--config", help="path to config.json")
    auth.add_argument("--env-file", help="path to .env file (default ./.env)")
    auth.add_argument(
        "--redirect-uri",
        dest="spotify_redirect_uri",
        help="must exactly match a Redirect URI in your Spotify app settings",
    )
    auth.add_argument(
        "--token-cache",
        dest="spotify_token_cache",
        help="where to store tokens (default ./.spotify-tokens.json)",
    )
    auth.add_argument("-v", "--verbose", action="store_true", default=False)

    return parser


def _exit_code_from_state(state: Dict[str, Any]) -> int:
    statuses = [r.get("status") for r in state.get("tracks", {}).values()]
    if any(s == TrackStatus.FAILED for s in statuses):
        return EXIT_FAILURES
    if any(s == TrackStatus.NEEDS_REVIEW for s in statuses):
        return EXIT_NEEDS_REVIEW
    return EXIT_OK


def _connect_slsk(cfg: Config) -> SlskClient:
    slsk = SlskClient(cfg.api_url, api_token=cfg.api_token)
    if not slsk.health():
        raise SlskApiError(
            0,
            "Nicotine+ API at %s is not responding. Start Nicotine+ and make "
            "sure the api-nicotine-plus plugin is enabled (see README)."
            % cfg.api_url,
        )
    status = slsk.status()
    if not status.get("connected"):
        raise SlskApiError(
            0,
            "Nicotine+ is running but not connected to the Soulseek server yet. "
            "Wait for it to log in and retry.",
        )
    return slsk


def _load_state_or_fail(store: StateStore, playlist_id: str) -> Dict[str, Any]:
    state = store.load()
    if state is None:
        raise ConfigError(
            "No state for playlist %s in %s. Run the 'download' command first."
            % (playlist_id, store.state_dir)
        )
    return state


def _build_spotify(cfg: Config) -> Tuple[str, SpotifyClient]:
    """Pick the Spotify auth path. 'auto' prefers cached user tokens, falls
    back to app-only tokens when only a client secret is configured."""
    mode = cfg.spotify_auth_mode
    has_user_tokens = oauth.load_tokens(cfg.spotify_token_cache) is not None
    if mode == "user" or (
        mode == "auto" and (has_user_tokens or not cfg.spotify_client_secret)
    ):
        provider = UserTokenProvider(
            cfg.spotify_client_id or "", cfg.spotify_token_cache
        )
        return "user", SpotifyClient(provider)
    provider = ClientCredentialsProvider(
        cfg.spotify_client_id or "", cfg.spotify_client_secret or ""
    )
    return "client", SpotifyClient(provider)


def _run_browser_auth(cfg: Config) -> Dict[str, Any]:
    return oauth.authorize_interactive(
        cfg.spotify_client_id or "",
        cfg.spotify_redirect_uri,
        cfg.spotify_token_cache,
        session=requests.Session(),
    )


def _verify_authorized_user(cfg: Config) -> int:
    """Call /me with the fresh user token: proves the token works against
    this app and shows WHICH account was authorized (the browser silently
    uses whoever is signed in at accounts.spotify.com)."""
    client = SpotifyClient(
        UserTokenProvider(cfg.spotify_client_id or "", cfg.spotify_token_cache)
    )
    try:
        me = client.get_me()
    except SpotifyForbiddenError as exc:
        print("")
        print("Browser sign-in completed, but the Spotify API REJECTS this user:")
        print("%s" % exc)
        return EXIT_ERROR
    print(
        "Authorized as: %s (account id: %s)"
        % (me.get("display_name") or "?", me.get("id") or "?")
    )
    print(
        "If that is not the account that owns your playlists/app, sign into "
        "the right account at accounts.spotify.com and run 'auth' again."
    )
    return EXIT_OK


def cmd_auth(cfg: Config) -> int:
    if not cfg.spotify_client_id:
        raise ConfigError(
            "SPOTIFY_CLIENT_ID is not set. Put it in the .env file first "
            "(README Part 5)."
        )
    print("Redirect URI in use: %s" % cfg.spotify_redirect_uri)
    print(
        "It must be listed EXACTLY like that in your Spotify app settings "
        "(developer.spotify.com/dashboard -> your app -> Settings -> "
        "Redirect URIs)."
    )
    _run_browser_auth(cfg)
    result = _verify_authorized_user(cfg)
    if result == EXIT_OK:
        print("Done. You can now run the 'download' command; your private "
              "playlists are readable too.")
    return result


def cmd_download(cfg: Config, playlist_id: str) -> int:
    auth_kind, spotify = _build_spotify(cfg)
    if auth_kind == "user":
        print("Spotify auth: browser user tokens (%s)" % cfg.spotify_token_cache)
    else:
        print("Spotify auth: app-only client credentials")
    print("Fetching playlist %s from Spotify..." % playlist_id)
    try:
        meta, tracks, skipped = fetch_playlist(spotify, playlist_id)
    except SpotifyForbiddenError as exc:
        # App-only tokens can no longer read playlists on newer apps; offer
        # the browser authorization right here.
        if auth_kind != "client":
            raise
        print("\n%s" % exc)
        if not sys.stdin.isatty():
            print("Run 'spotify-nicotine auth' once, then retry this command.")
            return EXIT_ERROR
        answer = input("Authorize in your browser now? [Y/n] ").strip().lower()
        if answer not in ("", "y", "yes"):
            return EXIT_ERROR
        _run_browser_auth(cfg)
        spotify = SpotifyClient(
            UserTokenProvider(cfg.spotify_client_id or "", cfg.spotify_token_cache)
        )
        meta, tracks, skipped = fetch_playlist(spotify, playlist_id)

    store = StateStore(cfg.state_dir, playlist_id)
    state = store.load() or new_state(meta)
    changes = merge_playlist(state, meta, tracks, skipped)

    if cfg.retry_no_results:
        reset = 0
        for record in state["tracks"].values():
            if (
                record.get("status") == TrackStatus.NEEDS_REVIEW
                and record.get("status_reason") == StatusReason.NO_RESULTS
            ):
                set_track_status(record, TrackStatus.PENDING)
                reset += 1
        print("Re-searching %d track(s) that previously found no results." % reset)

    store.save(state)

    print(
        "Playlist %r by %s: %d tracks (%d new, %d removed, %d restored, %d skipped)"
        % (
            meta.get("name", ""),
            meta.get("owner", ""),
            len(tracks),
            len(changes["added"]),
            len(changes["removed"]),
            len(changes["restored"]),
            len(skipped),
        )
    )

    slsk = _connect_slsk(cfg)
    if cfg.dry_run:
        print("Dry run: searching and scoring only, nothing will be queued.")

    run_download(cfg, state, slsk, store, log=print)

    for line in ui.summary_lines(state, verbose=cfg.verbose):
        print(line)

    review = [
        r
        for r in state["tracks"].values()
        if r.get("status") == TrackStatus.NEEDS_REVIEW
    ]
    if review and not cfg.dry_run and sys.stdin.isatty():
        answer = input(
            "Resolve %d low-confidence track(s) now? [Y/n] " % len(review)
        ).strip().lower()
        if answer in ("", "y", "yes"):
            run_resolve(cfg, slsk, store, state)
            for line in ui.summary_lines(state, verbose=cfg.verbose):
                print(line)

    return _exit_code_from_state(state)


def cmd_resolve(cfg: Config, playlist_id: str) -> int:
    store = StateStore(cfg.state_dir, playlist_id)
    state = _load_state_or_fail(store, playlist_id)
    slsk = _connect_slsk(cfg)
    reconcile(state, slsk, cfg, store, log=print)
    run_resolve(cfg, slsk, store, state)
    for line in ui.summary_lines(state, verbose=cfg.verbose):
        print(line)
    return _exit_code_from_state(state)


def cmd_status(cfg: Config, playlist_id: str) -> int:
    store = StateStore(cfg.state_dir, playlist_id)
    state = _load_state_or_fail(store, playlist_id)

    slsk = SlskClient(cfg.api_url, api_token=cfg.api_token)
    if slsk.health():
        reconcile(state, slsk, cfg, store, log=print)
    else:
        print(
            "(Nicotine+ API not reachable; showing last known state without "
            "refreshing transfer progress)"
        )
    apply_renames(state, cfg, store, log=print)

    for line in ui.summary_lines(state, verbose=True):
        print(line)
    return _exit_code_from_state(state)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args)
        if args.command == "auth":
            return cmd_auth(cfg)
        playlist_id = parse_playlist_ref(args.playlist)
        if args.command == "download":
            return cmd_download(cfg, playlist_id)
        if args.command == "resolve":
            return cmd_resolve(cfg, playlist_id)
        return cmd_status(cfg, playlist_id)
    except KeyboardInterrupt:
        print("\nInterrupted — state saved. Re-run the same command to resume.")
        return EXIT_INTERRUPTED
    except (ConfigError, SpotifyError, SlskApiError, oauth.OAuthError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
