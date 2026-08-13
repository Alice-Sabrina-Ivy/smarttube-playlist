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
from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
import platform
import re
import sys
import time
from importlib.metadata import version as metadata_version
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
from metadata import FETCH_TIMEOUT_S as METADATA_TIMEOUT_S, Metadata, fetch_metadata
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
        "com.google.android.apps.tv.dreamx,com.google.android.backdrop,"
        "com.android.dreams.basic,com.neilturner.aerialviews",
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
# Which keycode wakes a sleeping device. POWER is a TOGGLE, and that is the
# crux: hardware that ignores a toggle while asleep can never be woken, and
# raises no error to say so. NVIDIA Shield is the known case — its "Simplified
# wake buttons" setting makes a network-delivered POWER a silent no-op, and a
# Shield-specific Home Assistant integration works around it with a separate
# non-toggle power-on command carried on NVIDIA's own protocol (see CLAUDE.md).
#
# We don't speak that protocol, so instead of guessing at a better default,
# make it a variable a tester can turn: WAKEUP (224) and TV_POWER (177) are the
# candidates worth trying on unfamiliar hardware. WAKEUP is measured to be
# silently dropped on Google TV, which is exactly why this stays per-install
# rather than becoming the new default.
WAKE_KEYCODE = os.environ.get("WAKE_KEYCODE", "POWER").strip() or "POWER"
# Timeouts inside tv_play's launch path. Module-level so tests can stub
# them down to milliseconds for fast unit tests.
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

# ── beta-only diagnostics ────────────────────────────────────────────────────
# This project is verified on hardware the maintainer owns. Supporting anything
# else means asking someone who owns it to tell us what their device does — and
# a list of twenty questions over text message is a poor way to do that.
#
# So every device event we observe goes into a small ring buffer, and
# /api/diagnostics renders it as one pasteable report. The single most valuable
# field is the history of `current_app` values: it is how we learn what a given
# device calls its screensaver and its launcher, which is exactly the thing that
# breaks launching on unfamiliar hardware (screensavers silently swallow
# app-launch intents, so a screensaver we don't recognise is never dismissed).
#
# Bounded on purpose: a long-running container must not grow a log forever.
DEVICE_LOG_MAX = 200
_device_log: deque = deque(maxlen=DEVICE_LOG_MAX)
# Stamped at import so a report can say how long this container had been up.
# A box that has just restarted behaves differently from one that has been
# running a fortnight, and that difference has explained more than one
# "it worked yesterday".
_BOOT_MONO = time.monotonic()
_BOOT_AT = datetime.now(timezone.utc).isoformat()
# Counts remote reconnects. A device that drops off the LAN in standby is the
# most likely Shield-in-sleep behaviour, and without this a wake failure looks
# identical to a command that was never delivered.
_remote_reconnects = 0

# Foreground packages that are expected and therefore not screensaver suspects.
# Launchers are included because "sitting on the home screen" is normal, not a
# fault — listing them keeps the suspect list short enough to act on.
KNOWN_BENIGN_PACKAGES = frozenset({
    "com.google.android.tvlauncher",         # Android TV launcher
    "com.google.android.apps.tv.launcherx",  # Google TV launcher
    "com.google.android.leanbacklauncher",   # older Android TV launcher; ships
    "com.google.android.leanbacklauncher.recommendations",  # on Shield too
    "com.android.tv.settings",
    "com.google.android.katniss",            # Assistant / search overlay
    "android",                               # the system app-chooser dialog
    "com.google.android.tvrecommendations",  # recommendations service; can
                                             # flash foreground on older ATV
    "com.android.systemui",                  # transient system surfaces
    # Bell's Fibe TV app, and the HOME target on a Bell Streamer — so it is
    # both a normal foreground app and the device's home screen. Listed
    # because _looks_like_a_launcher can't catch it: nothing in the name says
    # "launcher", so without this a Bell report would hand the tester a
    # pasteable SCREENSAVER_PACKAGES line containing their own home screen.
    "com.quickplay.android.bellmediaplayer",
    # com.android.vending (Play Store) is deliberately NOT here: it appearing
    # right after a launch attempt means the configured SMARTTUBE_PACKAGE is
    # not installed under that id (the store listing opened instead) — its
    # presence in the events log is a diagnostic signal, not noise.
})


# pyytlounge sends the Lounge token as a URL QUERY PARAMETER on every bind
# call, and aiohttp's ClientResponseError stringifies as "... url=<the url>".
# So any Lounge failure we log with exc_info, or embed in a self-test report as
# repr(exc), can carry a live token — one that grants control of the user's
# YouTube playback. Session ids are the same class of problem.
_SECRET_QS_RE = re.compile(
    r"(?i)\b(loungeIdToken|SID|gsessionid|access_token|token)=[^&\s'\")]+"
)


def _redact_secrets(text: str) -> str:
    """Strip credential-bearing query parameters out of arbitrary text."""
    return _SECRET_QS_RE.sub(r"\1=<redacted>", text)


class _RedactingFormatter(logging.Formatter):
    """Scrub secrets from the FULLY RENDERED log line, tracebacks included.

    Redacting at format time rather than in a Filter is deliberate, and both
    reasons are load-bearing:

    1. A Filter runs before formatting, so it can never touch a rendered
       traceback — which is exactly where a leaked URL shows up.
    2. Inspecting `record.args` / `exc.args` for strings misses the case that
       matters most: aiohttp's ClientResponseError keeps a RequestInfo OBJECT
       in args[0], and the URL only becomes text when the formatter calls its
       __str__. A string-typed check sails straight past it.

    Wraps whatever formatter the handler already had, so log layout is
    unchanged.
    """

    def __init__(self, inner: logging.Formatter):
        super().__init__()
        self._inner = inner

    def format(self, record: logging.LogRecord) -> str:
        try:
            return _redact_secrets(self._inner.format(record))
        except Exception:
            return self._inner.format(record)  # logging must never raise


def _install_log_redaction() -> None:
    """Wrap every root handler's formatter. Called after basicConfig, which is
    what installs the handler in the first place — attaching earlier would
    silently wrap nothing."""
    for handler in logging.getLogger().handlers:
        current = handler.formatter or logging.Formatter()
        if not isinstance(current, _RedactingFormatter):
            handler.setFormatter(_RedactingFormatter(current))


_install_log_redaction()


def _record_device_event(kind: str, value) -> None:
    """Append one observed device event. Never raises: diagnostics must not be
    able to break playback."""
    try:
        _device_log.append({
            "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "value": str(value),
        })
    except Exception:
        # Non-fatal by design — diagnostics must never break playback — but
        # logged, because a silent except here once hid a NameError and made
        # the report claim the device had produced no events at all.
        log.warning("diagnostics: could not record %s event", kind, exc_info=True)


def _diagnostic_host() -> Optional[str]:
    """The TV's address, unmasked.

    Beta builds report the real address: it is genuinely useful when diagnosing
    an unfamiliar device (wrong subnet, wrong device, stale pairing), and these
    reports go to someone the operator chose to share them with. Credentials are
    still never included — the certificate, its key and the Lounge token stay
    out, and that part is not negotiable.

    If a report is pasted somewhere public, note this is an RFC1918 address and
    is meaningless outside the network it belongs to.
    """
    return state.host


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
    _record_device_event("current_app", new_app)
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
    global _remote_reconnects
    if available:
        _remote_reconnects += 1
    # Logged because a device that drops off the LAN in standby is the most
    # likely Shield-in-sleep behaviour, and without it a failed wake looks
    # identical to a command that was never delivered.
    _record_device_event("remote_available", available)
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
    _record_device_event("is_on", is_on)
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
    # Seed the diagnostics log with what the device looks like right now. The
    # buffer otherwise only records *changes*, so a tester who opens the report
    # before touching anything would see an empty list and reasonably conclude
    # the feature was broken.
    _record_device_event("connected", "is_on=%s current_app=%s" % (
        getattr(remote, "is_on", None), state.last_current_app))
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
    3. SmartTube NOT foreground → tv_play() sends the deep-link Intent,
       which foregrounds SmartTube and starts playback in one step.
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
    # Capture session-level transitions in the device log BEFORE the
    # suppression gate — the report needs the raw truth. Connect/disconnect
    # timestamps separate SmartTube's own behaviour (its Lounge connection
    # self-launching the app, waking Shields at night — upstream #3170) from
    # anything we sent; the FINISHED line carries position/duration so the
    # known ~40-minute mid-video process-kill on Shield (upstream #5951) has a
    # recognisable signature instead of looking like our timer misfiring.
    if event_type in ("lounge.connected", "lounge.disconnected"):
        _record_device_event(event_type, state.suppress_lounge and "(suppressed)" or "")
    elif event_type == "lounge.finished":
        _record_device_event(
            "lounge_finished",
            f"{observation.video_id}@{observation.current_time}/{observation.duration}",
        )

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
        log.info("TV reports off — sending %s and waiting for boot",
                 WAKE_KEYCODE)
        await queue_controller.set_waking(True)
        try:
            loop = asyncio.get_running_loop()
            wake_started_at = loop.time()
            state.remote.send_key_command(WAKE_KEYCODE)
            _record_device_event("wake_sent", WAKE_KEYCODE)
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
                # the wake key once before giving up.
                log.warning(
                    "TV did not report on within %.0fs — connection likely "
                    "stale; reconnecting remote and retrying %s",
                    WAKE_TIMEOUT,
                    WAKE_KEYCODE,
                )
                if await _reconnect_remote():
                    if state.remote is not None and not bool(state.remote.is_on):
                        try:
                            state.remote.send_key_command(WAKE_KEYCODE)
                        except Exception:
                            log.warning("Retry %s after reconnect failed",
                                        WAKE_KEYCODE, exc_info=True)
                        came_on = await _wait_for_tv_on(WAKE_TIMEOUT, WAKE_POLL)
                    else:
                        # Reconnect happened to land while TV was already
                        # on (maybe the first POWER did take effect, just
                        # not visibly via the dead connection).
                        came_on = bool(state.remote and state.remote.is_on)
                if not came_on:
                    # Two attempts on a fresh connection both went nowhere, so
                    # this is very unlikely to be our side. The usual cause is
                    # the device ignoring this keycode while asleep.
                    log.warning(
                        "TV still not reporting on after reconnect + retry "
                        "with %s; trying launch anyway. If this device never "
                        "wakes, it may ignore that keycode while asleep — try "
                        "WAKE_KEYCODE=WAKEUP or WAKE_KEYCODE=TV_POWER. On "
                        "NVIDIA Shield also check Settings > Remotes & "
                        "accessories > Simplified wake buttons.",
                        WAKE_KEYCODE,
                    )
                    _record_device_event("wake_failed", WAKE_KEYCODE)
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
    # dreamx silently swallows send_launch_app_command intents of every
    # kind, including the vnd.youtube.launch:// one we rely on — leaving us
    # stuck with no error to see. Any
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
    # one step. Do NOT add a separate app-launch command before this:
    # the Intent already foregrounds SmartTube, and sending both is the
    # double-play regression (invariant 1).
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
        # Tally guest activity so a self-test can say whether someone used the
        # page mid-run. Its own start call doesn't count as interference.
        if request.url.path != "/api/selftest":
            global _client_write_count
            _client_write_count += 1
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
    SmartTube's Settings -> Remote control screen; older builds called it
    "Link with TV code"). Persists the resulting auth
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
def _reject_during_self_test() -> None:
    """Refuse play requests while a self-test is driving the TV.

    Without this the two send play signals concurrently — the double-play
    regression this project has fixed more times than any other (invariant 1).
    QueueController._send_to_tv cancels only tasks it created, so it cannot
    see the self-test; the guard has to live here.
    """
    if _self_test_active:
        raise HTTPException(
            409,
            "A device self-test is running — it will finish in about "
            f"{int(_self_test['eta_s'])}s. Try again then.",
        )


def _require_paired() -> None:
    if not state.remote or state.pairing_in_progress:
        raise HTTPException(503, "not paired — visit the web UI to pair")


