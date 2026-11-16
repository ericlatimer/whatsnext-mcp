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
# Sized to the upstream, not this server: What's Next's database is small and
# already busy at peaks, and a cache miss here is one or two API calls there.
# Override with the MCP_GLOBAL_PER_HOUR app setting.
GLOBAL_PER_HOUR = 300


def _parse_address(value: str):
    """An IP from "1.2.3.4", "1.2.3.4:port", "[v6]:port" or "v6", else None."""
    candidate = value.strip()
    if candidate.startswith('[') and ']' in candidate:  # [v6]:port
        candidate = candidate[1:candidate.index(']')]
    elif candidate.count(':') == 1:  # v4:port
        candidate = candidate.split(':')[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def client_ip(scope) -> str:
    """The caller's public address.

    On Azure Functions the host proxies each request from inside the instance:
    it rewrites X-Forwarded-For to 127.0.0.1 and the real caller arrives in
    the App Service front end's CLIENT-IP header ("addr:port"). Fall back to
    the rightmost public X-Forwarded-For entry (other proxies append the real
    client; anything to its left is caller-supplied), then the socket peer."""
    headers = {k.decode('latin-1').lower(): v.decode('latin-1') for k, v in scope.get('headers', [])}
    addr = _parse_address(headers.get('client-ip', ''))
    if addr and addr.is_global:
        return str(addr)
    for part in reversed(headers.get('x-forwarded-for', '').split(',')):
        addr = _parse_address(part)
        if addr and addr.is_global:
            return str(addr)
    client = scope.get('client')
    return client[0] if client else 'unknown'


# Headers a proxy might carry the caller's address in. Logged (never the API
# key) when no public address is found, to see what the Functions host sends.
_FORWARDING_HEADERS = ('x-forwarded-for', 'x-client-ip', 'x-original-for', 'client-ip', 'x-real-ip',
                       'x-azure-clientip', 'x-azure-socketip', 'forwarded', 'x-arr-log-id', 'x-ms-client-ip')


def forwarding_headers(scope) -> dict:
    headers = {k.decode('latin-1').lower(): v.decode('latin-1') for k, v in scope.get('headers', [])}
    return {k: headers[k][:120] for k in _FORWARDING_HEADERS if k in headers} | {'_names': sorted(headers)}


def _is_public(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


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
        self._last_unresolved_log = float('-inf')

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
        if not _is_public(ip) and now - self._last_unresolved_log > 300:
            self._last_unresolved_log = now
            log.warning('CLIENT_IP_UNRESOLVED peer=%s headers=%s', ip, forwarding_headers(scope))
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
