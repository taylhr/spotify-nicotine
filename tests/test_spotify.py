import json

import pytest

from spotify_nicotine import oauth
from spotify_nicotine.spotify import (
    API_BASE,
    ClientCredentialsProvider,
    SpotifyAuthError,
    SpotifyClient,
    SpotifyError,
    SpotifyForbiddenError,
    TOKEN_URL,
    UserTokenProvider,
    fetch_playlist,
)

from tests.fakes import FakeResponse, FakeSession, TickClock


def token_response(token="tok1", expires_in=3600):
    return FakeResponse(200, {"access_token": token, "expires_in": expires_in})


def make_client(handler):
    tick = TickClock()
    session = FakeSession(handler)
    provider = ClientCredentialsProvider(
        "cid", "csecret", session=session, clock=tick.clock
    )
    client = SpotifyClient(provider, session=session, sleep=tick.sleep)
    return client, session, tick


def make_track_obj(track_id, name, **overrides):
    track = {
        "id": track_id,
        "type": "track",
        "is_local": False,
        "name": name,
        "artists": [{"name": "Eagles"}],
        "album": {"name": "Hotel California"},
        "duration_ms": 391_000,
        "external_ids": {"isrc": "USEE10001993"},
    }
    track.update(overrides)
    return track


def make_playlist_item(track_id, name, **overrides):
    """Feb 2026 API shape: playlist entries carry the track under 'item'."""
    return {"item": make_track_obj(track_id, name, **overrides)}


def make_legacy_playlist_item(track_id, name, **overrides):
    """Pre-2026 shape: entries carry the track under 'track'."""
    return {"track": make_track_obj(track_id, name, **overrides)}


class TestClientCredentials:
    def test_missing_credentials_rejected(self):
        with pytest.raises(SpotifyAuthError):
            ClientCredentialsProvider("", "")

    def test_token_fetched_once_and_reused(self):
        def handler(method, url, kwargs):
            if url == TOKEN_URL:
                return token_response()
            return FakeResponse(200, {"ok": True})

        client, session, _ = make_client(handler)
        client._get(API_BASE + "/x")
        client._get(API_BASE + "/y")
        token_calls = [c for c in session.calls if c[1] == TOKEN_URL]
        assert len(token_calls) == 1
        api_call = [c for c in session.calls if c[1].endswith("/x")][0]
        assert api_call[2]["headers"]["Authorization"] == "Bearer tok1"

    def test_expired_token_refreshed(self):
        tokens = iter(["tok1", "tok2"])

        def handler(method, url, kwargs):
            if url == TOKEN_URL:
                return token_response(next(tokens), expires_in=100)
            return FakeResponse(200, {"ok": True})

        client, session, tick = make_client(handler)
        client._get(API_BASE + "/x")
        tick.now += 3600
        client._get(API_BASE + "/y")
        token_calls = [c for c in session.calls if c[1] == TOKEN_URL]
        assert len(token_calls) == 2

    def test_401_refreshes_once_and_retries(self):
        state = {"api_calls": 0}
        tokens = iter(["stale", "fresh"])

        def handler(method, url, kwargs):
            if url == TOKEN_URL:
                return token_response(next(tokens))
            state["api_calls"] += 1
            auth = kwargs["headers"]["Authorization"]
            if auth == "Bearer stale":
                return FakeResponse(401, {"error": {"status": 401}})
            return FakeResponse(200, {"ok": True})

        client, _, _ = make_client(handler)
        assert client._get(API_BASE + "/x") == {"ok": True}
        assert state["api_calls"] == 2

    def test_bad_credentials_error(self):
        def handler(method, url, kwargs):
            return FakeResponse(400, {"error": "invalid_client"}, text="invalid_client")

        client, _, _ = make_client(handler)
        with pytest.raises(SpotifyAuthError, match="client id"):
            client._get(API_BASE + "/x")


