import asyncio

from aiohttp.test_utils import TestClient, TestServer

from ledger_bot.main import build_health_app


def test_health_endpoint(db):
    async def go():
        async with TestClient(TestServer(build_health_app())) as c:
            for path in ("/health", "/"):
                r = await c.get(path)
                assert r.status == 200 and await r.text() == "ok"
            assert (await c.head("/health")).status == 200  # UptimeRobot может слать HEAD
    asyncio.run(go())
