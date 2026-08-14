"""Spotify Web API client.

Two ways to get tokens:

- ``UserTokenProvider`` — user authorization (Authorization Code + PKCE, see
  oauth.py). The default: works with playlists (public and the user's own
  private ones) on all developer apps.
- ``ClientCredentialsProvider`` — app-only tokens. Kept for older developer
  apps; Spotify returns 403 on playlist endpoints for apps created since 2025.
"""

import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import requests

from spotify_nicotine import oauth
from spotify_nicotine.models import Track

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
MAX_RETRIES = 5

EDITORIAL_404_HINT = (
    "Spotify returned 404 for this playlist. Note: since November 2024, "
    "Spotify-owned editorial/algorithmic playlists (Today's Top Hits, Discover "
    "Weekly, ...) are not accessible to new developer apps. User-created "
    "playlists work fine. Also double-check the playlist id, and that the "
    "account you authorized can see the playlist."
)

FORBIDDEN_HINT = (
    "Spotify returned 403 Forbidden. For developer apps created since 2025, "
    "Spotify blocks app-only (Client Credentials) tokens from reading "
    "playlists — even public ones. Fix: run 'spotify-nicotine auth' once to "
    "authorize in your browser (this also unlocks your private playlists). "
    "If you already authorized in the browser, the authorized account may "
    "simply not have access to this playlist."
)

USER_NOT_REGISTERED_HINT = (
    "Spotify rejected the authorized user (Spotify said: %r).\n"
    "Your developer app is in Development Mode: only the account that OWNS "
    "the app, plus accounts listed under its User Management, may use it.\n"
    "Two things to check:\n"
    "  1. Which account did the browser actually authorize? It silently uses "
    "whoever is signed in at accounts.spotify.com — possibly not the account "
    "you created the app with. Run 'spotify-nicotine auth' again; it prints "
    "'Authorized as: ...' at the end.\n"
    "  2. If you want to use a different account than the app owner: open "
    "developer.spotify.com/dashboard -> your app -> Settings -> User "
    "Management, add that account's full name and email, save, and retry "
    "(no need to re-authorize)."
)


def forbidden_message(detail: str) -> str:
    if "not registered" in detail.lower():
        return USER_NOT_REGISTERED_HINT % detail
    return "%s (Spotify said: %r)" % (FORBIDDEN_HINT, detail)


class SpotifyError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class SpotifyAuthError(SpotifyError):
    pass


class SpotifyForbiddenError(SpotifyError):
    pass


ITEMS_FORBIDDEN_HINT = (
    "Spotify refused to return this playlist's contents (both the current "
    "/items endpoint and the legacy /tracks endpoint). Since the February "
    "2026 Web API changes, Development Mode apps only receive playlist "
    "contents for playlists the AUTHORIZED account owns or collaborates on "
    "— arbitrary public playlists are no longer readable. Check that the "
    "'Authorized as:' account from 'spotify-nicotine auth' is the owner of "
    "this playlist, and note the app owner must also have an active Spotify "
    "Premium subscription."
)


