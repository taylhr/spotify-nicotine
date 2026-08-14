import base64
import hashlib
import json
import os
import socket
import stat
import threading
import urllib.parse
import urllib.request

import pytest

from spotify_nicotine import oauth

from tests.fakes import FakeResponse, FakeSession


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestPkce:
    def test_challenge_is_s256_of_verifier(self):
        verifier, challenge = oauth.generate_pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert challenge == expected
        assert 43 <= len(verifier) <= 128

    def test_authorize_url_params(self):
        url = oauth.build_authorize_url("cid", "http://127.0.0.1:9/cb", "st8", "chal")
        parsed = urllib.parse.urlparse(url)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        assert url.startswith(oauth.AUTHORIZE_URL)
        assert params["client_id"] == "cid"
        assert params["response_type"] == "code"
        assert params["redirect_uri"] == "http://127.0.0.1:9/cb"
        assert params["state"] == "st8"
        assert params["code_challenge_method"] == "S256"
        assert params["code_challenge"] == "chal"
        assert "playlist-read-private" in params["scope"]


class TestTokenCache:
    def test_roundtrip_and_permissions(self, tmp_path):
        path = str(tmp_path / "tokens.json")
        oauth.save_tokens(path, {"refresh_token": "r", "access_token": "a"})
        assert oauth.load_tokens(path) == {"refresh_token": "r", "access_token": "a"}
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_load_missing_or_invalid(self, tmp_path):
        assert oauth.load_tokens(str(tmp_path / "nope.json")) is None
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert oauth.load_tokens(str(bad)) is None
        no_refresh = tmp_path / "norefresh.json"
        no_refresh.write_text(json.dumps({"access_token": "a"}))
        assert oauth.load_tokens(str(no_refresh)) is None


class TestTokenEndpoint:
    def test_exchange_code_payload(self):
        session = FakeSession(
            lambda m, u, k: FakeResponse(
                200,
                {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
            )
        )
        tokens = oauth.exchange_code(
            session, "cid", "thecode", "http://127.0.0.1:9/cb", "verif",
            clock=lambda: 1000.0,
        )
        method, url, kwargs = session.calls[0]
        assert (method, url) == ("POST", oauth.TOKEN_URL)
        assert kwargs["data"] == {
            "grant_type": "authorization_code",
            "code": "thecode",
            "redirect_uri": "http://127.0.0.1:9/cb",
            "client_id": "cid",
            "code_verifier": "verif",
        }
        assert tokens["access_token"] == "at"
        assert tokens["expires_at"] == 1000.0 + 3600

    def test_refresh_keeps_old_refresh_token_when_not_rotated(self):
        session = FakeSession(
            lambda m, u, k: FakeResponse(200, {"access_token": "new", "expires_in": 60})
        )
        merged = oauth.refresh_tokens(
            session, "cid", {"refresh_token": "old", "access_token": "stale"},
            clock=lambda: 5.0,
        )
        assert merged["access_token"] == "new"
        assert merged["refresh_token"] == "old"
        assert session.calls[0][2]["data"]["grant_type"] == "refresh_token"

    def test_refresh_adopts_rotated_refresh_token(self):
        session = FakeSession(
            lambda m, u, k: FakeResponse(
                200, {"access_token": "new", "refresh_token": "rotated", "expires_in": 60}
            )
        )
        merged = oauth.refresh_tokens(session, "cid", {"refresh_token": "old"})
        assert merged["refresh_token"] == "rotated"

    def test_error_raises(self):
        session = FakeSession(
            lambda m, u, k: FakeResponse(400, {"error": "invalid_grant"}, text="invalid_grant")
        )
        with pytest.raises(oauth.OAuthError, match="400"):
            oauth.refresh_tokens(session, "cid", {"refresh_token": "r"})


class TestCallbackParsing:
    def test_valid_callback(self):
        code, err = oauth.parse_callback_path("/cb?code=abc&state=s1", "/cb", "s1")
        assert code == "abc" and err is None

    def test_wrong_path_ignored(self):
        assert oauth.parse_callback_path("/favicon.ico", "/cb", "s1") == (None, None)

    def test_state_mismatch(self):
        code, err = oauth.parse_callback_path("/cb?code=abc&state=EVIL", "/cb", "s1")
        assert code is None and "State mismatch" in err

    def test_user_denied(self):
        code, err = oauth.parse_callback_path("/cb?error=access_denied&state=s1", "/cb", "s1")
        assert code is None and "access_denied" in err

    def test_redirect_uri_must_have_port(self):
        with pytest.raises(oauth.OAuthError, match="port"):
            oauth.parse_redirect_uri("http://127.0.0.1/callback")


class TestInteractiveFlow:
    def test_full_flow_against_local_server(self, tmp_path):
        """End-to-end: browser stub hits the real loopback server, code is
        exchanged, tokens cached."""
        port = free_port()
        redirect = "http://127.0.0.1:%d/callback" % port
        cache = str(tmp_path / "tokens.json")

        def handler(method, url, kwargs):
            return FakeResponse(
                200,
                {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
            )

        session = FakeSession(handler)

        def fake_browser(url):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            state = params["state"][0]

            def hit():
                urllib.request.urlopen(
                    "%s?code=thecode&state=%s" % (redirect, state), timeout=5
                ).read()

            threading.Thread(target=hit, daemon=True).start()

        tokens = oauth.authorize_interactive(
            "cid",
            redirect,
            cache,
            session=session,
            open_browser=fake_browser,
            log=lambda msg: None,
            clock=lambda: 100.0,
            timeout_s=10,
        )
        assert tokens["access_token"] == "at"
        assert tokens["client_id"] == "cid"
        cached = oauth.load_tokens(cache)
        assert cached["refresh_token"] == "rt"
        assert cached["expires_at"] == 3700.0
        # the exchange used the code delivered via the callback
        assert session.calls[0][2]["data"]["code"] == "thecode"

    def test_missing_client_id(self, tmp_path):
        with pytest.raises(oauth.OAuthError, match="SPOTIFY_CLIENT_ID"):
            oauth.authorize_interactive(
                "", "http://127.0.0.1:1/cb", str(tmp_path / "t.json"),
                session=FakeSession(lambda m, u, k: FakeResponse(200, {})),
            )
