import asyncio

from guard import GuardMiddleware, client_ip


def _scope(headers=None, client=('10.0.0.5', 1234)):
    return {'type': 'http', 'path': '/mcp', 'client': client,
            'headers': [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}


async def _ok_app(scope, receive, send):
    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    await send({'type': 'http.response.body', 'body': b'ok'})


def _status(mw, scope) -> int:
    sent = []

    async def send(msg):
        sent.append(msg)

    asyncio.run(mw(scope, None, send))
    return sent[0]['status']


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_client_ip_takes_rightmost_public_forwarded_address():
    # Leftmost entry is caller-supplied (spoofable); Azure appends the real one.
    scope = _scope({'x-forwarded-for': '8.8.8.8, 1.1.1.1:5555, 10.1.2.3'})
    assert client_ip(scope) == '1.1.1.1'


def test_client_ip_prefers_azure_client_ip_header():
    # What the Functions host actually sends (seen in prod logs, 2026-11-16).
    scope = _scope({'x-forwarded-for': '127.0.0.1', 'client-ip': '24.17.215.191:52049'})
    assert client_ip(scope) == '24.17.215.191'
    assert client_ip(_scope({'client-ip': '[2001:4860:4860::8888]:443'})) == '2001:4860:4860::8888'


def test_client_ip_falls_back_to_socket_peer():
    assert client_ip(_scope()) == '10.0.0.5'
    assert client_ip(_scope({'x-forwarded-for': 'garbage, 192.168.1.1'})) == '10.0.0.5'


def test_per_ip_limit_and_window_reset():
    clock = FakeClock()
    mw = GuardMiddleware(_ok_app, per_ip_per_minute=3, global_per_hour=100, clock=clock)
    a = _scope({'x-forwarded-for': '1.1.1.1'})
    b = _scope({'x-forwarded-for': '9.9.9.9'})
    assert [_status(mw, a) for _ in range(4)] == [200, 200, 200, 429]
    assert _status(mw, b) == 200  # other callers unaffected
    clock.t += 61
    assert _status(mw, a) == 200


def test_global_cap():
    mw = GuardMiddleware(_ok_app, per_ip_per_minute=100, global_per_hour=2, clock=FakeClock())
    statuses = [_status(mw, _scope({'x-forwarded-for': f'8.8.4.{i + 1}'})) for i in range(3)]
    assert statuses == [200, 200, 429]


def test_api_key_required_when_configured():
    mw = GuardMiddleware(_ok_app, api_key='s3cret', clock=FakeClock())
    assert _status(mw, _scope()) == 401
    assert _status(mw, _scope({'x-api-key': 'wrong'})) == 401
    assert _status(mw, _scope({'x-api-key': 's3cret'})) == 200
    assert _status(mw, _scope({'authorization': 'Bearer s3cret'})) == 200


def test_no_key_configured_means_open():
    mw = GuardMiddleware(_ok_app, api_key=None, clock=FakeClock())
    assert _status(mw, _scope()) == 200


def test_lifespan_passes_through():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope['type'])

    asyncio.run(GuardMiddleware(app, api_key='k')({'type': 'lifespan'}, None, None))
    assert seen == ['lifespan']
