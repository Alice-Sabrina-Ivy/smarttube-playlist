"""Onkyo, Integra, and Pioneer (2016+), via eISCP on TCP port 60128.

UNTESTED against real hardware. The packet framing and command constants below
were read from https://github.com/miracle2k/onkyo-eiscp and cross-checked
against aioonkyo (the library Home Assistant ships today). Citations inline.

Integra is Onkyo's custom-install brand running the same firmware — the
protocol's own name is "Integra Serial Control Protocol". Pioneer models from
2016 onward (post-acquisition) use the same stack; **earlier Pioneer models
speak a different protocol on port 8102 and are deliberately not supported**,
because the mute *query* command for that protocol could not be verified and
guessing it would produce a mute button that half-works.

Failure mode if a command is wrong: the receiver SILENTLY DISCARDS the packet.
The TCP write succeeds, nothing comes back, the button does nothing, and there
is no error anywhere. That is why set commands here do a best-effort read and
log when no acknowledgement arrives — it is the only way an untested backend
can tell you it isn't working.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Optional

from .base import AvrClient

log = logging.getLogger("smarttube-playlist.avr.onkyo")

# onkyo-eiscp eiscp/core.py: ONKYO_PORT = 60128
EISCP_PORT = 60128
# onkyo-eiscp's own comment on its read loop: "The protocol docs claim that a
# response should arrive within 50ms... In my tests, however, the interval
# needed to be at least 200ms before I managed to see any response, and only
# after 300ms reproducably, so use a generous timeout."
READ_TIMEOUT = 2.0


def build_packet(command: str) -> bytes:
    """Wrap an ISCP command in its 16-byte eISCP header.

    Header, from onkyo-eiscp eiscp/core.py eISCPPacket.__init__:
        struct.pack('! 4s I I b 3s', b'ISCP', 16, len(iscp_message), 0x01,
                    b'\\x00\\x00\\x00')
    Payload, from ISCPMessage.__str__:
        '!1{}\\r'  -- "!" start character, "1" = unit type "receiver"

    We terminate with CRLF rather than bare CR, matching aioonkyo
    (ISCPInstructionData.end = b"\\r\\n"); the spec allows CR, LF or CRLF.

    CRITICAL: data_size counts the ENTIRE payload — the "!", the "1", the
    command, and the terminator. An off-by-one here makes the receiver drop
    the packet with no feedback whatsoever. Pinned by
    test_onkyo_data_size_counts_the_whole_payload.
    """
    payload = b"!1" + command.encode("ascii") + b"\r\n"
    header = struct.pack("! 4s I I B 3s", b"ISCP", 16, len(payload), 1, b"\x00\x00\x00")
    return header + payload


# commands.py 'MVL': ('UP', {'name': 'level-up'}) / ('DOWN', {'name': 'level-down'})
VOL_UP = build_packet("MVLUP")
VOL_DOWN = build_packet("MVLDOWN")
# commands.py 'AMT': ('QSTN', 'gets the Audio Muting State')
MUTE_QUERY = build_packet("AMTQSTN")
# aioonkyo parameter.py MutingParam.ON = "01" / OFF = "00"
MUTE_ON = build_packet("AMT01")
MUTE_OFF = build_packet("AMT00")
# Reply is PWR00 (standby) or PWR01 (on).
PING = build_packet("PWRQSTN")

# An atomic toggle does exist (commands.py 'AMT': ('TG', {'name': 'toggle'}))
# but aioonkyo deliberately omits it, and query-then-opposite keeps parity with
# the other backends, so we do the same.


class OnkyoClient(AvrClient):
    DEFAULT_PORT = EISCP_PORT
    TESTED_ON_HARDWARE = False

    async def _read_packet(self, reader) -> str:
        """Read exactly one eISCP packet and return its ISCP payload.

        Never assume one TCP read equals one packet: take the 16-byte header
        first, then exactly data_size more bytes.
        """
        header = await asyncio.wait_for(reader.readexactly(16), timeout=READ_TIMEOUT)
        magic, hdr_size, data_size, _version, _pad = struct.unpack("! 4s I I B 3s", header)
        if magic != b"ISCP":
            raise ValueError(f"not an eISCP packet: {magic!r}")
        body = await asyncio.wait_for(
            reader.readexactly(data_size), timeout=READ_TIMEOUT,
        )
        text = body.decode("ascii", errors="replace")
        # Strip the "!1" prefix, the end-of-transmission 0x1A some models
        # append, and any trailing CR/LF.
        return text.lstrip("!1").rstrip("\x1a\r\n")

    async def _exchange(self, packet: bytes, want_prefix: Optional[str] = None) -> str:
        async with self._connection() as (reader, writer):
            writer.write(packet)
            await writer.drain()
            deadline = asyncio.get_running_loop().time() + READ_TIMEOUT
            got_anything = False
            while asyncio.get_running_loop().time() < deadline:
                try:
                    msg = await self._read_packet(reader)
                except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
                    break
                got_anything = True
                if want_prefix is None or msg.startswith(want_prefix):
                    return msg
                # Unsolicited status push — keep reading for the one we want.
            if want_prefix is not None and not got_anything:
                log.warning(
                    "no acknowledgement from Onkyo/Pioneer at %s for %r — if "
                    "the buttons do nothing, the command set may be wrong for "
                    "this model; please report it", self.host, packet[16:],
                )
            return ""

    async def volume_up(self) -> None:
        async with self._lock:
            await self._exchange(VOL_UP, want_prefix="MVL")

    async def volume_down(self) -> None:
        async with self._lock:
            await self._exchange(VOL_DOWN, want_prefix="MVL")

    async def mute_toggle(self) -> Optional[bool]:
        async with self._lock:
            try:
                msg = await self._exchange(MUTE_QUERY, want_prefix="AMT")
            except Exception:
                log.warning("Onkyo mute query failed", exc_info=True)
                return None
            if not msg:
                return None
            # AMT00 = off, AMT01 = on.
            is_muted = msg[3:].startswith("01")
            try:
                await self._exchange(MUTE_OFF if is_muted else MUTE_ON)
            except Exception:
                log.warning("Onkyo mute set failed", exc_info=True)
                return None
            return not is_muted

    async def ping(self) -> bool:
        try:
            async with self._lock:
                return bool(await self._exchange(PING, want_prefix="PWR"))
        except Exception:
            return False
