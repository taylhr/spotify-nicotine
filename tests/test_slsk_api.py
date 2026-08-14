import pytest

from spotify_nicotine.slsk_api import SearchWatch, SlskApiError, SlskClient

from tests.fakes import FakeResponse, FakeSession, TickClock


def make_client(handler, api_token=None):
    tick = TickClock()
    session = FakeSession(handler)
    client = SlskClient(
        "http://127.0.0.1:12339",
        api_token=api_token,
        session=session,
        sleep=tick.sleep,
        clock=tick.clock,
    )
    return client, session, tick


class TestPlumbing:
    def test_api_token_header_sent(self):
        client, session, _ = make_client(
            lambda m, u, k: FakeResponse(200, {"status": "ok"}), api_token="sekrit"
        )
        client.health()
        assert session.calls[0][2]["headers"]["X-API-Token"] == "sekrit"

    def test_no_auth_header_without_token(self):
        client, session, _ = make_client(
            lambda m, u, k: FakeResponse(200, {"status": "ok"})
        )
        client.health()
        assert "X-API-Token" not in session.calls[0][2]["headers"]

    def test_504_retried_once_then_succeeds(self):
        responses = [FakeResponse(504, {"error": "timeout"}), FakeResponse(200, {"a": 1})]
        client, session, tick = make_client(lambda m, u, k: responses.pop(0))
        assert client.status() == {"a": 1}
        assert len(session.calls) == 2
        assert tick.sleeps == [1.0]

    def test_504_twice_raises(self):
        client, _, _ = make_client(lambda m, u, k: FakeResponse(504, {"error": "t"}))
        with pytest.raises(SlskApiError) as exc:
            client.status()
        assert exc.value.status == 504

    def test_400_error_message_surfaced(self):
        client, _, _ = make_client(
            lambda m, u, k: FakeResponse(400, {"error": "query is required"})
        )
        with pytest.raises(SlskApiError, match="query is required"):
            client.start_search("x")

    def test_health_false_on_error(self):
        client, _, _ = make_client(lambda m, u, k: FakeResponse(500, {"error": "boom"}))
        assert client.health() is False


class TestSearch:
    def test_start_search_disables_switch_page(self):
        client, session, _ = make_client(
            lambda m, u, k: FakeResponse(200, {"ok": True, "token": 42})
        )
        assert client.start_search("eagles hotel california") == 42
        method, url, kwargs = session.calls[0]
        assert method == "POST" and url.endswith("/search")
        assert kwargs["json"]["switch_page"] is False
        assert kwargs["json"]["mode"] == "global"

    def test_collect_results_early_stop_when_stable(self):
        totals = iter([10, 40, 40, 40, 40, 40, 40])
        items = [{"file_path": "f%d.mp3" % i} for i in range(40)]

        def handler(method, url, kwargs):
            params = kwargs.get("params") or {}
            if params.get("limit") == 1:
                return FakeResponse(200, {"total": next(totals), "items": []})
            offset = params.get("offset", 0)
            return FakeResponse(
                200, {"total": 40, "items": items[offset : offset + 1000]}
            )

        client, session, tick = make_client(handler)
        collected = client.collect_results(token=42, timeout_s=30)
        assert len(collected) == 40
        # stable totals conclude shortly after min_wait, nowhere near timeout
        assert tick.now <= 6

    def test_collect_results_zero_results_concludes_early(self):
        def handler(method, url, kwargs):
            return FakeResponse(200, {"total": 0, "items": []})

        client, _, tick = make_client(handler)
        collected = client.collect_results(token=42, timeout_s=20)
        assert collected == []
        # empty searches conclude at zero_result_wait (8s), not timeout (20s)
        assert 8 <= tick.now < 12

    def test_collect_results_times_out_while_growing(self):
        state = {"total": 0}

        def handler(method, url, kwargs):
            params = kwargs.get("params") or {}
            if params.get("limit") == 1:
                state["total"] += 5
                return FakeResponse(200, {"total": state["total"], "items": []})
            offset = params.get("offset", 0)
            rows = [{"file_path": "f%d.mp3" % i} for i in range(state["total"])]
            return FakeResponse(200, {"total": state["total"], "items": rows[offset:]})

        client, _, tick = make_client(handler)
        collected = client.collect_results(token=42, timeout_s=10)
        assert tick.now >= 10
        assert len(collected) > 0

    def test_collect_results_paginates(self):
        items = [{"file_path": "f%d.mp3" % i} for i in range(1500)]

        def handler(method, url, kwargs):
            params = kwargs.get("params") or {}
            limit = params.get("limit")
            if limit == 1:
                return FakeResponse(200, {"total": 1500, "items": []})
            offset = params.get("offset", 0)
            return FakeResponse(
                200, {"total": 1500, "items": items[offset : offset + limit]}
            )

        client, _, _ = make_client(handler)
        collected = client.collect_results(token=1, timeout_s=30)
        assert len(collected) == 1500

    def test_collect_results_respects_max_results(self):
        items = [{"file_path": "f%d.mp3" % i} for i in range(3000)]

        def handler(method, url, kwargs):
            params = kwargs.get("params") or {}
            limit = params.get("limit")
            if limit == 1:
                return FakeResponse(200, {"total": 3000, "items": []})
            offset = params.get("offset", 0)
            return FakeResponse(
                200, {"total": 3000, "items": items[offset : offset + limit]}
            )

        client, _, _ = make_client(handler)
        collected = client.collect_results(token=1, timeout_s=8, max_results=2000)
        assert len(collected) == 2000


