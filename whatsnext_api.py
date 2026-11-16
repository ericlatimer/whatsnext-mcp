"""Client for What's Next's public API (https://api.whatsnext.beer).

Only public, unauthenticated GET routes are called, so this server can never
expose anything the website doesn't. Responses are cached briefly (taplists
change slowly) and reshaped to an allow-list of fields: the raw API rows carry
internal fields (reconciliation status, match confidence) that callers don't
need, and venue-written text is length-capped plain text.
"""

import math
import re
import time
from typing import Any

import httpx

API_BASE = 'https://api.whatsnext.beer'
SITE_BASE = 'https://whatsnext.beer'
USER_AGENT = 'whatsnext-mcp/0.1 (+https://whatsnext.beer)'

CACHE_TTL_SECONDS = 300
CACHE_MAX_ENTRIES = 512
MAX_TEXT_CHARS = 300

# The API's radius buckets (taplist_display_data_access.RADIUS_MILES_MAP).
RADIUS_MILES = {'quarter_mile': 0.25, 'half_mile': 0.5, '1_mile': 1.0, '5_miles': 5.0,
                '10_miles': 10.0, '25_miles': 25.0}

_CONTROL_CHARS_RE = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
_WHITESPACE_RE = re.compile(r'\s+')


class UpstreamError(RuntimeError):
    pass


def clean_text(value: Any, limit: int = MAX_TEXT_CHARS) -> str | None:
    """Plain, single-line, length-capped text. Venue-written fields go through
    this so a menu can't smuggle a long instruction block into a model's context."""
    if value is None:
        return None
    text = _WHITESPACE_RE.sub(' ', _CONTROL_CHARS_RE.sub(' ', str(value))).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _display(row: dict, field: str) -> str | None:
    """Display value the website shows: new_* (admin/parser-corrected) over raw_*."""
    return row.get(f'new_{field}') or row.get(f'raw_{field}') or row.get(field)


def shape_beer(row: dict) -> dict:
    beer_id = row.get('canonical_beer_id')
    first_seen = row.get('first_seen')
    return {
        'beer': clean_text(row.get('canonical_beer_name') or _display(row, 'beer_name'), 120),
        'brewery': clean_text(row.get('beer_brewery_name') or row.get('canonical_brewery_name')
                              or _display(row, 'brewery'), 120),
        'style': clean_text(row.get('new_style') or row.get('style'), 80),
        'abv': row.get('abv'),
        'fresh_hop': bool(row.get('is_fresh_hop')),
        # Our own generated description first; the venue's menu text only as a fallback.
        'description': clean_text(row.get('ai_description') or row.get('description')),
        'venue': clean_text(row.get('venue_name'), 120),
        'venue_address': clean_text(row.get('venue_address'), 160),
        'distance_miles': row.get('distance_miles'),
        'on_tap_since': first_seen[:10] if isinstance(first_seen, str) else None,
        'beer_url': f'{SITE_BASE}/beer/{beer_id}' if beer_id else None,
        'venue_url': f"{SITE_BASE}/venues/{row['venue_slug']}" if row.get('venue_slug') else None,
    }


def _miles_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def shape_venue(row: dict, latitude: float, longitude: float) -> dict:
    places = row.get('places_data') or {}
    loc = places.get('location') or {}
    distance = None
    if loc.get('latitude') is not None and loc.get('longitude') is not None:
        distance = round(_miles_between(latitude, longitude, loc['latitude'], loc['longitude']), 2)
    city = ', '.join(p for p in (row.get('address_locality'), row.get('address_region')) if p)
    return {
        'venue': clean_text(row.get('name'), 120),
        'address': clean_text(places.get('formattedAddress'), 160),
        'city': city or None,
        'distance_miles': distance,
        'venue_url': f"{SITE_BASE}/venues/{row['slug']}" if row.get('slug') else None,
    }


class WhatsNextApi:
    def __init__(self, base_url: str = API_BASE, client: httpx.AsyncClient | None = None,
                 clock=time.monotonic):
        self._base_url = base_url
        self._client = client
        self._clock = clock
        self._cache: dict[tuple, tuple[float, Any]] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base_url, timeout=20.0,
                                             headers={'User-Agent': USER_AGENT})
        return self._client

    async def get(self, path: str, params: dict) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        key = (path, tuple(sorted(params.items())))
        now = self._clock()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        try:
            resp = await self._http().get(path, params=params)
        except httpx.HTTPError as e:
            raise UpstreamError(f"What's Next API unreachable: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise UpstreamError(f"What's Next API returned HTTP {resp.status_code}")
        data = resp.json()
        if len(self._cache) >= CACHE_MAX_ENTRIES:
            # Drop expired entries first, then the oldest if still full.
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            if len(self._cache) >= CACHE_MAX_ENTRIES:
                self._cache.pop(min(self._cache, key=lambda k: self._cache[k][0]))
        self._cache[key] = (now + CACHE_TTL_SECONDS, data)
        return data

    async def taplist(self, *, latitude: float, longitude: float, radius: str, limit: int,
                      search: str | None = None, style: str | None = None,
                      brewery: str | None = None, venue: str | None = None,
                      fresh_hop: bool = False) -> dict:
        return await self.get('/taplist', {
            'latitude': latitude, 'longitude': longitude, 'radius': radius, 'limit': limit,
            'search': search, 'style': style, 'breweryName': brewery, 'venueName': venue,
            'freshHop': 'true' if fresh_hop else None, 'sortBy': 'distance',
        })

    async def browse_venues(self, *, latitude: float, longitude: float,
                            search: str | None = None) -> dict:
        return await self.get('/venues/browse', {
            'latitude': latitude, 'longitude': longitude, 'search': search,
        })
