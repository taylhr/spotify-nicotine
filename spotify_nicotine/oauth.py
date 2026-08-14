"""Spotify user authorization: Authorization Code flow with PKCE.

Spotify blocks app-only (client-credentials) tokens from reading playlists for
developer apps created since 2025, so the supported path is a one-time browser
authorization as the user. PKCE needs only the client id (no secret). Tokens
are cached on disk and refreshed automatically afterwards.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Optional

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# playlist-read-* also covers the user's private/collaborative playlists;
# public playlists need no scope once we act as a real user.
SCOPES = "playlist-read-private playlist-read-collaborative"

CALLBACK_TIMEOUT_S = 300
EXPIRY_SLACK_S = 60

SUCCESS_HTML = (
    "<html><body style='font-family:sans-serif'><h2>Authorized ✓</h2>"
    "<p>spotify-nicotine received the code. You can close this tab and "
    "return to the terminal.</p></body></html>"
)
ERROR_HTML = (
    "<html><body style='font-family:sans-serif'><h2>Authorization failed</h2>"
    "<p>%s</p></body></html>"
)


class OAuthError(Exception):
    pass


# -- PKCE ------------------------------------------------------------------


def generate_pkce_pair() -> "tuple[str, str]":
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(
    client_id: str, redirect_uri: str, state: str, code_challenge: str
) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
    }
    return "%s?%s" % (AUTHORIZE_URL, urllib.parse.urlencode(params))


# -- token cache -----------------------------------------------------------


def load_tokens(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict) or "refresh_token" not in data:
        return None
    return data


def save_tokens(path: str, tokens: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(tokens, handle, indent=2)


# -- token endpoint calls --------------------------------------------------


def _token_request(
    session: Any, payload: Dict[str, str], clock: Callable[[], float]
) -> Dict[str, Any]:
    response = session.request(
        "POST",
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if response.status_code != 200:
        raise OAuthError(
            "Spotify token endpoint returned HTTP %d: %s"
            % (response.status_code, response.text[:300])
        )
    data = response.json()
    data["expires_at"] = clock() + int(data.get("expires_in", 3600))
    return data


def exchange_code(
    session: Any,
    client_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    return _token_request(
        session,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
        clock,
    )


def refresh_tokens(
    session: Any,
    client_id: str,
    tokens: Dict[str, Any],
    clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Refresh an access token; PKCE refresh tokens rotate, so keep the new
    one when Spotify sends it."""
    fresh = _token_request(
        session,
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
        },
        clock,
    )
    merged = dict(tokens)
    merged.update(fresh)
    if not fresh.get("refresh_token"):
        merged["refresh_token"] = tokens["refresh_token"]
    return merged


# -- loopback callback server ----------------------------------------------


def parse_redirect_uri(redirect_uri: str) -> "tuple[str, int, str]":
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    if parsed.port is None:
        raise OAuthError(
            "Redirect URI %r must include an explicit port, e.g. "
            "http://127.0.0.1:8080/callback" % redirect_uri
        )
    return host, parsed.port, parsed.path or "/"


def parse_callback_path(
    path: str, expected_path: str, expected_state: str
) -> "tuple[Optional[str], Optional[str]]":
    """Returns (code, error_message); code is None until a valid callback."""
    parsed = urllib.parse.urlparse(path)
    if parsed.path != expected_path:
        return None, None  # favicon etc.: ignore, keep waiting
    params = urllib.parse.parse_qs(parsed.query)
    if params.get("error"):
        return None, "Spotify reported: %s" % params["error"][0]
    if params.get("state", [None])[0] != expected_state:
        return None, "State mismatch (possible CSRF); run auth again."
    code = params.get("code", [None])[0]
    if not code:
        return None, "Callback carried no authorization code."
    return code, None


def wait_for_callback(
    redirect_uri: str,
    expected_state: str,
    timeout_s: float = CALLBACK_TIMEOUT_S,
    ready: Optional[threading.Event] = None,
) -> str:
    host, port, expected_path = parse_redirect_uri(redirect_uri)
    result: Dict[str, Optional[str]] = {"code": None, "error": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib API name)
            code, error = parse_callback_path(self.path, expected_path, expected_state)
            if code is None and error is None:
                self.send_response(404)
                self.end_headers()
                return
            result["code"], result["error"] = code, error
            body = SUCCESS_HTML if code else ERROR_HTML % (error or "")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *args: Any) -> None:
            pass

    try:
        server = HTTPServer((host, port), Handler)
    except OSError as exc:
        raise OAuthError(
            "Cannot listen on %s:%d for the Spotify callback (%s). Is another "
            "program using the port? The redirect URI in your Spotify app "
            "settings, the config, and this listener must all match."
            % (host, port, exc)
        )

    server.timeout = 1.0
    if ready is not None:
        ready.set()
    deadline = time.monotonic() + timeout_s
    try:
        while result["code"] is None and result["error"] is None:
            if time.monotonic() >= deadline:
                raise OAuthError(
                    "Timed out after %d seconds waiting for the browser "
                    "authorization." % int(timeout_s)
                )
            server.handle_request()
    finally:
        server.server_close()

    if result["error"]:
        raise OAuthError(result["error"])
    return result["code"]  # type: ignore[return-value]


# -- top-level interactive flow --------------------------------------------


def authorize_interactive(
    client_id: str,
    redirect_uri: str,
    cache_path: str,
    session: Any,
    open_browser: Callable[[str], Any] = webbrowser.open,
    log: Callable[[str], None] = print,
    clock: Callable[[], float] = time.time,
    timeout_s: float = CALLBACK_TIMEOUT_S,
) -> Dict[str, Any]:
    """Run the one-time browser authorization and cache the tokens."""
    if not client_id:
        raise OAuthError(
            "SPOTIFY_CLIENT_ID is not set (environment or .env file)."
        )

    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    url = build_authorize_url(client_id, redirect_uri, state, challenge)

    # Bind the listener before opening the browser so the redirect cannot race
    # it; run it on a thread and open the browser once it is ready.
    ready = threading.Event()
    outcome: Dict[str, Any] = {}

    def _wait() -> None:
        try:
            outcome["code"] = wait_for_callback(
                redirect_uri, state, timeout_s=timeout_s, ready=ready
            )
        except OAuthError as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=_wait, daemon=True)
    thread.start()
    if not ready.wait(timeout=10):
        raise OAuthError("Callback listener failed to start.")

    log("Opening your browser to authorize with Spotify...")
    log("If nothing opens, paste this URL into a browser on this machine:")
    log("  %s" % url)
    open_browser(url)
    thread.join()

    if "error" in outcome:
        raise outcome["error"]

    tokens = exchange_code(
        session, client_id, outcome["code"], redirect_uri, verifier, clock=clock
    )
    tokens["client_id"] = client_id
    save_tokens(cache_path, tokens)
    log("Authorization complete. Tokens cached in %s" % cache_path)
    return tokens
