"""YouTube Lounge protocol monitor.

Connects to SmartTube on the TV via the YouTube Lounge HTTPS API — the
same TV-code linking protocol the official YouTube mobile app uses. SmartTube
implements this on Android TV. Replaces the previous pychromecast-based
castmonitor.py because the Lounge protocol does NOT trigger the TV's "blue
cast icon" UI overlay that pychromecast does (Cast UI is a mandatory part
of Google's Cast spec for unauthenticated senders; Lounge is pure HTTPS to
youtube.com/api/lounge so no receiver UI is involved).

What this gives us:
- Real playback observation: video_id, current_time, duration, player_state
- Playback control: play_video, pause, play, seek_to
- Auto-advance via the end-of-video state transition

Constraint worth knowing: SmartTube must be in the foreground for Lounge
commands to take effect. We still use androidtvremote2 to power the TV
on and foreground SmartTube when needed (cold boot, app switched away).

Pairing flow:
- One-time admin step: SmartTube on TV displays a 12-digit code
- User pastes that code into our /api/lounge/pair endpoint
- We exchange it for a long-lived lounge token, persist to /data/lounge.json
- All subsequent runs reuse the persisted token
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from pyytlounge import YtLoungeApi
from pyytlounge.event_listener import EventListener
from pyytlounge.events import State

log = logging.getLogger("smarttube-playlist.lounge")


# pyytlounge's `_process_event` indexes `args[0]` for several event types
# without first checking that `args` is non-empty. SmartTube has been
# observed sending `onSubtitlesTrackChanged` with empty args, which raises
# IndexError, kills the subscribe loop, and forces a reconnect — which
# surfaces as a momentary "Lounge offline" mid-cold-boot.
#
# Wrap `_process_event` so a single malformed event is logged and dropped
# instead of tearing down the session. Idempotent — only wraps once even
# if lounge.py is reloaded (tests do this).
def _screen_present_in(args) -> Optional[bool]:
    """Does a `loungeStatus` event's device list contain the TV?

    Computed from the list itself, per event. pyytlounge only ASSIGNS
    `_screen_name`/`_device_info` when a LOUNGE_SCREEN entry is present and
    never clears them, so a later status without the screen leaves stale
    attributes behind — reading those would report a screen that has left.
    None when the event cannot be parsed, so a malformed status is "unknown"
    rather than "absent".
    """
    try:
        devices = json.loads(args[0]["devices"])
        return any(d.get("type") == "LOUNGE_SCREEN" for d in devices)
    except Exception:
        return None


def _install_pyytlounge_event_guard() -> None:
    original = YtLoungeApi._process_event
    if getattr(original, "_smarttube_playlist_guard", False):
        return

    async def guarded(self, event_type, args):
        # Screen presence, reported to whoever built this api instance.
        # `loungeStatus` arrives in the bind response (inside connect()) and
        # again whenever the lounge's membership changes, so this is both the
        # connect-time reading and the live one. The hook is a plain attribute
        # the monitor sets before connect(); an api without it is left alone.
        present = None
        if event_type == "loungeStatus":
            present = _screen_present_in(args)
        elif event_type == "loungeScreenDisconnected":
            present = False
        try:
            await original(self, event_type, args)
        except (IndexError, KeyError) as exc:
            log.warning(
                "pyytlounge dropped malformed %s event (args=%r): %s",
                event_type, args, exc,
            )
        # After the original: its loungeStatus branch can raise
        # NotSupportedException for a blacklisted client, and that must keep
        # propagating exactly as before.
        hook = getattr(self, "_stp_on_screen_presence", None)
        if present is not None and hook is not None:
            try:
                hook(present)
            except Exception:
                log.exception("screen-presence hook raised")

    guarded._smarttube_playlist_guard = True  # type: ignore[attr-defined]
    YtLoungeApi._process_event = guarded


_install_pyytlounge_event_guard()


# Time to wait before treating a mid-playback Stopped state as a
# genuine end-of-playback (vs a brief ad-insertion transition).
# 5s is comfortable above typical ad-insertion blips (1-3s) and
# below the threshold where users would expect their queue to react.
STOPPED_PERSISTED_DELAY = 5.0

# Force a Lounge reconnect when current_time hasn't advanced this
# many consecutive periodic refreshes while we expect playback to be
# active. After tv_play takes the deep-link Intent path (used when
# SmartTube is foreground-but-idle and Lounge.setPlaylist isn't
# reliable), SmartTube's Lounge-layer state stops getting updates —
# get_now_playing keeps returning the same stale frame for minutes.
# A forced reconnect creates a new Lounge session against the same
# screen, which gives SmartTube a chance to push current state and
# unstuck us. Tradeoff: ~1-2 lost seconds of position info during
# the reconnect, vs the alternative of remote pause / navigation
# being undetectable for ages.
STUCK_CT_POLL_THRESHOLD = 5  # 5 polls × 3s = 15s
# While the frame says Paused a frozen position is expected, so the detector
# waits ~3x longer before forcing a rebind. Not disabled: on a deep-link
# session the layer is deaf and the rebind is the only thing that will ever
# show a resume made with the TV remote.
STUCK_CT_PAUSED_POLL_THRESHOLD = 15  # 15 polls × 3s = 45s

# How often the periodic refresh asks SmartTube for its current state.
# Module-level rather than a local so tests can patch it — when a timing
# constant is only reachable as a local, no test can exercise the behaviour
# it governs, which is how the wedge below survived the whole suite.
REFRESH_INTERVAL = 3.0

# How long the TV must have been absent from the lounge before the absence is
# REPORTED (status, UI, the remedy warning). The raw reading is not debounced —
# a pause during a dip genuinely reaches nobody, so the functional fallbacks
# key off it immediately. But a healthy screen drops out of the device list
# for 32-85 seconds roughly every 4-5 minutes as routine churn (measured on a
# freshly rebooted device, once mid-playback, position streaming throughout),
# and surfacing every dip flashed "SMARTTUBE NOT LISTENING" plus TV-settings
# remedy text at users several times an hour for a state that fixes itself.
# Arrivals are still believed instantly. A real death (the original incident
# ran for hours) is reported within one bind-plus-grace, ~8 minutes worst case.
SCREEN_OFFLINE_GRACE = 180.0

# Consecutive get_now_playing() failures before we stop believing the
# session and force a reconnect. The loop used to swallow these forever on
# the assumption that "the subscribe loop will reconnect if needed" — true
# only when subscribe() itself raises. When the session dies underneath a
# subscribe that is still blocked, nothing ever noticed and the monitor
# reported itself healthy indefinitely (measured: over an hour in
# production, recoverable only by restarting the container).
MAX_REFRESH_FAILURES = 3


@dataclass
class LoungeObservation:
    """Snapshot of what the Lounge session is reporting."""
    available: bool = False         # connected and receiving events
    video_id: Optional[str] = None
    current_time: Optional[float] = None
    duration: Optional[float] = None
    state: Optional[str] = None     # "Playing" | "Paused" | "Stopped" | etc.

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "video_id": self.video_id,
            "current_time": self.current_time,
            "duration": self.duration,
            "state": self.state,
        }


# Events emitted via the on_event callback.
EVENT_CONNECTED    = "lounge.connected"
EVENT_DISCONNECTED = "lounge.disconnected"
EVENT_NOW_PLAYING  = "lounge.now_playing"   # video changed
EVENT_STATE        = "lounge.state"         # play/pause state changed
EVENT_POSITION     = "lounge.position"      # routine position update
EVENT_FINISHED     = "lounge.finished"      # video ended naturally


LoungeEventCallback = Callable[[str, LoungeObservation], Awaitable[None]]


def serialize_auth(api: YtLoungeApi) -> dict:
    """Build a dict suitable for `load_auth_state()` from a paired YtLoungeApi.

    pyytlounge's `store_auth_state()` and `load_auth_state()` are NOT
    symmetric (different field names + missing version/expiry), so we use
    this to produce the load-compatible shape.
    """
    a = api.auth
    return {
        "version": 0,
        "screenId": a.screen_id,
        "loungeIdToken": a.lounge_id_token,
        "refreshToken": a.refresh_token,
        "expiry": a.expiry,
    }


class _LoungeListener(EventListener):
    """Bridge from pyytlounge events to LoungeMonitor's async event flow."""

    def __init__(self, monitor: "LoungeMonitor"):
        super().__init__()
        self._monitor = monitor

    async def now_playing_changed(self, event):
        await self._monitor._on_now_playing(event)

    async def playback_state_changed(self, event):
        await self._monitor._on_playback_state(event)

    async def disconnected(self, event):
        await self._monitor._on_disconnected_event(event)


