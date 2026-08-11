"""Sony, via the Audio Control API (JSON-RPC over HTTP) on port 10000.

UNTESTED against real hardware. Commands below come from Sony's own Audio
Control API reference, cross-checked against openHAB's sonyaudio binding and
node-red-contrib-sony-audio.

Coverage is thin on receivers — STR-DN1080 is the only one Sony ever listed —
but real on the HT-series soundbars (HT-A7000, HT-A9, HT-ZF9, HT-ST5000,
HT-CT800), which is exactly the kind of hardware that sits under a TV running
SmartTube. No pairing or pre-shared key is needed for these methods.

Failure mode if a command is wrong: the device returns **HTTP 200 with an
`error` key in the body**. Treating 200 as success would make failures
invisible, so every response is checked for `error` and the code is surfaced.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .base import AvrClient

log = logging.getLogger("smarttube-playlist.avr.sony")

REQUEST_TIMEOUT = 4.0

# Sony error codes worth naming: 40800 = target not controllable (usually
# standby), 40801 = value out of range.
ERR_NOT_CONTROLLABLE = 40800


class SonyClient(AvrClient):
    # "Home Audio" (receivers and soundbars). Portable speakers use 54480; an
    # explicit port in config.json is honoured for those.
    DEFAULT_PORT = 10000
    TESTED_ON_HARDWARE = False

    async def _rpc(self, service: str, method: str, params: Any, version: str = "1.1") -> Optional[dict]:
        """POST one JSON-RPC call. Returns the parsed body, or None on failure.

        `version` is per-method and mandatory — a wrong one errors rather than
        falling back. `id` must be an integer in 1..2147483647; 0 is explicitly
        forbidden by the API.
        """
        url = f"http://{self.host}:{self.port}/sony/{service}"
        payload = {"method": method, "id": 1, "params": params, "version": version}
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                body = resp.json()
        except Exception as e:
            log.warning("Sony %s at %s failed: %s", method, self.host, e)
            return None
        if "error" in body:
            code = body["error"][0] if isinstance(body["error"], list) else body["error"]
            if code == ERR_NOT_CONTROLLABLE:
                log.warning(
                    "Sony at %s refused %s — the device is probably in standby",
                    self.host, method,
                )
            else:
                log.warning("Sony %s returned error %s", method, body["error"])
            return None
        return body

    async def volume_up(self) -> None:
        # The volume value is a STRING. {"volume": 1} is rejected; "+1" is
        # correct (openHAB SonyAudioHandler.java: change = "+1" / "-1").
        async with self._lock:
            await self._rpc("audio", "setAudioVolume", [{"volume": "+1", "output": ""}])

    async def volume_down(self) -> None:
        async with self._lock:
            await self._rpc("audio", "setAudioVolume", [{"volume": "-1", "output": ""}])

    async def mute_toggle(self) -> Optional[bool]:
        async with self._lock:
            body = await self._rpc("audio", "getVolumeInformation", [{"output": ""}])
            if not body:
                return None
            # getVolumeInformation's result is DOUBLE-nested: result[0][0],
            # unlike getPowerStatus which is result[0].
            try:
                info = body["result"][0][0]
            except (KeyError, IndexError, TypeError):
                log.warning("Sony volume info had an unexpected shape: %r", body)
                return None
            mute = info.get("mute", "")
            if mute == "":
                log.warning("Sony device at %s reports no mute support", self.host)
                return None
            if mute == "toggle":
                # Some devices only support toggling, not absolute set.
                ok = await self._rpc("audio", "setAudioMute", [{"mute": "toggle", "output": ""}])
                return None if ok is None else True
            is_muted = mute == "on"
            ok = await self._rpc(
                "audio", "setAudioMute",
                [{"mute": "off" if is_muted else "on", "output": ""}],
            )
            return None if ok is None else (not is_muted)

    async def ping(self) -> bool:
        # Note params is [] here, not [{}] — the system service differs from
        # the audio service in that respect.
        async with self._lock:
            return await self._rpc("system", "getPowerStatus", []) is not None
