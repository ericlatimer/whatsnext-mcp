"""Abuse controls in front of the MCP endpoint (an ASGI middleware).

- Optional shared API key (MCP_API_KEY): while the server is unlisted, only
  callers holding the key get in. Unset = open.
- Per-IP sliding-window rate limit, plus a global cap so one busy hour can't
  run up the What's Next Lambda/DB bill behind it.

State is in-process and the Function App is capped at 2 instances, so the
effective limits can be up to 2x the numbers below
(at work you'd move this to API Management or Redis).
"""

import hmac
import ipaddress
import json
import logging
import time
from collections import deque

log = logging.getLogger('whatsnext_mcp.guard')

PER_IP_PER_MINUTE = 30
GLOBAL_PER_HOUR = 1500


def client_ip(scope) -> str:
    """Rightmost public address in X-Forwarded-For (Azure's front end appends the
    real client; anything to its left is caller-supplied and spoofable), else the
    socket peer."""
    headers = {k.decode('latin-1').lower(): v.decode('latin-1') for k, v in scope.get('headers', [])}
    for part in reversed(headers.get('x-forwarded-for', '').split(',')):
        candidate = part.strip()
        if candidate.startswith('[') and ']' in candidate:  # [v6]:port
            candidate = candidate[1:candidate.index(']')]
        elif candidate.count(':') == 1:  # v4:port
            candidate = candidate.split(':')[0]
        try:
            addr = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if addr.is_global:
            return str(addr)
    client = scope.get('client')
    return client[0] if client else 'unknown'


class SlidingWindow:
    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window = window_seconds
        self.hits: dict[str, deque] = {}

    def allow(self, key: str, now: float) -> bool:
        q = self.hits.setdefault(key, deque())
        while q and q[0] <= now - self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def prune(self, now: float) -> None:
        for key in [k for k, q in self.hits.items() if not q or q[-1] <= now - self.window]:
            del self.hits[key]


class GuardMiddleware:
    def __init__(self, app, *, api_key: str | None = None, per_ip_per_minute: int = PER_IP_PER_MINUTE,
                 global_per_hour: int = GLOBAL_PER_HOUR, clock=time.monotonic):
        self.app = app
        self.api_key = api_key or None
        self.per_ip = SlidingWindow(per_ip_per_minute, 60)
        self.global_ = SlidingWindow(global_per_hour, 3600)
        self.clock = clock
        self._last_prune = 0.0

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        if self.api_key and not self._has_key(scope):
            return await _reply(send, 401, 'Missing or invalid API key.')

        now = self.clock()
        if now - self._last_prune > 300:
            self.per_ip.prune(now)
            self._last_prune = now
        ip = client_ip(scope)
        if not self.per_ip.allow(ip, now):
            log.warning('RATE_LIMITED per_ip ip=%s', ip)
            return await _reply(send, 429, 'Too many requests; slow down.', retry_after=60)
        if not self.global_.allow('*', now):
            log.warning('RATE_LIMITED global ip=%s', ip)
            return await _reply(send, 429, 'Server is busy; try again later.', retry_after=300)
        return await self.app(scope, receive, send)

    def _has_key(self, scope) -> bool:
        headers = {k.decode('latin-1').lower(): v.decode('latin-1') for k, v in scope.get('headers', [])}
        supplied = headers.get('x-api-key', '')
        auth = headers.get('authorization', '')
        if not supplied and auth.lower().startswith('bearer '):
            supplied = auth[7:].strip()
        return bool(supplied) and hmac.compare_digest(supplied, self.api_key)


async def _reply(send, status: int, message: str, retry_after: int | None = None):
    body = json.dumps({'error': message}).encode()
    headers = [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())]
    if retry_after:
        headers.append((b'retry-after', str(retry_after).encode()))
    await send({'type': 'http.response.start', 'status': status, 'headers': headers})
    await send({'type': 'http.response.body', 'body': body})
