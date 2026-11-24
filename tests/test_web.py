from aiohttp.test_utils import TestClient, TestServer

from vinylpilot.web import WebApp


async def _client(password=None):
    app = WebApp("http://127.0.0.1:1", password).build()  # core unreachable on purpose
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_index_and_static():
    c = await _client()
    try:
        r = await c.get("/")
        assert r.status == 200 and "VinylPilot" in await r.text()
        assert (await c.get("/static/logo.svg")).status == 200
    finally:
        await c.close()


async def test_proxy_only_allows_known_paths_and_reports_core_down():
    c = await _client()
    try:
        assert (await c.get("/api/core/../../etc/passwd")).status == 404
        assert (await c.get("/api/core/whatever")).status == 404
        r = await c.get("/api/core/status")
        assert r.status == 503
    finally:
        await c.close()


async def test_optional_password():
    c = await _client("secret")
    try:
        assert (await c.get("/")).status == 401
        r = await c.get("/", headers={"Authorization": "Basic eDpzZWNyZXQ="})  # x:secret
        assert r.status == 200
    finally:
        await c.close()