class TestUserTokenProvider:
    def make_cache(self, tmp_path, expires_at, refresh="rt", access="cached"):
        path = str(tmp_path / "tokens.json")
        oauth.save_tokens(
            path,
            {
                "access_token": access,
                "refresh_token": refresh,
                "expires_at": expires_at,
                "client_id": "cached_cid",
            },
        )
        return path

    def test_fresh_cached_token_used_without_refresh(self, tmp_path):
        path = self.make_cache(tmp_path, expires_at=10_000)
        session = FakeSession(lambda m, u, k: FakeResponse(500, {}))
        provider = UserTokenProvider("cid", path, session=session, clock=lambda: 100.0)
        assert provider.get_token() == "cached"
        assert session.calls == []

    def test_expired_token_refreshed_and_rotation_saved(self, tmp_path):
        path = self.make_cache(tmp_path, expires_at=50)
        session = FakeSession(
            lambda m, u, k: FakeResponse(
                200,
                {"access_token": "new", "refresh_token": "rotated", "expires_in": 3600},
            )
        )
        provider = UserTokenProvider("cid", path, session=session, clock=lambda: 100.0)
        assert provider.get_token() == "new"
        assert session.calls[0][2]["data"]["refresh_token"] == "rt"
        cached = oauth.load_tokens(path)
        assert cached["refresh_token"] == "rotated"

    def test_client_id_falls_back_to_cache(self, tmp_path):
        path = self.make_cache(tmp_path, expires_at=50)
        session = FakeSession(
            lambda m, u, k: FakeResponse(200, {"access_token": "new", "expires_in": 60})
        )
        provider = UserTokenProvider("", path, session=session, clock=lambda: 100.0)
        provider.get_token()
        assert session.calls[0][2]["data"]["client_id"] == "cached_cid"

    def test_no_cache_instructs_auth(self, tmp_path):
        provider = UserTokenProvider("cid", str(tmp_path / "missing.json"))
        with pytest.raises(SpotifyAuthError, match="auth"):
            provider.get_token()

    def test_failed_refresh_instructs_reauth(self, tmp_path):
        path = self.make_cache(tmp_path, expires_at=50)
        session = FakeSession(
            lambda m, u, k: FakeResponse(400, {"error": "invalid_grant"}, text="revoked")
        )
        provider = UserTokenProvider("cid", path, session=session, clock=lambda: 100.0)
        with pytest.raises(SpotifyAuthError, match="auth"):
            provider.get_token()


class TestRetriesAndErrors:
    def test_429_honors_retry_after(self):
        responses = iter(
            [
                FakeResponse(429, {"e": 1}, headers={"Retry-After": "7"}),
                FakeResponse(200, {"ok": True}),
            ]
        )

        def handler(method, url, kwargs):
            if url == TOKEN_URL:
                return token_response()
            return next(responses)

        client, _, tick = make_client(handler)
        assert client._get(API_BASE + "/x") == {"ok": True}
        assert 7.0 in tick.sleeps

    def test_403_raises_forbidden_with_auth_hint_and_detail(self):
        def handler(method, url, kwargs):
            if url == TOKEN_URL:
                return token_response()
            return FakeResponse(
                403, {"error": {"status": 403, "message": "Some upstream detail"}}
            )

        client, _, _ = make_client(handler)
        with pytest.raises(SpotifyForbiddenError) as exc:
            client._get(API_BASE + "/playlists/xyz")
        assert "spotify-nicotine auth" in str(exc.value)
        assert "Some upstream detail" in str(exc.value)

    def test_403_user_not_registered_gets_user_management_hint(self):
        def handler(method, url, kwargs):
            if url == TOKEN_URL:
                return token_response()
            return FakeResponse(
                403,
                {
                    "error": {
                        "status": 403,
                        "message": "User not registered in the Developer Dashboard",
                    }
                },
            )

        client, _, _ = make_client(handler)
        with pytest.raises(SpotifyForbiddenError) as exc:
            client._get(API_BASE + "/me")
        message = str(exc.value)
        assert "User Management" in message
        assert "accounts.spotify.com" in message

    def test_get_me(self):
        def handler(method, url, kwargs):
            if url == TOKEN_URL:
                return token_response()
            assert url.endswith("/me")
            return FakeResponse(200, {"display_name": "Taylor", "id": "rhyx"})

        client, _, _ = make_client(handler)
        assert client.get_me() == {"display_name": "Taylor", "id": "rhyx"}

    def test_404_mentions_editorial_restriction(self):
        def handler(method, url, kwargs):
            if url == TOKEN_URL:
                return token_response()
            return FakeResponse(404, {"error": {"status": 404}})

        client, _, _ = make_client(handler)
        with pytest.raises(SpotifyError, match="editorial"):
            client._get(API_BASE + "/playlists/xyz")


