"""The one error shape, the client's kind mapping, offline mode and release metadata.

Every failing tool call must surface ``{"error": {"kind", "message",
"retry_after_s"}}`` in the MCP error text. The HTTP side is driven by
``httpx.MockTransport`` handlers that answer with the status under test;
nothing here touches the network (an autouse fixture in ``conftest.py``
makes any real transport fail).
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

import sports_data_mcp
from sports_data_mcp import server
from sports_data_mcp.errors import KINDS, SportsDataError, bad_input, from_api_error, parse_tool_error
from sports_data_mcp.mlb.client import USER_AGENT, APIError, MLBClient, kind_for_status

REPO_ROOT = Path(__file__).resolve().parent.parent
TEAMS = ("/teams", {"sportId": 1})
TEAMS_URL = "https://statsapi.mlb.com/api/v1/teams?sportId=1"

# Arguments that pass each tool's own validation, so the only failure left is the client's.
TOOL_CALLS: dict[str, dict[str, Any]] = {
    "mlb_ping": {},
    "mlb_search_player": {"name": "Aaron Judge"},
    "mlb_player_stats": {"player_id": 592450, "season": 2025},
    "mlb_team_schedule": {"team": 147, "start": "2025-07-01", "end": "2025-07-07"},
    "mlb_standings": {"season": 2025},
    "mlb_game_boxscore": {"game_pk": 777300},
    "mlb_live_linescore": {"game_pk": 777300},
}


def call_tool(name: str, arguments: dict[str, Any]):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def tool_error(name: str, arguments: dict[str, Any]) -> tuple[ToolError, dict[str, Any]]:
    """Call ``name`` expecting the documented error; return the exception and its parsed document."""
    with pytest.raises(ToolError) as excinfo:
        call_tool(name, arguments)
    exc = excinfo.value
    assert not isinstance(exc, UnexpectedToolError), "must be an anticipated ToolError, not a crash"
    assert str(exc).startswith(f"Error executing tool {name}: ")  # the SDK's prefix, then our JSON
    assert isinstance(exc.__cause__, SportsDataError)
    doc = parse_tool_error(str(exc))
    assert doc == exc.__cause__.payload
    return exc, doc


def assert_shape(doc: dict[str, Any], kind: str) -> dict[str, Any]:
    assert set(doc) == {"error"}
    error = doc["error"]
    assert list(error) == ["kind", "message", "retry_after_s"]
    assert error["kind"] == kind and kind in KINDS
    assert isinstance(error["message"], str) and error["message"]
    assert error["retry_after_s"] is None or (isinstance(error["retry_after_s"], int) and error["retry_after_s"] >= 0)
    return error


class Upstream:
    """A mock MLB API that answers every request with one canned response."""

    def __init__(self, status: int, *, headers: dict[str, str] | None = None, text: str | None = None, json_body: Any = None):
        self.calls = 0
        self.status, self.headers, self.text, self.json_body = status, headers or {}, text, json_body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.json_body is not None:
            return httpx.Response(self.status, headers=self.headers, json=self.json_body, request=request)
        return httpx.Response(self.status, headers=self.headers, text=self.text or "", request=request)


@pytest.fixture
def inject(cache_dir: Path, clock, sleeps: list[float]):
    """Build a client over ``handler``, install it as the server's client, and return it."""
    made: list[MLBClient] = []

    def _inject(handler, **kw) -> MLBClient:
        c = MLBClient(cache_dir=cache_dir, transport=httpx.MockTransport(handler), clock=clock, sleep=sleeps.append, **kw)
        made.append(c)
        server.set_client(c)
        return c

    yield _inject
    server.set_client(None)
    for c in made:
        c.close()


# -- one test per error kind, through the MCP tool layer --------------------------


