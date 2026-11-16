"""End-to-end smoke test over real streamable HTTP: list tools, call each once.

Usage: .venv/bin/python smoke.py [URL]     (default http://localhost:7071/mcp, i.e. func start)
Set MCP_API_KEY to send the key (as x-api-key) when the server requires one.
"""

import asyncio
import json
import os
import sys

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

URL = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:7071/mcp'
# Fremont, Seattle
LAT, LON = 47.6505, -122.3493


async def main() -> int:
    headers = {'x-api-key': os.environ['MCP_API_KEY']} if os.environ.get('MCP_API_KEY') else {}
    async with httpx2.AsyncClient(headers=headers, timeout=60) as http:
        async with Client(streamable_http_client(URL, http_client=http)) as client:
            tools = await client.list_tools()
            print('tools:', [t.name for t in tools.tools])
            calls = [
                ('find_beers_on_tap', {'latitude': LAT, 'longitude': LON, 'query': 'hazy', 'limit': 3}),
                ('find_venues', {'latitude': LAT, 'longitude': LON, 'limit': 3}),
                ('venue_taplist', {'venue': 'Bottleworks', 'latitude': LAT, 'longitude': LON}),
            ]
            failed = 0
            for name, args in calls:
                result = await client.call_tool(name, args)
                payload = result.structured_content or {}
                if result.is_error:
                    failed += 1
                    print(f'\n{name}: ERROR', result.content)
                    continue
                print(f'\n{name}:', json.dumps(payload, indent=1)[:900])
            return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
