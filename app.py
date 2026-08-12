"""SmartTube Playlist — fire YouTube videos at SmartTube on a Google TV / Android TV.

LAN-only web service. Uses the Android TV Remote v2 protocol via
tronikos/androidtvremote2.

v1: real queue/playlist with best-effort auto-advance. See README for the
model and known limitations.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from androidtvremote2 import (
    AndroidTVRemote,
    CannotConnect,
    ConnectionClosed,
    InvalidAuth,
)
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import httpx

from events import Broadcaster
from lounge import LoungeMonitor, LoungeObservation
from metadata import Metadata, fetch_metadata
from playlist import QueueController, QueueItem, make_item
from ratelimit import RateLimiter

# ── config ───────────────────────────────────────────────────────────────────
# Log timestamps are UTC: the container carries no timezone, so asctime renders
# in UTC. Set a TZ environment variable on the container if you'd rather read
# them in local time. Nothing user-facing depends on this — every timestamp we
# serialise is UTC with an explicit offset, so each browser renders times in its
# own local zone regardless of what the container clock is set to.
_VALID_LOG_LEVELS = frozenset(
    ("CRITICAL", "FATAL", "ERROR", "WARN", "WARNING", "INFO", "DEBUG", "NOTSET")
)


def _resolve_log_level(raw: Optional[str]) -> str:
    """Normalise LOG_LEVEL, falling back to INFO on anything unrecognised.

    logging.basicConfig raises ValueError on an unknown level name, and its
    table is uppercase-only — so a perfectly reasonable `LOG_LEVEL=debug`
    used to kill the process at import, before any handler existed to report
    why. A misconfigured log level must never be fatal.
    """
    level = (raw or "").strip().upper()
    return level if level in _VALID_LOG_LEVELS else "INFO"


_RAW_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
_LOG_LEVEL = _resolve_log_level(_RAW_LOG_LEVEL)
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("smarttube-playlist")
if _resolve_log_level(_RAW_LOG_LEVEL) != (_RAW_LOG_LEVEL or "").strip().upper():
    log.warning(
        "LOG_LEVEL=%r is not a recognised level; using INFO", _RAW_LOG_LEVEL
    )

# Single source of version truth. release.py writes VERSION, the Dockerfile
# copies it into the image, and /api/status serves it — so a running container
# can always say what it is. Falls back rather than crashing if the file is
# missing, since a stale image is still more useful than a dead one.
def _read_version() -> str:
    try:
        return (Path(__file__).resolve().parent / "VERSION").read_text(
            encoding="utf-8"
        ).strip() or "unknown"
    except OSError:
        return "unknown"


VERSION = _read_version()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CERT_FILE = DATA_DIR / "cert.pem"
KEY_FILE = DATA_DIR / "key.pem"
CONFIG_FILE = DATA_DIR / "config.json"
LOUNGE_AUTH_FILE = DATA_DIR / "lounge.json"

# Operator escape hatch for re-pairing. There is deliberately no HTTP endpoint
# that clears credentials — that would hand any LAN client a one-shot denial of
# service — so resetting requires access to the container's configuration.
RESET_PAIRING = os.environ.get("RESET_PAIRING", "").strip().lower() in (
    "1", "true", "yes", "on",
)
# A container cannot rewrite its own environment, so RESET_PAIRING stays set
# across restarts. Without this marker the flag would wipe the pairing on every
# single boot, which is a worse outage than the hole it closes. The marker makes
# the reset one-shot; clearing the flag deletes it and re-arms the mechanism.
RESET_MARKER = DATA_DIR / ".reset_done"

# Extra Host values to trust, comma-separated — for reverse-proxy setups that
# terminate on a real domain name. See _host_header_is_trusted.
ALLOWED_HOSTS = frozenset(
    h.strip().lower()
    for h in os.environ.get("ALLOWED_HOSTS", "").split(",")
    if h.strip()
)

CLIENT_NAME = os.environ.get("CLIENT_NAME", "SmartTube Playlist")
SMARTTUBE_PACKAGE = os.environ.get("SMARTTUBE_PACKAGE", "org.smarttube.stable")
# Packages we treat as "screensavers" — they intercept and silently drop
# send_launch_app_command intents (verified empirically with dreamx on
# Google TV). We detect these on entry to tv_play and dismiss with a
# keypress before attempting the launch sequence.
SCREENSAVER_PACKAGES = frozenset(
    p.strip() for p in os.environ.get(
        "SCREENSAVER_PACKAGES",
        "com.google.android.apps.tv.dreamx,com.google.android.backdrop",
    ).split(",") if p.strip()
)
# Keycode used to dismiss a foreground screensaver. HOME is the most
# reliable on Google TV — unambiguously goes to the launcher. BACK
# also works. DPAD_CENTER and WAKEUP look like they work when tested
# via ADB's `input keyevent` but are silently dropped when delivered
# through the Android TV Remote v2 protocol that androidtvremote2
# (and therefore our app) uses — verified empirically with 0/3 dismiss
# success rate for both, so they are NOT supported here.
SCREENSAVER_DISMISS_KEY = os.environ.get("SCREENSAVER_DISMISS_KEY", "HOME").strip()
WAKE_DELAY = float(os.environ.get("WAKE_DELAY", "15.0"))         # minimum total wake time after POWER
WAKE_TIMEOUT = float(os.environ.get("WAKE_TIMEOUT", "30.0"))     # max time to wait for is_on=True
WAKE_POLL = float(os.environ.get("WAKE_POLL", "0.5"))
# Timeouts inside tv_play's launch path. Module-level so tests can stub
# them down to milliseconds for fast unit tests.
SMARTTUBE_FG_TIMEOUT = 3.0       # market://launch -> SmartTube foregrounded
                                 # (measured: ~166ms typical; this is a poll-
                                 # with-early-exit, so it just caps worst-case)
SMARTTUBE_FG_POLL = 0.3
# Lounge sender can be in a 5-60s exponential backoff sleep after SmartTube
# has been backgrounded (e.g. during the screensaver). 15s comfortably
# covers a typical post-foreground reconnect; we ALSO poke the monitor to
# break out of its backoff sleep below, so the timeout is a safety net.
LOUNGE_CONNECT_TIMEOUT = 15.0
LOUNGE_CONNECT_POLL = 0.3
LOUNGE_OBSERVATION_TIMEOUT = 3.0 # Lounge reports SmartTube's actual playback state
LOUNGE_OBSERVATION_POLL = 0.2
SCREENSAVER_DISMISS_DELAY = 0.3  # after the dismiss key, time for the OS to
                                 # settle (measured: ~100ms typical for HOME
                                 # to take dreamx off screen; we poll with
                                 # early-exit so this caps worst-case wait)
# Post-setPlaylist nudge window: max time to wait for Lounge to report
# state=Playing after setPlaylist, before sending a play() nudge for a
# Paused load. Only used on the cold-boot / re-foreground path; hot-path
# setPlaylist auto-plays and skips the nudge entirely.
POST_SETPLAYLIST_TIMEOUT = 2.0
POST_SETPLAYLIST_POLL = 0.2
# Sequence of keycodes sent when /api/skip empties the queue. Comma-separated
# so multi-step "go idle" routines work. The default "HOME,BACK" mirrors
# pressing the home button then back on the physical remote — that's what
# triggers the standard Google TV ambient/wallpaper screensaver. Single-key
# alternatives like "SLEEP" or "POWER" turn the display fully off; "HOME"
# alone just lands on the launcher; an empty value disables the behavior.
IDLE_KEYCODE = os.environ.get("IDLE_KEYCODE", "HOME,BACK").strip()
IDLE_KEYCODE_DELAY = float(os.environ.get("IDLE_KEYCODE_DELAY", "0.6"))
# Optional AV receiver for volume control. androidtvremote2 exposes
# `volume_info` read-only (no setter), so volume over the remote protocol would
# mean VOLUME_UP/VOLUME_DOWN keycodes — and whether those land is
# device-dependent. The Google TV Streamer reports volume_info.max=0 and routes
# its remote's volume straight to the amplifier over HDMI-CEC, so there is
# nothing for us to drive there. Other Android TV hardware may well accept the
# keycodes; nobody has tested it, and we don't implement that path yet.
# Talking to the receiver directly works regardless of the TV.
#
# Volume rides HDMI-CEC. Sending these keycodes over the Android TV Remote
# protocol makes the streamer issue a CEC volume command to whatever is doing
# the audio — TV speakers, soundbar, or an AV receiver — so it needs no
# configuration and works regardless of brand. Verified on real hardware
# against a Denon: each press moved the amp one step.
#
# It only works when the device has CEC volume control switched on. That is
# the Android default, but it can be off (it was on the maintainer's Streamer,
# which is why this looked impossible at first). When it's off the keycode is
# accepted and then quietly not translated, so the buttons do nothing. That
# prerequisite is documented in the README rather than detected, because
# reading the setting needs ADB and the runtime deliberately has none.
#
# KEYCODE_MUTE (91) is NOT the one to use: the protocol's own docs say it
# "Mutes the microphone, unlike KEYCODE_VOLUME_MUTE". Speaker mute is
# VOLUME_MUTE (164). The obvious-looking name is the wrong one.
VOLUME_KEYCODES = {
    "up": "VOLUME_UP",
    "down": "VOLUME_DOWN",
    "mute": "VOLUME_MUTE",
}

YT_REGEX = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")
ID_REGEX = re.compile(r"^[A-Za-z0-9_-]{11}$")
# t= or start= query param. Accepts bare seconds, or h/m/s components like
# "2m", "1h30m", "90s", "1h2m3s". Mirrors what YouTube's player accepts.
START_REGEX = re.compile(r"[?&](?:t|start)=([^&\s#]+)", re.IGNORECASE)
TIMESTAMP_HMS = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$", re.IGNORECASE)

INDEX_HTML = Path(__file__).parent / "index.html"


# ── singleton state ──────────────────────────────────────────────────────────
class State:
    remote: Optional[AndroidTVRemote] = None
    host: Optional[str] = None
    pairing_in_progress: bool = False
    last_current_app: Optional[str] = None  # for kill-switch transition detection
    http_client: Optional[httpx.AsyncClient] = None
    lounge_monitor: Optional[LoungeMonitor] = None
    # Sticky "is SmartTube backgrounded?" flag used to filter Lounge events
    # so a backgrounded SmartTube's stale Lounge pushes don't render as
    # live state. Updated only when the current_app callback fires a
    # transition — NOT re-checked on every Lounge event, which had a race:
    # routine events arriving during transient current_app values (system
    # overlays, ad insertion brief switches, etc.) would trip the check
    # and blank the snapshot for a tick, causing UI flicker.
    suppress_lounge: bool = False


state = State()
broadcaster = Broadcaster()
rate_limiter = RateLimiter()
queue_controller: QueueController  # initialized in lifespan


def _is_tv_paired() -> bool:
    """True if a complete TV-remote pairing exists on disk. Used to gate
    /api/pair/start so a hostile LAN client can't wipe the cert by
    re-triggering the pairing flow on a paired service."""
    return CERT_FILE.exists() and KEY_FILE.exists() and CONFIG_FILE.exists()


def _is_lounge_paired() -> bool:
    """True if a Lounge token exists on disk. Gates /api/lounge/pair so a LAN
    client can't overwrite a working token and hijack playback control."""
    return LOUNGE_AUTH_FILE.exists()