def _negotiated_features() -> Optional[list]:
    """Which protocol features the device and library agreed on.

    Reaches for a private attribute because the library exposes no public
    accessor; guarded so a library change degrades to None rather than
    breaking the report.
    """
    try:
        proto = getattr(state.remote, "_remote_message_protocol", None)
        active = getattr(proto, "_active_features", None)
        if active is None:
            return None
        return {
            "negotiated": sorted(
                f.name for f in type(active) if f in active and f.name
            ),
            # The raw bitmask matters on its own: a missing KEY bit is the
            # documented cause of "every keycode silently dead" (remedy is
            # device-side: clear the Android TV Remote Service app's storage
            # and re-pair), and a missing IME bit explains empty current_app.
            "raw": int(active),
        }
    except Exception:
        return None


def _qc_safe(fn):
    """Read from queue_controller if it exists yet.

    It is assigned in the FastAPI lifespan, not at module level, so the NAME
    is absent until startup runs — an `is not None` guard raises NameError
    rather than helping. The diagnostics endpoint must never be the thing that
    500s, since it is what someone reaches for when everything else is broken.
    """
    qc = globals().get("queue_controller")
    if qc is None:
        return None
    try:
        return fn(qc)
    except Exception:
        return None


def _diagnostic_environment() -> dict:
    """Build and config provenance.

    Every question of the form "which version were you running / what are your
    settings / how long had it been up" is a round trip, and all of it is
    free to include. Env values are read from OUR OWN named constants, never
    by walking os.environ — a NAS container's environment routinely holds
    unrelated secrets.
    """
    libs = {}
    for name in ("androidtvremote2", "pyytlounge", "httpx", "fastapi"):
        try:
            libs[name] = metadata_version(name)
        except Exception:
            libs[name] = "unknown"
    return {
        "version": VERSION,
        "channel": "beta",
        "booted_at": _BOOT_AT,
        "uptime_s": round(time.monotonic() - _BOOT_MONO, 1),
        "python": sys.version.split()[0],
        "platform": f"{platform.system()}/{platform.machine()}",
        "libs": libs,
        # The constants actually in force, so observed timings can be judged
        # without the reader looking any of them up.
        "settings": {
            "smarttube_package": SMARTTUBE_PACKAGE,
            "screensaver_packages": sorted(SCREENSAVER_PACKAGES),
            "screensaver_dismiss_key": SCREENSAVER_DISMISS_KEY,
            "screensaver_dismiss_delay": SCREENSAVER_DISMISS_DELAY,
            "idle_keycode": IDLE_KEYCODE,
            "wake_keycode": WAKE_KEYCODE,
            "wake_delay": WAKE_DELAY,
            "wake_timeout": WAKE_TIMEOUT,
            "lounge_connect_timeout": LOUNGE_CONNECT_TIMEOUT,
            "lounge_observation_timeout": LOUNGE_OBSERVATION_TIMEOUT,
            "resume_verify_timeout": RESUME_VERIFY_TIMEOUT,
            "metadata_timeout": METADATA_TIMEOUT_S,
        },
    }


def _diagnostic_lounge() -> dict:
    """Everything about the Lounge session EXCEPT anything that could control it.

    Never include: lounge_id_token, refresh_token, screen_id, _sid, _gsession,
    the serialised auth blob, or any repr of the YtLoungeApi object — its
    __repr__ prints the token and both session ids. A leaked token grants
    control of the tester's YouTube playback, and this document gets pasted
    into a chat window.

    `paired` and `connected` are separate answers: paired-but-not-connected
    means an expired token or SmartTube not running, and those need different
    advice. Token AGE is included (file mtime only, never contents) because
    Lounge tokens expire in roughly two weeks.
    """
    out = {"paired": _is_lounge_paired(), "connected": False}
    mon = state.lounge_monitor
    if mon is None:
        out["monitor_running"] = False
        return out
    out["monitor_running"] = True
    try:
        out["connected"] = bool(mon.is_connected)
    except Exception:
        pass
    try:
        obs = mon.observation
        out["observation"] = {
            "available": obs.available,
            "video_id": obs.video_id,
            "current_time": obs.current_time,
            # The field the near-end rule depends on. Its absence means
            # auto-advance can only ever run off the scraped duration.
            "duration": obs.duration,
            "state": obs.state,
        }
    except Exception:
        pass
    try:
        api = getattr(mon, "_api", None)
        if api is not None:
            # Private attrs on purpose: the public screen_name /
            # screen_device_name properties raise when not linked.
            out["screen_name"] = getattr(api, "_screen_name", None)
            info = getattr(api, "_device_info", None)
            if isinstance(info, dict):
                out["receiver"] = {
                    k: info.get(k)
                    for k in ("brand", "model", "deviceType", "clientName")
                    if info.get(k)
                }
    except Exception:
        pass
    try:
        if LOUNGE_AUTH_FILE.exists():
            age = time.time() - LOUNGE_AUTH_FILE.stat().st_mtime
            out["token_age_days"] = round(age / 86400.0, 1)
    except Exception:
        pass
    return out


def _diagnostic_warnings() -> list:
    """Conditions that make a report misleading if not called out."""
    out = []
    if state.remote is not None and not _get_current_app():
        out.append(
            "current_app is empty. This is derived from the remote protocol's "
            "IME feature; if IME is disabled on the device, foreground-app "
            "detection stops working and the screensaver-dismiss, kill-switch "
            "and SmartTube-foreground checks all silently degrade."
        )
    if state.remote is not None and not _is_lounge_paired():
        out.append(
            "Lounge is not paired, so playback position and end-of-video "
            "detection fall back to a scraped-duration timer."
        )
    return out


def _build_diagnostics() -> dict:
    """One pasteable report for someone beta testing on hardware we don't own.

    Beta builds only. Never contains credentials: no certificate, no private
    key, no Lounge token. It does include the TV's local address, which is
    useful for diagnosis — see _diagnostic_host.

    Split out from the route so the self-test can embed the same payload
    directly; keep it that way, so the two can never drift.
    """
    seen = [e["value"] for e in _device_log if e["kind"] == "current_app"]
    suspects = sorted({
        pkg for pkg in seen
        if pkg
        and pkg != SMARTTUBE_PACKAGE
        and pkg not in SCREENSAVER_PACKAGES
        and pkg not in KNOWN_BENIGN_PACKAGES
    })

    remote = state.remote
    return {
        "version": VERSION,
        "channel": "beta",
        "tv_paired": _is_tv_paired(),
        "lounge_paired": _is_lounge_paired(),
        "lounge_connected": bool(
            state.lounge_monitor and state.lounge_monitor.is_connected
        ),
        "host": _diagnostic_host(),
        "device": {
            # Free from the protocol, and firmware version is load-bearing:
            # SHIELD Experience before 9.2 is documented to ignore the remote
            # for ~60s after waking, which is longer than our whole wake
            # sweep and would read as "this device refuses network wake".
            "info": (
                dict(getattr(remote, "device_info", None) or {}) if remote else None
            ),
            "is_on": getattr(remote, "is_on", None) if remote else None,
            "current_app": _get_current_app(),
            # The field that distinguished our two known devices: the Streamer
            # reports max=0 and drives volume over CEC, the Chromecast reports
            # a real range and attenuates its own output.
            "volume_info": (
                dict(getattr(remote, "volume_info", None) or {}) if remote else None
            ),
            # current_app is derived from the IME feature. If IME isn't in the
            # negotiated set, foreground detection cannot work on this device
            # and half the app degrades silently — worth knowing directly
            # rather than inferring it from empty readings.
            "protocol_features": _negotiated_features(),
        },
        "config": {
            "smarttube_package": SMARTTUBE_PACKAGE,
            "screensaver_packages": sorted(SCREENSAVER_PACKAGES),
            "screensaver_dismiss_key": SCREENSAVER_DISMISS_KEY,
            "idle_keycode": IDLE_KEYCODE,
            "wake_keycode": WAKE_KEYCODE,
            "wake_delay": WAKE_DELAY,
        },
        # Packages seen in the foreground that we neither expect nor know how to
        # dismiss. On new hardware this is usually the screensaver, and usually
        # the reason a video won't start.
        "unrecognised_foreground_packages": suspects,
        # current_app is not a first-class protocol property: androidtvremote2
        # derives it from the IME feature. With IME disabled it is permanently
        # empty, which silently degrades the kill-switch, suppress_lounge,
        # screensaver detection and the SmartTube-foreground check all at once,
        # with no error raised anywhere. Say so rather than reporting a device
        # that merely looks idle.
        "warnings": _diagnostic_warnings(),
        "environment": _diagnostic_environment(),
        "lounge": _diagnostic_lounge(),
        # Our own view of the world. Probes read the Lounge observation
        # directly, bypassing suppress_lounge — so a probe can report success
        # while the tester's page showed nothing playing. That contradiction
        # is unresolvable without seeing this.
        "app_state": {
            "suppress_lounge": state.suppress_lounge,
            "last_current_app": state.last_current_app,
            "pairing_in_progress": getattr(state, "pairing_in_progress", None),
            "remote_reconnects": _remote_reconnects,
            "has_pending_sends": _qc_safe(lambda qc: qc.has_pending_sends()),
        },
        "queue": _qc_safe(lambda qc: qc.snapshot()),
        "events": list(_device_log),
    }


@app.get("/api/diagnostics")
async def diagnostics():
    """The passive report, unchanged: reads state, sends nothing to the TV."""
    return _build_diagnostics()



# ── Beta: device self-test ───────────────────────────────────────────────────
#
# One button in the UI runs every probe below against the tester's real device
# and produces a single pasteable report.
#
# Why this exists rather than "run the test suite on their machine": the pytest
# suite cannot observe a TV, by design. Every test substitutes a fake for the
# device (invariant 6), so results are a pure function of (source, env vars) —
# identical on every machine on the same commit. The only signals that WOULD
# vary are env vars, and they are inverted: the wake-keycode assertions go red
# exactly when a Shield owner has applied the documented WAKE_KEYCODE fix. Real
# sends to a real device are the only thing that answers a firmware question.
#
# Three rules a probe must never break:
#
#  1. NEVER put the device to sleep. The wake sweep exists precisely because
#     some hardware ignores wake commands — so a probe that sleeps first can
#     strand the device with no recovery but the physical remote, in exactly
#     the case it was written to detect. Wake probes run ONLY when the device
#     is already off and skip themselves otherwise.
#  2. NEVER let a probe race tv_play. QueueController._send_to_tv cancels only
#     tasks it created, so a second sender outside that guard reintroduces the
#     double-play regression (invariant 1). Adds are refused with 409 for the
#     duration of a run.
#  3. NEVER stomp on someone's viewing. Several guests share this page and only
#     one pressed the button — the disruptive probes skip themselves when the
#     queue has a current item.

