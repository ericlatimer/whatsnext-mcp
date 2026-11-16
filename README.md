# What's Next MCP server

A public, read-only [MCP](https://modelcontextprotocol.io) server for live beer taplists from
[What's Next](https://whatsnext.beer): what's on tap right now at bars, bottle shops and breweries,
mostly in the Seattle area.

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
- **Abuse controls** (`guard.py`): per-IP limit (30/min), global cap (1,500/hour), optional API key
  (`MCP_API_KEY`) for an unlisted launch, 5-minute response cache.
- **Stateless streamable HTTP**, so any instance can serve any request.

## Hosting

Azure Functions **Flex Consumption** as a custom handler (`host.json`, `configurationProfile:
mcp-custom-handler`, preview). It scales to zero and is capped at 2 instances as a cost ceiling.
Rate-limit state is in-process, so with 2 instances the effective limits can be up to twice the configured
ones. At higher scale, API Management or Redis would hold it instead.

Gotcha: set the HTTP trigger's `perInstanceConcurrency` explicitly (`provision.sh` does). With it unset and a
1-2 instance cap, the Flex front end queued requests for 15-45 s before they ever reached the host.

```bash
deploy/provision.sh            # resource group, storage, function app, settings, publish
deploy/provision.sh publish    # code-only redeploy
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