class TestSearchWatch:
    def test_never_concludes_before_min_wait(self):
        watch = SearchWatch(0.0, timeout_s=20)
        assert watch.record_poll(50, 1.0) is False
        assert watch.record_poll(50, 2.0) is False
        assert watch.record_poll(50, 3.0) is False

    def test_concludes_when_stable_after_min_wait(self):
        watch = SearchWatch(0.0, timeout_s=20)
        watch.record_poll(50, 1.0)
        watch.record_poll(50, 2.0)
        assert watch.record_poll(50, 4.5) is True

    def test_growth_resets_stability(self):
        watch = SearchWatch(0.0, timeout_s=20)
        watch.record_poll(10, 4.0)
        watch.record_poll(10, 5.0)
        assert watch.record_poll(30, 6.0) is False  # growth: not stable
        assert watch.record_poll(30, 7.0) is False  # stable_count 1 of 2
        assert watch.record_poll(30, 8.0) is True

    def test_zero_results_concludes_at_zero_wait(self):
        watch = SearchWatch(0.0, timeout_s=20)
        assert watch.record_poll(0, 7.0) is False
        assert watch.record_poll(0, 8.0) is True

    def test_timeout_always_concludes(self):
        watch = SearchWatch(0.0, timeout_s=10)
        for second in range(1, 10):
            assert watch.record_poll(second * 5, float(second)) is False
        assert watch.record_poll(999, 10.0) is True

    def test_max_results_concludes(self):
        watch = SearchWatch(0.0, timeout_s=20, max_results=100)
        assert watch.record_poll(150, 1.0) is True

    def test_enough_results_concludes_even_while_growing(self):
        watch = SearchWatch(0.0, timeout_s=20, enough_results=150)
        # growing totals never stabilize, but past enough_results we conclude
        assert watch.record_poll(80, 2.0) is False
        assert watch.record_poll(120, 3.0) is False
        assert watch.record_poll(160, 5.0) is True

    def test_enough_results_still_respects_min_wait(self):
        watch = SearchWatch(0.0, timeout_s=20, enough_results=150)
        assert watch.record_poll(500, 1.0) is False  # before min_wait
        assert watch.record_poll(600, 4.0) is True


class TestTransfers:
    def test_enqueue_payload_shape(self):
        client, session, _ = make_client(
            lambda m, u, k: FakeResponse(
                200, {"ok": True, "queued": True, "duplicate": False, "status": "Queued"}
            )
        )
        result = client.enqueue(
            "peer1",
            "Music\\Eagles\\05 - Hotel California.mp3",
            15_662_354,
            file_attributes={"0": 320, "1": 391},
        )
        assert result["queued"] is True
        method, url, kwargs = session.calls[0]
        assert url.endswith("/downloads/enqueue")
        body = kwargs["json"]
        assert body["username"] == "peer1"
        assert body["virtual_path"] == "Music\\Eagles\\05 - Hotel California.mp3"
        assert body["size"] == 15_662_354
        assert body["file_attributes"] == {"0": 320, "1": 391}
        assert body["bypass_filter"] is False
        assert "folder_path" not in body

    def test_enqueue_with_dest_dir(self):
        client, session, _ = make_client(
            lambda m, u, k: FakeResponse(200, {"ok": True, "queued": True})
        )
        client.enqueue("u", "p.mp3", 1, folder_path="/music/My Playlist")
        assert session.calls[0][2]["json"]["folder_path"] == "/music/My Playlist"

    def test_downloads_forces_active_only_false(self):
        client, session, _ = make_client(
            lambda m, u, k: FakeResponse(
                200, {"direction": "downloads", "count": 1, "items": [{"status": "Queued"}]}
            )
        )
        items = client.downloads()
        assert items == [{"status": "Queued"}]
        assert session.calls[0][2]["params"]["active_only"] == "false"
