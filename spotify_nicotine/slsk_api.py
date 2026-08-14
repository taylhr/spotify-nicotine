"""Thin client for the Nicotine+ REST API plugin (api-nicotine-plus)."""

import time
from typing import Any, Callable, Dict, List, Optional

import requests


class SlskApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class SearchWatch:
    """Non-blocking maturity tracker for one running search.

    Soulseek searches have no completion signal; results stream in for a few
    seconds and then dry up. This encodes when to stop waiting:

    - never conclude before ``min_wait`` (results need time to propagate),
    - with results: conclude once the total has been stable for
      ``stable_polls`` consecutive polls,
    - without results: conclude at ``zero_result_wait`` (rare tracks arrive
      late, but not endlessly),
    - always conclude at ``timeout_s`` or ``max_results``.

    Feed one observed total per poll via record_poll(); it returns True when
    collection should conclude. Used by the blocking collect_results() and by
    the orchestrator's concurrent search scheduler.
    """

    def __init__(
        self,
        started_at: float,
        timeout_s: float,
        stable_polls: int = 2,
        min_wait: float = 4.0,
        zero_result_wait: float = 8.0,
        enough_results: int = 150,
        max_results: int = 2000,
    ):
        self.started_at = started_at
        self.timeout_s = timeout_s
        self.stable_polls = stable_polls
        self.min_wait = min_wait
        self.zero_result_wait = zero_result_wait
        self.enough_results = enough_results
        self.max_results = max_results
        self.last_total = -1
        self.stable_count = 0

    def record_poll(self, total: int, now: float) -> bool:
        if total == self.last_total:
            self.stable_count += 1
        else:
            self.stable_count = 0
            self.last_total = total

        elapsed = now - self.started_at
        if elapsed >= self.timeout_s:
            return True
        if total >= self.max_results:
            return True
        if total <= 0:
            return elapsed >= self.zero_result_wait
        if total >= self.enough_results and elapsed >= self.min_wait:
            # Popular searches keep trickling results for the whole window;
            # waiting for a stable total would burn the full timeout for the
            # easiest tracks. This many results is plenty to rank.
            return True
        return self.stable_count >= self.stable_polls and elapsed >= self.min_wait


class SlskClient:
    def __init__(
        self,
        base_url: str,
        api_token: Optional[str] = None,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self.session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock

    # -- plumbing ---------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {}
        if self.api_token:
            headers["X-API-Token"] = self.api_token
        return headers

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        retry_504: bool = True,
    ) -> Dict[str, Any]:
        url = self.base_url + path
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SlskApiError(
                0,
                "Cannot reach the Nicotine+ API at %s (%s). Is Nicotine+ running "
                "with the api-nicotine-plus plugin enabled?" % (self.base_url, exc),
            )

        # The plugin proxies work onto the Nicotine+ main thread; a busy GUI can
        # time out one call without anything being wrong. Retry once.
        if response.status_code == 504 and retry_504:
            self._sleep(1.0)
            return self._request(method, path, params, json_body, retry_504=False)

        if response.status_code != 200:
            try:
                message = response.json().get("error", response.text)
            except ValueError:
                message = response.text
            raise SlskApiError(response.status_code, message)
        return response.json()

    # -- endpoints --------------------------------------------------------

    def health(self) -> bool:
        try:
            return self._request("GET", "/health").get("status") == "ok"
        except SlskApiError:
            return False

    def status(self) -> Dict[str, Any]:
        return self._request("GET", "/status")

    def start_search(self, query: str) -> int:
        payload = {"query": query, "mode": "global", "switch_page": False}
        return int(self._request("POST", "/search", json_body=payload)["token"])

    def get_results(
        self, token: int, offset: int = 0, limit: int = 1000
    ) -> Dict[str, Any]:
        return self._request(
            "GET",
            "/search/results",
            params={"token": token, "offset": offset, "limit": limit},
        )

    def collect_results(
        self,
        token: int,
        timeout_s: float,
        poll_interval: float = 1.0,
        stable_polls: int = 2,
        min_wait: float = 4.0,
        zero_result_wait: float = 8.0,
        max_results: int = 2000,
    ) -> List[Dict[str, Any]]:
        """Block until the search matures (see SearchWatch), then fetch all
        results. Thin and empty searches conclude fast instead of burning the
        full timeout."""
        watch = SearchWatch(
            self._clock(),
            timeout_s,
            stable_polls=stable_polls,
            min_wait=min_wait,
            zero_result_wait=zero_result_wait,
            max_results=max_results,
        )
        while True:
            self._sleep(poll_interval)
            total = int(self.get_results(token, offset=0, limit=1).get("total", 0))
            if watch.record_poll(total, self._clock()):
                break

        return self.fetch_results(token, min(max(watch.last_total, 0), max_results))

    def fetch_results(self, token: int, upto: int) -> List[Dict[str, Any]]:
        """Fetch up to `upto` cached results for a token, paginating."""
        items: List[Dict[str, Any]] = []
        offset = 0
        while offset < upto:
            page = self.get_results(token, offset=offset, limit=1000)
            page_items = page.get("items", [])
            if not page_items:
                break
            items.extend(page_items)
            offset += len(page_items)
        return items[:upto]

    def enqueue(
        self,
        username: str,
        virtual_path: str,
        size: int,
        file_attributes: Optional[Dict[str, Any]] = None,
        folder_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "username": username,
            "virtual_path": virtual_path,
            "size": size,
            "file_attributes": file_attributes or {},
            "bypass_filter": False,
        }
        if folder_path:
            payload["folder_path"] = folder_path
        return self._request("POST", "/downloads/enqueue", json_body=payload)

    def downloads(self) -> List[Dict[str, Any]]:
        # active_only=false is essential: the default hides Finished/failed
        # transfers, which the monitor and reconciler must see.
        data = self._request("GET", "/downloads", params={"active_only": "false"})
        return data.get("items", [])