def test_not_found(inject, sleeps) -> None:
    api = Upstream(404, json_body={"messageNumber": 11, "message": "Game not found"})
    inject(api)
    _exc, doc = tool_error("mlb_game_boxscore", {"game_pk": 1})
    error = assert_shape(doc, "not_found")
    assert "HTTP 404" in error["message"] and "Game not found" in error["message"]
    assert error["retry_after_s"] is None
    assert api.calls == 1 and sleeps == []  # 404 is not retried


def test_rate_limited(inject, sleeps) -> None:
    api = Upstream(429, headers={"Retry-After": "7"}, text="slow down")
    inject(api)
    _exc, doc = tool_error("mlb_ping", {})
    error = assert_shape(doc, "rate_limited")
    assert error["retry_after_s"] == 7
    assert "HTTP 429" in error["message"]
    assert api.calls == 2 and sleeps == [7.0]  # one retry, honouring Retry-After


def test_upstream(inject, sleeps) -> None:
    api = Upstream(503, text="upstream down")
    inject(api)
    _exc, doc = tool_error("mlb_standings", {"season": 2025})
    error = assert_shape(doc, "upstream")
    assert "HTTP 503" in error["message"]
    assert error["retry_after_s"] is None
    assert api.calls == 2 and len(sleeps) == 1


def test_bad_input(inject) -> None:
    api = Upstream(200, json_body={"people": []})
    inject(api)
    _exc, doc = tool_error("mlb_search_player", {"name": "A"})
    error = assert_shape(doc, "bad_input")
    assert "at least 2 characters" in error["message"]
    assert error["retry_after_s"] is None
    assert api.calls == 0  # rejected before any request


def test_offline(inject) -> None:
    api = Upstream(200, json_body={"teams": []})
    inject(api, offline=True)
    _exc, doc = tool_error("mlb_ping", {})
    error = assert_shape(doc, "offline")
    assert "offline" in error["message"] and TEAMS_URL in error["message"]
    assert error["retry_after_s"] is None
    assert api.calls == 0


# -- the client's mapping of HTTP outcomes to kinds ----------------------------------


def make_client(handler, cache_dir: Path, clock, sleeps: list[float], **kw) -> MLBClient:
    return MLBClient(cache_dir=cache_dir, transport=httpx.MockTransport(handler), clock=clock, sleep=sleeps.append, **kw)


@pytest.mark.parametrize(
    ("status", "kind"),
    [(400, "bad_input"), (401, "upstream"), (403, "upstream"), (404, "not_found"), (429, "rate_limited"),
     (500, "upstream"), (502, "upstream"), (503, "upstream"), (None, "upstream")],
)
def test_kind_for_status(status: int | None, kind: str) -> None:
    assert kind_for_status(status) == kind
    assert APIError(status, "x").kind == kind


def test_client_404_is_not_found(cache_dir, clock, sleeps) -> None:
    client = make_client(Upstream(404, text="nope"), cache_dir, clock, sleeps)
    with pytest.raises(APIError) as excinfo:
        client.get("/people/0/stats")
    err = excinfo.value
    assert (err.status, err.kind, err.retry_after_s) == (404, "not_found", None)


@pytest.mark.parametrize(
    ("header", "retry_after_s", "slept"),
    [("7", 7, 7.0), ("1.5", 2, 2.0), ("0", 0, 0.0), ("-3", 0, 0.0), ("120", 120, 10.0), (None, None, 1.0), ("soon", None, 1.0)],
)
def test_client_429_is_rate_limited_with_retry_after(cache_dir, clock, sleeps, header, retry_after_s, slept) -> None:
    headers = {} if header is None else {"Retry-After": header}
    api = Upstream(429, headers=headers, text="rate limited")
    client = make_client(api, cache_dir, clock, sleeps)
    with pytest.raises(APIError) as excinfo:
        client.get(*TEAMS)
    err = excinfo.value
    assert (err.status, err.kind, err.retry_after_s) == (429, "rate_limited", retry_after_s)
    assert api.calls == 2
    assert sleeps == [slept]  # the retry waits Retry-After (capped at 10 s), else the default backoff


