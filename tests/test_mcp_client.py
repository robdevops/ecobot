"""The servers' test client (tests/mcp_client.py): the real servers in memory, read and called through the SDK's client."""

import asyncio
import time

from lib.alert_log import AlertLog
from lib.mcp_server import ALERTS_URI, FEED_URI
from lib.service.context import Context
from tests.fakes import TZ


async def test_ecobot_mcp_in_memory_lists_calls_reads_and_announces_changes(serve_mcp, tmp_path):
    alerts = AlertLog(tmp_path / "a.sqlite", TZ)
    mcp = await serve_mcp(Context(tz=TZ), alerts, sources=["Ecowitt"])
    try:
        assert "about" in await mcp.tools() and {ALERTS_URI, FEED_URI} <= set(await mcp.resources())
        assert any(t.startswith(ALERTS_URI) for t in await mcp.templates())
        assert "sources" in await mcp.read("ecobot://station")
        publishing = asyncio.create_task(mcp.server.watch_alerts())
        async with mcp.listen(ALERTS_URI) as updates:
            alerts.append("gusts", "💨 A gust of 72 km/h", "Gusts", expires=time.time() + 3600)
            await asyncio.wait_for(updates.__anext__(), 5)
        got = await mcp.alerts(ALERTS_URI)
        assert [a["text"] for a in got["alerts"]] == ["💨 A gust of 72 km/h"]
        publishing.cancel()
    finally:
        alerts.close()


async def test_irrigation_mcp_in_memory_is_the_real_server_over_a_fake_controller(serve_irrigation_mcp, tmp_path):
    mcp = await serve_irrigation_mcp(tmp_path / "irrigation")
    status = await mcp.call("irrigation_status")
    assert status["switch"] is False and status["paused"] is False
    assert mcp.server.device.commands == [] and "irrigation://feed" in await mcp.resources()
