"""The irrigation controller, through the Tuya cloud API (a "smart irrigation timer", category sfkzq).

Every request is signed with HMAC-SHA256 over the client id, the access token, a timestamp, a nonce and a description of the request
(method, hash of the body, path). The access token is fetched when needed and kept until shortly before it expires. The device's data
points used here: `switch` (valve on or off), `countdown` (seconds the run lasts), `weather_delay` (`cancel`, `24h`, `48h`, `72h`: the
device skips its own schedule for that long), `battery_percentage` and `work_state` (`auto`, `manual`, `idle`).
"""

import hashlib
import hmac
import json
import logging
import time
import uuid

import httpx

from ..config import Config

log = logging.getLogger(__name__)

TOKEN_PATH = "/v1.0/token?grant_type=1"
TOKEN_INVALID = {1010, 1011}                # Tuya's "token invalid" and "token expired" codes
DELAYS = ("24h", "48h", "72h", "cancel")    # the device's weather_delay range
MAX_MINUTES = 60                            # the longest run asked for from a button


class IrrigationError(Exception):
    """The controller could not be read or told something. The message is safe to log (no secrets, no raw response)."""


def sign(client_id: str, secret: str, token: str, timestamp: str, nonce: str, method: str, path: str, body: bytes) -> str:
    """Tuya's request signature: HMAC-SHA256, upper-case hex, of client_id + token + timestamp + nonce + the string describing the request."""
    to_sign = f"{method}\n{hashlib.sha256(body).hexdigest()}\n\n{path}"
    return hmac.new(secret.encode(), (client_id + token + timestamp + nonce + to_sign).encode(), hashlib.sha256).hexdigest().upper()


class Tuya:
    def __init__(self, cfg: Config, transport=None):
        self.device = cfg.tuya_device_id
        self.client_id, self.secret = cfg.tuya_client_id, cfg.tuya_client_secret
        self.http = httpx.AsyncClient(base_url=cfg.tuya_base_url, timeout=20, transport=transport)
        self.token = ""
        self.token_expires = 0.0

    async def close(self):
        await self.http.aclose()

    async def _send(self, method: str, path: str, payload: dict | None, token: str):
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode() if payload is not None else b""
        timestamp, nonce = str(int(time.time() * 1000)), uuid.uuid4().hex
        headers = {"client_id": self.client_id, "sign_method": "HMAC-SHA256", "t": timestamp, "nonce": nonce,
                   "sign": sign(self.client_id, self.secret, token, timestamp, nonce, method, path, body)}
        if token:
            headers["access_token"] = token
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = await self.http.request(method, path, content=body if payload is not None else None, headers=headers)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as e:   # the URL and body stay out of the message
            raise IrrigationError(f"Tuya request failed ({type(e).__name__})") from e

    async def _authenticate(self):
        data = await self._send("GET", TOKEN_PATH, None, "")
        if not data.get("success"):
            raise IrrigationError(f"Tuya refused the credentials (code {data.get('code')}: {data.get('msg')})")
        result = data["result"]
        self.token = result["access_token"]
        self.token_expires = time.time() + max(60, int(result.get("expire_time", 7200)) - 60)

    async def request(self, method: str, path: str, payload: dict | None = None, retry: bool = True):
        """The `result` of a signed request. An expired token is replaced and the request sent once more."""
        if not self.token or time.time() >= self.token_expires:
            await self._authenticate()
        data = await self._send(method, path, payload, self.token)
        if not data.get("success"):
            if retry and data.get("code") in TOKEN_INVALID:
                self.token = ""
                return await self.request(method, path, payload, retry=False)
            raise IrrigationError(f"Tuya error {data.get('code')}: {data.get('msg')}")
        return data.get("result")

    async def status(self) -> dict:
        """{code: value} of the device's status points (switch, battery_percentage, weather_delay, countdown, work_state, ...)."""
        result = await self.request("GET", f"/v1.0/devices/{self.device}/status")
        return {item["code"]: item["value"] for item in result or [] if "code" in item}

    async def command(self, *commands: tuple[str, object]):
        """Send one or more (code, value) commands together."""
        await self.request("POST", f"/v1.0/devices/{self.device}/commands",
                           {"commands": [{"code": code, "value": value} for code, value in commands]})

    async def water(self, minutes: int):
        """Open the valve for this many minutes: the run's length and the switch, in one command."""
        if not 1 <= minutes <= MAX_MINUTES:
            raise IrrigationError(f"A run must be 1 to {MAX_MINUTES} minutes")
        await self.command(("countdown", minutes * 60), ("switch", True))

    async def delay(self, duration: str):
        if duration not in DELAYS:
            raise IrrigationError(f"The delay must be one of {', '.join(DELAYS)}")
        await self.command(("weather_delay", duration))

    async def set_switch(self, on: bool):
        await self.command(("switch", bool(on)))
