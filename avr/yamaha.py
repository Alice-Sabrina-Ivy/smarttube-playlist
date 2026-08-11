"""Yamaha, via YNCA on TCP port 50000.

UNTESTED against real hardware. Every command below was read out of the source
of https://github.com/mvdwetering/ynca — the library Home Assistant's
yamaha_ynca integration is built on — and cross-checked against the real-device
capture logs committed in that repo. Citations are inline so a future
maintainer can re-verify rather than trust this comment.

Covers RX-V, RX-A (Aventage), TSR, HTR and CX-A lines from roughly 2010 onward.

Frame: "@{SUBUNIT}:{FUNCTION}={VALUE}" terminated by CRLF.
    Source: src/ynca/protocol.py
        put(): self._send_queue.put(f"@{subunit}:{funcname}={parameter}")
        get(): self.put(subunit, funcname, "?")
    The terminator is CRLF, NOT the bare CR that Denon uses: YncaProtocol
    subclasses serial.threaded.LineReader without overriding TERMINATOR, whose
    default is b"\\r\\n". docs/PROTOCOL.md states "Commands conclude with
    Carriage Return + Line Feed".

Failure mode if a command is wrong: the receiver replies with a bare
"@UNDEFINED" line, which is detectable and logged below. A valid command sent
to a zone in standby returns "@RESTRICTED" instead — that is the "your receiver
is off" case, logged distinctly because it is user-fixable.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .base import AvrClient

log = logging.getLogger("smarttube-playlist.avr.yamaha")

# YNCA requires >=100ms between commands.
#   Source: protocol.py COMMAND_SPACING = 0.1, "YNCA spec specifies that there
#   should be at least 100 milliseconds between commands".
COMMAND_SPACING = 0.1
# Generous: the socket also carries unsolicited status pushes, so a reply can
# be several lines back.
READ_TIMEOUT = 2.0

VOL_UP = b"@MAIN:VOL=Up\r\n"        # subunits/zone.py do_vol_up(), value="Up"
VOL_DOWN = b"@MAIN:VOL=Down\r\n"    # subunits/zone.py do_vol_down()
MUTE_QUERY = b"@MAIN:MUTE=?\r\n"    # docs/all_commands_ever_seen.txt
MUTE_ON = b"@MAIN:MUTE=On\r\n"      # enums.py Mute.ON = "On"
MUTE_OFF = b"@MAIN:MUTE=Off\r\n"    # enums.py Mute.OFF = "Off"
# protocol.py _send_handler: 'message = "@SYS:MODELNAME=?"  # Use MODELNAME as
# keep-alive, supported by all'. Doubles as our wake-up dummy, below.
PING = b"@SYS:MODELNAME=?\r\n"


class YamahaClient(AvrClient):
    DEFAULT_PORT = 50000
    TESTED_ON_HARDWARE = False

    async def _exchange(self, command: bytes, want_prefix: Optional[bytes] = None) -> str:
        """Wake the receiver, send one command, optionally read a reply.

        The wake dummy is not optional. protocol.py connection_made() says:
        "When the device is in low power mode the first command is to wake up
        and gets lost", and the library sends two keep-alives on connect. A
        persistent-connection library pays that once; our short-lived design
        pays it on every command. Skipping it yields "works only when the
        receiver was recently used" — the worst possible remote-debug symptom.
        """
        async with self._connection() as (reader, writer):
            writer.write(PING)
            await writer.drain()
            await asyncio.sleep(COMMAND_SPACING)
            writer.write(command)
            await writer.drain()
            if want_prefix is None:
                return ""
            # Read until the line we asked for. The socket carries unsolicited
            # pushes plus the MODELNAME reply to our own dummy, so a single
            # readline would usually return the wrong thing.
            deadline = asyncio.get_running_loop().time() + READ_TIMEOUT
            while asyncio.get_running_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(
                        reader.readuntil(b"\r\n"), timeout=READ_TIMEOUT,
                    )
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    return ""
                line = raw.decode("ascii", errors="replace").strip()
                if line.startswith("@UNDEFINED"):
                    log.warning("Yamaha rejected %r as undefined", command)
                    return ""
                if line.startswith("@RESTRICTED"):
                    log.warning(
                        "Yamaha refused %r — zone is in standby or the "
                        "function is unavailable right now", command,
                    )
                    return ""
                if line.startswith(want_prefix.decode("ascii")):
                    return line
            return ""

    async def volume_up(self) -> None:
        async with self._lock:
            await self._exchange(VOL_UP)

    async def volume_down(self) -> None:
        async with self._lock:
            await self._exchange(VOL_DOWN)

    async def mute_toggle(self) -> Optional[bool]:
        async with self._lock:
            try:
                line = await self._exchange(MUTE_QUERY, want_prefix=b"@MAIN:MUTE=")
            except Exception:
                log.warning("Yamaha mute query failed", exc_info=True)
                return None
            if not line:
                return None
            value = line.split("=", 1)[1] if "=" in line else ""
            # Mute is FOUR-valued, not two: On, Off, "Att -20 dB", "Att -40 dB"
            # (enums.py class Mute). Anything that isn't exactly Off counts as
            # muted — a `== "On"` check silently fails to unmute a receiver
            # sitting in attenuated mute.
            is_muted = value != "Off"
            try:
                await asyncio.sleep(COMMAND_SPACING)
                await self._exchange(MUTE_OFF if is_muted else MUTE_ON)
            except Exception:
                log.warning("Yamaha mute set failed", exc_info=True)
                return None
            return not is_muted

    async def ping(self) -> bool:
        try:
            async with self._lock:
                line = await self._exchange(PING, want_prefix=b"@SYS:MODELNAME=")
            return bool(line)
        except Exception:
            return False
