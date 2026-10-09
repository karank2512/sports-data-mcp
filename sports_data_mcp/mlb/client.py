"""Thin cached client for the public MLB Stats API.

Pure stdlib + httpx. Every ``GET`` is cached on disk as JSON, keyed by the
sha256 of the full request URL, so repeated calls (and offline runs) never
touch the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://statsapi.mlb.com/api/v1"
DEFAULT_CACHE_DIR = Path("~/.cache/sports-data-mcp")
DEFAULT_TTL_S = 3600
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_BACKOFF_S = 1.0
MAX_BACKOFF_S = 10.0

Params = Mapping[str, Any] | None


class APIError(Exception):
    """Raised when the MLB Stats API returns an error or cannot be reached.

    ``status`` is the HTTP status code, or ``None`` when no response was
    received (network failure, timeout, offline cache miss).
    """

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"APIError(status={self.status!r}, message={self.message!r})"


class MLBClient:
    """Cached GET client for ``https://statsapi.mlb.com/api/v1``.

    Args:
        base_url: API root; ``path`` arguments to :meth:`get` are appended.
        cache_dir: Directory for cached responses. Created on first write.
        ttl_s: Default cache lifetime in seconds. ``0`` disables cache hits.
        offline: When true, serve only from cache and never open a socket.
        timeout_s: httpx timeout for every request.
        transport: Optional ``httpx.BaseTransport`` (tests use ``MockTransport``).
        clock: Time source, defaults to :func:`time.time`.
        sleep: Backoff sleeper, defaults to :func:`time.sleep`.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        cache_dir: str | os.PathLike[str] | None = None,
        ttl_s: float = DEFAULT_TTL_S,
        *,
        offline: bool = False,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        backoff_s: float = DEFAULT_BACKOFF_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache_dir = Path(cache_dir if cache_dir is not None else DEFAULT_CACHE_DIR).expanduser()
        self.ttl_s = ttl_s
        self.offline = offline
        self.timeout_s = timeout_s
        self.backoff_s = backoff_s
        self._clock = clock
        self._sleep = sleep
        self._http = httpx.Client(
            timeout=timeout_s,
            transport=transport,
            headers={"User-Agent": "sports-data-mcp/0.1"},
            follow_redirects=True,
        )

    # -- public API ---------------------------------------------------------

    def url_for(self, path: str, params: Params = None) -> str:
        """Return the exact URL that ``get(path, params)`` would request."""
        url = httpx.URL(f"{self.base_url}/{path.lstrip('/')}")
        if params:
            url = url.copy_merge_params({k: v for k, v in params.items() if v is not None})
        return str(url)

    def cache_path(self, url: str) -> Path:
        """Path of the cache file for ``url`` (sha256 of the full URL)."""
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def get(
        self,
        path: str,
        params: Params = None,
        *,
        ttl_s: float | None = None,
        offline: bool | None = None,
    ) -> Any:
        """GET ``path`` and return the decoded JSON body."""
        data, _cached = self.fetch(path, params, ttl_s=ttl_s, offline=offline)
        return data

    def fetch(
        self,
        path: str,
        params: Params = None,
        *,
        ttl_s: float | None = None,
        offline: bool | None = None,
    ) -> tuple[Any, bool]:
        """Like :meth:`get` but also report whether the result came from cache.

        Raises:
            APIError: on 4xx/5xx after retry, on transport failures after
                retry, or on a cache miss in offline mode.
        """
        ttl = self.ttl_s if ttl_s is None else ttl_s
        use_offline = self.offline if offline is None else offline
        url = self.url_for(path, params)

        entry = self._read_cache(url)
        if entry is not None:
            if use_offline:
                return entry["data"], True
            age = self._clock() - float(entry.get("fetched_at", 0))
            if age < ttl:
                return entry["data"], True

        if use_offline:
            raise APIError(None, f"offline: no cached response for {url}")

        data = self._request(url)
        self._write_cache(url, data)
        return data, False

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> MLBClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- network -----------------------------------------------------------

    def _request(self, url: str) -> Any:
        last_error: APIError | None = None
        for attempt in range(2):
            try:
                response = self._http.get(url)
            except httpx.HTTPError as exc:
                last_error = APIError(None, f"{type(exc).__name__}: {exc}")
                if attempt == 0:
                    self._sleep(self.backoff_s)
                    continue
                raise last_error from exc

            status = response.status_code
            if status == 429 or status >= 500:
                last_error = APIError(status, _error_message(response))
                if attempt == 0:
                    self._sleep(_retry_delay(response, self.backoff_s))
                    continue
                raise last_error
            if status >= 400:
                raise APIError(status, _error_message(response))
            try:
                return response.json()
            except ValueError as exc:
                raise APIError(status, f"invalid JSON from {url}: {exc}") from exc
        raise last_error if last_error is not None else APIError(None, "request failed")  # pragma: no cover

    # -- cache -------------------------------------------------------------

    def _read_cache(self, url: str) -> dict[str, Any] | None:
        path = self.cache_path(url)
        try:
            with path.open("r", encoding="utf-8") as fh:
                entry = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or "data" not in entry:
            return None
        return entry

    def _write_cache(self, url: str, data: Any) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_path(url)
        entry = {"url": url, "fetched_at": self._clock(), "data": data}
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=self.cache_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entry, fh, separators=(",", ":"))
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _error_message(response: httpx.Response) -> str:
    text = response.text.strip()
    if len(text) > 200:
        text = text[:200] + "..."
    reason = response.reason_phrase or "error"
    return f"HTTP {response.status_code} {reason} for {response.url}" + (f": {text}" if text else "")


def _retry_delay(response: httpx.Response, default: float) -> float:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(max(float(header), 0.0), MAX_BACKOFF_S)
        except ValueError:
            pass
    return default