class TestPlaylistFetch:
    def test_pagination_follows_next_on_items_endpoint(self):
        page2_url = API_BASE + "/playlists/p1/items?offset=100"

        def handler(method, url, kwargs):
            if url == TOKEN_URL:
                return token_response()
            if url.endswith("/playlists/p1/items") and (kwargs.get("params") or {}).get("limit"):
                return FakeResponse(
                    200,
                    {
                        "items": [make_playlist_item("t1", "Song One")],
                        "next": page2_url,
                    },
                )
            if url == page2_url:
                return FakeResponse(
                    200,
                    {"items": [make_playlist_item("t2", "Song Two")], "next": None},
                )
            raise AssertionError("unexpected url %s" % url)

        client, _, _ = make_client(handler)
        items = list(client.iter_playlist_items("p1"))
        assert [i["item"]["id"] for i in items] == ["t1", "t2"]

    def test_falls_back_to_legacy_tracks_endpoint(self):
        """Grandfathered apps may still serve the pre-2026 /tracks endpoint."""

        def handler(method, url, kwargs):
            if url == TOKEN_URL:
                return token_response()
            if url.endswith("/playlists/p1/items"):
                return FakeResponse(404, {"error": {"status": 404}})
            if url.endswith("/playlists/p1/tracks"):
                return FakeResponse(
                    200,
                    {
                        "items": [make_legacy_playlist_item("t1", "Song One")],
                        "next": None,
                    },
                )
            raise AssertionError("unexpected url %s" % url)

        client, _, _ = make_client(handler)
        items = list(client.iter_playlist_items("p1"))
        assert [i["track"]["id"] for i in items] == ["t1"]

    def test_both_endpoints_403_gives_ownership_hint(self):
        def handler(method, url, kwargs):
            if url == TOKEN_URL:
                return token_response()
            return FakeResponse(403, {"error": {"status": 403, "message": "forbidden"}})

        client, _, _ = make_client(handler)
        with pytest.raises(SpotifyForbiddenError, match="owns or collaborates"):
            list(client.iter_playlist_items("p1"))

    def test_meta_reads_new_items_container(self):
        def handler(method, url, kwargs):
            if url == TOKEN_URL:
                return token_response()
            return FakeResponse(
                200,
                {"id": "p1", "name": "Mix", "items": {"total": 7}},
            )

        client, _, _ = make_client(handler)
        assert client.get_playlist_meta("p1")["total_tracks"] == 7

    def test_fetch_playlist_skips_and_collapses(self):
        items = [
            make_playlist_item("t1", "Song One"),
            {"item": None},
            {"item": {"type": "episode", "name": "Podcast Ep", "id": "e1"}},
            {
                "item": {
                    "type": "track",
                    "is_local": True,
                    "id": None,
                    "name": "My Local Rip",
                }
            },
            make_playlist_item("t1", "Song One"),  # duplicate occurrence
            make_playlist_item("t2", "Song Two"),
        ]

        def handler(method, url, kwargs):
            if url == TOKEN_URL:
                return token_response()
            if url.endswith("/playlists/p1"):
                return FakeResponse(
                    200,
                    {
                        "id": "p1",
                        "name": "Mix",
                        "owner": {"display_name": "taylor"},
                        "snapshot_id": "snapA",
                        "external_urls": {"spotify": "https://open.spotify.com/playlist/p1"},
                        "items": {"total": 6},
                    },
                )
            if url.endswith("/playlists/p1/items"):
                return FakeResponse(200, {"items": items, "next": None})
            raise AssertionError("unexpected url %s" % url)

        client, _, _ = make_client(handler)
        meta, tracks, skipped = fetch_playlist(client, "p1")

        assert meta["name"] == "Mix" and meta["snapshot_id"] == "snapA"
        assert meta["total_tracks"] == 6
        assert meta["fetched_at"]
        assert [t.id for t in tracks] == ["t1", "t2"]
        assert tracks[0].positions == [0, 4]
        assert tracks[0].isrc == "USEE10001993"
        reasons = [s["reason"] for s in skipped]
        assert reasons == ["missing_track", "episode", "local_track"]

    def test_user_provider_reads_playlist(self, tmp_path):
        """The whole client works on top of cached user tokens."""
        cache = str(tmp_path / "tokens.json")
        oauth.save_tokens(
            cache,
            {"access_token": "ut", "refresh_token": "rt", "expires_at": 10_000},
        )

        def handler(method, url, kwargs):
            assert kwargs["headers"]["Authorization"] == "Bearer ut"
            return FakeResponse(200, {"items": [], "next": None})

        session = FakeSession(handler)
        provider = UserTokenProvider("cid", cache, session=session, clock=lambda: 0.0)
        client = SpotifyClient(provider, session=session)
        assert list(client.iter_playlist_items("p1")) == []
