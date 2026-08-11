"""Denon and Marantz, via the legacy Telnet-style protocol on TCP port 23.

The only backend in this package verified against real hardware.

Why not HEOS: the HEOS JSON protocol is more capable (sources, zones, groups,
playback control) but the legacy protocol is dead simple for volume + mute and
is supported by essentially every Denon and Marantz since the early 2010s. We
only need three buttons.

Marantz is the same firmware lineage and speaks the same commands.

Protocol summary (all commands ASCII, terminated by a bare CR):
    MVUP        master volume up 1 step
    MVDOWN      master volume down 1 step
    MV?         query current master volume (response: MV<NN> e.g. MV52)
    MUON        mute on
    MUOFF       mute off
    MU?         query mute state (response: MUON or MUOFF)

The receiver responds asynchronously over the same socket. Set commands are
fire-and-forget; query commands need a brief read.

Failure mode if a command is wrong: silent no-op. Nothing comes back, nothing
is logged by the receiver. That is why the read below is best-effort.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .base import AvrClient

log = logging.getLogger("smarttube-playlist.avr.denon")

READ_TIMEOUT = 1.0


class DenonClient(AvrClient):
    DEFAULT_PORT = 23
    TESTED_ON_HARDWARE = True

    async def _send_and_read(self, cmd: str) -> str:
        """Open a connection, send one command, read the response if any."""
        async with self._connection() as (reader, writer):
            writer.write((cmd + "\r").encode("ascii"))
            await writer.drain()
            # Best-effort read — set commands echo back the new state, query
            # commands return the answer. Either way one line is enough. If
            # nothing arrives within READ_TIMEOUT, treat it as "command
            # accepted, receiver is silent", which is also normal.
            try:
                data = await asyncio.wait_for(
                    reader.readuntil(b"\r"), timeout=READ_TIMEOUT,
                )
                return data.decode("ascii", errors="replace").strip()
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                return ""

    async def volume_up(self) -> None:
        async with self._lock:
            await self._send_and_read("MVUP")

    async def volume_down(self) -> None:
        async with self._lock:
            await self._send_and_read("MVDOWN")

    async def mute_toggle(self) -> Optional[bool]:
        """The protocol has no atomic toggle: query, then send the opposite."""
        async with self._lock:
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
        try:
            async with self._lock:
                resp = await self._send_and_read("PW?")
            return "PWON" in resp or "PWSTANDBY" in resp
        except Exception:
            return False
