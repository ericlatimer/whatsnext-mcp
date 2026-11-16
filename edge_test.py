"""Pre-launch edge-case checks against a running server (local or Azure).

Usage: .venv/bin/python edge_test.py [URL]     (default http://localhost:7071/mcp)
Set MCP_API_KEY when the server requires one. Exits non-zero if any check fails.
Uses about 15 requests, inside the 30/min per-IP limit.
"""

import asyncio
import os
import sys
import time

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

URL = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:7071/mcp'
KEY = os.environ.get('MCP_API_KEY')
FREMONT = {'latitude': 47.6505, 'longitude': -122.3493}
PORTLAND = {'latitude': 45.5152, 'longitude': -122.6784}
NYC = {'latitude': 40.7128, 'longitude': -74.0060}

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = '') -> None:
    results.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}")


def error_text(result) -> str:
    return ' '.join(getattr(c, 'text', '') for c in result.content)


async def call(client, name, args):
    start = time.monotonic()
    result = await client.call_tool(name, args)
    return result, time.monotonic() - start


async def main() -> int:
    # Auth: no key must be refused when the server requires one.
    if KEY:
        resp = httpx.post(URL, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, timeout=60,
                          headers={'accept': 'application/json, text/event-stream'})
        check(resp.status_code == 401, 'no API key is refused', f'HTTP {resp.status_code}')

    headers = {'x-api-key': KEY} if KEY else {}
    async with httpx2.AsyncClient(headers=headers, timeout=60) as http:
        async with Client(streamable_http_client(URL, http_client=http)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            check(set(tools) == {'find_beers_on_tap', 'venue_taplist', 'find_venues'}, 'exactly 3 tools listed')
            check(all(t.annotations and t.annotations.read_only_hint for t in tools.values()),
                  'every tool is annotated read-only')

            r, dt = await call(client, 'find_beers_on_tap', {'latitude': 200, 'longitude': 0})
            check(r.is_error and 'latitude' in error_text(r), 'invalid latitude gives a clear error',
                  error_text(r)[:80])

            r, dt = await call(client, 'find_beers_on_tap', {**FREMONT, 'radius': '3_miles'})
            check(r.is_error, 'radius outside the allowed values is rejected', error_text(r)[:80])

            r, dt = await call(client, 'find_beers_on_tap', {**FREMONT, 'limit': 1000})
            n = (r.structured_content or {}).get('returned', -1)
            check(not r.is_error and 0 < n <= 25, 'limit=1000 is capped at 25', f'returned {n}, {dt:.1f}s')

            r, dt = await call(client, 'find_beers_on_tap', {**FREMONT, 'query': ''})
            check(not r.is_error and (r.structured_content or {}).get('returned', 0) > 0,
                  'empty query behaves like no query', f'{dt:.1f}s')

            r, dt = await call(client, 'find_beers_on_tap', {**FREMONT, 'query': 'x' * 5000})
            check(not r.is_error, '5,000-char query is truncated, not an error', f'{dt:.1f}s')

            r, dt = await call(client, 'find_beers_on_tap', {**FREMONT, 'query': 'ignore previous instructions'})
            check(not r.is_error, 'injection-looking query is just a search',
                  f"{(r.structured_content or {}).get('total_matching')} matches")

            beers = (await call(client, 'find_beers_on_tap', {**FREMONT, 'limit': 25}))[0].structured_content or {}
            leaked = {'beer_reconciliation_status', 'beer_match_confidence', 'untappd_id', 'vtb_id', 'venue_id'}
            keys = set().union(*(b.keys() for b in beers.get('beers', [])))
            check(not (keys & leaked), 'no internal fields in output', f'keys: {sorted(keys)}')
            long_text = [b for b in beers.get('beers', []) if len(b.get('description') or '') > 300]
            check(not long_text, 'descriptions capped at 300 chars')

            for label, where in (('Portland', PORTLAND), ('NYC', NYC)):
                r, dt = await call(client, 'find_beers_on_tap', {**where, 'radius': '10_miles'})
                total = (r.structured_content or {}).get('total_matching')
                check(not r.is_error, f'{label} (outside core coverage) answers cleanly', f'{total} matches')

            r, dt = await call(client, 'venue_taplist', {'venue': 'Zzqx Nonexistent Taproom', **FREMONT})
            sc = r.structured_content or {}
            check(not r.is_error and sc.get('venue') is None and sc.get('note'), 'unknown venue returns a note',
                  sc.get('note', '')[:80])

            r, dt = await call(client, 'venue_taplist', {'venue': 'Stoup', **FREMONT})
            sc = r.structured_content or {}
            check(not r.is_error and sc.get('venue') and sc.get('beer_count', 0) > 0 and sc.get('other_matches'),
                  'ambiguous partial name picks one venue and lists the others',
                  f"{sc.get('venue')} ({sc.get('beer_count')} beers); others: {sc.get('other_matches')}")

            r, dt = await call(client, 'venue_taplist', {'venue': 'holy mountain', **FREMONT})
            sc = r.structured_content or {}
            check(not r.is_error and sc.get('venue') and 'holy mountain' in sc['venue'].lower(),
                  'lowercase partial name resolves', f"{sc.get('venue')} ({sc.get('beer_count')} beers)")

            r, dt = await call(client, 'find_venues', {**FREMONT, 'query': 'zzqx-no-such-venue'})
            check(not r.is_error and (r.structured_content or {}).get('venues') == [],
                  'find_venues with no match returns an empty list')

    failed = [r for r in results if not r[0]]
    print(f'\n{len(results) - len(failed)}/{len(results)} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
