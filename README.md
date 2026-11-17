# What's Next MCP server

A public, read-only [MCP](https://modelcontextprotocol.io) server for live beer taplists from
[What's Next](https://whatsnext.beer): what's on tap right now at bars, bottle shops and breweries,
mostly in the Seattle area.

## Use it

Public, no key or account needed. Server URL:

```
https://whatsnext-mcp.azurewebsites.net/mcp
```

- **Claude Code:** `claude mcp add --transport http whatsnext https://whatsnext-mcp.azurewebsites.net/mcp`
- **Claude.ai / Claude Desktop:** Settings → Connectors → add a custom connector with the URL above.
- **VS Code** (`.vscode/mcp.json`):
  ```json
  { "servers": { "whatsnext": { "type": "http", "url": "https://whatsnext-mcp.azurewebsites.net/mcp" } } }
  ```
- **Cursor** (`~/.cursor/mcp.json`):
  ```json
  { "mcpServers": { "whatsnext": { "url": "https://whatsnext-mcp.azurewebsites.net/mcp" } } }
  ```
- **Try the tools by hand:** `npx @modelcontextprotocol/inspector`, then connect to the URL (transport: Streamable HTTP).

Then ask things like "what fresh hop beers are on tap near Ballard?" or "what's pouring at Holy Mountain?".
Coverage is mostly the Seattle area. Fair use: 30 requests a minute per caller.

## Tools

| Tool | What it does |
|---|---|
| `find_beers_on_tap` | Beers on tap near a lat/long, nearest first. Optional free-text query, style, brewery, fresh-hop filter. Up to 25. |
| `venue_taplist` | Everything on tap at one venue, by (partial) name. |
| `find_venues` | Venues near a lat/long, optionally filtered by name. |

All tools are annotated read-only and idempotent and return structured JSON with links back to the site.

## Design

- **Public API only.** The server calls `https://api.whatsnext.beer`'s unauthenticated GET routes, never the
  database, so it can't expose anything the website doesn't.
- **Allow-listed output.** API rows carry internal fields (reconciliation status, match confidence); only
  user-facing fields are returned.
- **Prompt-injection hygiene.** Venue-written text is flattened to one line, stripped of control characters,
  and capped at 300 characters. What's Next's own description is preferred over the venue's.
- **Abuse controls** (`guard.py`): per-IP limit (30/min), global cap (300/hour per instance, sized to What's Next's small database; `MCP_GLOBAL_PER_HOUR` overrides), optional API key
  (`MCP_API_KEY`) for an unlisted launch, 5-minute response cache.
- **Stateless streamable HTTP**, so any instance can serve any request.
- **Own telemetry** (`telemetry.py`): requests (including 401/429), one span per tool call, and this server's
  log lines go to Application Insights through OpenTelemetry. The Functions host doesn't record proxied requests
  reliably, and Flex has no HTTP-status metrics. Two log alerts email the owner: server errors, and sustained 429s.

## Hosting

Azure Functions **Flex Consumption** as a custom handler (`host.json`, `configurationProfile:
mcp-custom-handler`, preview). It scales to zero and is capped at 2 instances as a cost ceiling.
Rate-limit state is in-process, so with 2 instances the effective limits can be up to twice the configured
ones. At higher scale, API Management or Redis would hold it instead.

Gotcha: the host proxies requests from inside the instance and rewrites `X-Forwarded-For` to `127.0.0.1`. The
caller's address is in the front end's `CLIENT-IP` header, which the front end overwrites (a spoofed value doesn't
get through; tested). `guard.client_ip` reads that first.

Gotcha: set the HTTP trigger's `perInstanceConcurrency` explicitly (`provision.sh` does). With it unset and a
1-2 instance cap, the Flex front end queued requests for 15-45 s before they ever reached the host.

```bash
./deploy.sh                    # routine deploy: tests, git push, publish, live edge-case checks
ALERT_EMAIL=you@example.com deploy/provision.sh   # one-time setup: resources, scale settings, alerts, publish
REQUIRE_KEY=1 ALERT_EMAIL=... deploy/provision.sh # same, but require an API key (unlisted launch)
```

## Local development

```bash
python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest                # unit tests
.venv/bin/python server.py                # http://localhost:8765/mcp
PATH=".venv/bin:$PATH" func start         # same server behind the Functions host, :7071
.venv/bin/python smoke.py [URL]           # end-to-end: list tools, call each (MCP_API_KEY env if needed)
.venv/bin/python edge_test.py [URL]       # pre-launch edge cases: bad input, caps, leaks, auth, venue matching
```

## License

MIT. See [LICENSE](LICENSE).