# Off switch for an operator who doesn't want a guest-pressable button that
# moves the TV. Default on: the whole point of the beta build is that the
# tester shouldn't have to configure anything.
SELF_TEST_ENABLED = os.environ.get("SELF_TEST", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

# Two stable, long-lived public videos. The short one is 19 seconds
# deliberately — a probe that hijacks someone's TV should hand it back fast.
# The long one gives the setPlaylist swap and the pause/resume probes
# something still playing to act on.
SELF_TEST_VIDEO_SHORT = os.environ.get("SELF_TEST_VIDEO", "jNQXAC9IVRw")
SELF_TEST_VIDEO_LONG = os.environ.get("SELF_TEST_VIDEO_LONG", "aqz-KE-bpKQ")
# Used to tell OUR playback apart from somebody else's. The idle guard re-reads
# live state, which is right — an add can land mid-run — but the probes start
# their clip outside the queue, so without this the run reads its own video as
# a stranger's viewing and stands down from the measurements it exists to take.
_SELF_TEST_VIDEO_IDS = frozenset((SELF_TEST_VIDEO_SHORT, SELF_TEST_VIDEO_LONG))

# Wake candidates tried in order. WAKE_KEYCODE first so a tester who already
# set it gets their answer confirmed rather than buried behind three others.
SELF_TEST_WAKE_CANDIDATES = ("POWER", "WAKEUP", "TV_POWER")

# How long each probe waits for the device to do something. Named rather than
# inline so tests can patch them down — and so the ETA the UI shows and the
# waits the probes actually take can be read side by side and kept honest.
# Intent-only wake. Tied to production's WAKE_TIMEOUT for the same reason the
# key sweep is: a shorter window turns "woke slowly" into "never woke".
SELF_TEST_WAKE_INTENT_TIMEOUT = WAKE_TIMEOUT
# Per keycode candidate. Tied to production's WAKE_TIMEOUT rather than set
# independently: a shorter probe window mis-attributes the win. A device that
# wakes at ~25s would have POWER time out, then WAKEUP time out, then wake
# during TV_POWER's wait — crediting TV_POWER for POWER's work, and telling
# the tester to configure a keycode that did nothing. The sweep is slow, but
# it only ever runs on a device that is already asleep, where nothing else
# can run anyway.
SELF_TEST_WAKE_KEY_TIMEOUT = WAKE_TIMEOUT
SELF_TEST_PLAY_TIMEOUT = 20.0          # deep link -> foreground + Lounge sees it
SELF_TEST_SWAP_TIMEOUT = 15.0          # setPlaylist -> Playing on the new video
SELF_TEST_TRANSPORT_TIMEOUT = 5.0      # pause/resume -> state confirmed
SELF_TEST_IDLE_TIMEOUT = 8.0           # IDLE_KEYCODE -> whatever it lands on
# The short probe clip is ~19s. Waiting it out is the only way to observe an
# end-of-video, which is the entire auto-advance mechanism.
SELF_TEST_FINISH_TIMEOUT = 40.0
# How long after a successful wake we sample for the device becoming USABLE.
# is_on flips within ~1s on instant-on hardware while the OS is still deaf;
# SHIELD Experience before 9.2 documents "remote stops responding for 60
# seconds after wake from sleep" (NVIDIA 9.2 release notes). 75s covers that
# window with margin, and the sampling breaks out on the first sign of life —
# a healthy device costs seconds, and only the sleep run pays at all.
SELF_TEST_READINESS_TIMEOUT = 75.0
# The screensaver suspect protocol: how long an unrecognised foreground
# package gets to prove a launch Intent landed before we conclude it was
# swallowed (the defining screensaver behaviour, per the dreamx finding).
SELF_TEST_SUSPECT_TIMEOUT = 8.0
SELF_TEST_FOREGROUND_SAMPLES = 10      # current_app liveness sampling
SELF_TEST_FOREGROUND_INTERVAL = 0.5
SELF_TEST_POLL = 0.5                   # how often a probe re-reads device state


class _ProbeSkip(Exception):
    """Probe doesn't apply to the state the device is in. Not a failure."""


class _ProbeUnmeasurable(Exception):
    """Probe ran but the instrument it depends on is broken on this device.

    Distinct from failure on purpose: "current_app is always empty so we
    cannot tell whether SmartTube came to the foreground" is a fact about our
    visibility, not about the device's behaviour, and reporting it as a
    failure would send someone chasing a bug that isn't there.
    """


_self_test_active = False
_self_test_task: Optional[asyncio.Task] = None
# Counts state-changing HTTP requests (incremented in the CSRF middleware,
# which already sees every mutating call). A run compares start and end: if a
# guest added a video mid-run, the measurements are suspect and the report has
# to say so rather than let someone chase a phantom device fault.
_client_write_count = 0

# Answers posted back from the UI after a run. Kept beside the run rather
# than inside it so a late answer can still be folded into an existing report.
_self_test_answers: dict = {}

_self_test: dict = {
    "status": "idle",       # idle | running | done | error
    "run_id": None,
    "started_at": None,
    "eta_s": 0.0,
    "probes": [],
    "report": None,
}


async def _probe_snapshot(ctx: dict) -> dict:
    """Everything the passive report already knows. Free, and it anchors the
    rest: whoever reads the report needs the config in effect to interpret
    every probe below it."""
    return _build_diagnostics()


async def _probe_snapshot_after(ctx: dict) -> dict:
    """The same report again, after the run has moved the device.

    Probe 1's copy of `_device_log` is frozen at run start, so every
    foreground transition the run itself caused — the screensaver it dismissed,
    the launcher it passed through, where IDLE_KEYCODE landed — was invisible
    in the report. That log is what BETA-TESTING.md calls the most useful
    thing in the whole thing, and on unfamiliar hardware the packages a run
    surfaces are exactly the ones we don't yet know about.

    Also re-runs the foreground liveness check when a wake probe woke the
    device, since the first attempt correctly skipped on a sleeping TV.
    """
    if ctx.get("woke_by_intent") or ctx.get("woke_by_key"):
        try:
            ctx["foreground_recheck"] = await _probe_foreground_readability(ctx)
        except (_ProbeSkip, _ProbeUnmeasurable) as exc:
            ctx["foreground_recheck"] = {"skipped": str(exc)}
    out = _build_diagnostics()
    if "foreground_recheck" in ctx:
        out["foreground_recheck_after_wake"] = ctx["foreground_recheck"]
    return out


async def _probe_foreground_readability(ctx: dict) -> dict:
    """Can we read the foreground app at all, and does it ever change?

    This runs early because it is the instrument six later probes measure
    with. `current_app` is not a first-class protocol property — the library
    derives it from the IME feature — so on a device with IME disabled it is
    permanently empty and every foreground-dependent conclusion below becomes
    meaningless. Better to know that up front than to report six device
    failures that are really one blind spot of ours.
    """
    remote = state.remote
    if remote is not None and not bool(getattr(remote, "is_on", False)):
        # A sleeping device reports no foreground app, which is not the same
        # finding as "IME is disabled and foreground detection is broken".
        # Without this the TV-off run — the one BETA-TESTING.md explicitly
        # asks for — reports usable=false and marks everything downstream
        # unmeasurable, sending a tester to check a setting that is fine.
        raise _ProbeSkip(
            "device is off, so it reports no foreground app. This check needs "
            "an awake device — it re-runs automatically if a wake probe "
            "succeeds."
        )

    samples = []
    for i in range(SELF_TEST_FOREGROUND_SAMPLES):
        samples.append(_get_current_app() or "")
        if i < SELF_TEST_FOREGROUND_SAMPLES - 1:
            await asyncio.sleep(SELF_TEST_FOREGROUND_INTERVAL)
    non_empty = [s for s in samples if s]
    ctx["current_app_usable"] = bool(non_empty)
    return {
        "samples": samples,
        "non_empty": len(non_empty),
        "of": len(samples),
        "distinct": sorted(set(non_empty)),
        "usable": bool(non_empty),
        "note": (
            "usable=false means foreground detection is dead on this device; "
            "the screensaver-dismiss, kill-switch and SmartTube-foreground "
            "checks all silently degrade, and the probes below that rely on it "
            "are reported unmeasurable rather than failed."
        ),
    }


async def _probe_metadata(ctx: dict) -> dict:
    """Can the container reach YouTube's watch page, and how slow is it?

    An environment check, not a device one — but it belongs here because a
    failed scrape is not cosmetic: the fallback hands the duration timer 600
    seconds, so a 10-hour video gets skipped after 10 minutes. Without this
    probe that shows up in a report as a mysterious auto-advance bug.
    """
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    meta = await fetch_metadata(SELF_TEST_VIDEO_SHORT, client=state.http_client)
    elapsed = loop.time() - t0
    looks_like_fallback = (
        meta.title == SELF_TEST_VIDEO_SHORT
        and meta.channel == "unknown"
        and meta.duration_s == 600
    )
    return {
        "seconds": round(elapsed, 2),
        "timeout_s": METADATA_TIMEOUT_S,
        "title": meta.title,
        "channel": meta.channel,
        "duration_s": meta.duration_s,
        "is_live": meta.is_live,
        "scrape_failed": looks_like_fallback,
        "note": (
            "scrape_failed=true means the title/channel/duration trio is the "
            "fallback, not real data — auto-advance would fire at 10:00 on "
            "every video."
        ),
    }


async def _await_playback_observed(vid: str, timeout: float) -> Optional[float]:
    """Seconds until Lounge reports `vid` with a real position, or None.

    `current_time` as well as `video_id`, because the Lounge cloud cache
    reports a video id for a player that is not running (invariant 4).
    """
    mon = state.lounge_monitor
    if mon is None:
        return None
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    deadline = t0 + timeout
    while loop.time() < deadline:
        obs = mon.observation
        if obs.video_id == vid and obs.current_time is not None:
            return round(loop.time() - t0, 2)
        await asyncio.sleep(SELF_TEST_POLL)
    return None


async def _sample_post_wake_readiness(ctx: dict) -> dict:
    """After a wake: how long until the device is actually usable?

    `is_on` is a liar on instant-on hardware — it flips within ~1s while the
    OS is still booting, and on pre-9.2 SHIELD Experience the device then
    ignores the network for up to 60s ("remote stops responding for 60 seconds
    after wake from sleep", NVIDIA's own release notes). Our WAKE_DELAY=15
    expires inside that window, so a launch sent on schedule is silently
    swallowed and would previously have been misread as a deep-link failure.

    Strictly read-only: samples current_app, sends nothing. Breaks out on the
    first non-empty read, so a healthy device costs seconds.
    """
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    readable_after = None
    deadline = t0 + SELF_TEST_READINESS_TIMEOUT
    while loop.time() < deadline:
        if _get_current_app():
            readable_after = round(loop.time() - t0, 2)
            break
        await asyncio.sleep(SELF_TEST_POLL)
    if readable_after is not None:
        ctx["current_app_usable"] = True
    ctx["post_wake_readiness_s"] = readable_after
    return {
        "current_app_readable_after_wake_s": readable_after,
        "readiness_sample_limit_s": SELF_TEST_READINESS_TIMEOUT,
        "configured_wake_delay_s": WAKE_DELAY,
        "wake_delay_sufficient": (
            readable_after is not None and readable_after <= WAKE_DELAY
        ),
        "readiness_note": (
            "How long after waking the device became usable, measured from "
            "the wake, not from the keypress. null means it never did within "
            "the window: either foreground detection is off on this device "
            "(see the foreground probe) or it ignores the network for a while "
            "after waking — documented on SHIELD Experience before 9.2."
        ),
    }


async def _probe_launch_intent_wakes(ctx: dict) -> dict:
    """THE question: does an app-launch Intent by itself wake a sleeping device?

    If yes, hardware that ignores the POWER toggle while asleep can still be
    woken with machinery we already have — no second protocol, no extra
    pairing, no power button in the UI. Production already sends this Intent
    after a failed wake ("trying launch anyway") but records nothing, so a
    device that woke this way is indistinguishable in our logs from someone
    picking up the physical remote. This probe is the controlled version:
    no wake key first, so anything that happens is attributable to the Intent.

    Runs only when the device is ALREADY off. Never sleeps it to create the
    condition — see rule 1 above.
    """
    remote = state.remote
    if remote is None:
        raise _ProbeSkip("no remote connection")
    if bool(getattr(remote, "is_on", False)):
        raise _ProbeSkip(
            "device is already on. To test waking, put the STREAMING DEVICE "
            "itself to sleep (not just the TV picture \u2014 on a Shield or "
            "similar box, the box is what sleeps), then run the self-test "
            "again."
        )
    # Record BEFORE anything that can raise. This flag is what tells the
    # verdict that waking was genuinely tested; if a queued video or a dropped
    # connection made us skip out below, the verdict would otherwise tell the
    # tester to redo the one run they did correctly.
    ctx["device_was_off"] = True
    # This probe sends a real play signal, so it lives under the same rule as
    # the playback probes even though the device is asleep: if our queue owns
    # something, tv_play is the one that should be waking this device.
    _require_idle(ctx)

    deep_link = (
        f"vnd.youtube.launch://www.youtube.com/watch?v={SELF_TEST_VIDEO_SHORT}"
    )
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    try:
        remote.send_launch_app_command(deep_link)
    except Exception as exc:
        # A device asleep on WiFi may have dropped off the LAN entirely. That
        # is a finding, not a crash, and it is a completely different fix from
        # "the device ignores wake commands".
        ctx["intent_send_error"] = repr(exc)
        return {
            "device_was_off": True,
            "wake_key_sent": None,
            "woke": False,
            "send_error": _redact_secrets(repr(exc)),
            "note": (
                "The command could not even be sent \u2014 the device was "
                "unreachable on the network while asleep. That is a different "
                "problem from ignoring wake commands: check whether it is on "
                "WiFi and whether its network stays up in standby."
            ),
        }
    _record_device_event("selftest_launch_sent", "wake-probe (no wake key)")
    # Whoever sends a play signal owns undoing it. Without this the idle probe
    # skips and a device woken by this probe is left playing our clip — the
    # same bug the Playwright run caught in _probe_deep_link_play.
    ctx["launched"] = True
    woke = await _wait_for_tv_on(SELF_TEST_WAKE_INTENT_TIMEOUT, SELF_TEST_POLL)
    elapsed = loop.time() - t0
    ctx["woke_by_intent"] = woke

    # If the Intent woke it, it also STARTED this video — so adopt that as the
    # run's playback rather than leaving it unclaimed. Without this the best
    # possible outcome produced the least data: the play probe skips (rightly,
    # it would be a second signal for the same video), `played` stayed unset,
    # and swap and transport then skipped too.
    observed_s = None
    readiness = {}
    if woke:
        # Readiness first: it usually resolves in seconds and the playback
        # wait below then measures a device we know is listening.
        readiness = await _sample_post_wake_readiness(ctx)
        observed_s = await _await_playback_observed(
            SELF_TEST_VIDEO_SHORT, SELF_TEST_PLAY_TIMEOUT,
        )
        ctx["played"] = observed_s is not None

    return {
        **readiness,
        "device_was_off": True,
        "wake_key_sent": None,
        "woke": woke,
        "seconds": round(elapsed, 2) if woke else None,
        "lounge_saw_it_after_s": observed_s,
        "note": (
            "woke=true would mean a launch Intent alone wakes this device — a "
            "wake path needing no power command at all. woke=false is also a "
            "real answer and sends the sweep below."
        ),
    }


async def _probe_wake_keycodes(ctx: dict) -> dict:
    """Which keycode, if any, wakes this device?

    POWER is a toggle; hardware that ignores toggles while asleep cannot be
    woken and raises nothing to say so. This sweeps the candidates and names
    the winner, which is exactly the value a tester should then put in
    WAKE_KEYCODE. Only runs if the device is still off after the Intent probe.
    """
    remote = state.remote
    if remote is None:
        raise _ProbeSkip("no remote connection")
    if bool(getattr(remote, "is_on", False)):
        if ctx.get("woke_by_intent"):
            raise _ProbeSkip(
                "device already woke from the launch Intent — no wake key needed"
            )
        if ctx.get("device_was_off"):
            # It was asleep when the Intent went out and it is awake now, but
            # the Intent probe's wait expired before the state flip landed.
            # The Intent still did it. Without this the verdict asserts "the
            # device never woke" while snapshot_after shows is_on: true — the
            # kind of self-contradiction that costs a round trip to resolve.
            ctx["woke_by_intent"] = True
            return {
                "woke_with": None,
                "woke_late_from_intent": True,
                "attempts": [],
                "configured_wake_keycode": WAKE_KEYCODE,
                "note": (
                    "No keycode was needed. The device was asleep, the launch "
                    "Intent alone woke it, and it just took longer than the "
                    "Intent probe waited — see wake_intent."
                ),
            }
        raise _ProbeSkip(
            "device is already on. To test waking, put the STREAMING DEVICE "
            "itself to sleep (not just the TV picture \u2014 on a Shield or "
            "similar box, the box is what sleeps), then run the self-test "
            "again."
        )

    # Configured key first, then the rest, de-duplicated.
    candidates = [WAKE_KEYCODE]
    for k in SELF_TEST_WAKE_CANDIDATES:
        if k not in candidates:
            candidates.append(k)

    attempts = []
    winner = None
    for key in candidates:
        loop = asyncio.get_running_loop()
        # Re-read power state before EVERY candidate, not just at the top.
        # POWER and TV_POWER are toggles: if an earlier candidate actually
        # worked but _wait_for_tv_on missed the transition (is_on lags on
        # Quick Resume hardware), sending the next one would turn a
        # just-woken device straight back off — the exact stranding this
        # probe is supposed to protect against, arrived at from the other
        # direction.
        try:
            if bool(getattr(remote, "is_on", False)):
                winner = (winner or attempts[-1]["key"]) if attempts else None
                if winner:
                    attempts[-1]["woke"] = True
                    attempts[-1]["late"] = True
                    # Must be set here too, not only on the fast path: it is
                    # what tells snapshot_after to re-run the foreground check
                    # now that the device is actually awake.
                    ctx["woke_by_key"] = winner
                break
        except Exception:
            pass
        t0 = loop.time()
        try:
            remote.send_key_command(key)
        except Exception as exc:
            attempts.append({"key": key, "woke": False, "error": repr(exc)})
            continue
        woke = await _wait_for_tv_on(SELF_TEST_WAKE_KEY_TIMEOUT, SELF_TEST_POLL)
        attempts.append({
            "key": key,
            "woke": woke,
            "seconds": round(loop.time() - t0, 2) if woke else None,
        })
        if woke:
            winner = key
            ctx["woke_by_key"] = key
            break

    readiness = await _sample_post_wake_readiness(ctx) if winner else {}
    return {
        **readiness,
        "attempts": attempts,
        "woke_with": winner,
        "configured_wake_keycode": WAKE_KEYCODE,
        "note": (
            "late=true on an attempt means the device reported on only after "
            "we had given up waiting for it — that key DID work, just slower "
            "than the timeout. "
            "woke_with names the keycode to put in WAKE_KEYCODE. If every "
            "attempt failed, the device is refusing network wake entirely — "
            "on an NVIDIA Shield check Settings > Remotes & accessories > "
            "Simplified wake buttons, which makes a network POWER a no-op."
        ),
    }


def _looks_like_a_launcher(pkg: str) -> bool:
    """Cheap name heuristic for "this is a home screen, not a screensaver".

    We cannot enumerate every carrier's launcher — a Bell, Rogers or Sky box
    ships its own — but launchers are named like launchers, and the cost of
    being wrong is asymmetric: calling a screensaver a launcher just means we
    ask the tester, while calling a LAUNCHER a screensaver puts it in
    SCREENSAVER_PACKAGES and fires a dismiss key every time anyone sits on
    their own home screen.
    """
    p = (pkg or "").lower()
    return any(t in p for t in ("launcher", ".home", "homescreen", "tvhome"))


async def _probe_screensaver(ctx: dict) -> dict:
    """What is this device's screensaver called, and does our dismiss key work?

    The single most common reason playback fails on unfamiliar hardware:
    screensavers silently swallow launch Intents, so an unrecognised one means
    videos never start and nothing errors.
    """
    if not ctx.get("current_app_usable"):
        raise _ProbeUnmeasurable("current_app is unreadable on this device")
    current = _get_current_app()
    if not current:
        raise _ProbeSkip("no foreground app reported right now")
    if current not in SCREENSAVER_PACKAGES:
        if current in KNOWN_BENIGN_PACKAGES or current == SMARTTUBE_PACKAGE:
            return {
                "screensaver_active": False,
                "foreground": current,
                "note": (
                    "No screensaver was up when the test ran. To capture "
                    "yours, leave the device untouched until the screensaver "
                    "appears and run the self-test again from a phone."
                ),
            }
        # An UNRECOGNISED, non-launcher package in the foreground — and the
        # guide explicitly asks for a run with the screensaver up, so in that
        # flow this package IS the screensaver. Previously this returned
        # "no screensaver was up… run again", an unresolvable loop on exactly
        # the hardware the beta exists for. Measure it instead: send the
        # launch Intent once; a screensaver swallows it (the defining failure
        # mode, verified on dreamx), a real app plays it.
        _require_idle(ctx)
        deep_link = (
            f"vnd.youtube.launch://www.youtube.com/watch?v={SELF_TEST_VIDEO_SHORT}"
        )
        state.remote.send_launch_app_command(deep_link)
        _record_device_event(
            "selftest_launch_sent", f"screensaver-suspect probe ({current})"
        )
        # Ours to clean up either way: if the Intent lands late (slow device)
        # the idle probe still hands the TV back.
        ctx["launched"] = True
        ctx["screensaver_suspect_tested"] = True

        loop = asyncio.get_running_loop()
        landed = None
        deadline = loop.time() + SELF_TEST_SUSPECT_TIMEOUT
        while loop.time() < deadline:
            if _get_current_app() == SMARTTUBE_PACKAGE:
                landed = "foreground"
                break
            mon = state.lounge_monitor
            if mon is not None:
                obs = mon.observation
                if (obs.video_id == SELF_TEST_VIDEO_SHORT
                        and obs.current_time is not None):
                    landed = "lounge"
                    break
            await asyncio.sleep(SELF_TEST_POLL)

        looks_launcher = _looks_like_a_launcher(current)
        if landed:
            ctx["played"] = True
            return {
                "screensaver_active": False,
                "suspect_package": current,
                "intent_swallowed": False,
                "intent_landed_via": landed,
                "note": (
                    f"{current} was in the foreground but did NOT swallow the "
                    "launch Intent — a video started, so it behaves like a "
                    "real app. Do NOT add it to SCREENSAVER_PACKAGES."
                ),
            }

        # Swallowed. A LAUNCHER that swallows an Intent is a different and
        # more likely story than a screensaver: it usually means SmartTube
        # isn't installed under the configured package id, so Android had
        # nothing to hand the Intent to and the home screen simply stayed put.
        # Branding it a screensaver would be a confident wrong answer with a
        # harmful fix attached.
        #
        # Only now is the dismiss key justified — sending it on the landed
        # branch would HOME someone out of a live app.
        state.remote.send_key_command(SCREENSAVER_DISMISS_KEY)
        _record_device_event(
            "selftest_dismiss_sent", f"{SCREENSAVER_DISMISS_KEY} at {current}"
        )
        deadline = loop.time() + max(SCREENSAVER_DISMISS_DELAY, 3.0)
        after = current
        while loop.time() < deadline:
            after = _get_current_app() or ""
            if after and after != current:
                break
            await asyncio.sleep(0.2)
        if looks_launcher:
            return {
                "screensaver_active": False,
                "suspect_package": current,
                "likely_role": "launcher",
                "intent_swallowed": True,
                "dismiss_key": SCREENSAVER_DISMISS_KEY,
                "dismissed": bool(after) and after != current,
                "foreground_after": after,
                "note": (
                    f"{current} looks like this device's HOME SCREEN, and it "
                    "swallowed a launch Intent. That usually means SmartTube "
                    "is not installed under the package id this app is "
                    "configured for — Android had nothing to hand the Intent "
                    "to, so the home screen stayed put. Check "
                    "smarttube_package_candidate and the tester's SmartTube "
                    "build. Deliberately NOT suggested for "
                    "SCREENSAVER_PACKAGES: a launcher listed there makes "
                    "every play send a pointless dismiss key at the home "
                    "screen. If this really is the screensaver, say so and "
                    "we'll add it by hand."
                ),
            }
        return {
            "screensaver_active": True,
            "screensaver_package": current,
            "recognised": False,
            "intent_swallowed": True,
            "dismiss_key": SCREENSAVER_DISMISS_KEY,
            "dismissed": bool(after) and after != current,
            "foreground_after": after,
            "note": (
                f"MEASURED, not guessed: {current} swallowed a launch Intent "
                "— the defining screensaver behaviour — and `dismissed` says "
                "whether our key clears it. Adding this package to "
                "SCREENSAVER_PACKAGES is the change that makes videos start "
                "on this device."
            ),
        }

    state.remote.send_key_command(SCREENSAVER_DISMISS_KEY)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(SCREENSAVER_DISMISS_DELAY, 3.0)
    after = current
    while loop.time() < deadline:
        after = _get_current_app() or ""
        if after not in SCREENSAVER_PACKAGES:
            break
        await asyncio.sleep(0.2)
    return {
        "screensaver_active": True,
        "screensaver_package": current,
        "recognised": True,
        "dismiss_key": SCREENSAVER_DISMISS_KEY,
        "dismissed": after not in SCREENSAVER_PACKAGES,
        "foreground_after": after,
    }


async def _probe_deep_link_play(ctx: dict) -> dict:
    """Does the production play path actually start a video on this device?

    This is the exact primitive tv_play uses when SmartTube isn't foreground,
    which is most adds. Measures two things separately, because they fail
    separately: did SmartTube come to the foreground, and did Lounge ever
    report it actually playing our video.
    """
    remote = state.remote
    if remote is None:
        raise _ProbeSkip("no remote connection")
    if ctx.get("screensaver_suspect_tested"):
        raise _ProbeSkip(
            "the screensaver probe already exercised the deep link this run — "
            "see its intent_swallowed result. Firing it again would be two "
            "play signals for one video."
        )
    if ctx.get("woke_by_intent"):
        # The wake probe already deep-linked this exact video seconds ago.
        # Sending it again is two play signals for one video — the shape
        # invariant 1 exists to prevent — and the timing would read as a
        # cold-start measurement when it is really a re-fire.
        raise _ProbeSkip(
            "the wake probe already started this video with the same deep "
            "link; see wake_intent for the launch timing"
        )
    _require_idle(ctx)

    vid = SELF_TEST_VIDEO_SHORT
    deep_link = f"vnd.youtube.launch://www.youtube.com/watch?v={vid}"
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    remote.send_launch_app_command(deep_link)
    _record_device_event("selftest_launch_sent", vid)
    # Set BEFORE any waiting: this is what tells the idle probe there is
    # something to hand back. Gating that on Lounge confirming playback would
    # strand a tester who never paired Lounge with our test video playing on
    # their TV — the probe sent it, so the probe owns cleaning it up.
    ctx["launched"] = True

    fg_seconds = None
    lounge_seconds = None
    deadline = loop.time() + SELF_TEST_PLAY_TIMEOUT
    while loop.time() < deadline:
        if fg_seconds is None and _get_current_app() == SMARTTUBE_PACKAGE:
            fg_seconds = round(loop.time() - t0, 2)
        mon = state.lounge_monitor
        if lounge_seconds is None and mon is not None:
            obs = mon.observation
            if obs.video_id == vid and obs.current_time is not None:
                lounge_seconds = round(loop.time() - t0, 2)
        if fg_seconds is not None and lounge_seconds is not None:
            break
        await asyncio.sleep(0.5)

    mon = state.lounge_monitor
    obs = mon.observation if mon is not None else None
    ctx["played"] = lounge_seconds is not None

    # If SmartTube never came to the foreground, name what did. The Intent
    # resolves to whatever handles vnd.youtube.launch:// regardless of our
    # configured package, so an older SmartTube build (or the official YouTube
    # app) shows up here — and that is a one-line fix the tester can apply,
    # not something to diagnose over email.
    candidate = None
    if fg_seconds is None:
        for pkg in reversed([e["value"] for e in _device_log
                             if e["kind"] == "current_app" and e["value"]]):
            if pkg != SMARTTUBE_PACKAGE and pkg not in SCREENSAVER_PACKAGES \
                    and pkg not in KNOWN_BENIGN_PACKAGES:
                candidate = pkg
                break

    return {
        "video_id": vid,
        "smarttube_package_candidate": candidate,
        "smarttube_foreground_after_s": fg_seconds,
        "smarttube_foreground_measurable": bool(ctx.get("current_app_usable")),
        "lounge_saw_it_after_s": lounge_seconds,
        "lounge_state": getattr(obs, "state", None),
        "lounge_video_id": getattr(obs, "video_id", None),
        "lounge_current_time": getattr(obs, "current_time", None),
        "note": (
            "lounge_saw_it_after_s=null with a populated lounge_state for a "
            "different video usually means the Lounge cloud cache is reporting "
            "a stale session rather than this device."
        ),
    }


async def _probe_lounge_swap(ctx: dict) -> dict:
    """Does Lounge setPlaylist swap videos on this device without a relaunch?

    The smooth-swap path. Only meaningful if the deep-link probe actually got
    something playing — setPlaylist against an idle SmartTube loads a video
    without starting it, which is a different (documented) behaviour and not
    what this measures.
    """
    _require_idle(ctx)
    mon = state.lounge_monitor
    if mon is None or not mon.is_connected:
        raise _ProbeSkip("Lounge not connected")
    if not ctx.get("played"):
        raise _ProbeSkip("nothing was playing to swap from")

    vid = SELF_TEST_VIDEO_LONG
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await mon.play_video(vid, None)
    swapped_s = None
    deadline = loop.time() + SELF_TEST_SWAP_TIMEOUT
    while loop.time() < deadline:
        obs = mon.observation
        if obs.video_id == vid and obs.state == "Playing":
            swapped_s = round(loop.time() - t0, 2)
            break
        await asyncio.sleep(SELF_TEST_POLL)
    ctx["swapped"] = swapped_s is not None
    obs = mon.observation
    return {
        "video_id": vid,
        "swapped_after_s": swapped_s,
        "lounge_state": obs.state,
        "lounge_video_id": obs.video_id,
    }


async def _probe_transport(ctx: dict) -> dict:
    """Do pause and resume take effect, and does the device report them back?

    Verified by observation rather than by the call returning — Lounge.play()
    against a torn-down player succeeds over HTTPS and does nothing, which is
    the whole reason production verifies this transition.
    """
    _require_idle(ctx)
    mon = state.lounge_monitor
    if mon is None or not mon.is_connected:
        raise _ProbeSkip("Lounge not connected")
    if not (ctx.get("played") or ctx.get("swapped")):
        raise _ProbeSkip("nothing was playing to pause")

    async def _await_state(target: str, timeout: float) -> Optional[float]:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        deadline = t0 + timeout
        while loop.time() < deadline:
            if mon.observation.state == target:
                return round(loop.time() - t0, 2)
            await asyncio.sleep(0.3)
        return None

    await mon.pause()
    paused_s = await _await_state("Paused", SELF_TEST_TRANSPORT_TIMEOUT)
    await asyncio.sleep(1.0)
    await mon.play()
    resumed_s = await _await_state("Playing", SELF_TEST_TRANSPORT_TIMEOUT)
    return {
        "paused_confirmed_after_s": paused_s,
        "resumed_confirmed_after_s": resumed_s,
        "final_state": mon.observation.state,
        "note": (
            "null means Lounge never reported the transition. SmartTube only "
            "pushes state on transitions and sometimes not even then, so a null "
            "here is weaker evidence than a number — say whether the TV "
            "actually paused."
        ),
    }


async def _probe_end_of_video(ctx: dict) -> dict:
    """Let a video actually END, and watch what the device reports.

    The single biggest hole in this test until now. Auto-advance is a core
    feature and nothing exercised it: the play probe returns a few seconds
    into the clip, the swap probe replaces it, and no video ever reached its
    own end — so `lounge.finished`, the near-end rule and the persistent-
    Stopped detector were all completely unobserved on the tester's hardware.

    Cheap only because the probe clip is ~19 seconds. It restarts that clip
    and waits for the end, recording what Lounge reports and when.

    Deliberately does NOT go through QueueController: the self-test owns no
    queue item, so nothing here can advance a real queue. (That is also why
    `_on_lounge_finished` had to be tightened first — with `state.current`
    None its cross-video guard was skipped entirely, and this probe would
    have triggered a spurious advance the moment the clip ended.)
    """
    _require_idle(ctx)
    mon = state.lounge_monitor
    if mon is None or not mon.is_connected:
        raise _ProbeSkip(
            "Lounge not connected, so we cannot see a video end. Pair with "
            "SmartTube and run again — this is the only check that covers "
            "whether the next video starts by itself."
        )

    vid = SELF_TEST_VIDEO_SHORT
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await mon.play_video(vid, None)
    ctx["launched"] = True

    # Sample the whole way through: a frozen current_time is itself the
    # finding (it means the duration timer is the only thing that could ever
    # advance the queue on this device).
    samples = []
    states = []
    duration = None
    last_ct = None
    ended_at = None
    deadline = loop.time() + SELF_TEST_FINISH_TIMEOUT
    while loop.time() < deadline:
        obs = mon.observation
        if obs.video_id == vid:
            if obs.duration:
                duration = obs.duration
            if obs.current_time is not None and obs.current_time != last_ct:
                last_ct = obs.current_time
                samples.append(round(obs.current_time, 1))
            if obs.state and (not states or states[-1] != obs.state):
                states.append(obs.state)
        if obs.state in ("Stopped", "Ended"):
            ended_at = round(loop.time() - t0, 2)
            break
        await asyncio.sleep(SELF_TEST_POLL)

    obs = mon.observation
    near_end = (
        duration is not None and last_ct is not None
        and (duration - last_ct) <= 5.0
    )
    ctx["played"] = bool(samples)
    return {
        "video_id": vid,
        "duration_reported": duration,
        "position_samples": samples[:40],
        "position_advanced": len(samples) > 1,
        "state_sequence": states,
        "final_state": obs.state,
        "ended_after_s": ended_at,
        "last_position": last_ct,
        "stopped_within_5s_of_end": near_end,
        "waited_s": SELF_TEST_FINISH_TIMEOUT,
        "note": (
            "This is what auto-advance runs on. position_advanced=false means "
            "the device never reports progress, so the queue can only advance "
            "on a scraped-duration timer and any pause or seek desyncs it. "
            "ended_after_s=null with a full duration means the end-of-video "
            "signal never arrived, so the next video would not start by "
            "itself. stopped_within_5s_of_end is the exact rule the queue "
            "uses to tell a real ending from a mid-video blip."
        ),
    }


async def _probe_volume(ctx: dict) -> dict:
    """Does anything observable happen when we send volume keys?

    Read this one carefully: a no-change result is genuinely ambiguous. Volume
    reaches the speakers by two routes — the device relays to an amp over
    HDMI-CEC (nothing local moves, so we see nothing even when it works), or it
    attenuates its own output (volume_info moves). No-change is therefore
    consistent with "CEC working perfectly", "CEC ignored downstream", and
    "IR mode, which can never work over the network". Only the tester's ears
    separate them, which is why the report asks.
    """
    remote = state.remote
    if remote is None:
        raise _ProbeSkip("no remote connection")
    if not bool(getattr(remote, "is_on", False)):
        # Firing volume keys at a sleeping device returns "no change", which
        # is indistinguishable from the normal CEC reading — a false negative
        # that reads like a real result.
        raise _ProbeSkip("device is off; volume can't be measured while asleep")

    def _vol():
        try:
            return dict(getattr(remote, "volume_info", None) or {})
        except Exception:
            return None

    before = _vol()
    for _ in range(2):
        remote.send_key_command(VOLUME_KEYCODES["up"])
        await asyncio.sleep(0.4)
    await asyncio.sleep(0.6)
    after_up = _vol()
    for _ in range(2):
        remote.send_key_command(VOLUME_KEYCODES["down"])
        await asyncio.sleep(0.4)
    await asyncio.sleep(0.6)
    after_restore = _vol()

    # Mute is a shipped button and was never exercised, while the report
    # listed its keycode — implying coverage it didn't have. Toggled twice so
    # the device is left as we found it. VOLUME_MUTE (164), never MUTE (91),
    # which mutes the microphone.
    remote.send_key_command(VOLUME_KEYCODES["mute"])
    await asyncio.sleep(1.0)
    after_mute = _vol()
    remote.send_key_command(VOLUME_KEYCODES["mute"])
    await asyncio.sleep(1.0)
    after_unmute = _vol()

    moved = bool(before and after_up and before != after_up)
    return {
        "mute_info_after_mute": after_mute,
        "mute_info_after_unmute": after_unmute,
        "mute_observable": bool(
            after_mute and after_unmute and after_mute != after_unmute
        ),
        "volume_info_before": before,
        "volume_info_after_up": after_up,
        "volume_info_after_restore": after_restore,
        "volume_info_moved": moved,
        "restored": after_restore == before,
        "keycodes": dict(VOLUME_KEYCODES),
        "note": (
            "volume_info_moved=true means this device attenuates its own "
            "output. false is NOT a failure — it is the normal reading for a "
            "device relaying over HDMI-CEC, and the app cannot tell that apart "
            "from CEC being ignored. Answer the volume question at the bottom "
            "of this report; that is the only thing that resolves it."
        ),
    }


async def _probe_idle_return(ctx: dict) -> dict:
    """What does this device land on when we hand the TV back?

    Doubles as the cleanup step for the playback probes and as the best chance
    of learning the device's screensaver/launcher package names, which is the
    single most useful unknown on unfamiliar hardware.
    """
    remote = state.remote
    if remote is None:
        raise _ProbeSkip("no remote connection")
    if not (ctx.get("launched") or ctx.get("played") or ctx.get("swapped")):
        raise _ProbeSkip("nothing was started, nothing to hand back")

    keys = [k.strip() for k in IDLE_KEYCODE.split(",") if k.strip()]
    for i, key in enumerate(keys):
        remote.send_key_command(key)
        if i < len(keys) - 1:
            await asyncio.sleep(IDLE_KEYCODE_DELAY)

    seen = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SELF_TEST_IDLE_TIMEOUT
    while loop.time() < deadline:
        app = _get_current_app() or ""
        if app and (not seen or seen[-1] != app):
            seen.append(app)
        await asyncio.sleep(SELF_TEST_POLL)

    final = seen[-1] if seen else None
    return {
        "idle_keycode": IDLE_KEYCODE,
        "foreground_sequence": seen,
        "final_foreground": final,
        "final_is_known_screensaver": final in SCREENSAVER_PACKAGES if final else None,
        "note": (
            "This is where the device lands when we hand the TV back. Usually "
            "that is the LAUNCHER, not the screensaver — the screensaver only "
            "appears minutes later, long after this probe stops watching. Do "
            "NOT add final_foreground to SCREENSAVER_PACKAGES on the strength "
            "of this alone: adding a launcher there makes every play send a "
            "pointless dismiss key. To capture the real screensaver, run the "
            "self-test again once it is actually on screen."
        ),
    }


def _tv_is_busy(ignore_video_ids: frozenset = frozenset()) -> bool:
    """Is anything playing right now that isn't ours?

    Two clauses, and the second is the one a queue-only check misses: a video
    started from the TV's own remote, or one our queue ceded via the
    external-switch logic, leaves `state.current` None while the TV plays on.
    `/api/skip` already learned this; the self-test has to inherit it or a
    guest can wipe out someone's viewing by pressing a diagnostics button.

    The Lounge clause is gated on the device actually being ON. The Lounge
    "current playlist" lives on YouTube's cloud and reports dormant players as
    Playing indefinitely (invariant 4), so without that gate a stale ghost
    would make a sleeping TV look busy — and permanently skip the wake probes,
    which are the only ones that need a sleeping TV.
    """
    qstate = queue_controller.state
    if qstate.current is not None or qstate.queue:
        return True
    remote = state.remote
    if remote is None or not bool(getattr(remote, "is_on", False)):
        return False
    lng = qstate.lounge or {}
    video_id = lng.get("video_id")
    # Matching by video id rather than by "has the run launched anything yet"
    # keeps the guard live: a guest's video landing mid-run still stops the
    # probes, because it is a different id.
    if video_id and video_id in ignore_video_ids:
        return False
    return bool(
        lng.get("available")
        and video_id
        and lng.get("current_time") is not None
        and lng.get("state") == "Playing"
    )


def _require_idle(ctx: dict) -> None:
    """Refuse to move the TV when someone is watching something.

    Re-reads live state rather than trusting the start-of-run snapshot. The
    409 guard on /api/queue only refuses NEW adds; one that passed the guard
    microseconds before the run began is still inside its metadata scrape (up
    to METADATA_TIMEOUT_S) and lands mid-run, taking ownership of the queue
    while a stale snapshot still says idle.
    """
    if ctx.get("was_busy") or _tv_is_busy(ignore_video_ids=_SELF_TEST_VIDEO_IDS):
        raise _ProbeSkip(
            "something is playing or queued; skipped so the test doesn't "
            "interrupt it"
        )


# id, label, seconds budgeted (drives the ETA), probe
_SELF_TEST_PROBES = (
    ("snapshot", "Reading device + config", 1.0, _probe_snapshot),
    ("foreground", "Checking foreground-app detection", 6.0, _probe_foreground_readability),
    ("metadata", "Checking YouTube reachability", 16.0, _probe_metadata),
    ("wake_intent", "Testing wake by launch Intent", 130.0, _probe_launch_intent_wakes),
    ("wake_keys", "Sweeping wake keycodes", 170.0, _probe_wake_keycodes),
    ("screensaver", "Identifying the screensaver", 17.0, _probe_screensaver),
    ("play", "Starting a test video", 21.0, _probe_deep_link_play),
    ("swap", "Swapping video via Lounge", 16.0, _probe_lounge_swap),
    ("transport", "Testing pause and resume", 13.0, _probe_transport),
    ("finish", "Watching a video reach its end", 43.0, _probe_end_of_video),
    ("volume", "Testing volume keys", 11.0, _probe_volume),
    ("idle", "Handing the TV back", 10.0, _probe_idle_return),
    ("snapshot_after", "Re-reading device state", 7.0, _probe_snapshot_after),
)

# Questions the app genuinely cannot answer by itself, each with the prompt
# the UI shows. Phrasing is deliberate: a bare "did the volume change?" gets a
# yes/no, when what actually resolves the ambiguity is WHICH volume moved —
# the TV's own speakers or a receiver — because that distinguishes the device
# attenuating its own output from it relaying a CEC command downstream, and
# those need different advice.
def _hints_shield(detail) -> list:
    """NVIDIA Shield interpretation. Sourced; see CLAUDE.md."""
    hints = []
    intent_woke = detail("wake_intent").get("woke")
    attempts = detail("wake_keys").get("attempts") or []
    swept_and_failed = bool(attempts) and not detail("wake_keys").get("woke_with")
    if swept_and_failed and not intent_woke:
        hints.append(
            "Shield + no wake: this is almost always Settings > Remotes & "
            "accessories > Simplified wake buttons — BOTH toggles must be "
            "OFF. Every reported Shield wake failure with this protocol "
            "was fixed there; none needed a different keycode. Keep "
            "WAKE_KEYCODE=POWER."
        )
    readiness = (
        detail("wake_intent").get("current_app_readable_after_wake_s")
        or detail("wake_keys").get("current_app_readable_after_wake_s")
    )
    if readiness is not None and readiness > WAKE_DELAY:
        hints.append(
            f"Shield woke but took {readiness}s to become usable — "
            f"longer than WAKE_DELAY={WAKE_DELAY:g}. SHIELD Experience "
            "before 9.2 documents 'remote stops responding for 60 seconds "
            "after wake from sleep'; updating the firmware to 9.2+ is the "
            "real fix, WAKE_DELAY is the workaround. Note: sw_version in "
            "this report is the Android TV Remote Service app version, "
            "NOT the firmware — read Settings > Device Preferences > "
            "About for the Experience version."
        )
    if detail("volume").get("volume_info_moved") is False:
        hints.append(
            "Shield volume is governed by Settings > Display & Sound > "
            "Volume control. 2019 models default to HDMI-CEC (works with "
            "no local change visible — same as our verified devices); "
            "2015/2017 default to Digital (a change WOULD have been "
            "visible in volume_info, so no-change there is a real "
            "failure). IR mode relays network volume out the remote's IR "
            "blaster — it works only when the blaster faces the "
            "amplifier, so treat it as unreliable rather than impossible."
        )
    return hints


def _hints_bell_streamer(detail) -> list:
    """Bell Streamer (Askey STI6130) — researched, nothing hardware-verified.

    Same silicon as Google's own ADT-3 developer kit with a Bell ROM and an
    Android TV Operator Tier launcher on top. Everything here is sourced from
    documentation and forum reports; the first real report replaces it.
    """
    hints = [
        "Bell Streamer is Android TV Operator Tier on Askey STI6130 hardware "
        "— the same board as Google's ADT-3 dev kit, with a Bell launcher on "
        "top. Nothing about it is hardware-verified by us yet, so treat every "
        "line in this report as new information rather than a check against "
        "known-good behaviour."
    ]
    attempts = detail("wake_keys").get("attempts") or []
    if (bool(attempts) and not detail("wake_keys").get("woke_with")
            and not detail("wake_intent").get("woke")):
        hints.append(
            "Bell documents this box powering itself OFF after 30 minutes "
            "idle — that is a deeper state than the sleep our wake path is "
            "built for, and it may drop off Wi-Fi entirely (it is a Wi-Fi "
            "only device, no Ethernet). Before changing any keycode, check "
            "whether the box was merely asleep or fully off, and whether it "
            "was still reachable at all — see remote_available in the events "
            "log and remote_reconnects."
        )
    ss = detail("screensaver")
    if ss.get("likely_role") == "launcher" or ss.get("intent_swallowed"):
        hints.append(
            "On this box HOME lands on Bell's own Fibe TV launcher rather "
            "than an Android TV home screen, so our screensaver-dismiss and "
            "idle-return keys may behave differently from every device we "
            "have verified. Whatever package this report names as the "
            "foreground here is genuinely new data."
        )
    return hints


# Keyed by profile id. `match` is a tuple of (manufacturer_substring,
# model_substring) AND-pairs, OR'd together; None means "don't care".
#
# Matching the two fields SEPARATELY is load-bearing, not fussiness: a
# flattened "manufacturer model" string makes the token "streamer" match both
# Google's TV Streamer and a Bell box whose model is literally "Streamer",
# and the first profile in iteration order would silently win.
_DEVICE_PROFILES = {
    "google_tv_streamer": {
        "label": "Google TV Streamer (4K)",
        "match": ((None, "streamer"),),
        "hints": None,
        "verified": True,
    },
    "chromecast_gtv": {
        "label": "Chromecast with Google TV",
        "match": ((None, "chromecast"),),
        "hints": None,
        "verified": True,
    },
    "shield": {
        "label": "NVIDIA Shield",
        "match": (("nvidia", None), (None, "shield")),
        "hints": _hints_shield,
        "verified": False,
    },
    "bell_streamer": {
        "label": "Bell Streamer / Bell Fibe TV box",
        # Deliberately empty: selectable in the dropdown, never auto-detected.
        # We have never seen what this box reports over the protocol, and a
        # guessed match pair that fired on the wrong device would be worse
        # than no detection at all. The pair goes in when the first report
        # tells us what it actually says.
        "match": (),
        "hints": _hints_bell_streamer,
        "verified": False,
    },
}


def _detect_profile() -> Optional[str]:
    """Best guess at the device family from what the protocol reports."""
    try:
        info = dict(getattr(state.remote, "device_info", None) or {})
    except Exception:
        return None
    manufacturer = (info.get("manufacturer") or "").lower()
    model = (info.get("model") or "").lower()
    if not (manufacturer or model):
        return None
    for pid, prof in _DEVICE_PROFILES.items():
        for want_mfr, want_model in prof["match"]:
            if want_mfr and want_mfr not in manufacturer:
                continue
            if want_model and want_model not in model:
                continue
            return pid
    return None


# (id, prompt, profiles, choices)
#   profiles: () = ask everyone. Otherwise only for those device profiles.
#   choices:  () = free text. Otherwise a dropdown of (value, label).
#
# Scoping matters more than it looks. A tester shown two questions that
# obviously don't apply to their hardware learns that this form isn't for
# them, and leaves the whole thing blank — and these answers are the half the
# app cannot measure for itself.
_SELF_TEST_QUESTIONS = (
    ("device_profile",
     "Which device is this running on? We use this to ask the right "
     "follow-up questions — pick the closest match.",
     (),
     tuple((pid, prof["label"]) for pid, prof in _DEVICE_PROFILES.items())
     + (("other", "Something else / not sure"),)),
    ("device_model",
     "Which device is this? (e.g. NVIDIA Shield TV Pro 2019)",
     (), ()),

    ("device_software_version",
     "Its software version — Settings \u2192 Device Preferences \u2192 About",
     (), ()),

    ("did_the_device_wake",
     "If it was asleep: did it wake up by itself, and roughly how long did it "
     "take?",
     (), ()),

    ("did_the_video_play_on_screen",
     "Did a video actually appear and play, or did the screen stay put?",
     (), ()),

    ("did_pause_and_resume_work",
     "When the test paused, did the picture actually stop and then start "
     "again \u2014 or did it play straight through?",
     (), ()),

    ("what_got_louder",
     "During the volume check, what changed \u2014 the TV\u2019s own speakers, a "
     "soundbar/receiver, or nothing at all? This one matters most.",
     (), ()),

    ("volume_mode_setting",
     "On a Shield: Settings \u2192 Display & Sound \u2192 Volume control \u2014 is it set "
     "to HDMI-CEC, Digital, or IR?",
     ("shield",), ()),

    ("simplified_wake_buttons",
     "NVIDIA Shield only — Settings → Remotes & accessories → Simplified "
     "wake buttons: were BOTH switches OFF before this run? With them on, "
     "waking over the network is impossible and every wake result below is "
     "meaningless.",
     ("shield",), ()),

    ("what_did_you_put_to_sleep",
     "If you ran the asleep test: did you sleep the streaming box itself, or "
     "just switch off the TV picture?",
     (), ()),

    ("smarttube_build",
     "SmartTube → Settings → About: which build and version?",
     (), ()),

    ("did_the_next_video_start_by_itself",
     "Worth five minutes if you can: add TWO short videos from the page and "
     "let the first play to its end. Did the second start on its own? Nothing "
     "in the automated test can check this.",
     (), ()),

    ("normal_add_from_the_page",
     "Also worth doing: with the box asleep, add one video from the page the "
     "ordinary way. Did it play, and roughly how many seconds from pressing "
     "Add to the picture appearing?",
     (), ()),

    ("anything_odd_on_screen",
     "Anything you saw that the app couldn\u2019t \u2014 error messages, a chooser "
     "dialog, the wrong app opening, odd flicker?",
     (), ()),
)


def _suggested_configuration(probes: list) -> dict:
    """Turn measurements into the settings to actually change.

    The difference between a report that ends the conversation and one that
    starts it. "wake_keys.woke_with = WAKEUP" requires the reader to know that
    WAKE_KEYCODE exists and defaults to POWER; `WAKE_KEYCODE=WAKEUP` can be
    pasted into a compose file. Every entry carries the probe it came from, so
    a wrong suggestion can be traced rather than trusted.
    """
    by_id = {p["id"]: p for p in probes}

    def detail(pid):
        d = by_id.get(pid, {}).get("detail")
        return d if isinstance(d, dict) else {}

    env, notes = [], []

    woke_with = detail("wake_keys").get("woke_with")
    if woke_with and woke_with != WAKE_KEYCODE:
        env.append({
            "var": "WAKE_KEYCODE", "value": woke_with,
            "because": f"this device woke on {woke_with}, not {WAKE_KEYCODE}",
            "from_probe": "wake_keys",
        })

    # A foreground package we neither recognise nor know how to dismiss, seen
    # while the device was idle, is the classic unknown screensaver.
    after = detail("snapshot_after")
    suspects = [
        pkg for pkg in (after.get("unrecognised_foreground_packages") or [])
        if pkg not in KNOWN_BENIGN_PACKAGES
    ]
    ss = detail("screensaver")
    if ss.get("screensaver_active") and ss.get("dismissed") is False:
        notes.append(
            f"{SCREENSAVER_DISMISS_KEY} did not dismiss this device's "
            f"screensaver ({ss.get('screensaver_package')}). Try "
            "SCREENSAVER_DISMISS_KEY=BACK."
        )
    # A suspect the screensaver probe MEASURED (it swallowed a launch
    # Intent) outranks one merely seen in the foreground log — and must not
    # be suggested twice at two confidence levels.
    if ss.get("intent_swallowed") and ss.get("screensaver_package"):
        measured_pkg = ss["screensaver_package"]
        env.append({
            "var": "SCREENSAVER_PACKAGES",
            "value": ",".join(sorted(SCREENSAVER_PACKAGES | {measured_pkg})),
            "because": (
                f"{measured_pkg} swallowed a launch Intent — measured, the "
                "defining screensaver behaviour — so until it is listed, "
                "videos silently fail to start whenever it is on screen"
            ),
            "from_probe": "screensaver",
            "confidence": "measured",
        })
        suspects = [pkg for pkg in suspects if pkg != measured_pkg]
    # A carrier device's launcher is unrecognised by definition (we only know
    # Google's), so it lands in `suspects` every time. Suggesting it would put
    # a home screen in SCREENSAVER_PACKAGES.
    launchers = [pkg for pkg in suspects if _looks_like_a_launcher(pkg)]
    suspects = [pkg for pkg in suspects if not _looks_like_a_launcher(pkg)]
    for pkg in launchers:
        notes.append(
            f"{pkg} was in the foreground and we don't recognise it, but it "
            "looks like this device's home screen rather than a screensaver — "
            "so it is NOT suggested for SCREENSAVER_PACKAGES. Confirm what it "
            "is and we'll add it to the known-benign list, which only affects "
            "reporting."
        )
    for pkg in suspects:
        env.append({
            "var": "SCREENSAVER_PACKAGES",
            "value": ",".join(sorted(SCREENSAVER_PACKAGES | {pkg})),
            "because": (
                f"{pkg} was in the foreground and we neither recognise nor "
                "dismiss it. ONLY apply this if it is actually the "
                "screensaver — a launcher here would make every play send a "
                "pointless dismiss key."
            ),
            "from_probe": "snapshot_after",
            "confidence": "needs confirming",
        })

    cand = detail("play").get("smarttube_package_candidate")
    if cand and cand != SMARTTUBE_PACKAGE:
        env.append({
            "var": "SMARTTUBE_PACKAGE", "value": cand,
            "because": (
                f"the deep link opened {cand}, not the configured "
                f"{SMARTTUBE_PACKAGE} — foreground checks are comparing "
                "against the wrong package"
            ),
            "from_probe": "play",
        })

    # Timings that beat our own constants.
    slow = []
    for pid, field, const_name, const in (
        ("wake_intent", "seconds", "WAKE_DELAY", WAKE_DELAY),
        ("wake_keys", "seconds", "WAKE_DELAY", WAKE_DELAY),
        ("play", "lounge_saw_it_after_s",
         "LOUNGE_OBSERVATION_TIMEOUT", LOUNGE_OBSERVATION_TIMEOUT),
    ):
        observed = detail(pid).get(field)
        if isinstance(observed, (int, float)) and observed > const:
            slow.append({
                "constant": const_name, "current_s": const,
                "observed_s": observed, "from_probe": pid,
            })
    # Prefer the READINESS measurement (time until the device was usable)
    # over the is_on flip: instant-on hardware reports on within ~1s while
    # still deaf, so the flip understates and readiness is the number
    # WAKE_DELAY actually has to cover.
    readiness = (
        detail("wake_intent").get("current_app_readable_after_wake_s")
        or detail("wake_keys").get("current_app_readable_after_wake_s")
    )
    wake_secs = detail("wake_intent").get("seconds")
    basis, from_probe = None, None
    if readiness is not None and readiness > WAKE_DELAY:
        basis, from_probe = readiness, "wake readiness sampling"
    elif isinstance(wake_secs, (int, float)) and wake_secs > WAKE_DELAY:
        basis, from_probe = wake_secs, "wake_intent"
    if basis is not None:
        env.append({
            "var": "WAKE_DELAY",
            "value": str(round(basis + 5)),
            "because": (
                "this device took longer to become usable after waking than "
                "WAKE_DELAY allows, so play commands were arriving before it "
                "could hear them"
            ),
            "from_probe": from_probe,
            "confidence": "measured",
        })

    return {
        "env_vars_to_set": env,
        "constants_that_look_too_short": slow,
        "notes": notes,
        "how_to_apply": (
            "Add any env_vars_to_set to the `environment:` block of your "
            "docker-compose.yml, then `docker compose up -d`. Anything under "
            "constants_that_look_too_short is a code change, not a setting."
        ) if (env or slow) else (
            "No settings to change, but see `notes` — something was seen that "
            "needs a human to identify."
            if notes else "Nothing to change — the defaults fit this device."
        ),
    }


def _device_hints(probes: list, profile: Optional[str] = None) -> list:
    """Device-specific interpretation, written into the report itself.

    Everything here is sourced research, keyed on what the device reports —
    so a Shield report arrives pre-interpreted instead of needing us to
    remember the folklore. Nothing in here sends anything.
    """
    by_id = {p["id"]: p for p in probes}

    def detail(pid):
        d = by_id.get(pid, {}).get("detail")
        return d if isinstance(d, dict) else {}

    hints = []
    # The tester's declared profile wins over detection when present — they
    # can see the box; we are reading two strings off a protocol.
    pid = profile or _detect_profile()
    prof = _DEVICE_PROFILES.get(pid or "")
    if prof and prof.get("hints"):
        hints.extend(prof["hints"](detail))

    # Universal tail — NOT device-keyed. Must stay outside the profile
    # dispatch: a non-standard SmartTube package id is most likely on exactly
    # the devices we have no profile for.
    cand = detail("play").get("smarttube_package_candidate") or ""
    if "teamsmart" in cand or "liskovsoft" in cand:
        hints.append(
            f"This install runs a LEGACY SmartTube id ({cand}). SmartTube's "
            "signing key was compromised around Nov 2025 and the app was "
            "re-released under new ids; the in-app updater cannot cross the "
            "rename, so long-time installs stay on the old id forever. "
            "Recommend a fresh install of current stable (32.10s or later) "
            "rather than pointing SMARTTUBE_PACKAGE at the legacy id — "
            "intent-launched videos also played unauthenticated before "
            "31.94s, which breaks age-restricted playback and watch history."
        )
    return hints


def _self_test_verdict(probes: list, ctx: dict) -> dict:
    """Did this run actually learn anything?

    The failure mode this exists for: `status: "ok"` only means the probe
    didn't raise, the UI colours nothing for a skip, and the docs tell the
    tester that plenty of skips are normal. So a run that established almost
    nothing looks exactly like a healthy one — progress bar full, "Finished",
    send it over — and the round trip is discovered wasted days later, after
    someone doing you a favour has already spent their evening.

    So state it plainly at the top of the report, in the tester's language.
    """
    by_id = {p["id"]: p for p in probes}

    def ok(pid):
        return by_id.get(pid, {}).get("status") == "ok"

    def detail(pid):
        d = by_id.get(pid, {}).get("detail")
        return d if isinstance(d, dict) else {}

    learned, missing = [], []

    woke_intent = detail("wake_intent").get("woke")
    woke_key = detail("wake_keys").get("woke_with")
    if ctx.get("device_was_off"):
        if woke_intent:
            learned.append("the launch Intent alone wakes this device")
        elif woke_key:
            learned.append(f"this device wakes on {woke_key}")
        else:
            missing.append(
                "The device never woke, from the Intent or any keycode. That "
                "is itself a finding, but check the wake settings first — on "
                "an NVIDIA Shield, Settings > Remotes & accessories > "
                "Simplified wake buttons makes network wake impossible."
            )
    else:
        missing.append(
            "Waking was not tested: the device was already awake. This is the "
            "single most useful thing this test can measure. Put the DEVICE "
            "itself to sleep — the streaming box, not just the TV picture — "
            "and run it again."
        )

    if detail("foreground").get("usable") is False:
        missing.append(
            "Foreground-app detection is not working on this device, so "
            "several results below are marked unmeasurable rather than "
            "failed. Worth reporting on its own."
        )

    if detail("play").get("lounge_saw_it_after_s") is not None or ctx.get("played"):
        learned.append("a video can be started and was confirmed playing")
    elif ok("play"):
        missing.append(
            "A video was started but never confirmed playing. If you have not "
            "paired with SmartTube (the 12-digit code), do that and run again "
            "— without it we are guessing at what the TV actually did."
        )

    if not _is_lounge_paired():
        missing.append(
            "Not paired with SmartTube, so playback position, pause/resume "
            "and the video-swap checks could not run at all. Pairing takes a "
            "minute and roughly doubles what this report can tell us."
        )

    if ok("screensaver") and detail("screensaver").get("screensaver_active") is False:
        missing.append(
            "No screensaver was on screen, so we did not learn what this "
            "device's screensaver is called — the most common reason videos "
            "fail to start on unfamiliar hardware. Leave it untouched until "
            "the screensaver appears, then run this again from your phone."
        )

    return {
        "useful": bool(learned),
        "learned": learned,
        "did_not_learn": missing,
        "summary": (
            ("Learned: " + "; ".join(learned) + ". ") if learned
            else "This run did not establish much. "
        ) + (
            f"{len(missing)} thing(s) still unanswered — see did_not_learn."
            if missing else "Nothing important was missed."
        ),
    }


async def _run_self_test(run_id: str) -> None:
    global _self_test_active
    loop = asyncio.get_running_loop()
    started = loop.time()
    writes_at_start = _client_write_count
    # No ignore-list at the START of a run: if a probe video is already
    # playing then someone really is watching it, and it is not ours yet.
    ctx: dict = {"was_busy": _tv_is_busy()}

    try:
        for entry in _self_test["probes"]:
            probe = entry.pop("_fn")
            entry["status"] = "running"
            _self_test["eta_s"] = max(
                0.0, sum(e["budget_s"] for e in _self_test["probes"]
                         if e["status"] in ("pending", "running"))
            )
            t0 = loop.time()
            try:
                entry["detail"] = await probe(ctx)
                entry["status"] = "ok"
            except _ProbeSkip as exc:
                entry["status"] = "skipped"
                entry["detail"] = {"reason": str(exc)}
            except _ProbeUnmeasurable as exc:
                entry["status"] = "unmeasurable"
                entry["detail"] = {"reason": str(exc)}
            except asyncio.CancelledError:
                entry["status"] = "cancelled"
                raise
            except Exception as exc:
                log.warning("Self-test probe %s failed", entry["id"], exc_info=True)
                entry["status"] = "failed"
                # Redacted: a Lounge failure's repr can carry the token from
                # the request URL, and this string is pasted into a chat.
                entry["detail"] = {"error": _redact_secrets(repr(exc))}
            entry["seconds"] = round(loop.time() - t0, 2)

        interference = _client_write_count - writes_at_start
        _self_test["report"] = {
            "run_id": run_id,
            # First key in the report on purpose: whoever reads it should see
            # what the run did and didn't establish before any probe detail.
            "verdict": _self_test_verdict(_self_test["probes"], ctx),
            # Second key on purpose: what to change, right after what we found.
            "suggested_configuration": _suggested_configuration(
                _self_test["probes"]
            ),
            # Sourced, device-keyed interpretation — a Shield report explains
            # itself instead of needing the folklore remembered.
            "device_hints": _device_hints(
                _self_test["probes"], _effective_profile()
            ),
            "kind": "smarttube-playlist self-test",
            "version": VERSION,
            "channel": "beta",
            "started_at": _self_test["started_at"],
            "duration_s": round(loop.time() - started, 1),
            "device_was_busy_at_start": ctx["was_busy"],
            "interference": {
                "other_client_actions_during_run": interference,
                "note": (
                    "non-zero means someone used the page while the test ran; "
                    "timings and playback results may be unreliable."
                ) if interference else "none",
            },
            "probes": [
                {k: v for k, v in e.items() if k != "budget_s"}
                for e in _self_test["probes"]
            ],
            "questions_for_the_tester": _questions_payload(),
            "device_profile": {
                "declared": _declared_profile(),
                "detected": _detect_profile(),
                "effective": _effective_profile(),
                "note": (
                    "declared is what the tester picked; detected is what the "
                    "protocol reported. A disagreement is itself worth "
                    "reading — it means this device identifies as something "
                    "we did not expect."
                ),
            },
        }
        _self_test["status"] = "done"
    except asyncio.CancelledError:
        _self_test["status"] = "error"
        _self_test["report"] = {"run_id": run_id, "error": "cancelled"}
        raise
    except Exception as exc:
        log.exception("Self-test run failed")
        _self_test["status"] = "error"
        _self_test["report"] = {
            "run_id": run_id, "error": _redact_secrets(repr(exc)),
        }
    finally:
        _self_test["eta_s"] = 0.0
        _self_test_active = False


@app.post("/api/selftest")
async def selftest_start():
    """Kick off a self-test. Returns immediately; poll GET for progress.

    POST rather than GET on purpose: the CSRF middleware no-ops on GET, so a
    GET version would be triggerable cross-origin by any page a guest happens
    to load — and this endpoint moves the TV.
    """
    global _self_test_active, _self_test_task
    if not SELF_TEST_ENABLED:
        raise HTTPException(503, "Self-test is disabled (SELF_TEST=0)")
    # Single-flight before the pairing check: "one is already running" is the
    # more specific, more useful answer, and it stays correct even if the
    # remote drops mid-run.
    if _self_test_active:
        raise HTTPException(409, "A self-test is already running")
    # Refuse to start on top of an in-flight tv_play. The 409 on /api/queue
    # only refuses NEW adds; an add that got in a moment earlier is still
    # inside its metadata scrape or its wake sequence, and starting a run
    # alongside it puts two senders on the TV at once.
    if queue_controller.state.waking or queue_controller.has_pending_sends():
        raise HTTPException(
            409,
            "The TV is mid-launch from a video someone just added. Wait for it "
            "to start, then run the self-test.",
        )
    _require_paired()

    probes = [
        {
            "id": pid, "label": label, "budget_s": budget,
            "status": "pending", "detail": None, "seconds": None,
            "_fn": fn,
        }
        for pid, label, budget, fn in _SELF_TEST_PROBES
    ]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _self_test.update({
        "status": "running",
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "eta_s": sum(p["budget_s"] for p in probes),
        "probes": probes,
        "report": None,
    })
    _self_test_answers.clear()
    _self_test_active = True
    _self_test_task = asyncio.create_task(_run_self_test(run_id))
    return {
        "run_id": run_id,
        "eta_s": _self_test["eta_s"],
        "probes": [{"id": p["id"], "label": p["label"]} for p in probes],
    }


class SelfTestAnswers(BaseModel):
    answers: dict


def _declared_profile() -> Optional[str]:
    """What the tester picked, if anything. 'other' is a real answer."""
    v = (_self_test_answers.get("device_profile") or "").strip()
    return v or None


def _effective_profile() -> Optional[str]:
    """Declared wins over detected — they can see the box, we are reading two
    strings off a protocol. But a declared 'other' must NOT clobber a
    confident detection: it means "I don't recognise the list", not "the
    detection is wrong"."""
    declared = _declared_profile()
    if declared and declared != "other":
        return declared
    return _detect_profile()


def _questions_for(profile: Optional[str]) -> tuple:
    """The questions worth showing this tester."""
    return tuple(
        q for q in _SELF_TEST_QUESTIONS
        if not q[2] or (profile and profile in q[2])
    )


def _questions_payload() -> list:
    """Questions for the UI, filtered by profile, with answers folded in.

    Anything already ANSWERED is included even when the current profile
    filters it out — a tester who answers the Shield questions and then
    switches the dropdown must not have their typing vanish from the report.
    """
    answers = _question_answers()
    shown = {q[0] for q in _questions_for(_effective_profile())}
    return [
        {"id": qid, "question": prompt,
         "choices": [{"value": v, "label": l} for v, l in choices],
         "answer": (answers.get(qid) or "").strip()}
        for qid, prompt, _profiles, choices in _SELF_TEST_QUESTIONS
        if qid in shown or (answers.get(qid) or "").strip()
    ]


def _question_answers() -> dict:
    """Tester answers, with device_model prefilled from the protocol.

    The device already told us its manufacturer and model; asking someone to
    type "NVIDIA SHIELD Android TV" by hand is how blanks happen. Their own
    typed answer always wins.
    """
    answers = dict(_self_test_answers)
    if not (answers.get("device_profile") or "").strip():
        detected = _detect_profile()
        if detected:
            answers["device_profile"] = detected
    if not (answers.get("device_model") or "").strip():
        try:
            info = dict(getattr(state.remote, "device_info", None) or {})
            guess = " ".join(
                x for x in (info.get("manufacturer"), info.get("model")) if x
            )
            if guess:
                answers["device_model"] = guess
        except Exception:
            pass
    return answers


@app.post("/api/selftest/answers")
async def selftest_answers(req: SelfTestAnswers):
    """Fold the tester's answers into the finished report.

    Separate from the run because the answers arrive after it: the questions
    only make sense once they have watched what happened. Values are capped
    and stored as data only — this is untrusted text from any LAN guest that
    ends up pasted into someone else's chat window.
    """
    if not SELF_TEST_ENABLED:
        raise HTTPException(503, "Self-test is disabled (SELF_TEST=0)")
    # From the FULL list, never the profile-filtered one: a tester who
    # answers the Shield questions and then switches the dropdown would
    # otherwise have that POST silently rejected and their typing lost.
    known = {q[0] for q in _SELF_TEST_QUESTIONS}
    for key, value in (req.answers or {}).items():
        if key in known and isinstance(value, str):
            _self_test_answers[key] = value[:500]
    report = _self_test.get("report")
    if isinstance(report, dict) and "questions_for_the_tester" in report:
        report["questions_for_the_tester"] = _questions_payload()
        # Hints are built at report time, before the tester picks anything —
        # so recompute them once a profile arrives, or a declared Bell box
        # would keep whatever the detection guessed at.
        report["device_profile"] = {
            "declared": _declared_profile(),
            "detected": _detect_profile(),
            "effective": _effective_profile(),
        }
        report["device_hints"] = _device_hints(
            _self_test.get("probes") or [], _effective_profile(),
        )
    return {"saved": len(_self_test_answers)}


@app.get("/api/selftest")
async def selftest_status():
    """Progress while running; the full report once done."""
    return {
        "enabled": SELF_TEST_ENABLED,
        # Worst-case run length, so the UI's "takes about N minutes" comes from
        # the same numbers the probes actually use instead of a hardcoded
        # string that drifts the first time a probe is added.
        "total_budget_s": sum(p[2] for p in _SELF_TEST_PROBES),
        "status": _self_test["status"],
        "run_id": _self_test["run_id"],
        "eta_s": round(_self_test["eta_s"], 1),
        "questions": _questions_payload(),
        "probes": [
            {"id": p["id"], "label": p["label"], "status": p["status"],
             "seconds": p["seconds"]}
            for p in _self_test["probes"]
        ],
        "report": _self_test["report"],
    }


@app.get("/api/queue")
async def get_queue():
    return queue_controller.snapshot()


@app.post("/api/queue")
async def add_to_queue(req: AddReq, request: Request):
    _reject_during_self_test()
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
    _reject_during_self_test()
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
    _reject_during_self_test()
    await queue_controller.pause()
    return {"ok": True, "paused": queue_controller.state.paused}


@app.post("/api/resume")
async def resume_playback():
    """Resume the current video on the TV (via Lounge or MEDIA_PLAY) and
    re-enable auto-advance. If the queue stalled with no current item but
    with items queued, starts the next one. Idempotent."""
    _reject_during_self_test()
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
    _reject_during_self_test()
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
    _reject_during_self_test()
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
    _reject_during_self_test()
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
