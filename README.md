# sports-data-mcp

MCP server over public sports stats APIs (MLB first): players, teams, schedules, box scores and standings as tools an agent can call.

Data comes from the public MLB Stats API (`statsapi.mlb.com`). This project is not affiliated with, endorsed by, or sponsored by MLB. Check the API's terms before any commercial use.

## Status

Pre-alpha. So far: a cached MLB Stats API client, a stdio MCP server with `mlb_ping`, `mlb_search_player` and `mlb_player_stats`, and an offline test suite.

## Install

Requires Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/sports-data-mcp --help
```

The `sports-data-mcp` console script starts the server on stdio. Options:

| Flag | Meaning | Default |
| --- | --- | --- |
| `--cache-dir DIR` | where JSON responses are cached | `~/.cache/sports-data-mcp` |
| `--ttl SECONDS` | how long a cached response is reused | `3600` |
| `--offline` | serve only from the cache; never open a socket | off |

The same settings can be given as environment variables: `SPORTS_DATA_MCP_CACHE_DIR`, `SPORTS_DATA_MCP_TTL_S`, `SPORTS_DATA_MCP_OFFLINE=1`, and `SPORTS_DATA_MCP_BASE_URL` to point at a different API root.

## Claude Desktop

Add to `claude_desktop_config.json` (Settings → Developer → Edit Config), using the absolute path to the console script:

```json
{
  "mcpServers": {
    "sports-data": {
      "command": "/path/to/sports-data-mcp/.venv/bin/sports-data-mcp"
    }
  }
}
```

Restart Claude Desktop. Ask "ping the MLB API" and the `mlb_ping` tool should answer with `{ok, cached, teams}`.

## Claude Code

```bash
claude mcp add sports-data -- /path/to/sports-data-mcp/.venv/bin/sports-data-mcp
```

Or add the same `mcpServers` block to `.mcp.json` in your project.

## Tools

| Tool | Returns |
| --- | --- |
| `mlb_ping()` | `{ok: true, cached: bool, teams: int}` from `/teams?sportId=1`. `cached` is true when the answer came from disk without a request. |
| `mlb_search_player(name, active_only=true)` | Up to 10 `{id, full_name, team, position, bats, throws, active}` from `/people/search?names=`. `name` must be at least 2 characters (shorter is a tool error). `active_only` drops inactive people. `team` is null when the API omits `currentTeam`. |
| `mlb_player_stats(player_id, season, group="hitting", type="season")` | `{player_id, season, group, type, splits, cached, source_url}` from `/people/{id}/stats`. `group` is `hitting`, `pitching` or `fielding`; `type` is `season` (for `season`) or `career` (ignores `season`). Each split is a flat dict: the API's stat names verbatim (`gamesPlayed`, `avg`, `era`...) plus `season`, `game_type`, `team`, `team_id`, `player_id`. An unknown player id is a tool error carrying the API's message. |

Tool errors (bad input, API 4xx/5xx after retry, offline cache miss) come back as MCP `isError` results with the message in the content, so the calling model can read them.

## How caching works

Every GET is cached on disk as JSON, keyed by the sha256 of the full URL. A response is reused until it is older than the TTL (per client, or per call). In offline mode the cache is served regardless of age and a miss is an error rather than a network call. Responses with 429 or 5xx status are retried once with backoff (honouring `Retry-After` up to 10 s), then raised as `APIError(status, message)`. Other 4xx errors are raised immediately. Errors are never cached. Each request has a 10 second timeout.

## Development

```bash
make test                                   # offline; serves tests/fixtures/ through an httpx MockTransport
make test TESTS=tests/test_players.py       # one file
make test PYTEST_ARGS="-x -k stats"         # extra pytest options
make record-fixtures                        # needs network: re-records tests/fixtures/*.json from statsapi.mlb.com
```

`tests/fixtures/index.json` maps each fixture name to the exact URL it answers. Most fixtures are real recordings; an entry marked `"synthetic": true` is a hand-written placeholder in the documented response shape, waiting for `make record-fixtures` to replace it with a real response. Tests derive their expected values from the fixture contents so they keep passing after re-recording. Tests never contact the network: an autouse fixture makes any real `httpx` transport fail.

## What it does not do

- No live or streaming data; everything is a cached GET of a public endpoint.
- No other sports yet. Only the MLB Stats API is wired up, and only the tools in the table above are exposed.
- No authentication, API keys, or paid data sources. It uses only the public, unauthenticated API.
- No betting odds, projections, or any data that the MLB Stats API does not publish.
- No write operations of any kind.
- No guarantees about the upstream API, which is undocumented and may change or rate-limit without notice.

## License

MIT, see `LICENSE`. The license covers this code only, not the data returned by the MLB Stats API.
