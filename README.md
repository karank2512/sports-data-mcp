# sports-data-mcp

MCP server over public sports stats APIs (MLB first): players, teams, schedules, box scores and standings as tools an agent can call.

Data comes from the public MLB Stats API (`statsapi.mlb.com`). This project is not affiliated with, endorsed by, or sponsored by MLB. Check the API's terms before any commercial use.

## Status

Pre-alpha. The first packet lands the scaffold: a cached MLB Stats API client, a stdio MCP server with a single `mlb_ping` tool, and an offline test suite.

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

## How caching works

Every GET is cached on disk as JSON, keyed by the sha256 of the full URL. A response is reused until it is older than the TTL (per client, or per call). In offline mode the cache is served regardless of age and a miss is an error rather than a network call. Responses with 429 or 5xx status are retried once with backoff (honouring `Retry-After` up to 10 s), then raised as `APIError(status, message)`. Other 4xx errors are raised immediately. Errors are never cached. Each request has a 10 second timeout.

## Development

```bash
make test             # offline; serves tests/fixtures/ through an httpx MockTransport
make record-fixtures  # needs network: re-records tests/fixtures/*.json from statsapi.mlb.com
```

`tests/fixtures/index.json` maps each fixture name to the exact URL it answers. The checked-in fixtures are small hand-written stand-ins shaped like the real API (the index says `"synthetic": true`); run `make record-fixtures` to replace them with real recordings. Tests never contact the network: an autouse fixture makes any real `httpx` transport fail.

## What it does not do

- No live or streaming data; everything is a cached GET of a public endpoint.
- No other sports yet. Only the MLB Stats API is wired up, and only `mlb_ping` is exposed.
- No authentication, API keys, or paid data sources. It uses only the public, unauthenticated API.
- No betting odds, projections, or any data that the MLB Stats API does not publish.
- No write operations of any kind.
- No guarantees about the upstream API, which is undocumented and may change or rate-limit without notice.

## License

MIT, see `LICENSE`. The license covers this code only, not the data returned by the MLB Stats API.