def test_client_429_http_date_retry_after(cache_dir, clock, sleeps) -> None:
    clock.now = 1_700_000_000.0  # 2023-11-14T22:13:20Z
    api = Upstream(429, headers={"Retry-After": "Tue, 14 Nov 2023 22:13:50 GMT"}, text="later")
    client = make_client(api, cache_dir, clock, sleeps)
    with pytest.raises(APIError) as excinfo:
        client.get(*TEAMS)
    assert excinfo.value.retry_after_s == 30
    assert from_api_error(excinfo.value).retry_after_s == 30


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_client_5xx_is_upstream(cache_dir, clock, sleeps, status: int) -> None:
    api = Upstream(status, headers={"Retry-After": "3"}, text="down")
    client = make_client(api, cache_dir, clock, sleeps)
    with pytest.raises(APIError) as excinfo:
        client.get(*TEAMS)
    err = excinfo.value
    assert (err.status, err.kind, err.retry_after_s) == (status, "upstream", 3)
    assert api.calls == 2 and sleeps == [3.0]


@pytest.mark.parametrize("exc_type", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError])
def test_client_transport_failure_is_upstream(cache_dir, clock, sleeps, exc_type) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise exc_type("boom", request=request)

    client = make_client(handler, cache_dir, clock, sleeps)
    with pytest.raises(APIError) as excinfo:
        client.get(*TEAMS)
    err = excinfo.value
    assert (err.status, err.kind, err.retry_after_s) == (None, "upstream", None)
    assert exc_type.__name__ in err.message
    assert len(calls) == 2 and len(sleeps) == 1


def test_client_invalid_json_is_upstream(cache_dir, clock, sleeps) -> None:
    client = make_client(Upstream(200, text="<html>not json</html>"), cache_dir, clock, sleeps)
    with pytest.raises(APIError) as excinfo:
        client.get(*TEAMS)
    assert excinfo.value.kind == "upstream" and excinfo.value.status == 200


def test_client_400_is_bad_input(cache_dir, clock, sleeps) -> None:
    api = Upstream(400, json_body={"message": "bad request"})
    client = make_client(api, cache_dir, clock, sleeps)
    with pytest.raises(APIError) as excinfo:
        client.get("/schedule", {"startDate": "not-a-date"})
    assert excinfo.value.kind == "bad_input" and api.calls == 1


def test_client_offline_miss_is_offline(cache_dir, clock, sleeps) -> None:
    api = Upstream(200, json_body={"teams": []})
    client = make_client(api, cache_dir, clock, sleeps, offline=True)
    with pytest.raises(APIError) as excinfo:
        client.get(*TEAMS)
    err = excinfo.value
    assert (err.status, err.kind, err.retry_after_s) == (None, "offline", None)
    assert api.calls == 0


def test_user_agent_carries_the_version(client: MLBClient) -> None:
    assert USER_AGENT == f"sports-data-mcp/{sports_data_mcp.__version__}"
    assert client._http.headers["User-Agent"] == USER_AGENT


# -- the shape itself ----------------------------------------------------------------


def test_sports_data_error_is_a_tool_error_with_json_text() -> None:
    err = SportsDataError("rate_limited", "HTTP 429", 5)
    assert isinstance(err, ToolError)
    assert json.loads(str(err)) == {"error": {"kind": "rate_limited", "message": "HTTP 429", "retry_after_s": 5}}
    assert parse_tool_error("Error executing tool x: " + str(err)) == err.payload
    assert bad_input("nope").payload == {"error": {"kind": "bad_input", "message": "nope", "retry_after_s": None}}
    with pytest.raises(ValueError):
        SportsDataError("teapot", "no such kind")
    with pytest.raises(ValueError):
        parse_tool_error("Error executing tool x: plain text")