class LoungeMonitor:
    """Manages a Lounge session: pair, persist auth, connect, observe, control."""

    def __init__(
        self,
        *,
        device_name: str = "SmartTube Playlist",
        on_event: Optional[LoungeEventCallback] = None,
        should_refresh: Optional[Callable[[], bool]] = None,
        on_auth_refreshed: Optional[Callable[[dict], None]] = None,
    ):
        self.device_name = device_name
        self._on_event: LoungeEventCallback = on_event or _noop_event
        # When this returns False, the periodic refresh loop skips its
        # get_now_playing() poll. Lets the host gate refreshes on
        # "we have a current item we're tracking" — without that gate,
        # the refresh would keep repopulating Lounge state from
        # SmartTube's server cache even after we cleared our queue,
        # making old videos "pop back up" in the UI after a kill-switch.
        self._should_refresh: Callable[[], bool] = should_refresh or (lambda: True)
        # Called with the refreshed auth dict whenever _connect successfully
        # refreshed the lounge_id_token. Host persists it to disk so the
        # next startup uses the fresh token instead of the expired one.
        self._on_auth_refreshed: Optional[Callable[[dict], None]] = on_auth_refreshed
        self._api: Optional[YtLoungeApi] = None
        self._listener = _LoungeListener(self)
        self._subscribe_task: Optional[asyncio.Task] = None
        # How many connect attempts the subscribe loop has COMPLETED, whether
        # they succeeded or failed. Monotonic. Read by
        # `_wait_for_lounge_connected` so an add does not spend the whole
        # LOUNGE_CONNECT_TIMEOUT waiting for an answer that has already come
        # back no. Deliberately not folded into `is_connected`, which means
        # "session bound" and is polled for that meaning.
        self._connect_attempts = 0
        # The in-flight `subscribe()` call, as its own task so `_teardown`
        # can end it. pyytlounge blocks there on an untimed streaming GET and
        # only re-checks `connected()` after an event arrives, and
        # `disconnect()` does not close that stream — so without this a
        # teardown left the loop parked on a dead session forever.
        self._subscribe_call: Optional[asyncio.Task] = None
        # Set by `_teardown` immediately before it cancels `_subscribe_call`,
        # so the loop can tell "we ended this session on purpose, reconnect"
        # from "the monitor is being stopped".
        self._teardown_interrupt = False
        self._refresh_task: Optional[asyncio.Task] = None
        # Timer that fires EVENT_FINISHED if state stays Stopped
        # mid-playback for STOPPED_PERSISTED_DELAY seconds. Distinguishes
        # the brief Stopped transitions during ad insertion (which
        # resolve back to Playing quickly) from genuine "playback
        # ended" — including user backing out of the player to
        # SmartTube's home screen mid-video.
        self._stopped_persisted_task: Optional[asyncio.Task] = None
        self._observation = LoungeObservation()
        self._auth: Optional[dict] = None
        self._stopped = False
        # Is SmartTube actually IN the lounge our session is bound to?
        #
        # A Lounge session is a session with YouTube's cloud, not with the
        # TV. Measured in production 2026-08-23: SmartTube's remote-control
        # registration died while a video played, the cloud went on binding
        # us and answering pause/play/setPlaylist/getNowPlaying with HTTP 200,
        # `connected()` (a local SID check) stayed True — and nothing on our
        # side could tell that nobody was listening. The bind response's
        # `loungeStatus` device list CAN tell: it lists a LOUNGE_SCREEN entry
        # when the TV is present and nothing when it is not.
        #
        # Three-valued on purpose. None is "not observed" and leaves every
        # consumer behaving as it always did; only an observed False changes
        # anything. It is NOT folded into `is_connected`, which means "session
        # bound" and is polled by `_wait_for_lounge_connected` for the full
        # LOUNGE_CONNECT_TIMEOUT — redefining it would turn a 3s pre-deep-link
        # wait into 15s on every add.
        self._screen_online: Optional[bool] = None
        # When the current absence began (monotonic), whether its remedy
        # warning has fired, and whether the screen has ever been seen online
        # this session — together these produce `screen_online_settled`, the
        # debounced view the status surface reports. See SCREEN_OFFLINE_GRACE.
        self._screen_missing_since: Optional[float] = None
        self._screen_offline_warned = False
        self._screen_seen_online = False
        # Set by request_reconnect_now() to interrupt the subscribe loop's
        # backoff sleep — used by tv_play before it waits on Lounge, so we
        # don't sit out a 5-60s exponential backoff. That backoff is easy to
        # land in: the sender gives up while SmartTube is backgrounded behind
        # a screensaver, which is exactly when the next play arrives.
        self._wake_subscribe = asyncio.Event()

    @property
    def observation(self) -> LoungeObservation:
        return self._observation

    @property
    def screen_online(self) -> Optional[bool]:
        """Whether the TV is in the lounge our session is bound to.

        True/False only from an actual `loungeStatus` (or a
        `loungeScreenDisconnected`); None until one has been seen. A bound
        session with this False is the "connected, and nobody is listening"
        state — commands return True from the cloud and reach no device.
        """
        return self._screen_online

    @property
    def screen_online_settled(self) -> Optional[bool]:
        """The debounced presence view — what status pages should show.

        True the moment the TV is seen (arrivals are pushed and believed
        instantly). False only once an absence has PERSISTED past
        SCREEN_OFFLINE_GRACE — a healthy screen takes 32-85s dips every few
        minutes, and every one of them used to flash the remedy banner. While
        an absence is inside the grace, the last settled truth stands: True
        if the screen has been seen this session, None if it never has.
        """
        if self._screen_online is None:
            return None
        if self._screen_online:
            return True
        since = self._screen_missing_since
        if since is not None and time.monotonic() - since >= SCREEN_OFFLINE_GRACE:
            return False
        return True if self._screen_seen_online else None

    def _note_screen_presence(self, present: bool) -> None:
        previous = self._screen_online
        self._screen_online = bool(present)
        if self._screen_online:
            self._screen_seen_online = True
            if self._screen_missing_since is not None:
                gone = time.monotonic() - self._screen_missing_since
                if self._screen_offline_warned:
                    log.info(
                        "SmartTube is back in the lounge; playback sync restored"
                    )
                else:
                    log.info(
                        "SmartTube is back in the lounge (was out %.0fs — "
                        "routine churn)", gone,
                    )
            self._screen_missing_since = None
            self._screen_offline_warned = False
            return
        now = time.monotonic()
        if self._screen_missing_since is None:
            self._screen_missing_since = now
            if previous is True:
                log.info(
                    "SmartTube dropped out of the lounge — routine churn "
                    "unless it persists; the page is not told yet"
                )
        if (not self._screen_offline_warned
                and now - self._screen_missing_since >= SCREEN_OFFLINE_GRACE):
            # Persisted past anything a dip has ever measured: now it is the
            # real state, worth the alarm and the remedy — once per absence.
            self._screen_offline_warned = True
            log.warning(
                "SmartTube has been out of the lounge for over %.0fs: the "
                "session is still bound and YouTube still answers, but nothing "
                "reaches the TV. Pause falls back to the remote keycode and "
                "playback position is unavailable until it returns. On the "
                "TV: SmartTube -> Settings -> Remote control, off and on "
                "again.", now - self._screen_missing_since,
            )

    async def note_local_transport(self, new_state: str) -> None:
        """Record the effect of a transport command WE just delivered.

        The observation is a cache of what the device has TOLD us, and on a
        deep-link session SmartTube's Lounge layer tells us nothing: a
        MEDIA_PLAY/MEDIA_PAUSE keycode moves the picture and the frame goes
        on saying whatever it said. For a video we own that is harmless — the
        queue's own `paused` flag is authoritative and is published first.
        For playback we do NOT own the frame is the only state there is, and
        the page kept showing PAUSED ON TV over a video our own Play had just
        resumed (2026-08-31, three presses in a row, TV playing throughout).

        Same reasoning as publish-first: the viewer pressed the button and the
        keycode went out, so the expected effect is the best evidence we have
        until the device speaks — and any real push overrides it. Writing
        "Playing" also lets the external-playback refresh (which requires a
        Playing frame) and the stuck-ct self-heal take over, so a deaf layer
        gets rebound and re-read within seconds instead of at the next
        five-minute stream close.
        """
        obs = self._observation
        if not obs.available or not obs.video_id or obs.state == new_state:
            return
        obs.state = new_state
        await self._safe_emit(EVENT_STATE, obs)

    async def request_now_playing(self) -> bool:
        """Force ONE get_now_playing(), bypassing the should_refresh gate.

        `observation` is a passive cache: SmartTube pushes state on
        TRANSITIONS ONLY, so position during steady playback never arrives
        unless somebody asks. `_periodic_refresh_loop` normally does the
        asking, but it is gated on the queue owning a current item — and the
        self-test plays OUTSIDE the queue by design, so during a self-test
        that gate is shut and the observation is frozen at whatever the last
        transition left behind.

        Callers must have established that SmartTube is foregrounded and
        playing before calling this. That is the whole reason for the gate:
        polling against a BACKGROUNDED SmartTube auto-foregrounds it (a
        YouTube protocol behaviour), which on a Shield can even wake the
        device. This method deliberately cannot check that for itself, so it
        is not a general-purpose refresh. EVERY caller must establish that
        condition its own way before calling — waiting for a Playing state,
        or reading `current_app` and finding SmartTube foreground — and a
        caller that reads the foreground must read it FRESH, not before an
        arbitrary wait (a lock, a verify window): a single stale read is the
        shape `_foreground_blocks_media_key` exists to avoid.
        """
        api = self._api
        if api is None or not self._observation.available:
            return False
        try:
            await api.get_now_playing()
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("Explicit get_now_playing failed", exc_info=True)
            return False

    @property
    def connect_attempts(self) -> int:
        """Completed connect attempts by the subscribe loop. Monotonic."""
        return self._connect_attempts

    @property
    def is_paired(self) -> bool:
        return self._auth is not None

    @property
    def is_connected(self) -> bool:
        """True only when the library agrees the session is live.

        `_observation.available` is set once at connect and cleared only by
        `_teardown()`, so on its own it is not evidence of anything: when
        pyytlounge's `_connection_lost()` clears `_sid`/`_gsession` (it does
        so on HTTP 400 "Unknown SID", 410 "Gone" and 401 "Expired") every
        command starts failing instantly while this kept answering True.
        That answer feeds /api/status, the UI's Lounge badge, and tv_play's
        decision to spend LOUNGE_CONNECT_TIMEOUT waiting for a reply that
        can never come.
        """
        api = self._api
        if api is None or not self._observation.available:
            return False
        try:
            return bool(api.connected())
        except Exception:
            # A library that cannot answer is not a session we should
            # advertise as usable.
            return False

    # ── pairing (called from /api/lounge/pair endpoint) ──────────────────────

    @staticmethod
    async def pair_with_code(device_name: str, code: str) -> dict:
        """One-shot: exchange a 12-digit pairing code for an auth dict.
        Strips spaces/dashes from the code (TV displays it formatted)."""
        digits = "".join(c for c in code if c.isdigit())
        if not digits:
            raise ValueError("empty pairing code")

        async with YtLoungeApi(
            device_name, logger=log.getChild("pyytlounge"),
        ) as api:
            ok = await api.pair(digits)
            if not ok or not api.linked():
                raise RuntimeError("pair returned False or session not linked")
            return serialize_auth(api)

    def load_auth(self, auth_dict: dict) -> None:
        """Set auth from a previously-persisted pairing."""
        self._auth = auth_dict

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin the connection + subscribe loop. No-op if not paired."""
        if self._auth is None:
            log.info("Lounge: not paired; nothing to start")
            return
        self._stopped = False
        try:
            await self._connect()
        except Exception:
            log.warning("Lounge initial connect failed; subscribe loop will retry", exc_info=True)
        self._subscribe_task = asyncio.create_task(self._subscribe_loop())
        self._refresh_task = asyncio.create_task(self._periodic_refresh_loop())

    async def stop(self) -> None:
        self._stopped = True
        for task in (self._subscribe_task,
                     getattr(self, "_refresh_task", None),
                     getattr(self, "_stopped_persisted_task", None)):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        await self._teardown()
        self._screen_online = None
        self._screen_missing_since = None
        self._screen_offline_warned = False
        self._screen_seen_online = False

    def request_reconnect_now(self) -> None:
        """Wake the subscribe loop out of any current backoff sleep so it
        retries connect immediately. Intended for callers (tv_play) who
        just brought SmartTube to foreground and want Lounge usable
        ASAP, without waiting for the loop's exponential backoff.

        When the session is bound to a lounge the screen has LEFT, this also
        ends that session so the loop rebinds at once. Left alone, a
        screenless bind is only refreshed when YouTube closes the stream on
        its ~5-minute clock; an add is the one moment we know someone wants
        the TV now, and a fresh bind is how a screen that has come back gets
        seen before the launch decides how to play. The teardown is the
        self-heal kind (no DISCONNECTED to the queue controller), same as the
        stuck-ct path.
        """
        if self._api is not None and self._screen_online is False:
            log.info("Rebinding the Lounge session: SmartTube was not in the "
                     "lounge at the last bind")
            asyncio.create_task(self._teardown(notify=False))
        self._wake_subscribe.set()

    # ── playback control ─────────────────────────────────────────────────────

    async def play_video(self, video_id: str, start_s: Optional[int] = None) -> bool:
        """Push a video to play in SmartTube. If start_s is set, seeks
        after the video loads.

        Sends only setPlaylist (and seek_to if needed). We do NOT chase
        with an explicit play() — that's a leftover from when we used
        this on cold-boot, where SmartTube would sometimes load paused.
        On the hot path SmartTube auto-plays setPlaylist, and the
        redundant play() at +1s caused a visible play→pause→play
        stutter. Cold-boot reliability is now handled by the deep-link
        backup in tv_play, not by this command."""
        if self._api is None:
            return False
        try:
            ok = await self._api.play_video(video_id)
            if not ok:
                # setPlaylist itself was refused — pyytlounge answers 400
                # "Unknown SID" / 410 "Gone" / 401 "Expired" with False and no
                # exception. NOTHING reached the TV, so the caller's deep-link
                # fallback is exactly right.
                return False
            if start_s and start_s > 0:
                # Give SmartTube a moment to load the media before seeking.
                await asyncio.sleep(1.0)
                seek_ok = False
                try:
                    # The bool matters as much as the exception: `_command`
                    # reports a dead session by returning False.
                    seek_ok = bool(await self._api.seek_to(start_s))
                except Exception:
                    log.warning("Lounge seek_to(%s) raised", start_s,
                                exc_info=True)
                if not seek_ok:
                    # STILL a success, and this is the load-bearing part.
                    # The caller answers False by sending a deep link, and
                    # setPlaylist has already started the video — so returning
                    # False here means two play signals for one add, which is
                    # invariant 1 and audible as the clip restarting a second
                    # or two in. Losing the offset is the lesser harm: the
                    # video is playing, just from the beginning.
                    log.warning(
                        "Lounge setPlaylist for %s landed but the %ss offset "
                        "did not apply; leaving it playing from the start "
                        "rather than sending a second play signal",
                        video_id, start_s,
                    )
            return True
        except Exception:
            log.warning("Lounge play_video(%s) failed", video_id, exc_info=True)
            return False

    async def pause(self) -> bool:
        if self._api is None:
            return False
        try:
            # Load-bearing return value, same as seek_to and play_video below:
            # all three are a bare `return await self._command(...)` in
            # pyytlounge, and `_command` answers 400 "Unknown SID", 410 "Gone"
            # and 401 "Expired" with False and no exception. Hardcoding True
            # here skipped the MEDIA_PAUSE keycode fallback — the only one
            # there is — while QueueController.pause set paused/pause_source
            # anyway, so the TV played on under a UI that said paused and the
            # duration timer later ended a video still on screen.
            return bool(await self._api.pause())
        except Exception:
            log.warning("Lounge pause failed", exc_info=True)
            return False

    async def play(self) -> bool:
        if self._api is None:
            return False
        try:
            # Same reasoning as pause. Milder here, because _lounge_play
            # verifies the state actually reaches Playing and falls through
            # to tv_play when it does not — but that verification should not
            # be the only thing catching a session that is already dead.
            return bool(await self._api.play())
        except Exception:
            log.warning("Lounge play failed", exc_info=True)
            return False

    async def seek_to(self, seconds: float) -> bool:
        """Seek the currently-playing video to an absolute position.

        SmartTube clamps out-of-range values internally — beyond duration
        snaps to end-of-video (often triggering a finished event), below
        zero snaps to 0. We pass through whatever the caller gives us.
        """
        if self._api is None:
            return False
        try:
            # The RETURN VALUE is load-bearing, exactly as it is in play_video.
            # pyytlounge's `_command` returns False WITHOUT raising when the
            # bind channel answers 400 "Unknown SID", 410 "Gone" or 401
            # "Expired" — and `connected()` cannot see that coming, because it
            # is a purely local check on the cached SID. Discarding it made
            # /api/seek answer 200 for a seek the device never received, and
            # the caller then re-anchors the duration timer and the playhead
            # origin to a position playback never reached — which strands the
            # queue when the video really ends.
            return bool(await self._api.seek_to(seconds))
        except Exception:
            log.warning("Lounge seek_to(%s) failed", seconds, exc_info=True)
            return False

    # ── internals ────────────────────────────────────────────────────────────

    async def _connect(self) -> None:
        if self._auth is None:
            return
        # YtLoungeApi requires async-context-manager init. We manually enter
        # the context so the api lives across the lifetime of the session.
        api = YtLoungeApi(
            self.device_name,
            event_listener=self._listener,
            # Not optional. Left to itself pyytlounge builds
            # logging.Logger(...) through the constructor, whose parent is
            # None — so it never propagates to root, our redacting formatter
            # never sees it, and its records fall through to
            # logging.lastResort (stderr). It logs the Lounge token verbatim
            # at INFO and its exception tracebacks carry request URLs with the
            # token in the query string. A child of our logger propagates
            # normally and gets redacted like everything else.
            logger=log.getChild("pyytlounge"),
        )
        await api.__aenter__()
        # Before connect(): the bind response carries the loungeStatus that
        # says whether the TV is in this lounge, and pyytlounge processes it
        # inline inside connect(). The event guard reports through this hook.
        api._stp_on_screen_presence = self._note_screen_presence  # type: ignore[attr-defined]
        screen_was = self._screen_online
        try:
            api.load_auth_state(self._auth)
            await api.connect()
            if not api.connected():
                # connect() got a 200 from /bc/bind but the response didn't
                # contain a valid SID/gsession — typically means our cached
                # loungeIdToken has expired (Lounge tokens live ~weeks).
                # Refresh the token via the screen_id and retry connect.
                # Persists the fresh auth so next startup uses the new token.
                log.info(
                    "Lounge connect: stale lounge_id_token; calling refresh_auth() and retrying"
                )
                await api.refresh_auth()
                await api.connect()
                if not api.connected():
                    raise RuntimeError(
                        "connect() still not connected after refresh_auth(); "
                        "screen may have been unpaired on the TV"
                    )
                # Bubble the refreshed auth back to the host so it can persist.
                self._auth = serialize_auth(api)
                if self._on_auth_refreshed is not None:
                    try:
                        self._on_auth_refreshed(self._auth)
                    except Exception:
                        log.warning(
                            "auth-refresh callback raised; new token won't persist "
                            "across restart until the next successful refresh",
                            exc_info=True,
                        )
        except Exception:
            with contextlib.suppress(Exception):
                await api.__aexit__(None, None, None)
            raise
        self._api = api
        self._observation = LoungeObservation(available=True)
        log.info("Lounge connected: %s", api.screen_device_name or api.screen_name)
        if self._screen_online is False:
            # The line above reads "Lounge connected: None" here — the
            # fingerprint. One INFO line per bind; the loud remedy WARNING is
            # `_note_screen_presence`'s, and only once the absence persists
            # past SCREEN_OFFLINE_GRACE — a single absent bind is routinely a
            # dip in the TV's own connection, not a dead registration.
            log.info("SmartTube is not in the lounge at this bind")
        await self._safe_emit(EVENT_CONNECTED, self._observation)
        # Ask SmartTube to push current state. Without this we only get events
        # when state CHANGES — if a video has been playing since before we
        # connected, no events fire and we have no idea what's on the TV.
        try:
            await api.get_now_playing()
        except Exception:
            log.warning("Lounge get_now_playing on connect failed", exc_info=True)

    async def _teardown(self, *, notify: bool = True) -> None:
        """Drop the session. `notify=False` for a SELF-HEAL.

        The DISCONNECTED emit means "the player went away" to the queue
        controller, which schedules a player-close verify and can clear
        `current`. That is right when subscribe() genuinely ended; it is
        wrong when WE tore the session down to rebuild it, because the
        video is still playing. A ~17s network blip between the container
        and YouTube would otherwise clear the card and strand the queue.
        """
        if self._api is None:
            return
        api, self._api = self._api, None
        was_available = self._observation.available
        self._observation = LoungeObservation()
        # END THE SUBSCRIBE, don't just stop believing in it. `disconnect()`
        # POSTs a terminate over a SEPARATE request and leaves the streaming
        # response open, and pyytlounge only re-checks `connected()` after the
        # next event — which on a quiet session never comes. Without this
        # cancel the loop stayed parked inside `subscribe()` and no teardown
        # path ever recovered: not the stuck-ct self-heal, not the
        # consecutive-failure teardown, not `request_reconnect_now`. The
        # watchdog could not see it either, because it tests `task.done()` and
        # a blocked task is not done. Measured on hardware: Lounge died 15s
        # into a session and was still dead seven minutes later with the video
        # playing throughout.
        # ...but never the task we are RUNNING ON. pyytlounge awaits event
        # listeners inline inside subscribe(), and an onDisconnected event
        # routes here through `_on_disconnected_event` — so on that path this
        # IS the subscribe task. Cancelling it makes the next await raise
        # CancelledError, which `suppress(Exception)` cannot catch, and the
        # disconnect, the `__aexit__` that closes the aiohttp session, and the
        # DISCONNECTED emit are all skipped. Returning from the listener
        # unwinds subscribe() by itself, so there is nothing to cancel there.
        sub = self._subscribe_call
        if (sub is not None and not sub.done()
                and sub is not asyncio.current_task()):
            self._teardown_interrupt = True
            sub.cancel()
        with contextlib.suppress(Exception):
            await api.disconnect()
        with contextlib.suppress(Exception):
            await api.__aexit__(None, None, None)
        if was_available and notify:
            await self._safe_emit(EVENT_DISCONNECTED, self._observation)

    async def _periodic_refresh_loop(self) -> None:
        """Periodically poll get_now_playing() to keep observation fresh.

        SmartTube only sends nowPlaying / onStateChange events on
        actual state transitions, not periodically. So between
        transitions, observation.current_time can lag actual
        playback by tens of seconds while state=Playing — verified
        against `adb shell dumpsys media_session` reporting position
        50s+ ahead of our cached ct. Worse: when SmartTube resumes
        from a paused state via our Lounge.play() command, Lounge
        often doesn't push a state change event, so we keep
        reporting Paused indefinitely even though playback is real.

        Polling get_now_playing() every few seconds forces SmartTube
        to push current state. Idle-cheap: skips when not connected.

        Stuck-ct detection: if get_now_playing keeps returning the
        same current_time across STUCK_CT_POLL_THRESHOLD consecutive
        polls while we expect playback, force a Lounge reconnect.
        This handles the deep-link Intent path: tv_play falls through
        to send an Intent that starts playback reliably but bypasses
        Lounge protocol entirely, so SmartTube's Lounge-layer state
        keeps reporting stale data until the next forced refresh.
        Without this, remote-pause / navigate-to-home events take
        minutes to propagate to our UI.
        """
        # A sentinel, not None, for "we have taken no reading yet". The
        # detector used to seed last_ct with None and skip the comparison
        # while it stayed None — so a ct that was NEVER populated (the exact
        # signature of a wedged session) could never register as stuck, and
        # the only self-healing path in the monitor was unreachable in the
        # case it most needed to fire. With a sentinel, None == None counts.
        _NO_READING = object()
        last_ct = _NO_READING
        stuck_polls = 0
        consecutive_failures = 0
        while not self._stopped:
            try:
                await asyncio.sleep(REFRESH_INTERVAL)
            except asyncio.CancelledError:
                return
            api = self._api
            if api is None or not self._observation.available:
                last_ct = _NO_READING
                stuck_polls = 0
                consecutive_failures = 0
                continue
            # Only refresh when our host (the queue controller) has an
            # active item to track. Otherwise the refresh keeps pulling
            # SmartTube's stale Lounge cache and re-populating
            # state.lounge, making old videos "pop back up" in the UI
            # after the user navigates away. With this gate, the UI
            # naturally stays empty when nothing is queued.
            try:
                if not self._should_refresh():
                    last_ct = _NO_READING
                    stuck_polls = 0
                    consecutive_failures = 0
                    continue
            except Exception:
                pass
            if self._screen_online is False:
                # Nobody is in the lounge to answer. A poll here is a request
                # to no one, and its silence is not the wedge the stuck-ct
                # detector exists for — with the `_NO_READING` sentinel it
                # counted None == None as stuck and rebound the session every
                # ~18s for as long as an item was owned, to no effect. Stand
                # the poll and the counter down; the exception-driven
                # self-heal below stays armed, and the screen's return is
                # seen at the next bind (YouTube's ~5-minute stream end, or
                # `request_reconnect_now` on the next add).
                last_ct = _NO_READING
                stuck_polls = 0
                # Like the sibling skip branches above: a stretch with no
                # polls attempted must not let two old failures plus one new
                # one read as "three in a row".
                consecutive_failures = 0
                continue
            try:
                await api.get_now_playing()
                consecutive_failures = 0
            except asyncio.CancelledError:
                return
            except Exception:
                consecutive_failures += 1
                log.debug("Lounge periodic get_now_playing failed (%d in a row)",
                          consecutive_failures, exc_info=True)
                if consecutive_failures >= MAX_REFRESH_FAILURES:
                    # Do NOT keep waiting for the subscribe loop to notice.
                    # It only reconnects when subscribe() itself raises, and
                    # a session can die underneath a subscribe that stays
                    # blocked — which is precisely the wedge this guards.
                    log.info(
                        "Lounge get_now_playing failed %d times in a row — "
                        "tearing down so the subscribe loop rebuilds the session",
                        consecutive_failures,
                    )
                    consecutive_failures = 0
                    last_ct = _NO_READING
                    stuck_polls = 0
                    await self._teardown(notify=False)
                continue
            # Detect stuck ct. The get_now_playing response is processed
            # asynchronously via _on_now_playing; by the time we check
            # the next loop iteration, observation should reflect it.
            # Compare against the previous loop's observation.
            current_ct = self._observation.current_time
            if last_ct is not _NO_READING and current_ct == last_ct:
                stuck_polls += 1
                # A frozen position is EXPECTED while the frame says Paused,
                # so the paused threshold is longer: the rebind it eventually
                # forces is still wanted — it is the only thing that re-reads
                # a deaf layer after a resume made with the TV remote — but
                # once every ~45s, not every 15s, on a video someone paused.
                threshold = (STUCK_CT_PAUSED_POLL_THRESHOLD
                             if self._observation.state == "Paused"
                             else STUCK_CT_POLL_THRESHOLD)
                if stuck_polls >= threshold:
                    log.info(
                        "Lounge ct stuck at %s for %d polls (~%ds, frame %s) — "
                        "forcing reconnect to refresh SmartTube's Lounge-layer "
                        "state",
                        current_ct, stuck_polls,
                        int(stuck_polls * REFRESH_INTERVAL),
                        self._observation.state,
                    )
                    stuck_polls = 0
                    last_ct = _NO_READING
                    # Tear down the current session. The subscribe loop
                    # will create a fresh one on its next iteration,
                    # which triggers SmartTube to push current state on
                    # connect (see _connect's get_now_playing call).
                    await self._teardown(notify=False)
                    continue
            else:
                stuck_polls = 0
                last_ct = current_ct

    # How long a bound stream may deliver NOTHING before we stop believing in
    # it. Must stay comfortably above YouTube's own bind period — measured at
    # ~4.5-5 minutes, so a healthy stream ends and restarts well inside this —
    # or a healthy session would be recycled on a timer for no reason.
    SUBSCRIBE_INACTIVITY_TIMEOUT = 360.0

    async def _subscribe_loop(self) -> None:
        """Bulletproof outer wrapper around the actual subscribe loop.

        The inner loop SHOULD never raise an unhandled exception, but
        if it does (programming bug, unexpected pyytlounge behavior on a
        new YouTube protocol quirk, etc.) we'd silently lose the
        ability to reconnect for the entire process lifetime — the
        Lounge meta status would just stay OFFLINE until someone
        restarts the container. The outer try/except catches anything
        the inner loop fails to handle, logs it, sleeps, and restarts.
        """
        while not self._stopped:
            try:
                await self._subscribe_loop_body()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Lounge subscribe loop crashed unexpectedly; "
                    "restarting after 30s"
                )
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    raise

    async def _subscribe_loop_body(self) -> None:
        """Process the Lounge event stream. pyytlounge's subscribe() blocks
        for the duration of the session; if it raises (network blip, library
        bug on a malformed event, etc.), we tear down and reconnect after a
        short backoff. The backoff sleep can be interrupted by
        request_reconnect_now() — used by tv_play after foregrounding
        SmartTube so we get a Lounge session back fast."""
        backoff = 5.0
        while not self._stopped:
            if self._api is None:
                # Consume any pending wake BEFORE the attempt, never after a
                # failure: a `request_reconnect_now()` that lands while the
                # connect is in flight must survive to short-circuit the
                # backoff below, or the caller — an add, sitting in
                # `_wait_for_lounge_connected` — waits out a 5-60s backoff
                # for an answer the loop could have had immediately.
                self._wake_subscribe.clear()
                try:
                    await self._connect()
                except Exception:
                    log.debug("Lounge reconnect attempt failed", exc_info=True)
                # Counted whether it returned or raised: what a waiting caller
                # needs to know is that an attempt COMPLETED, not how it ended.
                self._connect_attempts += 1
                if self._api is None:
                    try:
                        await asyncio.wait_for(
                            self._wake_subscribe.wait(), timeout=backoff,
                        )
                        log.info(
                            "Lounge subscribe loop woken early "
                            "(request_reconnect_now); retrying connect"
                        )
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, 60.0)
                    continue
                backoff = 5.0
            try:
                # As its own task, so `_teardown` can end it — see the note
                # there. Awaiting `subscribe()` inline made every teardown
                # path a no-op that killed Lounge for the process lifetime.
                sub = asyncio.create_task(self._api.subscribe())
                self._subscribe_call = sub
                went_silent = False
                try:
                    # Bounded, because pyytlounge's subscribe() is an untimed
                    # streaming GET: on a SID YouTube has already invalidated
                    # it neither delivers nor ends, and the loop parks for the
                    # process lifetime. `_teardown` can end that, but every
                    # caller of it needs a trigger — and the stuck-ct self-heal
                    # only runs while the queue owns an item, so an IDLE
                    # session in this state has no trigger at all.
                    #
                    # Borrowed from youtube_lounge_rs, a Rust client for the
                    # same protocol, which wraps its stream read in an
                    # inactivity timeout and re-polls when it expires.
                    # pyytlounge exposes no SID-invalidation signal to use
                    # instead.
                    #
                    # Safe because the recovery is the routine path, not a new
                    # one: YouTube ends the bind stream every ~4.5-5 minutes
                    # anyway, so tear-down-and-reconnect already runs on every
                    # video longer than five minutes. Worst case we do slightly
                    # more often what we already do. Hence the generous
                    # threshold — silence past it is not a slow session, it is
                    # a session that is never coming back.
                    await asyncio.wait_for(
                        sub, timeout=self.SUBSCRIBE_INACTIVITY_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    went_silent = True
                finally:
                    self._subscribe_call = None
                if went_silent:
                    log.warning(
                        "Lounge stream delivered nothing for %.0fs and did not "
                        "end — treating it as a dead session and reconnecting",
                        self.SUBSCRIBE_INACTIVITY_TIMEOUT,
                    )
                else:
                    # subscribe() returning normally means the session ended.
                    log.info("Lounge subscribe ended; tearing down for reconnect")
                await self._teardown()
            except asyncio.CancelledError:
                if self._teardown_interrupt:
                    # OUR OWN teardown ended that session. Loop round and
                    # reconnect; the monitor is not being stopped.
                    self._teardown_interrupt = False
                    log.info("Lounge session ended by teardown; reconnecting")
                    continue
                raise
            except Exception:
                log.warning("Lounge subscribe error; will reconnect", exc_info=True)
                await self._teardown()
                self._wake_subscribe.clear()
                try:
                    await asyncio.wait_for(
                        self._wake_subscribe.wait(), timeout=backoff,
                    )
                except asyncio.TimeoutError:
                    pass

    # ── pyytlounge event handlers ────────────────────────────────────────────

    async def _on_now_playing(self, event) -> None:
        # Be conservative about overwrites here. pyytlounge constructs
        # NowPlayingEvent with these defaults when the underlying message
        # omits a field:
        #   videoId -> None
        #   state   -> State.Stopped     (NOT a real signal — the field's
        #                                  NotRequired in the protocol)
        # SmartTube fires nowPlaying both as the initial state push after
        # connect and as routine metadata refreshes mid-playback. A naive
        # overwrite blanks valid fields on every refresh:
        #   - video_id None would drop the current video
        #   - state Stopped would flicker the UI to "no longer playing"
        # So we only adopt video_id when the event actually carries one,
        # and we adopt state from now_playing only when it isn't the
        # placeholder "Stopped". Real Stopped transitions arrive via
        # playback_state events instead, where state is required.
        old_video = self._observation.video_id
        old_state = self._observation.state
        new_video = getattr(event, "video_id", None)
        if new_video is not None:
            self._observation.video_id = new_video
        ct = getattr(event, "current_time", None)
        if ct is not None:
            self._observation.current_time = ct
        dur = getattr(event, "duration", None)
        if dur is not None:
            self._observation.duration = dur
        new_state = _state_to_str(getattr(event, "state", None))
        if new_state is not None and new_state != "Stopped":
            self._observation.state = new_state
            # SmartTube reports plenty of recoveries through nowPlaying
            # rather than onStateChange. Only _on_playback_state used to
            # cancel the pending FINISHED timer, so a recovery arriving on
            # this path left it armed — see the re-arm site for what that
            # then did to the next Stopped.
            self._cancel_stopped_timer()
        await self._safe_emit(EVENT_POSITION, self._observation)
        self._maybe_schedule_gone_player_timer()
        if new_video and new_video != old_video:
            await self._safe_emit(EVENT_NOW_PLAYING, self._observation)
        # Also emit EVENT_STATE when a nowPlaying event carries a state
        # transition — SmartTube doesn't always pair onStateChange with
        # nowPlaying after BACK out of the player view, but the next
        # nowPlaying (often from our periodic get_now_playing refresh)
        # does carry the updated state. Without this, the queue
        # controller's _sync_paused_from_lounge mirror never fires and
        # state.pause_source stays None even though Lounge sees Paused.
        if (new_state is not None and new_state != "Stopped"
                and new_state != old_state):
            await self._safe_emit(EVENT_STATE, self._observation)

    async def _on_playback_state(self, event) -> None:
        old_state = self._observation.state
        new_state = _state_to_str(getattr(event, "state", None))
        ct = getattr(event, "current_time", None)
        if ct is not None:
            self._observation.current_time = ct
        dur = getattr(event, "duration", None)
        if dur is not None:
            self._observation.duration = dur
        self._observation.state = new_state
        await self._safe_emit(EVENT_POSITION, self._observation)
        self._maybe_schedule_gone_player_timer()

        # State changed away from Stopped — cancel any pending
        # "persistent Stopped" timer (this was an ad-insertion blip or
        # similar brief transition, not a real end).
        if new_state != "Stopped" and self._observation.current_time is not None:
            self._cancel_stopped_timer()

        if new_state != old_state:
            await self._safe_emit(EVENT_STATE, self._observation)
            # End-of-video: was playing/paused/buffering, now stopped — but
            # ONLY if we're actually near the end of the video. SmartTube
            # also reports brief "Stopped" states during transitions (app
            # foregrounding, deep-link handling, ad insertion, network
            # blips), and those would wrongly look like a video ending and
            # cause our queue to advance / clear `current` mid-playback.
            if (old_state in ("Playing", "Paused", "Buffering")
                    and new_state in ("Stopped",)):
                ct_now = self._observation.current_time
                dur_now = self._observation.duration
                near_end = (
                    ct_now is not None
                    and dur_now is not None
                    and dur_now > 0
                    and (dur_now - ct_now) <= 5.0
                )
                if near_end:
                    # Near-end Stopped → fire FINISHED immediately,
                    # don't wait for the persisted-delay timer.
                    await self._safe_emit(EVENT_FINISHED, self._observation)
                else:
                    # Mid-playback Stopped — could be a brief
                    # transition (ad insertion, app foregrounding,
                    # network blip) OR a real end (user backed out of
                    # the player to SmartTube's home screen). Schedule
                    # a delayed FINISHED: if state stays Stopped for
                    # STOPPED_PERSISTED_DELAY, fire FINISHED. If state
                    # changes back to Playing/etc within that window,
                    # the timer gets cancelled above.
                    log.info(
                        "Stopped state at %.1fs / %s — scheduling FINISHED "
                        "after %.0fs if state persists",
                        ct_now if ct_now is not None else -1,
                        dur_now, STOPPED_PERSISTED_DELAY,
                    )
                    # Cancel before re-arming. Without this a timer left
                    # pending by a recovery we saw through nowPlaying would
                    # simply be overwritten here — still scheduled, still
                    # holding its ORIGINAL deadline — and would then fire on
                    # this new Stopped after whatever was left of its window.
                    # A 5s ad-break debounce became ~1s and the ad break
                    # skipped the video.
                    self._cancel_stopped_timer()
                    self._stopped_persisted_task = asyncio.create_task(
                        self._fire_finished_if_stopped_persists()
                    )
        self._maybe_schedule_gone_player_timer()

    def _maybe_schedule_gone_player_timer(self) -> None:
        """A player can go away WITHOUT ever passing through Stopped.

        What it leaves behind is a frame that still carries the video and a
        state, but no position at all. Measured on hardware 2026-08-31: play a
        635s video with another queued, fast-forward into its last seconds
        with the TV's own remote, and the queue stalls for minutes. Nothing
        could advance it — the duration timer is still far out because the
        skip removed that much runtime, and every other finish check reads the
        position that just vanished.

        Neither state handler nulls the position itself; it goes blank when a
        teardown blanks the observation and the push that follows carries an
        id and a state but no time. So this is checked wherever a frame lands,
        not inside one handler's transition logic.

        Same evidence as a persistent Stopped — a player that is no longer
        there — so it gets the same treatment and the same debounce. A
        position that comes back cancels it, because Lounge drops one across
        reconnects routinely and a momentary gap must not end anybody's video.
        """
        # `.done()`, not `is not None`: the fire body never clears the attr,
        # so a completed timer otherwise blocks every LATER gone-player
        # detection until some position-carrying frame happens to run the
        # cancel path — the second video to vanish the same way stalled.
        task = self._stopped_persisted_task
        if task is not None and not task.done():
            return
        if self._observation.current_time is not None:
            return
        if not self._observation.video_id or self._observation.state is None:
            return
        if self._observation.state == "Playing":
            # A PLAYING state is positive evidence of a live player, not a
            # gone one — and blank positions are routine while alive
            # (measured: 18s of no position early in a deep-link launch,
            # against this timer's 5s debounce). The hardware evidence this
            # detector was built on was a PAUSED frame with no position.
            return
        log.info(
            "Playback state %s arrived with no position — scheduling FINISHED "
            "after %.0fs if it stays that way",
            self._observation.state, STOPPED_PERSISTED_DELAY,
        )
        self._stopped_persisted_task = asyncio.create_task(
            self._fire_finished_if_stopped_persists()
        )

    def _cancel_stopped_timer(self) -> None:
        """Cancel and forget any pending persistent-Stopped FINISHED timer."""
        task = self._stopped_persisted_task
        self._stopped_persisted_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _fire_finished_if_stopped_persists(self) -> None:
        """Fire FINISHED if state stays Stopped for STOPPED_PERSISTED_DELAY.

        Cancelled by _on_playback_state when state transitions away
        from Stopped — which is the typical ad-insertion behavior.
        Survives to fire when the user backs out of the player and
        SmartTube doesn't resume Playing.
        """
        try:
            await asyncio.sleep(STOPPED_PERSISTED_DELAY)
        except asyncio.CancelledError:
            return
        # Either shape of "the player is gone": still Stopped, or still
        # without a position. Anything that came back cancelled this.
        if self._observation.state == "Stopped":
            reason = "Stopped state persisted"
        elif (self._observation.current_time is None
                and self._observation.state != "Playing"):
            # Re-checked at fire time too: a state that turned Playing during
            # the debounce is the player answering, even if its position has
            # not arrived yet.
            reason = "no position returned"
        else:
            return
        log.info(
            "%s for %.0fs — firing FINISHED (player likely exited)",
            reason, STOPPED_PERSISTED_DELAY,
        )
        await self._safe_emit(EVENT_FINISHED, self._observation)

    async def _on_disconnected_event(self, event) -> None:
        log.info("Lounge disconnected event: %s", getattr(event, "reason", None))
        await self._teardown()

    async def _safe_emit(self, event_type: str, obs: LoungeObservation) -> None:
        try:
            await self._on_event(event_type, obs)
        except Exception:
            log.exception("Lounge event handler %s raised", event_type)


def _state_to_str(state) -> Optional[str]:
    """Convert pyytlounge's State enum value to a string we can compare."""
    if state is None:
        return None
    if isinstance(state, State):
        return state.name
    if hasattr(state, "name"):
        return state.name
    return str(state)


async def _noop_event(_event: str, _obs: LoungeObservation) -> None:
    return None