def _apply_reset_if_requested() -> None:
    """Honour RESET_PAIRING at startup, exactly once per time it's set.

    Runs before anything reads the data dir, so the service comes up unpaired
    and lands the user on the setup screen.
    """
    if not RESET_PAIRING:
        # Flag cleared — re-arm so the next RESET_PAIRING=1 boot fires.
        with contextlib.suppress(OSError):
            RESET_MARKER.unlink(missing_ok=True)
        return

    if RESET_MARKER.exists():
        log.warning(
            "RESET_PAIRING is still set but the reset has already run. "
            "Remove RESET_PAIRING from the container's environment and "
            "restart. Ignoring so your new pairing survives."
        )
        return

    removed = []
    for f in (CERT_FILE, KEY_FILE, CONFIG_FILE, LOUNGE_AUTH_FILE):
        try:
            if f.exists():
                f.unlink()
                removed.append(f.name)
        except OSError:
            log.exception("RESET_PAIRING: could not remove %s", f.name)

    try:
        RESET_MARKER.write_text(
            "RESET_PAIRING already ran. Clear RESET_PAIRING (or delete this "
            "file) to arm it again.\n"
        )
        _secure_data_file(RESET_MARKER)
    except OSError:
        log.exception("RESET_PAIRING: could not write the one-shot marker")

    log.warning(
        "RESET_PAIRING set — cleared %s. Set RESET_PAIRING=0 (or remove it) "
        "and restart, then re-pair from the web UI.",
        ", ".join(removed) if removed else "nothing (already unpaired)",
    )


def _secure_data_file(path: Path) -> None:
    """Set restrictive perms (0600) on a data file. Best-effort — silently
    skips on platforms (e.g. Windows host filesystems) where chmod is a no-op
    or unsupported. Defends against world-readable lounge tokens / TV certs
    on multi-tenant hosts."""
    try:
        if path.exists():
            path.chmod(0o600)
    except (OSError, NotImplementedError):
        pass


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            log.exception("config.json unreadable; ignoring")
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    _secure_data_file(CONFIG_FILE)


def build_remote(host: str) -> AndroidTVRemote:
    return AndroidTVRemote(
        client_name=CLIENT_NAME,
        certfile=str(CERT_FILE),
        keyfile=str(KEY_FILE),
        host=host,
    )


def extract_video_id(url_or_id: str) -> Optional[str]:
    """Pull a YouTube video ID out of a URL or accept a bare 11-char ID."""
    s = (url_or_id or "").strip()
    if ID_REGEX.match(s):
        return s
    m = YT_REGEX.search(s)
    return m.group(1) if m else None


def parse_youtube_timestamp(value: str) -> Optional[int]:
    """Parse a YouTube `t=` value into seconds. Accepts bare seconds ('120'),
    bare with suffix ('120s'), or h/m/s components ('1h2m3s', '2m'). Returns
    None if unparseable or zero."""
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    if v.isdigit():
        n = int(v)
        return n if n > 0 else None
    m = TIMESTAMP_HMS.match(v)
    if not m:
        return None
    h, mn, s = m.group(1), m.group(2), m.group(3)
    if not (h or mn or s):
        return None
    total = int(h or 0) * 3600 + int(mn or 0) * 60 + int(s or 0)
    return total if total > 0 else None


def extract_start_seconds(url_or_id: str) -> Optional[int]:
    """Pull a start-at timestamp out of a YouTube URL's `t=` or `start=`
    query parameter. Returns None if absent or unparseable."""
    s = (url_or_id or "").strip()
    if not s or ID_REGEX.match(s):
        return None
    m = START_REGEX.search(s)
    if not m:
        return None
    return parse_youtube_timestamp(m.group(1))