def test_from_api_error_keeps_kind_message_and_retry_after() -> None:
    api = APIError(429, "HTTP 429 Too Many Requests", retry_after_s=9)
    err = from_api_error(api)
    assert (err.kind, err.message, err.retry_after_s) == ("rate_limited", "HTTP 429 Too Many Requests", 9)
    assert from_api_error(APIError(None, "offline: miss", kind="offline")).kind == "offline"


@pytest.mark.parametrize("name", sorted(TOOL_CALLS))
def test_every_tool_uses_the_shape(inject, name: str) -> None:
    api = Upstream(200, json_body={})
    inject(api, offline=True)  # empty cache in offline mode: the one failure every tool can hit
    _exc, doc = tool_error(name, TOOL_CALLS[name])
    assert_shape(doc, "offline")
    assert api.calls == 0


def test_tool_list_is_complete() -> None:
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert tools == set(TOOL_CALLS)  # keep TOOL_CALLS in step with the server


# -- offline mode: --offline and SPORTS_DATA_MCP_OFFLINE=1 ---------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, cache_dir: Path):
    """Pin the env the server reads so ``main``/``get_client`` cannot leak between tests."""
    monkeypatch.setenv("SPORTS_DATA_MCP_OFFLINE", "")
    monkeypatch.setenv("SPORTS_DATA_MCP_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("SPORTS_DATA_MCP_TTL_S", "3600")
    monkeypatch.delenv("SPORTS_DATA_MCP_BASE_URL", raising=False)
    server.set_client(None)
    yield monkeypatch
    server.set_client(None)


@pytest.mark.parametrize(("value", "offline"), [("1", True), ("true", True), ("YES", True), ("", False), ("0", False), ("no", False)])
def test_env_var_selects_offline(clean_env, value: str, offline: bool) -> None:
    clean_env.setenv("SPORTS_DATA_MCP_OFFLINE", value)
    assert server.offline_from_env() is offline
    assert server.get_client().offline is offline


def test_offline_flag_selects_offline(clean_env, cache_dir: Path) -> None:
    ran: list[str] = []
    clean_env.setattr(server.mcp, "run", lambda transport: ran.append(transport))
    assert server.main(["--offline", "--cache-dir", str(cache_dir / "flag"), "--ttl", "5"]) == 0
    assert ran == ["stdio"]
    client = server.get_client()
    assert client.offline is True
    assert client.cache_dir == cache_dir / "flag" and client.ttl_s == 5.0

    server.set_client(None)
    clean_env.setenv("SPORTS_DATA_MCP_OFFLINE", "")
    assert server.main([]) == 0
    assert server.get_client().offline is False


def test_offline_server_serves_cache_only(clean_env, client: MLBClient, request_log) -> None:
    client.get(*TEAMS)  # warm the on-disk cache through the fixture transport
    assert len(request_log) == 1

    clean_env.setenv("SPORTS_DATA_MCP_OFFLINE", "1")
    server.set_client(None)
    offline_client = server.get_client()  # real httpx transport, which conftest makes fail on use
    assert offline_client.offline is True and offline_client.cache_dir == client.cache_dir

    result = call_tool("mlb_ping", {})
    assert result.is_error is not True
    assert result.structured_content["cached"] is True

    _exc, doc = tool_error("mlb_standings", {"season": 2025})  # not cached: error, no socket
    assert_shape(doc, "offline")
    assert len(request_log) == 1


# -- release metadata ----------------------------------------------------------------


def test_version_is_0_1_0_everywhere() -> None:
    assert sports_data_mcp.__version__ == "0.1.0"
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == sports_data_mcp.__version__
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.1.0]" in changelog


def test_ci_workflow_runs_make_test_on_both_platforms_and_pythons() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert "\t" not in text  # YAML forbids tabs for indentation
    assert "run: make test" in text
    assert "ubuntu-latest" in text and "macos-latest" in text
    assert '"3.11"' in text and '"3.12"' in text
    assert "statsapi.mlb.com" not in text  # CI is offline like the tests
