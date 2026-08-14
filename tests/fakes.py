"""Hand-rolled stand-ins for requests.Session and wall-clock time."""

from typing import Any, Callable, Dict, List, Optional, Tuple

from spotify_nicotine.slsk_api import SlskApiError


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        text: str = "",
    ):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self.text = text or ("" if json_data is None else str(json_data))

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Mimics requests.Session.request via a user-supplied handler.

    handler(method, url, kwargs) -> FakeResponse. Every call is recorded as
    (method, url, kwargs) in .calls.
    """

    def __init__(self, handler: Callable[[str, str, Dict[str, Any]], FakeResponse]):
        self.handler = handler
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        return self.handler(method, url, kwargs)


class FakeSlsk:
    """Stands in for SlskClient in orchestrator/resolve/cli tests."""

    def __init__(self, results: Optional[Dict[str, List[dict]]] = None):
        # results: query -> items; "default" applies to any other query
        self.results = results or {}
        self.searches: List[str] = []
        self.enqueues: List[Dict[str, Any]] = []
        self.transfers: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._token = 0
        self._last_query = ""
        self._token_query: Dict[int, str] = {}
        self.clock: Optional[Callable[[], float]] = None  # wire a TickClock
        self.search_times: List[float] = []  # dispatch times when clock wired
        self.evict_tokens = False  # simulate the plugin's LRU cache eviction
        self.grow_totals = False  # totals rise every poll (popular searches)
        self._poll_counts: Dict[int, int] = {}

    def health(self) -> bool:
        return True

    def status(self) -> Dict[str, Any]:
        return {"connected": True}

    def start_search(self, query: str) -> int:
        self.searches.append(query)
        self._last_query = query
        self._token += 1
        self._token_query[self._token] = query
        if self.clock is not None:
            self.search_times.append(self.clock())
        return self._token

    def _items_for_token(self, token: int) -> List[dict]:
        query = self._token_query.get(token, "")
        if query in self.results:
            return self.results[query]
        return self.results.get("default", [])

    def get_results(self, token: int, offset: int = 0, limit: int = 1000) -> Dict[str, Any]:
        if self.evict_tokens:
            raise SlskApiError(400, "Unknown search token: %d" % token)
        items = self._items_for_token(token)
        total = len(items)
        if self.grow_totals:
            self._poll_counts[token] = self._poll_counts.get(token, 0) + 1
            total += self._poll_counts[token]  # never stabilizes
        return {"total": total, "items": items[offset : offset + limit]}

    def fetch_results(self, token: int, upto: int) -> List[dict]:
        if self.evict_tokens:
            raise SlskApiError(400, "Unknown search token: %d" % token)
        return self._items_for_token(token)[:upto]

    def collect_results(self, token: int, timeout_s: float, **kwargs) -> List[dict]:
        return self._items_for_token(token)

    def enqueue(self, username, virtual_path, size, file_attributes=None, folder_path=None):
        self.enqueues.append(
            {
                "username": username,
                "virtual_path": virtual_path,
                "size": size,
                "file_attributes": file_attributes,
                "folder_path": folder_path,
            }
        )
        key = (username, virtual_path)
        duplicate = key in self.transfers
        self.transfers.setdefault(
            key,
            {
                "username": username,
                "virtual_path": virtual_path,
                "status": "Queued",
                "progress_pct": 0,
            },
        )
        return {"ok": True, "queued": True, "duplicate": duplicate, "status": "Queued"}

    def downloads(self) -> List[Dict[str, Any]]:
        return list(self.transfers.values())

    # test helpers
    def set_status(self, username: str, virtual_path: str, status: str) -> None:
        self.transfers[(username, virtual_path)]["status"] = status

    def drop(self, username: str, virtual_path: str) -> None:
        del self.transfers[(username, virtual_path)]


class TickClock:
    """Fake time: sleep() advances the clock instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now
