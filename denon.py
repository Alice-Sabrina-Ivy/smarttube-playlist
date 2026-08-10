"""Denon AVR control via the legacy Telnet-style protocol on TCP port 23.

Used as the volume backend when `DENON_HOST` is configured. Not loaded
otherwise — without it there is no volume control at all.

Why not HEOS: the HEOS JSON protocol is more capable (sources, zones,
groups, playback control) but the legacy protocol is dead simple for
volume + mute and is supported by essentially every Denon and Marantz
AVR since the early 2010s. We only need three buttons.

Protocol summary (all commands ASCII, terminated by `\\r`):
    MVUP        master volume up 1 step
    MVDOWN      master volume down 1 step
    MV?         query current master volume (response: MV<NN> e.g. MV52)
    MUON        mute on
    MUOFF       mute off
    MU?         query mute state (response: MUON or MUOFF)

The AVR responds asynchronously over the same socket. Each command is
fire-and-forget for set commands; query commands need a brief read.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

log = logging.getLogger("smarttube-playlist.denon")

DENON_PORT = 23
# Most commands complete in <100ms over LAN, but the AVR can lag during
# input switches / power transitions. 3s is generous.
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 1.0


class DenonClient:
    """Stateless thin wrapper around the legacy Denon Telnet protocol.

    Each call opens a fresh TCP connection, sends the command(s), reads
    any response, and closes. We don't hold a persistent socket because
    the AVR aggressively closes idle connections (~30s) and the
    reconnect overhead is irrelevant for volume buttons clicked once
    every few seconds at most.
    """

    def __init__(self, host: str, port: int = DENON_PORT):
        self.host = host
        self.port = port

    async def _send_and_read(self, cmd: str) -> str:
        """Open a connection, send one command, read response (if any)."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=CONNECT_TIMEOUT,
            )
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("Denon connect %s:%d failed: %s", self.host, self.port, e)
            raise
        try:
            writer.write((cmd + "\r").encode("ascii"))
            await writer.drain()
            # Best-effort read — set commands echo back the new state,
            # query commands return the answer. Either way one line is
            # enough for our purposes. If nothing comes in READ_TIMEOUT,
            # treat it as "command accepted, AVR is silent" (also
            # acceptable).
            try:
                data = await asyncio.wait_for(
                    reader.readuntil(b"\r"), timeout=READ_TIMEOUT,
                )
                return data.decode("ascii", errors="replace").strip()
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                return ""
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def volume_up(self) -> None:
        """One step louder. The AVR's step size is configured on-device
        (typically 0.5 or 1.0 dB); we don't override it."""
        await self._send_and_read("MVUP")

    async def volume_down(self) -> None:
        await self._send_and_read("MVDOWN")

    async def mute_toggle(self) -> Optional[bool]:
        """Flip mute state. Returns the new state (True=muted) or None
        on failure. The protocol has no atomic toggle, so we query first
        and send the opposite."""
        try:
            current = await self._send_and_read("MU?")
        except Exception:
            log.warning("Denon mute query failed", exc_info=True)
            return None
        # Response is MUON or MUOFF (sometimes with extra status lines).
        is_muted = "MUON" in current and "MUOFF" not in current
        cmd = "MUOFF" if is_muted else "MUON"
        try:
            await self._send_and_read(cmd)
        except Exception:
            log.warning("Denon mute set %s failed", cmd, exc_info=True)
            return None
        return not is_muted

    async def ping(self) -> bool:
        """Sanity check — query power state. Returns True if reachable."""
        try:
            resp = await self._send_and_read("PW?")
            return "PWON" in resp or "PWSTANDBY" in resp
        except Exception:
            return False
