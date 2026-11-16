"""One TLS context, made once and shared by every HTTP client the programs make (lib/tls.py): httpx would otherwise load certifi's
certificates afresh for each client, about 35 ms apiece on the event loop before the service listens."""

import re
import ssl
from pathlib import Path

from lib.tls import ssl_context

LIB = Path(__file__).resolve().parent.parent / "lib"


def test_the_context_is_made_once_and_checks_certificates_as_httpx_would():
    ctx = ssl_context()
    assert ssl_context() is ctx
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_every_http_client_is_given_the_shared_context():
    made = {str(p.relative_to(LIB)): re.findall(r"httpx\.AsyncClient\((.*?)\)\n", p.read_text(), re.S) for p in LIB.rglob("*.py")}
    made = {k: v for k, v in made.items() if v}
    assert len(made) >= 7                                     # the station, Home Assistant, AirGradient (both), forecast, pollen, Tuya
    assert all("verify=ssl_context()" in c for calls in made.values() for c in calls), made


async def test_a_client_made_without_a_test_transport_connects_with_it(tmp_path):
    from lib.ecowitt.homeassistant import HomeAssistant
    from lib.pollen.source import Pollen
    from tests.fakes import config
    clients = [Pollen(config(tmp_path)).client, HomeAssistant("http://ha", "t").client]
    try:
        assert all(c._transport._pool._ssl_context is ssl_context() for c in clients)
    finally:
        for c in clients:
            await c.aclose()