def parse_time_input(value: str) -> Optional[float]:
    """Parse a user-typed time string into seconds. More permissive than
    `parse_youtube_timestamp` — also accepts colon-separated formats
    ('5:30', '1:23:45') and treats 0 as a legitimate target (jump to
    the start). Returns None on unparseable input.

    Accepted formats:
        '90'          → 90
        '90.5'        → 90.5  (allows fractional seconds)
        '1:30'        → 90    (mm:ss)
        '1:23:45'     → 5025  (hh:mm:ss)
        '90s' '2m' '1h30m' '1h2m3s'  →  YouTube-style
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    # Colon-separated form. Two or three parts, all integer.
    if ":" in v:
        parts = v.split(":")
        if not (2 <= len(parts) <= 3):
            return None
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return None
        if any(n < 0 for n in nums):
            return None
        if len(parts) == 2:
            m, s = nums
            return float(m * 60 + s)
        h, m, s = nums
        return float(h * 3600 + m * 60 + s)
    # Bare numeric (int or float seconds). Reject negatives — this
    # function parses ABSOLUTE seek targets; relative offsets ('-30')
    # are handled by the seek endpoint's `by` parameter instead.
    try:
        n = float(v)
        return n if n >= 0 else None
    except ValueError:
        pass
    # Fall through to the YouTube-style parser (1h2m3s etc). Treat its
    # `None for zero` quirk as a real zero — caller can seek to 0.
    yt = parse_youtube_timestamp(v)
    return float(yt) if yt is not None else None


def _get_current_app() -> Optional[str]:
    """Read current foreground app from the TV; returns None if unavailable."""
    if state.remote is None:
        return None
    try:
        return state.remote.current_app
    except Exception:
        return None


def _on_current_app_changed(new_app: str) -> None:
    """Library callback: forwards SmartTube → other transitions to the
    queue controller's kill-switch entry point and updates the sticky
    suppress_lounge flag based on whether SmartTube is foreground."""
    prev = state.last_current_app
    state.last_current_app = new_app
    was_suppressed = state.suppress_lounge
    state.suppress_lounge = (new_app != SMARTTUBE_PACKAGE)
    queue_controller.on_current_app_changed(prev, new_app)
    # When suppress_lounge flips True→False (SmartTube returned to
    # foreground), Lounge's existing observation may have populated
    # WHILE we were suppressing — those events got blanked, and no new
    # ones will fire if SmartTube's state isn't changing. Republish
    # what the monitor knows now so the queue snapshot reflects
    # reality without waiting on the next state-change event.
    if (was_suppressed and not state.suppress_lounge
            and state.lounge_monitor is not None):
        obs = state.lounge_monitor.observation
        if obs.video_id is not None:
            asyncio.create_task(
                _on_lounge_event("lounge.position", obs)
            )


def _on_is_available_changed(available: bool) -> None:
    """Connection up/down. Push as a snapshot event so clients can render a
    'TV unreachable' banner."""
    event_type = "connection_restored" if available else "connection_lost"
    asyncio.create_task(broadcaster.publish(event_type, queue_controller.snapshot()))


def _on_is_on_changed(is_on: bool) -> None:
    """TV power state changed. Surfaces tv_on into queue state so the UI
    can render a 'waking TV' indicator while the TV is booting. When the
    TV powers OFF, also wipes the queue + current — turning off the TV is
    a clear signal that the user is done with whatever was queued."""
    log.info("TV power state: is_on=%s", is_on)
    asyncio.create_task(_handle_is_on_change(is_on))


async def _handle_is_on_change(is_on: bool) -> None:
    await queue_controller.update_tv_on(is_on)
    if not is_on:
        # Invalidate the cached foreground app — once the TV's off, we
        # don't know what it'll boot into next. Without this, the post-wake
        # `current_app` callback (TV typically lands at the launcher, not
        # SmartTube) would fire with prev=SmartTube/new=launcher and trip
        # the kill-switch, wiping a video the user queued during the wake
        # window. The kill-switch is meant for "user navigated away from
        # SmartTube", not "TV booted into the launcher."
        state.last_current_app = None
        await queue_controller.tv_off_reset()
    else:
        # TV came back on. Re-sync our state from whatever the library
        # currently reports for current_app: if Quick Resume restored
        # SmartTube as the foreground app, the library's cached value
        # may already match SmartTube and no current_app transition
        # callback will fire — leaving state.suppress_lounge stuck on
        # whatever it was before sleep (typically True, from
        # IDLE_KEYCODE backgrounding SmartTube). Stuck True blanks
        # every real Lounge event so the UI shows no playing state
        # even though SmartTube is genuinely playing.
        current = _get_current_app()
        if current is not None:
            state.last_current_app = current
            state.suppress_lounge = (current != SMARTTUBE_PACKAGE)


async def _wait_for_tv_on(timeout: float, poll: float) -> bool:
    """Poll state.remote.is_on until True or timeout. Returns whether the
    TV reported on within the window. Used by tv_play after sending POWER
    so we don't fire launch commands at a half-booted TV."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            if state.remote and bool(state.remote.is_on):
                return True
        except Exception:
            pass
        await asyncio.sleep(poll)
    return False


async def _reconnect_remote() -> bool:
    """Tear down the current state.remote and build a fresh connection.

    Used when the existing TLS connection appears stale — `send_key_command`
    succeeds at the Python level (no exception) but the keypress doesn't
    reach the TV. Empirically reproducible after the container has been
    running for many hours: the TCP socket is still open but the TV's
    androidtvremote2 service has lost its end. POWER, HOME, etc. all
    silently drop. A fresh connection (new cert handshake, new TLS
    socket) immediately works.

    Returns True if reconnect succeeded.
    """
    if state.host is None:
        log.warning("Cannot reconnect remote: no host known")
        return False
    log.info("Reconnecting androidtvremote2 (stale-connection mitigation)")
    old = state.remote
    state.remote = None
    if old is not None:
        try:
            old.disconnect()
        except Exception:
            log.debug("Old remote disconnect raised; continuing", exc_info=True)
    return await _attempt_startup_connect(state.host)