class ClientCredentialsProvider:
    """App-only token via the client-credentials flow (id + secret)."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        session: Optional[requests.Session] = None,
        clock: Callable[[], float] = time.time,
    ):
        if not client_id or not client_secret:
            raise SpotifyAuthError(
                "Spotify credentials missing. Set SPOTIFY_CLIENT_ID and "
                "SPOTIFY_CLIENT_SECRET (environment or .env file), or use "
                "'spotify-nicotine auth' for browser authorization."
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session or requests.Session()
        self._clock = clock
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    def get_token(self, force: bool = False) -> str:
        if not force and self._token and self._clock() < self._expires_at:
            return self._token
        try:
            response = self.session.request(
                "POST",
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self.client_id, self.client_secret),
                timeout=15,
            )
        except requests.RequestException as exc:
            raise SpotifyError("Cannot reach Spotify token endpoint: %s" % exc)
        if response.status_code != 200:
            raise SpotifyAuthError(
                "Spotify token request failed (HTTP %d). Check your client id/"
                "secret. Response: %s" % (response.status_code, response.text[:200])
            )
        data = response.json()
        self._token = data["access_token"]
        self._expires_at = self._clock() + int(data.get("expires_in", 3600)) - 60
        return self._token


class UserTokenProvider:
    """User token from the on-disk cache written by 'spotify-nicotine auth',
    refreshed automatically (PKCE refresh tokens rotate; rotations are saved).
    """

    def __init__(
        self,
        client_id: str,
        cache_path: str,
        session: Optional[requests.Session] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.client_id = client_id
        self.cache_path = cache_path
        self.session = session or requests.Session()
        self._clock = clock
        self._tokens: Optional[Dict[str, Any]] = None

    def get_token(self, force: bool = False) -> str:
        if self._tokens is None:
            self._tokens = oauth.load_tokens(self.cache_path)
        if self._tokens is None:
            raise SpotifyAuthError(
                "Not authorized with Spotify yet. Run 'spotify-nicotine auth' "
                "once (one-time browser sign-in); tokens are then cached in %s."
                % self.cache_path
            )
        if (
            not force
            and self._tokens.get("access_token")
            and self._clock() < self._tokens.get("expires_at", 0) - oauth.EXPIRY_SLACK_S
        ):
            return self._tokens["access_token"]

        client_id = self.client_id or self._tokens.get("client_id") or ""
        try:
            self._tokens = oauth.refresh_tokens(
                self.session, client_id, self._tokens, clock=self._clock
            )
        except oauth.OAuthError as exc:
            raise SpotifyAuthError(
                "Could not refresh the Spotify authorization (%s). Run "
                "'spotify-nicotine auth' again." % exc
            )
        oauth.save_tokens(self.cache_path, self._tokens)
        return self._tokens["access_token"]


class SpotifyClient:
    def __init__(
        self,
        auth: Any,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.auth = auth
        self.session = session or requests.Session()
        self._sleep = sleep

    # -- plumbing ---------------------------------------------------------

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        refreshed = False
        for _attempt in range(MAX_RETRIES):
            token = self.auth.get_token()
            try:
                response = self.session.request(
                    "GET",
                    url,
                    params=params,
                    headers={"Authorization": "Bearer %s" % token},
                    timeout=15,
                )
            except requests.RequestException as exc:
                raise SpotifyError("Spotify request failed: %s" % exc)

            if response.status_code == 200:
                return response.json()
            if response.status_code == 401 and not refreshed:
                refreshed = True
                self.auth.get_token(force=True)
                continue
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 1))
                self._sleep(retry_after)
                continue
            if response.status_code == 403:
                try:
                    body = response.json()
                    detail = (body.get("error") or {}).get("message") or str(body)[:200]
                except ValueError:
                    detail = (response.text or "")[:200]
                raise SpotifyForbiddenError(forbidden_message(detail), status=403)
            if response.status_code == 404:
                raise SpotifyError(EDITORIAL_404_HINT, status=404)
            raise SpotifyError(
                "Spotify API error (HTTP %d) for %s: %s"
                % (response.status_code, url, response.text[:200]),
                status=response.status_code,
            )
        raise SpotifyError("Spotify API retries exhausted for %s" % url)

    # -- endpoints --------------------------------------------------------

    def get_me(self) -> Dict[str, Any]:
        """Profile of the authorized user; also the cheapest way to verify a
        user token actually works against this app."""
        return self._get(API_BASE + "/me")

    def get_playlist_meta(self, playlist_id: str) -> Dict[str, Any]:
        data = self._get("%s/playlists/%s" % (API_BASE, playlist_id))
        # Feb 2026 API: the embedded contents field renamed tracks -> items.
        container = data.get("items") or data.get("tracks") or {}
        return {
            "id": data.get("id", playlist_id),
            "name": data.get("name", ""),
            "owner": (data.get("owner") or {}).get("display_name", ""),
            "snapshot_id": data.get("snapshot_id", ""),
            "url": (data.get("external_urls") or {}).get("spotify", ""),
            "total_tracks": container.get("total", 0),
        }

    def _first_items_page(self, playlist_id: str) -> Dict[str, Any]:
        """Fetch page 1 of the playlist contents.

        Feb 2026 API renamed GET /playlists/{id}/tracks to /items (the old
        path returns a bare 403). Try the current endpoint first, keep the
        legacy one as a fallback for grandfathered apps still on the old API.
        """
        errors = []
        for segment in ("items", "tracks"):
            url = "%s/playlists/%s/%s" % (API_BASE, playlist_id, segment)
            try:
                return self._get(url, params={"limit": 100})
            except SpotifyForbiddenError as exc:
                errors.append(exc)
            except SpotifyError as exc:
                if exc.status != 404:
                    raise
                errors.append(exc)
        if all(isinstance(exc, SpotifyForbiddenError) for exc in errors):
            raise SpotifyForbiddenError(ITEMS_FORBIDDEN_HINT, status=403)
        raise errors[0]

    def iter_playlist_items(self, playlist_id: str) -> Iterator[Dict[str, Any]]:
        page = self._first_items_page(playlist_id)
        while True:
            for item in page.get("items", []):
                yield item
            next_url = page.get("next")
            if not next_url:
                return
            page = self._get(next_url)  # the "next" URL carries the query string


def fetch_playlist(
    client: SpotifyClient, playlist_id: str
) -> Tuple[Dict[str, Any], List[Track], List[Dict[str, Any]]]:
    """Fetch playlist meta and items; returns (meta, tracks, skipped_items).

    Local files, podcast episodes, and null tracks (removed from catalog) are
    reported in skipped_items. Duplicate occurrences of one track collapse
    into a single Track with multiple positions.
    """
    meta = client.get_playlist_meta(playlist_id)
    from spotify_nicotine.state import now_iso

    meta["fetched_at"] = now_iso()

    tracks_by_id: Dict[str, Track] = {}
    order: List[str] = []
    skipped: List[Dict[str, Any]] = []

    for position, item in enumerate(client.iter_playlist_items(playlist_id)):
        # Feb 2026 API renamed the entry key track -> item; support both.
        raw = item.get("item") if "item" in item else item.get("track")
        if raw is None:
            skipped.append({"position": position, "reason": "missing_track"})
            continue
        if raw.get("type") == "episode":
            skipped.append(
                {"position": position, "reason": "episode", "name": raw.get("name", "")}
            )
            continue
        if raw.get("is_local") or not raw.get("id"):
            skipped.append(
                {
                    "position": position,
                    "reason": "local_track",
                    "name": raw.get("name", ""),
                }
            )
            continue

        track_id = raw["id"]
        existing = tracks_by_id.get(track_id)
        if existing is not None:
            existing.positions.append(position)
            continue
        tracks_by_id[track_id] = Track(
            id=track_id,
            name=raw.get("name", ""),
            artists=[a.get("name", "") for a in raw.get("artists", []) if a.get("name")],
            album=(raw.get("album") or {}).get("name", ""),
            duration_ms=int(raw.get("duration_ms") or 0),
            isrc=(raw.get("external_ids") or {}).get("isrc"),
            positions=[position],
        )
        order.append(track_id)

    return meta, [tracks_by_id[tid] for tid in order], skipped
