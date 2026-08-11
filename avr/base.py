"""Shared plumbing for AV receiver volume backends.

Deliberately thin. The four protocols here have almost nothing in common below
the four-method interface: Denon is CR-terminated ASCII, Yamaha is
CRLF-terminated ASCII with a wake-up dance, Onkyo wraps ASCII in a binary
header, and Sony is HTTP JSON. A "shared line protocol" base would need an
escape hatch per backend and be worse than the small duplication.

So this module owns exactly two things: the interface, and opening/closing a
TCP connection with a timeout.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import ClassVar, Optional

log = logging.getLogger("smarttube-playlist.avr")

# Most commands complete in <100ms over LAN, but a receiver can lag badly
# during input switches and power transitions.
CONNECT_TIMEOUT = 3.0


class AvrClient(ABC):
    """One receiver. Four operations. Nothing else.

    Implementations open a short-lived connection per command rather than
    holding a socket. Receivers close idle connections aggressively (Denon at
    ~30s, Yamaha at ~40s), and the reconnect cost is irrelevant for buttons
    pressed a few times a minute. It also keeps us off the wire entirely when
    nobody is touching the volume, which matters for Yamaha — see yamaha.py.
    """

    DEFAULT_PORT: ClassVar[int]
    #: False for backends built from documentation but never run against the
    #: real hardware. Surfaced in the UI so nobody is misled.
    TESTED_ON_HARDWARE: ClassVar[bool] = False

    def __init__(self, host: str, port: Optional[int] = None):
        self.host = host
        self.port = port or self.DEFAULT_PORT
        # Guests mash volume buttons. Yamaha additionally permits only one
        # control connection at a time (a receiver-side limit), so overlapping
        # commands there fail outright rather than interleaving. Cheap
        # insurance everywhere.
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def _connection(self):
        """Open a connection, guarantee it closes."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=CONNECT_TIMEOUT,
            )
        except (OSError, asyncio.TimeoutError) as e:
            log.warning(
                "%s connect %s:%d failed: %s",
                type(self).__name__, self.host, self.port, e,
            )
            raise
        try:
            yield reader, writer
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @abstractmethod
    async def volume_up(self) -> None:
        """One step louder. Step size is configured on the receiver."""

    @abstractmethod
    async def volume_down(self) -> None:
        """One step quieter."""

    @abstractmethod
    async def mute_toggle(self) -> Optional[bool]:
        """Flip mute. Returns the new state (True=muted), or None on failure."""

    @abstractmethod
    async def ping(self) -> bool:
        """Cheap reachability check. True if the receiver answered."""