async def _wait_for_smarttube_foreground(timeout: float, poll: float) -> bool:
    """Poll until SmartTube is the foreground app, or until timeout. Used
    after a market://launch send so we know SmartTube is actually ready
    to receive the YouTube deep link before we fire it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if _get_current_app() == SMARTTUBE_PACKAGE:
            return True
        await asyncio.sleep(poll)
    return False


async def _wait_for_lounge_connected(timeout: float, poll: float) -> bool:
    """Poll until our Lounge sender session is connected, or until timeout.
    Lounge typically (re)connects shortly after SmartTube foregrounds, so
    callers can use this to wait for the chance to send a setPlaylist
    command that will reach SmartTube.

    Returns immediately when there is no Lounge monitor at all. Without that
    short-circuit, an install where the user skipped Lounge pairing paid the
    full timeout on *every* play and resume — waiting for a session that can
    never appear — before falling back to the deep link.
    """
    if state.lounge_monitor is None:
        return False
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if state.lounge_monitor and state.lounge_monitor.is_connected:
            return True
        await asyncio.sleep(poll)
    return False


def _wire_callbacks(remote: AndroidTVRemote) -> None:
    """Register kill-switch + availability + power callbacks on a (re)connected remote."""
    state.last_current_app = _get_current_app()
    state.suppress_lounge = (
        state.last_current_app is not None
        and state.last_current_app != SMARTTUBE_PACKAGE
    )
    remote.add_current_app_updated_callback(_on_current_app_changed)
    remote.add_is_available_updated_callback(_on_is_available_changed)
    remote.add_is_on_updated_callback(_on_is_on_changed)


# ── Lounge bridge: forward LoungeMonitor events into the queue controller ───


async def _lounge_pause() -> bool:
    """Pause via Lounge if connected, fallback to MEDIA_PAUSE key otherwise."""
    if state.lounge_monitor and state.lounge_monitor.is_connected:
        if await state.lounge_monitor.pause():
            return True
    if state.remote is None:
        return False
    try:
        state.remote.send_key_command("MEDIA_PAUSE")
        return True
    except Exception:
        log.warning("MEDIA_PAUSE send failed", exc_info=True)
        return False


RESUME_VERIFY_TIMEOUT = 3.0
RESUME_VERIFY_POLL = 0.2


async def _lounge_play() -> bool:
    """Resume / start playback. Paths in order of preference:

    1. SmartTube foreground + Lounge connected → Lounge.play() AND
       verify state actually transitions to Playing within
       RESUME_VERIFY_TIMEOUT. If verified, in-place unpause is done.
    2. Lounge.play() succeeded HTTPS-wise but state stayed Paused
       (signal: SmartTube's PlaybackActivity was torn down, e.g. user
       paused then hit BACK on the remote) → fall through to tv_play()
       for the current item. tv_play's "foreground but idle" branch
       sends the deep link Intent and relaunches the player cleanly.
    3. SmartTube NOT foreground → tv_play() runs the full launch
       sequence (market://launch + deep link).
    4. Last resort → MEDIA_PLAY keycode.

    Note: we can't tell "player torn down" from Lounge observation
    alone — after BACK, current_time and state stay sticky at their
    pause-point values. The only reliable signal is "did Lounge.play()
    take effect" — so we always verify post-play. Cheap (<1s when it
    works, 3s when we need to fall through).
    """
    smarttube_fg = (_get_current_app() == SMARTTUBE_PACKAGE)
    lounge_ready = (
        state.lounge_monitor is not None
        and state.lounge_monitor.is_connected
    )
    if smarttube_fg and lounge_ready:
        if await state.lounge_monitor.play():
            # Verify Lounge.play() actually resumed playback. Against
            # a torn-down PlaybackActivity (post-BACK), the call
            # succeeds HTTPS-wise but state stays Paused indefinitely
            # — we'd silently return True and the user clicks Play
            # with no effect.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + RESUME_VERIFY_TIMEOUT
            while loop.time() < deadline:
                obs = state.lounge_monitor.observation
                if obs.state == "Playing":
                    return True
                await asyncio.sleep(RESUME_VERIFY_POLL)
            log.info(
                "Lounge.play() did not transition to Playing within %.1fs "
                "(state=%s, ct=%s) — player likely torn down, falling through",
                RESUME_VERIFY_TIMEOUT,
                state.lounge_monitor.observation.state,
                state.lounge_monitor.observation.current_time,
            )
    cur = queue_controller.state.current
    if cur and state.remote is not None:
        try:
            await tv_play(cur.video_id, cur.start_s)
            return True
        except Exception:
            log.warning("tv_play during resume failed", exc_info=True)
    if state.remote is None:
        return False
    try:
        state.remote.send_key_command("MEDIA_PLAY")
        return True
    except Exception:
        log.warning("MEDIA_PLAY send failed", exc_info=True)
        return False


# Single-entry cache for the currently-observed video's metadata. Lounge only
# tells us video_id; we scrape title/channel/thumbnail. Cache key is the
# video_id; replaced whenever Lounge reports a different video.
_lounge_meta: dict[str, dict] = {}
_lounge_meta_in_flight: set[str] = set()


async def _on_lounge_event(event_type: str, observation: LoungeObservation) -> None:
    """Forward Lounge monitor events to the queue controller, injecting
    title/channel/thumbnail for the current video_id (looked up from the
    queue if we queued it, scraped from YouTube otherwise).

    Suppresses updates while SmartTube is known-backgrounded — Lounge often
    stays connected and keeps reporting the last-played video's state long
    after the user has navigated to a different app or the screensaver,
    and we don't want to render that as if it were live. The flag is
    sticky (updated only by current_app callback transitions) so routine
    Lounge events while SmartTube is genuinely foreground are never
    suppressed by transient current_app reads."""
    if state.suppress_lounge:
        await queue_controller.on_lounge_event(
            event_type, LoungeObservation().to_dict(),
        )
        return
    obs_dict = observation.to_dict()
    vid = observation.video_id
    if vid:
        # Prefer the queue item's metadata if we queued this video ourselves.
        cur = queue_controller.state.current
        if cur and cur.video_id == vid:
            obs_dict["title"] = cur.title
            obs_dict["channel"] = cur.channel
            obs_dict["thumbnail_url"] = cur.thumbnail_url
        elif vid in _lounge_meta:
            obs_dict.update(_lounge_meta[vid])
        else:
            # Externally-started video — kick off metadata scrape so we have
            # title/channel/thumbnail to show. Until it returns, the UI will
            # see thumbnail_url=None and a placeholder.
            obs_dict["title"] = None
            obs_dict["channel"] = None
            obs_dict["thumbnail_url"] = None
            if vid not in _lounge_meta_in_flight:
                _lounge_meta_in_flight.add(vid)
                asyncio.create_task(_resolve_lounge_metadata(vid))
    await queue_controller.on_lounge_event(event_type, obs_dict)


async def _resolve_lounge_metadata(video_id: str) -> None:
    """Background scrape of title/channel/thumbnail for an externally-started
    video. Stores in the single-entry cache, then republishes the latest
    observation so the UI updates."""
    meta_dict: dict = {"title": None, "channel": None, "thumbnail_url": None}
    try:
        md = await fetch_metadata(video_id, client=state.http_client)
        meta_dict = {
            "title": md.title,
            "channel": md.channel,
            "thumbnail_url": md.thumbnail_url,
        }
    except Exception:
        log.exception("Lounge metadata lookup failed for %s", video_id)
    finally:
        _lounge_meta_in_flight.discard(video_id)
    # Single-entry cache — drop any stale predecessor.
    _lounge_meta.clear()
    _lounge_meta[video_id] = meta_dict
    # Republish current observation so SSE clients see the new metadata.
    if state.lounge_monitor is not None:
        obs = state.lounge_monitor.observation
        if obs.video_id == video_id:
            await _on_lounge_event("lounge.position", obs)


def _load_lounge_auth() -> Optional[dict]:
    if not LOUNGE_AUTH_FILE.exists():
        return None
    try:
        return json.loads(LOUNGE_AUTH_FILE.read_text())
    except Exception:
        log.exception("lounge.json unreadable; ignoring")
        return None


def _save_lounge_auth(auth: dict) -> None:
    LOUNGE_AUTH_FILE.write_text(json.dumps(auth, indent=2))
    _secure_data_file(LOUNGE_AUTH_FILE)


async def _start_lounge_monitor() -> None:
    """Start the Lounge monitor if we have persisted auth. Idempotent —
    replaces any existing monitor."""
    auth = _load_lounge_auth()
    if auth is None:
        log.info("Lounge: no persisted auth, skipping start")
        return
    await _stop_lounge_monitor()
    state.lounge_monitor = LoungeMonitor(
        device_name=CLIENT_NAME,
        on_event=_on_lounge_event,
        # Skip the periodic refresh poll when:
        #   (a) our queue is idle — without this gate, refresh keeps
        #       repopulating state.lounge from SmartTube's server
        #       cache, making old videos "pop back up" in the UI
        #       when the user re-foregrounds SmartTube;
        #   (b) we're paused — the stuck-ct detector inside the
        #       refresh loop would otherwise false-positive every
        #       paused session as "stuck" and trigger a useless
        #       reconnect;
        #   (c) SmartTube isn't foreground (state.suppress_lounge) —
        #       refreshing while the user is in TV settings / another
        #       app would not only be pointless but actively harmful:
        #       a fresh Lounge connection against an idle SmartTube
        #       can auto-bring SmartTube to the foreground (Lounge
        #       protocol behavior — TV app activates on remote
        #       session open). The stuck-ct detector forcing
        #       reconnects under this condition would yank the user
        #       out of whatever they're doing on the TV.
        should_refresh=lambda: (
            queue_controller.state.current is not None
            and not queue_controller.state.paused
            and not state.suppress_lounge
        ),
        # Persist a refreshed loungeIdToken to disk so the next startup
        # uses the fresh token instead of trying the expired one. Without
        # this, every container restart after a token expiry would fail
        # to connect Lounge until the user manually re-pairs — even
        # though the screen_id is still valid and refresh_auth() would
        # have worked.
        on_auth_refreshed=_save_lounge_auth,
    )
    state.lounge_monitor.load_auth(auth)
    await state.lounge_monitor.start()
    log.info("Lounge monitor started")


async def _stop_lounge_monitor() -> None:
    if state.lounge_monitor is not None:
        try:
            await state.lounge_monitor.stop()
        except Exception:
            log.exception("Lounge monitor stop failed")
        state.lounge_monitor = None


async def _lounge_watchdog() -> None:
    """Periodically verify the Lounge monitor's subscribe task is still
    alive, and restart the monitor from scratch if it's dead.

    Belt-and-suspenders for the case where the subscribe loop dies
    silently (e.g. an unhandled exception in pyytlounge's protocol
    handling) — without this, the UI would just show 'Playback sync:
    OFFLINE' indefinitely until someone restarts the container. The
    bulletproof outer wrapper in lounge.py's _subscribe_loop should
    prevent this in normal operation; this is the safety net if that
    wrapper somehow doesn't catch a failure mode.
    """
    while True:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return
        mon = state.lounge_monitor
        if mon is None:
            continue
        task = getattr(mon, "_subscribe_task", None)
        if task is None or not task.done():
            continue
        log.warning(
            "Lounge subscribe task is no longer running; restarting monitor"
        )
        try:
            await _start_lounge_monitor()
        except Exception:
            log.exception("Lounge watchdog restart failed; will retry next tick")


async def tv_play(video_id: str, start_s: Optional[int] = None) -> None:
    """Send a single video to SmartTube on the TV. If the TV is off, wakes
    it first; if SmartTube isn't foreground, launches it. Then pushes the
    video via Lounge (preferred) or via the YouTube deep link (fallback).

    Raises on failure — the queue controller logs but does not roll back state."""
    if state.remote is None:
        raise RuntimeError("not connected to TV")

    was_off = False
    try:
        was_off = not state.remote.is_on
    except Exception:
        was_off = False

    if was_off:
        log.info("TV reports off — sending POWER and waiting for boot")
        await queue_controller.set_waking(True)
        try:
            loop = asyncio.get_running_loop()
            wake_started_at = loop.time()
            state.remote.send_key_command("POWER")
            came_on = await _wait_for_tv_on(WAKE_TIMEOUT, WAKE_POLL)
            if not came_on:
                # First POWER didn't take effect. Probable cause: the
                # androidtvremote2 TLS connection has gone stale — TCP
                # socket still open from Python's view, but the TV no
                # longer routes commands from it. Verified empirically
                # by running a fresh-cert probe: identical POWER call
                # against a new connection wakes the TV within 1
                # second, while the long-running container's connection
                # silently drops the same command. Reconnect and retry
                # POWER once before giving up.
                log.warning(
                    "TV did not report on within %.0fs — connection likely "
                    "stale; reconnecting remote and retrying POWER",
                    WAKE_TIMEOUT,
                )
                if await _reconnect_remote():
                    if state.remote is not None and not bool(state.remote.is_on):
                        try:
                            state.remote.send_key_command("POWER")
                        except Exception:
                            log.warning("Retry POWER after reconnect failed",
                                        exc_info=True)
                        came_on = await _wait_for_tv_on(WAKE_TIMEOUT, WAKE_POLL)
                    else:
                        # Reconnect happened to land while TV was already
                        # on (maybe the first POWER did take effect, just
                        # not visibly via the dead connection).
                        came_on = bool(state.remote and state.remote.is_on)
                if not came_on:
                    log.warning(
                        "TV still not reporting on after reconnect + retry; "
                        "trying launch anyway"
                    )
            # Enforce a minimum total wake time — is_on may flip true (display
            # backlight on) before Android is ready for launch intents. On
            # Quick Resume TVs is_on flips ~instantly even while the OS is
            # still booting; the WAKE_DELAY is what bridges that gap.
            elapsed = loop.time() - wake_started_at
            if elapsed < WAKE_DELAY:
                await asyncio.sleep(WAKE_DELAY - elapsed)
        finally:
            # Make sure we always clear the flag — including on raise —
            # otherwise the UI would be stuck on WAKING forever.
            await queue_controller.set_waking(False)

    deep_link = f"vnd.youtube.launch://www.youtube.com/watch?v={video_id}"
    if start_s and start_s > 0:
        deep_link += f"&t={start_s}"

    current_app = _get_current_app()

    # Dismiss the screensaver if it's foreground. Verified empirically:
    # dreamx silently swallows send_launch_app_command intents — both
    # market://launch and vnd.youtube.launch:// — leaving us stuck. Any
    # navigation key dismisses the screensaver and returns the TV to
    # whatever app was foreground before it kicked in (typically SmartTube
    # or the launcher), and from there our normal launch flow works.
    if current_app in SCREENSAVER_PACKAGES:
        log.info("Dismissing screensaver (%s) via %s",
                 current_app, SCREENSAVER_DISMISS_KEY)
        state.remote.send_key_command(SCREENSAVER_DISMISS_KEY)
        # Poll for the dismiss to actually take effect, capped at
        # SCREENSAVER_DISMISS_DELAY. Polling lets us proceed early when
        # the dismiss completes (typically ~150ms via HOME) instead of
        # blindly waiting the full window.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SCREENSAVER_DISMISS_DELAY
        while loop.time() < deadline:
            current_app = _get_current_app()
            if current_app not in SCREENSAVER_PACKAGES:
                break
            await asyncio.sleep(0.1)
        else:
            current_app = _get_current_app()
        if current_app in SCREENSAVER_PACKAGES:
            log.warning("Screensaver still foreground after %s — launch may fail",
                        SCREENSAVER_DISMISS_KEY)

    smarttube_foreground = current_app == SMARTTUBE_PACKAGE
    # had_to_foreground=True means SmartTube isn't currently the
    # foreground app — cold boot, dreamx (after dismiss), launcher, or
    # a different app. In all these cases SmartTube's local media
    # session isn't fully alive: even if Lounge cache reports state
    # for some video, Lounge.play() and Lounge.setPlaylist against a
    # dormant SmartTube are unreliable (verified empirically — the
    # user-reported "video shows but doesn't start playing" was
    # exactly this case). The deep link is the right primitive here:
    # Android resolves `vnd.youtube.launch://` to SmartTube's Intent
    # handler, which launches SmartTube AND kicks playback fresh in
    # one step. No market://launch needed — the Intent itself
    # foregrounds the app.
    #
    # When SmartTube IS already foreground (had_to_foreground=False),
    # use Lounge primitives for a smooth swap (no PlaybackActivity
    # restart, no flicker).
    had_to_foreground = not smarttube_foreground

    if had_to_foreground:
        log.info(
            "SmartTube not foreground (current=%s) — using deep-link Intent "
            "(Android launches SmartTube + plays in one step)",
            current_app,
        )
        state.remote.send_launch_app_command(deep_link)
        return

    # SmartTube is foreground. Use Lounge for smooth swap / skip-redundant.
    if state.lounge_monitor is not None:
        state.lounge_monitor.request_reconnect_now()
    came_lounge = await _wait_for_lounge_connected(
        timeout=LOUNGE_CONNECT_TIMEOUT, poll=LOUNGE_CONNECT_POLL,
    )

    if came_lounge:
        # Wait for Lounge observation to populate before deciding —
        # video_id alone (without current_time) can be a stale cached
        # playlist; current_time confirms real playback.
        deadline = asyncio.get_running_loop().time() + LOUNGE_OBSERVATION_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            obs_inner = state.lounge_monitor.observation
            if obs_inner.video_id is not None and obs_inner.current_time is not None:
                break
            await asyncio.sleep(LOUNGE_OBSERVATION_POLL)
        obs = state.lounge_monitor.observation
        if (obs.video_id == video_id and obs.available
                and obs.current_time is not None):
            if obs.state == "Playing":
                log.info("SmartTube already playing %s @ %.1fs — skipping",
                         video_id, obs.current_time)
                return
            # In-place resume only for real mid-playback pauses (ct > 1s).
            # Paused at ct≈0 is a dormant load, not a user pause — fall
            # through to setPlaylist.
            if (obs.state == "Paused" and obs.current_time is not None
                    and obs.current_time > 1.0):
                try:
                    await state.lounge_monitor.play()
                    # Verify Lounge.play() actually took effect. Against
                    # a torn-down PlaybackActivity (e.g. user paused then
                    # BACK'd out, exiting the player view) the call
                    # succeeds HTTPS-wise but state stays Paused forever
                    # — we'd silently return success and the user gets
                    # no playback. ct + state alone don't reveal this;
                    # only post-call verification does.
                    loop = asyncio.get_running_loop()
                    verify_deadline = loop.time() + RESUME_VERIFY_TIMEOUT
                    while loop.time() < verify_deadline:
                        obs_check = state.lounge_monitor.observation
                        if obs_check.state == "Playing":
                            log.info(
                                "SmartTube has %s loaded but state=Paused @ %.1fs — "
                                "sent Lounge.play() (verified resume)",
                                video_id, obs.current_time,
                            )
                            return
                        await asyncio.sleep(RESUME_VERIFY_POLL)
                    log.info(
                        "Lounge.play() did not transition %s to Playing within %.1fs "
                        "(state=%s) — player likely torn down, falling through to deep link",
                        video_id, RESUME_VERIFY_TIMEOUT,
                        state.lounge_monitor.observation.state,
                    )
                except Exception:
                    log.warning("Lounge.play() failed; falling through",
                                exc_info=True)

        # Smooth-swap via setPlaylist is only reliable when SmartTube
        # has an ACTIVE PlaybackActivity to receive the playlist change.
        # If Lounge reports state=Playing for any video, SmartTube is
        # actively playing and a setPlaylist will swap to the new video
        # cleanly. Otherwise (idle SmartTube on home/browse screen,
        # stale cache, dormant after IDLE_KEYCODE wake), setPlaylist
        # loads the video but SmartTube doesn't auto-play — user sees
        # the video on screen but it never starts. Use the deep link
        # instead; Android Intent routes through SmartTube's
        # PlaybackActivity launcher which kicks playback fresh.
        smarttube_actively_playing = (
            obs.state == "Playing"
            and obs.video_id is not None
            and obs.current_time is not None
        )
        if smarttube_actively_playing:
            try:
                await state.lounge_monitor.play_video(video_id, start_s)
                log.info("Sent %s%s via Lounge.setPlaylist (smooth swap)",
                         video_id, f" @ {start_s}s" if start_s else "")
                return
            except Exception:
                log.warning("Lounge.play_video failed; falling back to deep link",
                            exc_info=True)
        else:
            log.info(
                "SmartTube foreground but idle (lounge state=%r video_id=%r) "
                "— deep link for reliable kick",
                obs.state, obs.video_id,
            )

    # SmartTube is foreground but not actively playing (or Lounge
    # unavailable). Deep link routes through Android's Intent system
    # to kick fresh playback.
    state.remote.send_launch_app_command(deep_link)
    log.info("Sent %s%s to %s via deep link",
             video_id, f" @ {start_s}s" if start_s else "", state.host)


async def _build_queue_item(video_id: str, start_s: Optional[int] = None) -> QueueItem:
    md: Metadata = await fetch_metadata(video_id, client=state.http_client)
    return make_item(
        video_id=md.video_id,
        title=md.title,
        channel=md.channel,
        duration_s=md.duration_s,
        is_live=md.is_live,
        thumbnail_url=md.thumbnail_url,
        start_s=start_s,
    )


def _client_ip(request: Request) -> str:
    """Direct peer IP. We don't honor X-Forwarded-* — this service is LAN-only;
    if you put it behind a reverse proxy you'd configure uvicorn with
    --proxy-headers and surface those here."""
    return request.client.host if request.client else "unknown"


def _check_rate_limit(request: Request) -> None:
    retry = rate_limiter.check(_client_ip(request))
    if retry > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests; retry in {retry:.0f}s",
            headers={"Retry-After": str(int(retry) + 1)},
        )


async def _attempt_startup_connect(host: str) -> bool:
    """Try once to connect to the TV remote. Returns True on success.
    Side-effect: populates state.remote / state.host / wires callbacks
    on success, leaves them None on failure.
    """
    try:
        log.info("Connecting to %s…", host)
        remote = build_remote(host)
        await remote.async_generate_cert_if_missing()
        await remote.async_connect()
        remote.keep_reconnecting()
        state.remote = remote
        state.host = host
        _wire_callbacks(remote)
        log.info("Connected to %s", host)
        try:
            tv_is_on = bool(remote.is_on)
        except Exception:
            tv_is_on = False
        await queue_controller.update_tv_on(tv_is_on)
        return True
    except InvalidAuth:
        log.warning("Stored cert is not paired; user must re-pair")
        return False
    except Exception:
        log.warning("Startup connect to %s failed; will retry", host, exc_info=False)
        return False


async def _retry_startup_connect_until_success(host: str) -> None:
    """Background task: retry the startup connect with exponential
    backoff until it succeeds or the lifespan ends.

    Triggered when the TV is unreachable at app startup — typical after
    a power outage where the NAS comes back online before the TV. The
    UI would otherwise be stuck on 'NOT CONFIGURED' (state.remote is
    None) even though the cert/key/config are all on disk; the
    /api/pair/start endpoint refuses because _is_tv_paired() sees the
    files. Without this retry the only fix was a manual container
    restart.

    Backoff caps at 60s.
    """
    backoff = 5.0
    while True:
        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            return
        if state.remote is not None:
            # Something else (e.g. a re-pair via UI) connected first.
            return
        if await _attempt_startup_connect(host):
            log.info("Background retry: connected to %s after startup failure", host)
            return
        backoff = min(backoff * 1.5, 60.0)


# ── lifespan ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    global queue_controller
    state.http_client = httpx.AsyncClient(
        timeout=5.0, follow_redirects=True, max_redirects=3,
    )
    queue_controller = QueueController(
        play_callable=tv_play,
        broadcaster=broadcaster,
        smarttube_package=SMARTTUBE_PACKAGE,
        get_current_app=_get_current_app,
        pause_callable=_lounge_pause,
        play_button_callable=_lounge_play,
    )

    # Before anything reads the data dir, so a requested reset lands the user
    # on the setup screen rather than half-connecting with stale credentials.
    _apply_reset_if_requested()

    # One-time cleanup for users upgrading from a version that didn't
    # restrict perms on persisted secrets.
    for f in (CERT_FILE, KEY_FILE, CONFIG_FILE, LOUNGE_AUTH_FILE):
        _secure_data_file(f)

    cfg = load_config()
    host = cfg.get("host")
    startup_retry_task: Optional[asyncio.Task] = None
    if host and CERT_FILE.exists() and KEY_FILE.exists():
        connected = await _attempt_startup_connect(host)
        if not connected:
            # If the TV was unreachable at startup (typical after a power
            # outage where the NAS recovers before the TV), schedule a
            # background retry. Without this we'd be stuck in a fake
            # "NOT CONFIGURED" state until someone restarts the container.
            startup_retry_task = asyncio.create_task(
                _retry_startup_connect_until_success(host)
            )
    else:
        log.info("Not configured — visit web UI to pair")

    # Lounge is independent of the TV remote connection — start it whenever
    # we have persisted auth, regardless of pairing state.
    try:
        await _start_lounge_monitor()
    except Exception:
        log.exception("Lounge monitor failed to start")

    # Watchdog: revive the Lounge monitor if its subscribe task dies.
    # See _lounge_watchdog for context — without this, an unhandled
    # exception in the monitor would leave Playback Sync OFFLINE until
    # someone manually restarts the container (a user reported exactly
    # this after a power outage where the NAS came back before their
    # router did).
    watchdog_task = asyncio.create_task(_lounge_watchdog())

    try:
        yield
    finally:
        watchdog_task.cancel()
        with contextlib.suppress(BaseException):
            await watchdog_task
        if startup_retry_task is not None and not startup_retry_task.done():
            startup_retry_task.cancel()
            with contextlib.suppress(BaseException):
                await startup_retry_task
        await _stop_lounge_monitor()
        await queue_controller.shutdown()
        if state.remote:
            try:
                state.remote.disconnect()
            except Exception:
                pass
        if state.http_client:
            await state.http_client.aclose()


app = FastAPI(title="SmartTube Playlist", lifespan=lifespan)


_PRIVATE_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".home.arpa")


def _host_header_is_trusted(hostname: Optional[str]) -> bool:
    """True if this Host header could plausibly be our own LAN identity.

    Anchors the CSRF check against something the caller can't mint. Comparing
    Origin to Host alone is worthless — both come from the client, so a
    DNS-rebinding attacker simply sends a matching pair: their page at
    `evil.com` re-resolves to the LAN IP, the browser dutifully sends
    `Origin: http://evil.com` and `Host: evil.com`, they agree, and the
    request sails through with full control of the TV.

    Rebinding needs a *registrable domain*, which always contains a dot. So we
    accept bare IPs (the normal way anyone reaches a LAN service), single-label
    names like `mynas` (not publicly registrable, so not a rebinding vector),
    and non-public suffixes such as `.local`. Anything else — a real domain —
    must be opted into via ALLOWED_HOSTS.
    """
    h = (hostname or "").strip().lower()
    if not h:
        return False
    if h in ALLOWED_HOSTS:
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    if "." not in h:
        return True
    return h.endswith(_PRIVATE_HOST_SUFFIXES)


@app.middleware("http")
async def csrf_origin_check(request: Request, call_next):
    """Same-origin guard for state-mutating requests.

    Two layers, because either alone is insufficient:

    1. The `Host` header must look like our own LAN identity
       (_host_header_is_trusted). This is what actually stops DNS rebinding;
       without it the Origin comparison below is self-referential and proves
       nothing.
    2. If `Origin` is present it must match the request's own origin — the
       classic cross-site check. Non-browser clients (curl, scripts,
       home-automation webhooks) don't send Origin and pass through.

    No-op on safe methods (GET/HEAD/OPTIONS) — those can't cause state
    changes by themselves.
    """
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not _host_header_is_trusted(request.url.hostname):
            log.warning(
                "blocked %s %s with untrusted Host=%r — if you reach this "
                "service by a domain name, add it to ALLOWED_HOSTS",
                request.method, request.url.path, request.url.hostname,
            )
            return Response("untrusted Host header", status_code=403)
        origin = request.headers.get("origin")
        if origin:
            expected = f"{request.url.scheme}://{request.url.netloc}"
            if origin != expected:
                log.warning(
                    "CSRF: blocked %s %s with cross-origin Origin=%r (expected %r)",
                    request.method, request.url.path, origin, expected,
                )
                return Response(
                    "cross-origin request blocked", status_code=403,
                )
    return await call_next(request)


# ── request models ───────────────────────────────────────────────────────────
class PairStartReq(BaseModel):
    host: str


class PairFinishReq(BaseModel):
    code: str


class AddReq(BaseModel):
    url: Optional[str] = None
    video_id: Optional[str] = None



# ── status / pairing endpoints ───────────────────────────────────────────────
@app.get("/api/status")
async def status():
    paired = state.remote is not None and not state.pairing_in_progress
    is_on = None
    current_app = None
    if paired and state.remote:
        try:
            is_on = bool(state.remote.is_on)
        except Exception:
            is_on = None
        try:
            current_app = state.remote.current_app
        except Exception:
            current_app = None
    lounge_paired = LOUNGE_AUTH_FILE.exists()
    lounge_connected = bool(state.lounge_monitor and state.lounge_monitor.is_connected)
    return {
        "version": VERSION,
        "configured": paired,
        "host": state.host,
        "pairing_in_progress": state.pairing_in_progress,
        "tv_on": is_on,
        "current_app": current_app,
        "lounge_paired": lounge_paired,
        "lounge_connected": lounge_connected,
        # null when no volume backend is configured; the string name of
        # the backend (currently only "denon") when one is active. The
        # frontend uses this to decide whether to render volume buttons.
        # Volume goes over HDMI-CEC through the paired remote, so it's
        # available whenever the TV is. Whether the device actually honours it
        # depends on its CEC volume setting, which we can't read without ADB.
        "volume_available": state.remote is not None,
    }


@app.post("/api/pair/start")
async def pair_start(req: PairStartReq):
    # Hard gate: once paired, refuse pair_start entirely. Without this gate
    # the endpoint is a one-shot LAN-side denial-of-service (anyone can
    # call it to wipe the existing cert) and an internal-network probe
    # vector (the host string flows to a TLS connection on port 6466).
    # Re-pairing requires file-system access — see README.
    if _is_tv_paired():
        raise HTTPException(
            409,
            "already paired — delete cert.pem/key.pem/config.json from /data to re-pair",
        )
    if state.pairing_in_progress:
        raise HTTPException(409, "pairing already in progress")

    if state.remote:
        try:
            state.remote.disconnect()
        except Exception:
            pass
        state.remote = None
        state.host = None

    # Stale cert leftovers from an aborted prior pair attempt.
    for f in (CERT_FILE, KEY_FILE):
        if f.exists():
            f.unlink()

    remote = build_remote(req.host.strip())
    await remote.async_generate_cert_if_missing()
    _secure_data_file(CERT_FILE)
    _secure_data_file(KEY_FILE)

    try:
        await remote.async_start_pairing()
    except CannotConnect:
        log.warning("Could not reach TV at %s during pair_start", req.host)
        raise HTTPException(502, "could not reach TV at the given address")
    except Exception:
        log.exception("start_pairing failed")
        raise HTTPException(500, "pairing start failed")

    state.remote = remote
    state.host = req.host.strip()
    state.pairing_in_progress = True
    return {"ok": True, "message": "Check your TV for a 6-character code"}


@app.post("/api/pair/cancel")
async def pair_cancel():
    """Cancel an in-progress pairing flow. No-op if not pairing.

    Narrow by design: only acts when state.pairing_in_progress is true,
    so this is not a back-door for wiping an established pairing (which
    is why the broader /api/reset endpoint was removed). When active,
    it disconnects the in-flight remote, deletes the freshly-generated
    cert/key (they're useless without a finished pairing), and clears
    the in-progress flag so the operator can start a new attempt."""
    if not state.pairing_in_progress:
        return {"ok": True, "cancelled": False}
    if state.remote:
        try:
            state.remote.disconnect()
        except Exception:
            pass
    state.remote = None
    state.host = None
    state.pairing_in_progress = False
    # The cert/key on disk are from this aborted attempt — useless, and
    # leaving them around makes _is_tv_paired() falsely report "paired"
    # (CONFIG_FILE doesn't exist yet so it actually wouldn't, but clean
    # up to be safe).
    for f in (CERT_FILE, KEY_FILE):
        if f.exists():
            try:
                f.unlink()
            except Exception:
                pass
    return {"ok": True, "cancelled": True}


@app.post("/api/pair/finish")
async def pair_finish(req: PairFinishReq):
    if not state.pairing_in_progress or not state.remote:
        raise HTTPException(409, "no pairing in progress")

    code = req.code.strip().upper()
    try:
        await state.remote.async_finish_pairing(code)
    except InvalidAuth:
        raise HTTPException(400, "invalid pairing code")
    except ConnectionClosed:
        raise HTTPException(502, "connection closed during pairing — try again")
    except Exception:
        log.exception("finish_pairing failed")
        raise HTTPException(500, "pairing finish failed")

    state.pairing_in_progress = False
    save_config({"host": state.host})

    try:
        await state.remote.async_connect()
        state.remote.keep_reconnecting()
        _wire_callbacks(state.remote)
        try:
            tv_is_on = bool(state.remote.is_on)
        except Exception:
            tv_is_on = False
        await queue_controller.update_tv_on(tv_is_on)
    except Exception:
        log.exception("post-pairing connect failed")
        raise HTTPException(500, "paired but post-pairing connect failed")

    return {"ok": True, "host": state.host}


# ── Lounge pairing endpoint ────────────────────────────────────────────────
#
# NOTE: there is intentionally no /api/reset or /api/lounge/unpair endpoint.
# Wiping pairing state is a destructive admin action that — without auth —
# is exposed to every device on the LAN as a one-shot DoS. To re-pair, an
# operator with file-system access to the data dir should delete the
# relevant file(s) directly:
#   - cert.pem / key.pem / config.json  → re-pair the TV remote
#   - lounge.json                        → re-pair YouTube Lounge


class LoungePairReq(BaseModel):
    code: str


@app.post("/api/lounge/pair")
async def lounge_pair(req: LoungePairReq):
    """Pair with SmartTube via a 12-digit YouTube Lounge code (shown by
    SmartTube's "Link with TV code" screen). Persists the resulting auth
    token to /data/lounge.json and starts the Lounge monitor.

    Refuses once a token exists, mirroring the pair_start guard: otherwise any
    LAN client could overwrite a working token, taking over playback control
    and silently breaking the real owner's session. Re-pair via RESET_PAIRING.
    """
    if _is_lounge_paired():
        raise HTTPException(
            409,
            "already paired with YouTube Lounge — set RESET_PAIRING=1 and "
            "restart the container to re-pair",
        )
    try:
        auth = await LoungeMonitor.pair_with_code(CLIENT_NAME, req.code)
    except ValueError:
        # ValueError from our own pair_with_code is "empty pairing code" —
        # the message itself is safe to expose.
        raise HTTPException(400, "empty or malformed pairing code")
    except Exception:
        log.exception("Lounge pairing failed")
        raise HTTPException(400, "pairing failed — check the code and try again")

    _save_lounge_auth(auth)
    await _start_lounge_monitor()
    return {"ok": True}


# ── queue endpoints ──────────────────────────────────────────────────────────
def _require_paired() -> None:
    if not state.remote or state.pairing_in_progress:
        raise HTTPException(503, "not paired — visit the web UI to pair")


@app.get("/api/queue")
async def get_queue():
    return queue_controller.snapshot()


@app.post("/api/queue")
async def add_to_queue(req: AddReq, request: Request):
    _require_paired()
    _check_rate_limit(request)
    raw = req.url or req.video_id or ""
    vid = extract_video_id(raw)
    if not vid:
        raise HTTPException(400, "could not extract a YouTube video ID from input")
    start_s = extract_start_seconds(raw)
    item = await _build_queue_item(vid, start_s=start_s)
    await queue_controller.add(item)
    return {"ok": True, "item": item.to_dict()}


@app.delete("/api/queue/{item_id}")
async def remove_from_queue(item_id: str):
    removed = await queue_controller.remove(item_id)
    if not removed:
        raise HTTPException(404, "item not in queue (already played, removed, or unknown id)")
    return {"ok": True}


@app.post("/api/queue/{item_id}/move/{direction}")
async def move_in_queue(item_id: str, direction: str):
    """Move a queued item one slot up or down. 404 if the item isn't
    queued; 400 if direction isn't 'up' or 'down'; 200 (no-op) if the
    item is already at the boundary it's being moved toward."""
    if direction not in ("up", "down"):
        raise HTTPException(400, "direction must be 'up' or 'down'")
    # Distinguish "not queued" from "at boundary": fetch the queue first.
    in_queue = any(
        i.id == item_id for i in queue_controller.state.queue
    )
    if not in_queue:
        raise HTTPException(404, "item not in queue (already played, removed, or unknown id)")
    moved = await queue_controller.move(item_id, direction)
    return {"ok": True, "moved": moved}


@app.post("/api/skip")
async def skip():
    """Advance to the next queued item. If skip leaves the queue empty
    AND something was playing before, send SLEEP to the TV — a manual
    skip past the last item is a clear signal the user is done, so put
    the TV into the ambient screensaver."""
    _require_paired()
    # Something is playing if our queue owns it OR Lounge actively reports it.
    # The second clause matters: a video started from the TV remote, or one our
    # queue ceded via the external-switch logic, leaves state.current None while
    # the UI still renders a "Now playing" card off the Lounge observation.
    # Gating solely on state.current made Skip a silent no-op in exactly that
    # state. `Playing` specifically — a cached Paused/Stopped observation is a
    # stale ghost, not playback, and must not trigger the idle sequence.
    lng = queue_controller.state.lounge or {}
    lounge_playing = bool(
        lng.get("available")
        and lng.get("video_id")
        and lng.get("current_time") is not None
        and lng.get("state") == "Playing"
    )
    had_playback = queue_controller.state.current is not None or lounge_playing
    await queue_controller.skip()
    queue_now_idle = (
        queue_controller.state.current is None
        and not queue_controller.state.queue
    )
    if had_playback and queue_now_idle and state.remote and IDLE_KEYCODE:
        keycodes = [k.strip() for k in IDLE_KEYCODE.split(",") if k.strip()]
        for i, kc in enumerate(keycodes):
            if i > 0:
                await asyncio.sleep(IDLE_KEYCODE_DELAY)
            try:
                state.remote.send_key_command(kc)
                log.info("Skip emptied the queue — sent %s", kc)
            except Exception:
                log.warning("%s send failed", kc, exc_info=True)
        # Verify the sequence actually reached a screensaver. Under
        # heavy load the androidtvremote2 send queue occasionally
        # drops or delays a key relative to the TV's UI transition,
        # leaving the TV on the launcher instead of the wallpaper.
        # Retry the last key up to twice if we haven't landed on a
        # screensaver package after the sequence.
        for retry in range(2):
            await asyncio.sleep(0.5)
            if _get_current_app() in SCREENSAVER_PACKAGES:
                break
            log.info("Idle sequence didn't reach screensaver; resending %s",
                     keycodes[-1])
            try:
                state.remote.send_key_command(keycodes[-1])
            except Exception:
                log.warning("retry %s send failed", keycodes[-1], exc_info=True)
                break
    return {"ok": True}


@app.post("/api/pause")
async def pause_playback():
    """Pause the current video on the TV (via Cast) and freeze auto-advance.
    Idempotent — repeated calls have no extra effect."""
    await queue_controller.pause()
    return {"ok": True, "paused": queue_controller.state.paused}


@app.post("/api/resume")
async def resume_playback():
    """Resume the current video on the TV (via Lounge or MEDIA_PLAY) and
    re-enable auto-advance. If the queue stalled with no current item but
    with items queued, starts the next one. Idempotent."""
    await queue_controller.resume()
    return {"ok": True, "paused": queue_controller.state.paused}


@app.post("/api/clear")
async def clear():
    await queue_controller.clear()
    return {"ok": True}


class SeekReq(BaseModel):
    # Absolute seek target: float seconds OR a string we'll parse via
    # parse_time_input (accepts '1:23', '1:23:45', '90', '90s', '1h30m'...).
    to: Optional[str] = None
    # Relative offset in seconds (positive = forward, negative = back).
    # When both are given, `to` wins.
    by: Optional[float] = None


@app.post("/api/seek")
async def seek(req: SeekReq):
    """Seek the currently-playing video. Supply either:
      - `to`: absolute target time (string, flexible parsing)
      - `by`: relative offset in seconds (e.g. -10 or +30)

    Returns the resolved target (in seconds) on success. Requires Lounge
    to be connected — without a live Lounge session we have no seek
    primitive on this hardware (the remote-protocol KEYCODE_MEDIA_*_FF
    keys aren't a thing across TV firmwares)."""
    if state.lounge_monitor is None or not state.lounge_monitor.is_connected:
        raise HTTPException(503, "Lounge not connected; can't seek")

    if req.to is not None:
        target = parse_time_input(req.to)
        if target is None:
            raise HTTPException(400, f"could not parse time {req.to!r}")
    elif req.by is not None:
        # Relative seek needs a current position to anchor on.
        obs = state.lounge_monitor.observation
        if obs.current_time is None:
            raise HTTPException(
                503, "no current playback position from Lounge; can't seek by offset",
            )
        target = float(obs.current_time) + float(req.by)
        if target < 0:
            target = 0.0
    else:
        raise HTTPException(400, "supply either `to` or `by`")

    ok = await state.lounge_monitor.seek_to(target)
    if not ok:
        raise HTTPException(502, "Lounge seek_to call failed")
    return {"ok": True, "target": target}


@app.post("/api/volume/{action}")
async def volume(action: str):
    """Adjust volume by relaying a keycode over HDMI-CEC.

    The streamer translates these into CEC volume commands aimed at whatever
    is producing the sound — TV speakers, a soundbar, or an AV receiver — so
    there is nothing to configure and no brand to pick.

    Requires CEC volume control to be enabled on the device (Android's
    default, but not universal). When it's off the keypress is accepted and
    silently not translated, and these buttons do nothing; see the README.
    """
    keycode = VOLUME_KEYCODES.get(action)
    if keycode is None:
        raise HTTPException(404, "unknown volume action — use up, down, or mute")
    if state.remote is None:
        raise HTTPException(503, "not paired with a TV")
    try:
        state.remote.send_key_command(keycode)
    except Exception:
        log.warning("volume %s (%s) failed", action, keycode, exc_info=True)
        raise HTTPException(502, "volume command failed")
    return {"ok": True, "action": action}


@app.get("/api/events")
async def events(request: Request):
    """SSE stream of QueueState snapshots. Each event carries the full state;
    clients replace their UI from the snapshot — no diffing."""
    initial = queue_controller.snapshot()
    stream = broadcaster.subscribe(initial=initial)

    async def _formatted():
        try:
            async for event in stream:
                # No 'event:' name field — keep them all as default-typed
                # messages so the client's single onmessage handler catches
                # them. The transition type is preserved as event['type'] in
                # the JSON payload for clients that want animation cues.
                yield {"data": json.dumps(event)}
        finally:
            await stream.aclose()

    return EventSourceResponse(_formatted())


# ── legacy one-shot play (kept for v0 webhook callers) ─────────────────────
@app.post("/api/play")
async def play(req: AddReq, request: Request):
    """LEGACY: clear queue + replace current with this item, atomically.

    Same rate-limit bucket as /api/queue. Backward compatible with v0 callers.
    Prefer /api/queue for new clients.
    """
    _require_paired()
    _check_rate_limit(request)
    raw = req.url or req.video_id or ""
    vid = extract_video_id(raw)
    if not vid:
        raise HTTPException(400, "could not extract a YouTube video ID from input")
    start_s = extract_start_seconds(raw)

    was_off = False
    if state.remote:
        try:
            was_off = not state.remote.is_on
        except Exception:
            was_off = False

    item = await _build_queue_item(vid, start_s=start_s)
    try:
        await queue_controller.replace_with(item)
    except ConnectionClosed:
        raise HTTPException(503, "TV connection lost; reconnecting in background")

    return {"ok": True, "video_id": vid, "tv_was_off": was_off}


# ── static index served at / (no static dir; index.html lives at repo root) ──
@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    """Liveness probe for container orchestrators (Portainer, k8s, etc.).
    Returns 200 if the FastAPI loop is responsive — does not assert anything
    about TV connection or pairing state. For richer status, use /api/status."""
    return {"ok": True}


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)
