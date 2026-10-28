"""The sensor's own web server, for the live reading: GET http://<device>/measures/current on the local network (AIRGRADIENT_LOCAL_URL,
e.g. http://airgradient_d83bda1c1e08.local). Nothing is asked of the cloud for it, and it is as fresh as the device's last measurement.
The device only knows "now", so history, charts and the archive still come from the cloud.

A `.local` name is looked up with mDNS by this process itself (the zeroconf package), so the host needs no avahi or nss-mdns; on Docker
that needs the host network (multicast does not cross the default bridge), or a plain IP address in the URL instead. Any other name
goes through the system resolver. The reading is returned in the cloud's field names, so the rest of the code does not care where it came
from: AirGradient's own correction (`pm02Compensated`) is the cloud's `pm02_corrected`."""

import asyncio
import ipaddress
import logging
import socket
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
log = logging.getLogger(__name__)

TIMEOUT = 3.0           # seconds for the lookup and for the device to answer: a sensor that is off must not slow the answers
RETRY_SECONDS = 300     # after a failure the device is left alone this long (the cloud answers meanwhile)
ADDRESS_SECONDS = 600   # a looked-up address is kept this long; a failure forgets it
FIELDS = {"pm01": "pm01", "pm02": "pm02", "pm02Compensated": "pm02_corrected", "pm10": "pm10", "rco2": "rco2",
          "tvocIndex": "tvocIndex", "noxIndex": "noxIndex", "atmp": "atmp", "atmpCompensated": "atmp_corrected", "rhum": "rhum",
          "rhumCompensated": "rhum_corrected"}   # the device's field -> the cloud's (metrics.STORED tries these)


async def mdns_address(host: str, timeout: float = TIMEOUT) -> str | None:
    """The IPv4 address a `.local` name has on the network, asked of the network by multicast; None when nothing answers."""
    from zeroconf import DNSOutgoing, DNSQuestion   # only a `.local` name needs it: it is not loaded for anything else
    from zeroconf.asyncio import AsyncZeroconf
    from zeroconf.const import _CLASS_IN, _FLAGS_QR_QUERY, _TYPE_A
    name = host.rstrip(".").lower() + "."
    aiozc = AsyncZeroconf()
    try:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            question = DNSOutgoing(_FLAGS_QR_QUERY)
            question.add_question(DNSQuestion(name, _TYPE_A, _CLASS_IN))
            aiozc.zeroconf.async_send(question)
            await asyncio.sleep(0.3)
            for record in aiozc.zeroconf.cache.async_all_by_details(name, _TYPE_A, _CLASS_IN):
                return socket.inet_ntoa(record.address)
        return None
    finally:
        await aiozc.async_close()


async def address_of(host: str) -> str | None:
    """An IP address (as given), a `.local` name (mDNS) or any other name (the system's resolver)."""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return host
    except ValueError:
        pass
    if host.lower().rstrip(".").endswith(".local"):
        return await mdns_address(host)
    found = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return found[0][4][0] if found else None


def cloud_shaped(data: dict, now: datetime) -> dict | None:
    """The device's reply as a cloud reading, stamped with the time it was read; None when it holds none of what the bot uses."""
    row = {cloud: data[local] for local, cloud in FIELDS.items() if isinstance(data.get(local), int | float)}
    if not row:
        return None
    return {**row, "timestamp": now.astimezone(UTC).isoformat().replace("+00:00", "Z")}


class LocalSensor:
    def __init__(self, url: str, transport=None, resolve=address_of, clock=time.time):
        url = url.strip().rstrip("/")
        self.base = url if "//" in url else f"http://{url}"
        parts = urlsplit(self.base)
        self.host, self.port = parts.hostname or "", parts.port
        self.client = httpx.AsyncClient(timeout=TIMEOUT, transport=transport)
        self.resolve, self.clock = resolve, clock
        self.address: tuple[str, float] | None = None   # (IP, when looked up)
        self.retry_at = 0.0
        self.working: bool | None = None                # for the log: say so when it changes
        self.serial: str | None = None                  # the sensor's serial number, from its last reply

    def describe(self) -> str:
        return f"{self.host}" + (f":{self.port}" if self.port else "")

    async def _address(self) -> str:
        if self.address is None or self.clock() - self.address[1] > ADDRESS_SECONDS:
            found = await asyncio.wait_for(self.resolve(self.host), TIMEOUT + 1)
            if not found:
                raise OSError(f"{self.host} was not found on the network")
            self.address = (found, self.clock())
        return self.address[0]

    async def read(self) -> dict | None:
        """The live reading in the cloud's field names, or None: not configured to answer just now (after a failure, for RETRY_SECONDS),
        unreachable, or no reading in what it said. Never raises."""
        if self.clock() < self.retry_at:
            return None
        try:
            ip = await self._address()
            host = f"[{ip}]" if ":" in ip else ip
            res = await self.client.get(f"http://{host}{':' + str(self.port) if self.port else ''}/measures/current",
                                        headers={"Host": self.host})
            res.raise_for_status()
            data = res.json()
            row = cloud_shaped(data, datetime.now(UTC))
            if row is None:
                raise ValueError("no readings in the reply")
            self.serial = data.get("serialno") or self.serial
        except Exception as e:
            self.address, self.retry_at = None, self.clock() + RETRY_SECONDS
            if self.working is not False:
                log.warning("AirGradient: the sensor's own server (%s) did not answer (%s: %s); using the cloud, trying again in %d min",
                            self.describe(), type(e).__name__, e, RETRY_SECONDS // 60)
            self.working = False
            return None
        self.working = True
        return row

    async def close(self):
        await self.client.aclose()
