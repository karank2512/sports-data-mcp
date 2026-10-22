# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-21

First release: a stdio MCP server over the public MLB Stats API with an
on-disk cache and an offline test suite.

### Added

- `MLBClient`: cached GET client for `statsapi.mlb.com/api/v1`. Responses are
  stored as JSON keyed by the sha256 of the full URL, reused until a TTL
  (default 3600 s, overridable per call) and served regardless of age in
  offline mode. 429 and 5xx responses and transport failures are retried once
  with backoff, honouring `Retry-After` up to 10 s.
- Tools: `mlb_ping`, `mlb_search_player`, `mlb_player_stats`,
  `mlb_team_schedule` (team ids or any team name, at most 31 days),
  `mlb_standings`, `mlb_game_boxscore` (trimmed box score) and
  `mlb_live_linescore` (30 s cache for polling).
- One error shape for every tool. A failing call is an MCP tool error whose
  text carries `{"error": {"kind", "message", "retry_after_s"}}` with `kind`
  one of `not_found` (404), `rate_limited` (429, `retry_after_s` from
  `Retry-After`), `upstream` (5xx, other 4xx, bad JSON, timeouts and
  connection failures), `bad_input` (rejected arguments or HTTP 400) and
  `offline` (cache miss in offline mode). `APIError` now carries `kind` and
  `retry_after_s`, and `Retry-After` HTTP dates are understood.
- `--offline` flag and `SPORTS_DATA_MCP_OFFLINE=1` serve the cache only and
  never open a socket; `--cache-dir`, `--ttl` and the matching
  `SPORTS_DATA_MCP_CACHE_DIR`, `SPORTS_DATA_MCP_TTL_S`, `SPORTS_DATA_MCP_BASE_URL`
  settings.
- `make test` (offline, fixtures through `httpx.MockTransport`) and
  `make record-fixtures` (re-records real responses).
- GitHub Actions workflow running `make test` on Ubuntu and macOS with
  Python 3.11 and 3.12.
- The `User-Agent` header reports the package version.
