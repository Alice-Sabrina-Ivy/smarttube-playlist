"""Queue state machine for SmartTube Playlist.

Module name: `playlist.py`, deliberately not `queue.py` — a top-level
`queue.py` would shadow Python's stdlib `queue` module, breaking anything
that lazy-imports `from queue import Empty` (e.g. concurrent.futures).

Owns:
- the canonical QueueState (current item, queue, paused flag)
- the asyncio.Lock guarding all mutations
- the auto-advance timer (best-effort, duration-based fallback; the
  Lounge `finished` event is the primary advance signal when connected)
- the kill-switch wiring from the TV's current_app callback

Does NOT own:
- the TV connection. A `play_callable(video_id)` is injected, so this module
  knows nothing about androidtvremote2 and is fully testable with a stub.
- broadcasting transport. A `Broadcaster` is injected; this module just calls
  `broadcaster.publish(event_type, snapshot)` after every mutation.

Threading model:
- All public methods are async and acquire the single instance lock.
- TV I/O (`play_callable`) is fired off the lock as a background task — the
  TV send can take seconds (wake + foreground + deep-link) and we don't want
  to block other queue ops behind it.
- The auto-advance timer is an asyncio.Task. A monotonically-increasing
  `_timer_gen` counter is the source of truth: any timer body that finishes
  its sleep with a stale gen exits without doing anything. This way cancel
  + race conditions don't double-fire.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional, Protocol

log = logging.getLogger("smarttube-playlist.queue")


# ── data ─────────────────────────────────────────────────────────────────────


@dataclass
class QueueItem:
    id: str
    video_id: str
    title: str
    channel: str
    duration_s: Optional[int]   # None => livestream, no auto-advance
    is_live: bool
    thumbnail_url: str
    added_at: datetime
    start_s: Optional[int] = None   # offset for &t= deep-link, in seconds

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "video_id": self.video_id,
            "title": self.title,
            "channel": self.channel,
            "duration_s": self.duration_s,
            "is_live": self.is_live,
            "thumbnail_url": self.thumbnail_url,
            "added_at": self.added_at.isoformat(),
            "start_s": self.start_s,
        }


def make_item(
    *,
    video_id: str,
    title: str,
    channel: str,
    duration_s: Optional[int],
    is_live: bool,
    thumbnail_url: str,
    now: Optional[datetime] = None,
    start_s: Optional[int] = None,
) -> QueueItem:
    return QueueItem(
        id=str(uuid.uuid4()),
        video_id=video_id,
        title=title,
        channel=channel,
        duration_s=duration_s,
        is_live=is_live,
        thumbnail_url=thumbnail_url,
        added_at=now or datetime.now(timezone.utc),
        start_s=start_s,
    )


def _blank_lounge() -> dict:
    """Default/cleared Lounge observation. Shape mirrors
    LoungeObservation.to_dict(); kept as a dict here so playlist.py doesn't
    depend on lounge.py and stays trivially testable."""
    return {
        "available": False,
        "video_id": None,
        "current_time": None,
        "duration": None,
        "state": None,
        # Populated by app.py via background metadata scrape — Lounge itself
        # only exposes the video_id, not display strings.
        "title": None,
        "channel": None,
        "thumbnail_url": None,
    }


# Time to wait after a SmartTube → not-SmartTube transition before
# firing the kill-switch. The androidtvremote2 library's current_app
# callback can fire spurious transient reports of "launcher" while
# SmartTube is actually still playing — observed at ~1s during normal
# playback. Debouncing filters those out: only fire kill-switch if
# foreground is STILL not SmartTube after this delay. Real user
# navigation away (BACK to launcher, switching apps) persists past
# the delay; flickers don't.
KILL_SWITCH_DEBOUNCE = 3.0

# Grace period after an item starts playing, during which a SmartTube ->
# not-SmartTube transition is attributed to our own handoff rather than to the
# user. Every advance between queue items goes SmartTube -> launcher ->
# SmartTube: the old player tears down, Android shows the launcher for a beat,
# and the new Intent brings it back. Without this the kill-switch fires in
# that gap and discards the item we JUST started, stranding everything behind
# it — measured on hardware, the second of three queued videos cleared about
# three seconds in. The 3s debounce alone does not cover it, because the
# launcher can still be foreground when the debounce expires.
KILL_SWITCH_START_GRACE = 12.0

# How many times the kill-switch may defer its decision before giving up.
# Every suppression re-arms rather than returning (see
# `_kill_switch_after_debounce`), so this is what stops a launch that never
# completes from spinning a re-check forever.
KILL_SWITCH_RECHECK_LIMIT = 5

# Time to wait after Lounge disconnects before assuming the player
# closed (vs a brief network blip / reconnect cycle). Empirically the
# subscribe loop reconnects within 1-2s for transient blips. SmartTube
# closing its player triggers a subscribe end too, but the reconnect
# returns a blank observation (no video_id) — that's the signal we use
# to distinguish "user backed out of player" from "transient blip"
# without needing an activity-level foreground signal.
PLAYER_CLOSE_VERIFY_DELAY = 8.0

# Time to wait after Lounge reports a different video_id than ours
# before assuming the user externally switched to a video we didn't
# queue (via the physical TV remote / SmartTube UI). Needs to be long
# enough to absorb the race window of our own auto-advance (we set
# state.current = next, then setPlaylist; Lounge can briefly still
# report the OLD video before our setPlaylist takes effect — that's
# what we DON'T want to mis-detect as an external switch). 5s is
# comfortable above typical setPlaylist propagation (1-2s).
EXTERNAL_SWITCH_DEBOUNCE = 5.0

# How close to `duration` the Lounge position must be for us to treat the
# current item as having played to its end. Same 5s window lounge.py uses to
# decide a Stopped transition is a real finish, and deliberately the same
# number: the two must not disagree about what "ended" means.
NEAR_END_SECONDS = 5.0

# How long after advancing off an item a `lounge.finished` for that same
# video_id is read as belonging to the item we LEFT rather than the one now
# playing. SmartTube emits a position update and a finish for one near-end
# transition, and since v1.02 the advance lands on the first of those — so the
# finish arrives moments later describing playback we have already retired.
# The video_id guard cannot separate them when two consecutive queue entries
# share a clip, which is ordinary.
#
# Kept SHORT deliberately. Both events come from the same `_on_playback_state`
# call, so a second is generous; and the cost of it being too long is bounded
# — a genuinely repeated clip that ends inside the window has its finish
# ignored and is advanced a moment later by its own duration timer instead,
# rather than stranding.
FINISH_CONSUMED_WINDOW = 3.0

# How far ahead of the current item's own elapsed time a Lounge position may
# be before we treat it as belonging to the PREVIOUS item. Absorbs launch
# latency and clock skew; small enough that a frame from the video we just
# left (typically parked at its end) is caught.
OBSERVATION_STALENESS_SLACK = 5.0

# How long after an item becomes current we still doubt a position that runs
# ahead of the wall clock. After this, we believe it.
#
# The staleness test exists for one measured bug: the second of two identical
# shorts was advanced off its predecessor's frame 3.7s in, because SmartTube
# keeps pushing about the previous playback for a few seconds and a shared
# video_id defeats the id guard. That is a SHORT-LIVED hazard.
#
# Treating it as permanent is what broke. A playhead legitimately runs ahead of
# wall-clock elapsed for the rest of a video whenever something moves it that
# we never see — and on this project's own reference setup that is routine, not
# exotic: SponsorBlock cuts segments out on the device, so a video with a
# minute of sponsor in it reaches its end a minute early. Every truthful frame
# was then called stale, `_lounge_says_finished()` answered False for the whole
# item, and since the video also finishes before its duration timer comes due,
# the kill-switch found no evidence of a finish and cleared `current` with the
# queue still full — the exact stranding this release exists to close.
OBSERVATION_TRUST_AFTER = 10.0

# How close to `duration` a position must be to count as "parked on the last
# frame" rather than "paused near the end".
#
# Much tighter than NEAR_END_SECONDS, and measured rather than chosen: a video
# that plays out lands within a second of its duration on this hardware —
# 18.566 of 19.0, and 634.42 of 635.0 in the skip run. Someone pausing lands
# wherever they pressed the button. That separation is what lets the queue hand
# off from a video that ended EARLY in wall-clock terms (a SponsorBlock skip
# means the duration timer has not come due) without skipping a pause made two
# or three seconds from the end.
#
# Wrong in the tight direction just means waiting for the duration timer, which
# is what happened before this existed. Wrong in the loose direction skips
# someone's video. Keep it tight.
END_PARK_SLACK = 1.5

# How long an item may be current without Lounge ever reporting it playing
# before we conclude the launch failed and move on.
#
# The failure this exists for costs a whole session. A link to a video that
# cannot play — deleted, private, region-blocked, or mistyped — fails its
# metadata scrape, so `_fallback()` gives it the 600s default duration. The
# deep link goes out, SmartTube loads nothing and parks at ct 0, and every
# path that could move the queue on is shut at once: the position is nowhere
# near the end, the duration timer is ten minutes away, and the kill-switch
# never fires because SmartTube sits in the foreground on its own error
# screen. Measured on hardware: the queue held for the full ten minutes, and
# Play did not help — only Skip did, which a guest has no way to guess.
#
# Generous on purpose. A cold start from a sleeping device measures 29.4s from
# the add to the first frame, and this is timed from the launch COMPLETING,
# by which point a healthy video starts within seconds. Being wrong here skips
# a video that would have played, so the window is set well past anything
# measured rather than close to it.
LAUNCH_CONFIRM_TIMEOUT = 45.0


@dataclass
class QueueState:
    current: Optional[QueueItem] = None
    current_started_at: Optional[datetime] = None
    queue: list[QueueItem] = field(default_factory=list)
    paused: bool = False
    # Who set `paused`. "ui" = our /api/pause endpoint (user clicked
    # Pause in the web UI). "lounge" = mirrored from Lounge's Paused
    # state by `_sync_paused_from_lounge` (user paused via TV remote
    # OR backed out of the player view — Lounge protocol can't tell
    # them apart). None when paused is False. The frontend's "fade
    # the now-playing card on prolonged frozen-Paused" heuristic
    # uses this to skip fading when we know the user explicitly
    # paused via our UI.
    pause_source: Optional[str] = None
    # TV power state mirrored from androidtvremote2's is_on callback.
    # None = unknown (e.g. before first connect).
    tv_on: Optional[bool] = None
    # True while a cold-boot wake-from-off sequence is in progress
    # (POWER sent, waiting for the TV to finish booting). The UI
    # uses this to render the "WAKING TV" indicator independent of
    # tv_on — Quick Resume / instant-on TVs flip is_on to True very
    # fast even while still booting, so tv_on alone undercounts the
    # actual wake window.
    waking: bool = False
    lounge: dict = field(default_factory=_blank_lounge)

    def snapshot(self) -> dict:
        return {
            "current": self.current.to_dict() if self.current else None,
            "current_started_at": (
                self.current_started_at.isoformat() if self.current_started_at else None
            ),
            "queue": [i.to_dict() for i in self.queue],
            "paused": self.paused,
            "pause_source": self.pause_source,
            "tv_on": self.tv_on,
            "waking": self.waking,
            "lounge": dict(self.lounge),
        }


class Broadcaster(Protocol):
    async def publish(self, event_type: str, snapshot: dict) -> None: ...


# ── controller ───────────────────────────────────────────────────────────────


class QueueController:
    def __init__(
        self,
        *,
        play_callable: Callable[[str, Optional[int]], Awaitable[None]],
        broadcaster: Broadcaster,
        smarttube_package: str,
        get_current_app: Callable[[], Optional[str]] = lambda: None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        pause_callable: Optional[Callable[[], Awaitable[bool]]] = None,
        play_button_callable: Optional[Callable[[], Awaitable[bool]]] = None,
    ):
        self.state = QueueState()
        self._lock = asyncio.Lock()
        self._timer_task: Optional[asyncio.Task] = None
        self._timer_gen = 0
        self._play = play_callable
        self._broadcaster = broadcaster
        self._smarttube_package = smarttube_package
        self._get_current_app = get_current_app
        self._sleeper = sleeper
        self._clock = clock
        # Pause / Play callables — typically wired to LoungeMonitor.pause/play
        # but kept generic so tests can stub them.
        self._cast_pause = pause_callable
        self._cast_play = play_button_callable
        # Track in-flight TV-send tasks so callers (e.g. shutdown) can await them.
        self._send_tasks: set[asyncio.Task] = set()
        # Serializes /api/resume. Distinct from _lock because the resume path
        # awaits network work (_cast_play) that must not run concurrently but
        # equally must not be held across the state lock.
        self._resume_lock = asyncio.Lock()
        # True once the current item's duration timer has come due at least
        # once. See _timer_body for why this, and not the Lounge position,
        # is what the kill-switch must trust about "did it finish".
        self._duration_elapsed = False
        # Where the PLAYHEAD started, which is not the same question as when
        # the item became current — and `current_started_at` is the answer to
        # the second one. It is read by the kill-switch's start grace ("did we
        # just hand off?"), by the frontend's STARTING badge and by its
        # wall-clock progress estimate, so moving it to follow a seek broke all
        # three: a rewind to 0:04 re-armed the 12s grace on a video that had
        # been playing for three quarters of an hour, and made the card claim
        # "STARTING…" straight after an action that proves playback is live.
        # Only `_observation_predates_current` wants the playhead origin, so it
        # gets its own field. None means "same as current_started_at".
        self._playhead_origin_at: Optional[datetime] = None
        # The video we last advanced away from, and when. Lets
        # `_on_lounge_finished` tell a finish about the item we just retired
        # from a finish about the one now playing, which the video_id guard
        # cannot do when two consecutive queue entries share a clip.
        self._finish_consumed_vid: Optional[str] = None
        self._finish_consumed_at: Optional[datetime] = None
        # Has Lounge ever reported the CURRENT item actually playing? A launch
        # that silently produced nothing looks identical to one still in
        # progress until this flips. See LAUNCH_CONFIRM_TIMEOUT.
        self._playback_confirmed = False
        self._launch_check_task: Optional[asyncio.Task] = None
        self._launch_gen = 0
        # External-switch debounce: when Lounge reports a video_id that
        # doesn't match state.current.video_id, we wait this many
        # seconds before assuming the user switched videos via the
        # physical remote (vs our own auto-advance race window).
        self._external_switch_task: Optional[asyncio.Task] = None

    # ── public API ───────────────────────────────────────────────────────────

    async def add(self, item: QueueItem) -> None:
        async with self._lock:
            # When our queue is idle (no current, not paused), an add
            # ALWAYS takes ownership — that's the user's intent when
            # they click "add" in our UI. Lounge state is deliberately
            # ignored here: it can report a stale "Playing" cache from
            # a prior session (or a sibling Lounge device) that would
            # otherwise block adds indefinitely, with the new item
            # parked in the queue waiting for a Lounge.finished that
            # may never come. tv_play handles the genuinely-already-
            # playing-same-video case via skip-redundant, so taking
            # ownership here is safe even when SmartTube IS currently
            # playing our video — the launch sequence just no-ops.
            should_start = (
                self.state.current is None
                and not self.state.paused
            )
            # Externally-paused replace: SmartTube is paused because
            # the user either backed out of the player (frontend has
            # faded the now-playing card) or paused via TV remote and
            # walked away (also faded). Either way our UI shows no
            # active playback. An add in this state means "play this
            # instead of whatever's parked" — supersede the hidden
            # current rather than queue behind it. We DON'T trigger
            # this when pause_source=="ui" because the user explicitly
            # paused via our UI; the card is still visible and queuing
            # is what they expect.
            replace_current = (
                not should_start
                and self.state.current is not None
                and self.state.paused
                and self.state.pause_source == "lounge"
            )
            if should_start or replace_current:
                self._begin_locked(item)
                if replace_current:
                    self.state.paused = False
                    self.state.pause_source = None
                event = "item_started"
            else:
                self.state.queue.append(item)
                event = "item_added"
            snapshot = self.state.snapshot()
            started_item = item if (should_start or replace_current) else None
        await self._broadcaster.publish(event, snapshot)
        if started_item:
            self._send_to_tv(started_item)

    async def remove(self, item_id: str) -> bool:
        """Remove a *queued* item. Does not affect `current`. Returns True if removed."""
        async with self._lock:
            before = len(self.state.queue)
            self.state.queue = [i for i in self.state.queue if i.id != item_id]
            removed = len(self.state.queue) != before
            snapshot = self.state.snapshot()
        if removed:
            await self._broadcaster.publish("item_removed", snapshot)
        return removed

    async def move(self, item_id: str, direction: str) -> bool:
        """Move a queued item one slot up (toward head) or down (toward tail).
        Does not affect `current` — only reorders queued items. Returns True
        if the move actually changed positions (False at the boundary or if
        the item isn't queued).

        Open to any client by design (no per-user ownership of items) — the
        product is shared-room, but the queue itself is collaborative. The
        original 'strict FIFO by added_at' constraint was relaxed once we
        added an explicit reorder UI.
        """
        if direction not in ("up", "down"):
            return False
        async with self._lock:
            idx = next(
                (i for i, item in enumerate(self.state.queue) if item.id == item_id),
                -1,
            )
            if idx < 0:
                return False
            new_idx = idx - 1 if direction == "up" else idx + 1
            if new_idx < 0 or new_idx >= len(self.state.queue):
                return False  # already at boundary
            self.state.queue[idx], self.state.queue[new_idx] = (
                self.state.queue[new_idx], self.state.queue[idx],
            )
            snapshot = self.state.snapshot()
        await self._broadcaster.publish("item_moved", snapshot)
        return True

    async def skip(self) -> None:
        """Advance to the next item, or idle if queue empty. Works even when paused."""
        await self._cancel_timer()
        await self._advance(reason="skip")

    async def pause(self) -> None:
        """Pause the playlist (and the TV if Cast is available). Idempotent."""
        # Send the Cast pause first, off the lock — it's network I/O.
        if self._cast_pause is not None:
            await self._cast_pause()
        async with self._lock:
            if self.state.paused:
                return
            self.state.paused = True
            self.state.pause_source = "ui"
            snapshot = self.state.snapshot()
        await self._broadcaster.publish("paused_toggled", snapshot)

    async def resume(self) -> None:
        """Resume the playlist (and the TV if Cast is available). Idempotent.

        If the queue stalled with no current playing (e.g. paused-timer-fire
        cleared it) and there are items queued, start the next one."""
        # Serialize the whole thing. `_cast_play` used to run BEFORE the lock,
        # so the idempotence check below could not short-circuit a second
        # concurrent call — and `_cast_play` can spend RESUME_VERIFY_TIMEOUT
        # and then a further tv_play verify before deep-linking, a ~6s window
        # in which the UI leaves the Play button enabled. Two impatient
        # clicks meant two play signals for one video (invariant 1).
        async with self._resume_lock:
            async with self._lock:
                if not self.state.paused and self.state.current is not None:
                    return
            if self._cast_play is not None:
                await self._cast_play()
            await self._resume_locked()

    async def _resume_locked(self) -> None:
        async with self._lock:
            if not self.state.paused and self.state.current is not None:
                return
            self.state.paused = False
            self.state.pause_source = None
            should_start = self.state.current is None and bool(self.state.queue)
            if should_start:
                next_item = self.state.queue.pop(0)
                self._begin_locked(next_item)
            else:
                next_item = None
            snapshot = self.state.snapshot()
        await self._broadcaster.publish("paused_toggled", snapshot)
        if next_item:
            await self._broadcaster.publish("item_started", self.state.snapshot())
            self._send_to_tv(next_item)

    async def clear(self) -> None:
        """Empty the queue. The currently-playing item keeps playing."""
        async with self._lock:
            if not self.state.queue:
                return
            self.state.queue.clear()
            snapshot = self.state.snapshot()
        await self._broadcaster.publish("queue_cleared", snapshot)

    async def replace_with(self, item: QueueItem) -> None:
        """One-shot: wipe queue, replace current, start the new item.

        Used by the legacy /api/play endpoint to preserve v0 semantics
        ('replaces whatever's on the TV') in one atomic operation. Honors
        the paused flag — if paused, the queue is wiped and `item` becomes
        the new current without auto-starting; unpause kicks it off.
        """
        await self._cancel_timer()
        async with self._lock:
            self.state.queue.clear()
            if self.state.paused:
                # Hold the new item at the head; unpause will start it.
                self.state.current = None
                self.state.current_started_at = None
                self.state.queue.append(item)
                started = None
            else:
                self._begin_locked(item)
                started = item
            snapshot = self.state.snapshot()
        event = "item_started" if started else "item_added"
        await self._broadcaster.publish(event, snapshot)
        if started:
            self._send_to_tv(started)

    def snapshot(self) -> dict:
        """Synchronous snapshot for read-only endpoints. Safe under GIL for this shape."""
        return self.state.snapshot()

    async def update_tv_on(self, is_on: bool) -> bool:
        """Set TV power state and broadcast a fresh snapshot. No-op if unchanged.

        Returns True only when the value actually CHANGED. Callers need that:
        the TV-off handler wipes the queue, and the library re-emits the
        current power state on every reconnect — so acting on the value
        rather than the transition throws away a video the user queued while
        the TV was off, which is exactly when they are most likely to queue
        one.
        """
        async with self._lock:
            if self.state.tv_on == is_on:
                return False
            self.state.tv_on = is_on
            snap = self.state.snapshot()
        await self._broadcaster.publish("tv_power", snap)
        return True

    async def set_waking(self, waking: bool) -> None:
        """Toggle the cold-boot 'waking TV' flag. Drives the UI's
        WAKING badge independently of tv_on, since Quick Resume TVs
        flip tv_on to True before the TV is actually ready to play."""
        async with self._lock:
            if self.state.waking == waking:
                return
            self.state.waking = waking
            snap = self.state.snapshot()
        await self._broadcaster.publish("waking", snap)

    async def tv_off_reset(self) -> None:
        """Wipe queue state because the TV powered off. Clears current,
        clears queue, clears paused, cancels any pending advance timer.
        Idempotent — broadcasts only if there was something to clear."""
        await self._cancel_timer()
        async with self._lock:
            had_state = (
                self.state.current is not None
                or bool(self.state.queue)
                or self.state.paused
            )
            self.state.current = None
            self.state.current_started_at = None
            self.state.queue.clear()
            self.state.paused = False
            self.state.pause_source = None
            # TV is off; whatever Lounge last reported is stale by definition.
            self.state.lounge = _blank_lounge()
            snap = self.state.snapshot()
        if had_state:
            await self._broadcaster.publish("queue_cleared", snap)

    async def shutdown(self) -> None:
        """Cancel the auto-advance timer and any in-flight TV-send tasks.
        Safe to call multiple times. Use from FastAPI lifespan teardown and
        from test cleanup."""
        await self._cancel_timer()
        # The launch-confirmation check has its own clock, so _cancel_timer
        # does not touch it and it would outlive the controller.
        self._launch_gen += 1
        if self._launch_check_task is not None and not self._launch_check_task.done():
            self._launch_check_task.cancel()
        self._launch_check_task = None
        for t in list(self._send_tasks):
            if not t.done():
                t.cancel()
        if self._send_tasks:
            with contextlib.suppress(BaseException):
                await asyncio.gather(*self._send_tasks, return_exceptions=True)

    # ── kill-switch entry (called from TV-library callback) ──────────────────

    def on_current_app_changed(self, prev: Optional[str], new: Optional[str]) -> None:
        """Library callback adapter. Sync entry point — schedules async handler."""
        if prev == self._smarttube_package and new != self._smarttube_package:
            # Suppress the kill-switch while we're orchestrating a cold-boot
            # wake. On Quick Resume / instant-on TVs the foreground app
            # often flickers through SmartTube → launcher → SmartTube (and
            # similar) during the wake sequence; the SmartTube → launcher
            # transient wrongly tripped the kill-switch, wiping the user's
            # just-added current item, cancelling the in-flight tv_play
            # mid-WAKE_DELAY, and clearing state.waking before the UI ever
            # rendered the WAKING badge. state.waking is the explicit
            # "we're mid-wake, transitions are expected" signal.
            if self.state.waking:
                log.info(
                    "Kill-switch suppressed (waking=True): SmartTube -> %s "
                    "during cold-boot wake sequence",
                    new,
                )
                return
            # Debounce: SmartTube's foreground reporting can flicker even
            # mid-playback (briefly transition to launcher then back to
            # SmartTube within ~1-2s). Observed empirically: at 23:42:55
            # during a smooth playback session, current_app callback
            # fired SmartTube → launcher; 3 seconds later the same
            # current_app read returned SmartTube. Without debouncing
            # we'd kill-switch on every flicker. Schedule the kill-switch
            # for KILL_SWITCH_DEBOUNCE seconds later; if foreground is
            # back on SmartTube by then, cancel.
            log.info(
                "Kill-switch deferred (%.1fs debounce): SmartTube -> %s",
                KILL_SWITCH_DEBOUNCE, new,
            )
            asyncio.create_task(self._kill_switch_after_debounce())

    async def _recheck_kill_switch(self, wait: float, attempt: int) -> None:
        """Sleep out a suppression window, then re-run the debounced check.

        The sleep is deliberately separate from KILL_SWITCH_DEBOUNCE's own so
        the grace case waits exactly the remainder of the grace rather than a
        fixed interval.
        """
        try:
            if wait > 0:
                await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        await self._kill_switch_after_debounce(attempt)

    async def _kill_switch_after_debounce(self, attempt: int = 0) -> None:
        """Wait KILL_SWITCH_DEBOUNCE seconds and re-check the foreground.
        Fire the kill-switch only if SmartTube is genuinely no longer
        foreground. Library callback flickers (mid-playback transient
        reports of launcher) get filtered out.

        Every "not yet" below RE-ARMS rather than returning. `on_current_app_changed`
        only schedules this on a SmartTube -> other TRANSITION, so once the
        viewer has settled on the launcher no further callback arrives — and a
        bare return therefore did not defer the decision, it abandoned it.
        Leaving SmartTube inside the start grace left `current` set for the
        rest of the video: a ticking card for something nobody is watching,
        and every later add parking silently because `add()` needs
        `current is None` to start anything. Bounded by
        KILL_SWITCH_RECHECK_LIMIT so a permanently-in-flight send cannot spin.
        """
        try:
            await asyncio.sleep(KILL_SWITCH_DEBOUNCE)
        except asyncio.CancelledError:
            return
        current = self._safe_current_app()
        if current == self._smarttube_package:
            log.info(
                "Kill-switch debounce: SmartTube is foreground again "
                "(was a flicker), not firing"
            )
            return

        def _recheck(reason: str, wait: float) -> None:
            if attempt >= KILL_SWITCH_RECHECK_LIMIT:
                log.info(
                    "Kill-switch debounce: %s, and %d re-checks have not "
                    "settled it; leaving the item alone",
                    reason, attempt,
                )
                return
            log.info("Kill-switch debounce: %s; re-checking in %.1fs",
                     reason, wait)
            asyncio.create_task(self._recheck_kill_switch(wait, attempt + 1))

        started = self.state.current_started_at
        if started is not None:
            try:
                since_start = (self._clock() - started).total_seconds()
            except Exception:
                since_start = None
            if since_start is not None and 0 <= since_start < KILL_SWITCH_START_GRACE:
                _recheck(
                    f"current item started {since_start:.1f}s ago, so the "
                    f"foreground gap may be our own handoff",
                    max(0.0, KILL_SWITCH_START_GRACE - since_start),
                )
                return
        if self.has_pending_sends():
            # OUR OWN launch is in flight, and it is what is moving the
            # foreground around: SmartTube tears down its player, Android
            # shows the launcher for a beat, then the Intent brings it back.
            # Reading that churn as the user leaving is how a freshly started
            # item got discarded seconds in.
            _recheck("our own launch is still in flight", KILL_SWITCH_DEBOUNCE)
            return
        if self.state.waking:
            # A launch is in flight. The debounce only re-read current_app,
            # so a video added within the window — while the TV is waking and
            # the foreground legitimately reads as the launcher — was wiped by
            # the transition that PRECEDED it. Everything queued behind it
            # then sat there, since the kill-switch does not advance.
            _recheck("a launch is in flight (waking)", KILL_SWITCH_DEBOUNCE)
            return
        if not current:
            # Unreadable foreground is not evidence of anything. Same "" trap
            # as the timer's check: on a device that never negotiates IME this
            # would fire the kill-switch on every transition callback.
            log.info(
                "Kill-switch debounce: foreground unreadable; not firing"
            )
            return

        log.info(
            "Kill-switch: SmartTube confirmed not-foreground after debounce "
            "(current=%s)", current,
        )
        await self._kill_switch()

    # ── lounge event entry (called by LoungeMonitor) ─────────────────────────

    async def on_lounge_event(self, event_type: str, observation: dict) -> None:
        """Process an event from the Lounge monitor.

        Always updates self.state.lounge and broadcasts the snapshot. Specific
        events also drive state-machine transitions (advance on FINISHED,
        mirror pause-state from Lounge play_state changes)."""
        async with self._lock:
            self.state.lounge = dict(observation)
            # Once we have seen our own item playing, it is not a failed
            # launch however it behaves afterwards.
            cur = self.state.current
            if (cur is not None and observation.get("available")
                    and observation.get("video_id") == cur.video_id):
                # Either signal will do, and the second matters: a viewer who
                # pauses in the first few seconds may never produce a Playing
                # event we see, and a position past zero already proves the
                # video ran.
                if (observation.get("state") == "Playing"
                        or (observation.get("current_time") or 0) > 0):
                    self._playback_confirmed = True
            snapshot = self.state.snapshot()

        # Routine position updates — emit a lightweight snapshot, no state-machine action.
        if event_type == "lounge.position":
            await self._broadcaster.publish("lounge_update", snapshot)
            await self._maybe_advance_at_end_of_video(observation)
            await self._maybe_skip_a_launch_that_never_started(observation)
            return

        await self._broadcaster.publish("lounge_update", snapshot)

        if event_type == "lounge.finished":
            await self._on_lounge_finished()
        elif event_type == "lounge.state":
            await self._sync_paused_from_lounge(observation)
            await self._maybe_advance_at_end_of_video(observation)
            await self._maybe_skip_a_launch_that_never_started(observation)
        elif event_type == "lounge.disconnected":
            await self._on_lounge_disconnected()
        elif event_type == "lounge.now_playing":
            await self._on_lounge_now_playing(observation)
        # We don't kill-switch on now_playing video changes — that
        # would race with our own queue advances (Lounge briefly
        # reports the old video's final state after we've already
        # moved on). The now_playing handler instead schedules a
        # debounced check to distinguish "user externally switched
        # videos" from the propagation window of our own setPlaylist.

    async def _on_lounge_disconnected(self) -> None:
        """Lounge subscribe ended. Could be a transient network blip
        (reconnects within 1-2s, observation comes back) OR the user
        backed out of SmartTube's player (SmartTube tears down the
        media session; reconnect happens but server reports no video).

        Distinguishing them without an activity-level foreground
        signal: schedule a delayed check. After PLAYER_CLOSE_VERIFY_DELAY
        seconds, if Lounge has reconnected with the SAME video_id as
        our current, it was a blip and we do nothing. If it has
        reconnected with a different (or null) video_id, the player
        closed and we should clear state.current — same effect as the
        kill-switch firing from a current_app transition."""
        async with self._lock:
            had_current = self.state.current is not None
            current_vid = self.state.current.video_id if self.state.current else None
        if not had_current:
            return
        log.info(
            "Lounge subscribe ended with current=%s — scheduling player-close "
            "verify in %.1fs",
            current_vid, PLAYER_CLOSE_VERIFY_DELAY,
        )
        asyncio.create_task(
            self._verify_player_closed_after_delay(current_vid)
        )

    async def _verify_player_closed_after_delay(self, expected_vid: str) -> None:
        try:
            await asyncio.sleep(PLAYER_CLOSE_VERIFY_DELAY)
        except asyncio.CancelledError:
            return
        async with self._lock:
            # State may have changed during the wait (user added a new
            # video, advanced, etc.) — only act if current still matches
            # what we noted at disconnect time.
            if (self.state.current is None
                    or self.state.current.video_id != expected_vid):
                return
            observed_vid = self.state.lounge.get("video_id")
        if observed_vid == expected_vid:
            # Lounge reconnected with the same video — was a blip.
            log.info(
                "Player-close verify: Lounge reconnected with %s, was a "
                "transient blip, not firing kill-switch",
                expected_vid,
            )
            return
        log.info(
            "Player-close verify: Lounge reports video_id=%r (expected %r) — "
            "player closed, firing kill-switch",
            observed_vid, expected_vid,
        )
        await self._kill_switch()

    async def _on_lounge_now_playing(self, observation: dict) -> None:
        """Lounge reports the playing video changed. Two cases:

        (a) Our auto-advance fired — Lounge confirms our setPlaylist
            took effect. observed video_id == state.current.video_id;
            nothing to do.
        (b) User externally selected a different video via the SmartTube
            UI on the physical remote. observed video_id !=
            state.current.video_id; we want to cede control: clear
            state.current so the UI shows the externally-playing video
            (via the loungeActive render path) and the duration timer
            stops trying to advance away from it.

        Race window: when our own _advance fires, state.current is set
        to NEW before our setPlaylist propagates. During that window
        Lounge may still report the OLD video as a final state push —
        cur.video_id (=NEW) != lng.video_id (=OLD) wrongly looks like
        case (b). Mitigation: debounce. Wait EXTERNAL_SWITCH_DEBOUNCE
        and re-check. By then either our setPlaylist took effect
        (Lounge moves to NEW, no mismatch) or the user really did pick
        something else (mismatch persists).
        """
        obs_vid = observation.get("video_id")
        if not obs_vid:
            return
        async with self._lock:
            cur = self.state.current
            if cur is None:
                return  # nothing to cede; lng path already drives the UI
            cur_vid = cur.video_id
        if obs_vid == cur_vid:
            return  # case (a) — our advance, Lounge agrees
        # Cancel any prior debounce — only the latest mismatch matters.
        if (self._external_switch_task is not None
                and not self._external_switch_task.done()):
            self._external_switch_task.cancel()
        log.info(
            "External-switch candidate: Lounge=%s, ours=%s — verifying in %.1fs",
            obs_vid, cur_vid, EXTERNAL_SWITCH_DEBOUNCE,
        )
        self._external_switch_task = asyncio.create_task(
            self._verify_external_switch(cur_vid, obs_vid)
        )

    async def _verify_external_switch(
        self, expected_our_vid: str, observed_vid: str,
    ) -> None:
        try:
            await asyncio.sleep(EXTERNAL_SWITCH_DEBOUNCE)
        except asyncio.CancelledError:
            return
        async with self._lock:
            cur = self.state.current
            if cur is None or cur.video_id != expected_our_vid:
                # Our state moved on (advance, clear, etc.) — nothing to do.
                return
            lng_vid = self.state.lounge.get("video_id")
            if lng_vid != observed_vid:
                # Lounge moved on — either back to our video, or somewhere
                # else (which will retrigger the now_playing handler).
                return
        # Confirmed: user is playing a different video externally. Cede
        # — cancel our duration timer for the old video, clear current.
        # Queue is preserved: if the user-picked video ends, _advance
        # will pick up our queue's next item normally.
        log.info(
            "External-switch confirmed: Lounge=%s, ours=%s — clearing state.current",
            observed_vid, expected_our_vid,
        )
        await self._cancel_timer()
        async with self._lock:
            # Re-check under lock — state could've changed in the gap.
            if self.state.current is None or self.state.current.video_id != expected_our_vid:
                return
            self.state.current = None
            self.state.current_started_at = None
            # Also clear paused. Without this, a leftover paused=True
            # from a prior UI pause on the now-ceded video means
            # add()'s `should_start` check refuses to start the
            # next-added video — user adds a video, it lands in the
            # queue and silently waits for a Resume that doesn't
            # make sense (we don't have anything to "resume" from).
            # Matches the behavior in _kill_switch.
            self.state.paused = False
            self.state.pause_source = None
            snapshot = self.state.snapshot()
        await self._broadcaster.publish("item_ended", snapshot)

    async def _on_lounge_finished(self) -> None:
        """Lounge says a video finished. Advance the queue, but only if the
        finished video positively matches our current item. Two failure
        cases this guards:

        1. SmartTube transition (one video → another): Lounge fires
           Paused→Stopped→Playing for the OLD video before the NEW
           video's now_playing arrives. observed_video != expected_video,
           we return without advancing.

        2. Blanked observation: the suppress_lounge filter in app.py
           blanks the observation passed to queue_controller when
           SmartTube isn't foreground. observed_video ends up None —
           which historically slipped past the old guard (which required
           BOTH sides to be truthy to ignore) and triggered a spurious
           advance, wiping the user's freshly-queued cur on cold boot.
           Treat None observed_video as "we don't actually know" and
           skip the advance.
        """
        async with self._lock:
            observed_video = self.state.lounge.get("video_id")
            expected_video = self.state.current.video_id if self.state.current else None
            # Have we already acted on this video ending? See
            # FINISH_CONSUMED_WINDOW. This is the one discriminator that works
            # when consecutive queue entries share a clip AND does not assume
            # the playhead tracks the wall clock.
            already_consumed = False
            if (observed_video is not None
                    and self._finish_consumed_vid == observed_video
                    and self._finish_consumed_at is not None):
                try:
                    since = (self._clock() - self._finish_consumed_at).total_seconds()
                except Exception:
                    since = None
                already_consumed = since is not None and 0 <= since < FINISH_CONSUMED_WINDOW
        # DELIBERATELY no wall-clock staleness test here, and it must stay that
        # way. `_observation_predates_current` looks like the right tool — the
        # video_id match below separates items only when the ids DIFFER, and
        # two consecutive queue entries sharing one is ordinary, so a FINISHED
        # belonging to the item we just left can be credited to its successor.
        # It was tried and it is strictly worse than the bug it fixes.
        #
        # That test asks "is the position ahead of the wall clock since this
        # item became current", and the position legitimately runs ahead in
        # cases nothing re-anchors: an idle-add that takes ownership of a video
        # already playing (tv_play's skip-redundant branch sends nothing, so
        # playback is mid-way while we have just stamped the clock), an
        # in-place resume, and any seek made with the TV's own remote, which we
        # never see. In every one of those a genuine end-of-video FINISHED gets
        # discarded — and unlike a missed near-end position there is no
        # fallback, because `_duration_elapsed` is false too and the kill-switch
        # then clears `current` and strands the queue. Trading a rare dropped
        # item for a reachable strand is the wrong way round.
        #
        # The remaining double-advance is real but bounded: one queue item can
        # be popped and superseded when the same clip is queued twice in a row.
        # Fixing it needs a discriminator that does not assume the playhead
        # tracks the wall clock. Tests:
        # test_a_finished_after_a_remote_fast_forward_still_advances,
        # test_a_finished_for_a_video_we_took_ownership_of_midway_still_advances.
        # Both sides must be known AND match. Requiring only `expected_video`
        # to be truthy let the None case through: with nothing of ours playing
        # — a stalled queue after the external-switch cede, or the beta
        # self-test which plays outside the queue by design — a foreign
        # video's end advanced OUR queue. "We don't own anything" is not a
        # reason to start something.
        if observed_video is None or expected_video is None or (
                observed_video != expected_video):
            log.info(
                "Lounge finished for %s but current expects %s — ignoring",
                observed_video, expected_video,
            )
            return
        if already_consumed:
            log.info(
                "Lounge finished for %s, but we already advanced off that "
                "video moments ago — this describes the item we left, not the "
                "one now playing; ignoring",
                observed_video,
            )
            return
        await self._cancel_timer()
        await self._advance(reason="lounge_finished")

    async def _sync_paused_from_lounge(self, observation: dict) -> None:
        """Mirror Lounge's play/pause state to our queue.paused flag — but
        only when Lounge is reporting OUR current video. SmartTube may
        be auto-resuming a different video (e.g. on cold boot, before our
        deep link takes effect) and mirroring that would set
        queue.paused for the wrong reason."""
        ls = observation.get("state")
        obs_vid = observation.get("video_id")
        async with self._lock:
            cur = self.state.current
            if cur is None or obs_vid != cur.video_id:
                return
            if ls == "Paused" and not self.state.paused:
                self.state.paused = True
                self.state.pause_source = "lounge"
            elif ls == "Playing" and self.state.paused:
                self.state.paused = False
                self.state.pause_source = None
            else:
                return
            snap = self.state.snapshot()
        await self._broadcaster.publish("paused_toggled", snap)

    async def _kill_switch(self) -> None:
        # SmartTube leaves the foreground for two very different reasons and
        # they need opposite handling. The user opening Netflix is what this
        # was written for. But a video REACHING ITS END also does it —
        # SmartTube tears down the player and drops to the launcher — and
        # treating that as "they walked away" cancelled the duration timer
        # and cleared `current` while leaving state.queue untouched, so
        # everything still queued was stranded permanently. Measured at 2 of
        # 5 runs on real hardware; the timer normally wins the race, and
        # loses whenever it deferred to a Lounge position that lagged near
        # the end of the video.
        if self.state.queue and self._current_item_finished():
            log.info(
                "SmartTube left foreground at end of %s (ct=%s/%s) — "
                "advancing the queue rather than clearing it",
                self.state.current.video_id if self.state.current else None,
                (self.state.lounge or {}).get("current_time"),
                (self.state.lounge or {}).get("duration"),
            )
            await self._advance(reason="finished_at_killswitch")
            return
        await self._cancel_timer()
        async with self._lock:
            had_state = (
                self.state.current is not None
                or self.state.paused
                or self.state.lounge.get("available")
                or bool(self.state.lounge.get("video_id"))
            )
            if not had_state:
                return
            self.state.current = None
            self.state.current_started_at = None
            # Also clear paused — kill-switch is a fresh-start signal. Without
            # this, a user who paused via the web UI right before SmartTube
            # left foreground would end up stuck: current=None AND paused=True,
            # so the next add() goes to the queue instead of starting.
            self.state.paused = False
            self.state.pause_source = None
            # Blank the cached Lounge observation: SmartTube has left the
            # foreground but Lounge often stays connected and keeps reporting
            # the last-played video's state. If we leave that populated, the
            # next add() sees lounge_active=True from stale data and queues
            # the video instead of playing it. Always clear, even if `current`
            # was already null at the kill-switch moment — what matters is
            # the Lounge data, not whether we had something queued.
            self.state.lounge = _blank_lounge()
            snapshot = self.state.snapshot()
        await self._broadcaster.publish("item_ended", snapshot)

    # ── internals ────────────────────────────────────────────────────────────

    def _begin_locked(self, item: QueueItem) -> None:
        """Mutate state to mark `item` as playing and schedule its timer.
        Caller MUST hold self._lock."""
        self.state.current = item
        self.state.current_started_at = self._clock()
        # New item, new clock: whatever the previous one's timer concluded
        # says nothing about this one, and neither does where its playhead was.
        self._duration_elapsed = False
        self._playhead_origin_at = None
        # And this item has not been seen playing yet, whatever the last one did.
        self._playback_confirmed = False
        # Nor does the previous one's Lounge frame, which is still sitting in
        # state.lounge parked at ITS end — that is why we advanced. The
        # video_id guard normally separates them, but two queue entries can
        # share a video_id (the same clip queued twice is ordinary), and then
        # the stale frame passes every check and declares this brand-new item
        # already finished. A hardware soak caught exactly that: the second of
        # two identical shorts ran 3.7s before being advanced off its
        # predecessor's frame. The next Lounge event repopulates this.
        self.state.lounge = _blank_lounge()
        self._schedule_timer_locked(item)
        # A launch that produces nothing goes QUIET, so it cannot be caught by
        # waiting for an event: the dud parks Paused, `should_refresh()` gates
        # the position poll on `not paused`, and the event stream we would be
        # listening to stops. It needs its own clock.
        self._launch_gen += 1
        self._launch_check_task = asyncio.create_task(
            self._confirm_launch_after(item, self._launch_gen)
        )

    def _schedule_timer_locked(self, item: QueueItem) -> None:
        """Caller MUST hold self._lock. No-op for livestreams (duration_s is None)."""
        # Always invalidate any prior timer before scheduling a new one.
        self._timer_gen += 1
        if item.duration_s is None:
            self._timer_task = None
            return
        gen = self._timer_gen
        self._timer_task = asyncio.create_task(
            self._timer_body(gen, float(item.duration_s))
        )

    # How many times the duration timer may defer to Lounge before advancing
    # anyway. Each deferral re-reads current_time, so a device whose position
    # genuinely advances converges long before this. It only bites when the
    # position is FROZEN — a dormant player that the cloud cache keeps
    # reporting at a fixed ct (invariant 4) — where deferring forever means
    # the queue silently stops advancing and the duration fallback, which
    # exists precisely for "Lounge cannot be trusted here", never fires.
    MAX_LOUNGE_DEFERRALS = 12

    async def _timer_body(self, gen: int, seconds: float,
                          deferrals: int = 0) -> None:
        try:
            await self._sleeper(seconds)
        except asyncio.CancelledError:
            return
        # Re-acquire the lock; we may have been superseded while sleeping.
        async with self._lock:
            if gen != self._timer_gen:
                return                  # superseded by another schedule/cancel
            if self.state.current is None:
                return                  # nothing to advance from (defensive)
            # The item's whole nominal length has now elapsed. Record it
            # BEFORE the deferral logic: a deferral means only that Lounge's
            # cached position disagrees, and that cache is measurably stale
            # at exactly this moment (10.0 of 19.0 on hardware, nine seconds
            # behind, because SmartTube stops pushing near the end). The
            # kill-switch needs to know the duration ran out even when the
            # position says otherwise, or it treats a finished video as a
            # user walking away and strands the rest of the queue.
            self._duration_elapsed = True
            current_video_id = self.state.current.video_id
            lng = dict(self.state.lounge)

        # If Lounge is connected and actively tracking *our* current video
        # (real position, not ghost state), use its position to decide
        # whether playback has actually finished. The scraped duration
        # (`seconds` here) can be off due to mid-roll ads stretching real
        # runtime, so blind advance-on-timer-fire ends videos early. But
        # SmartTube also doesn't reliably fire `Lounge.finished` at
        # natural end-of-video — it sometimes goes sticky-Paused at
        # ct≈duration without transitioning to Stopped — so deferring
        # indefinitely would strand the queue. Compromise: if Lounge ct
        # is within 5s of duration, treat the timer fire as end-of-video
        # and advance. Otherwise re-schedule the timer for the remaining
        # real time + a 5s buffer.
        if (lng.get("available")
                and lng.get("video_id") == current_video_id
                and lng.get("current_time") is not None):
            lng_ct = float(lng["current_time"])
            lng_dur = lng.get("duration") or seconds
            try:
                lng_dur_f = float(lng_dur)
            except (TypeError, ValueError):
                lng_dur_f = float(seconds)
            remaining = lng_dur_f - lng_ct
            if remaining > 5.0 and deferrals >= self.MAX_LOUNGE_DEFERRALS:
                # Position is not moving. Deferring again would strand the
                # queue for good, so fall through and let the duration
                # fallback do its job.
                log.warning(
                    "Duration timer for %s has deferred to Lounge %d times "
                    "and it still reports %.1fs remaining (ct=%.1f) — the "
                    "position looks frozen, advancing anyway",
                    current_video_id, deferrals, remaining, lng_ct,
                )
            elif remaining > 5.0:
                log.info(
                    "Duration timer fired for %s but Lounge reports %.1fs remaining; "
                    "rescheduling (lng_ct=%.1f lng_dur=%.1f)",
                    current_video_id, remaining, lng_ct, lng_dur_f,
                )
                async with self._lock:
                    # Verify state hasn't changed while we held no lock.
                    if (gen != self._timer_gen
                            or self.state.current is None
                            or self.state.current.video_id != current_video_id):
                        return
                    self._timer_gen += 1
                    next_gen = self._timer_gen
                    self._timer_task = asyncio.create_task(
                        self._timer_body(next_gen, remaining + 5.0,
                                         deferrals + 1)
                    )
                return
            # Says what was OBSERVED, not what we are about to do. The
            # branches below can still route to _end_current, and this line
            # used to claim "advancing" before they ran — so every log of a
            # stalled queue cheerfully reported that it had advanced, which
            # sent the 2026-08-16 investigation down the wrong path for a
            # while. Each exit now logs its own outcome.
            log.info(
                "Duration timer fired for %s; Lounge ct=%.1f is within 5s of "
                "duration=%.1f (Lounge.finished did not fire)",
                current_video_id, lng_ct, lng_dur_f,
            )

        # Timer fired legitimately; behavior depends on paused + foreground state.
        # `paused` here is ambiguous and the two readings need opposite
        # handling: a user who pressed Pause wants nothing to happen, but a
        # video that reached its last frame ALSO reports Paused — measured on
        # the reference hardware, a lone video ends Paused rather than
        # Stopped, and `_sync_paused_from_lounge` mirrors that just as this
        # timer comes due. Treating the second case as the first abandoned
        # every remaining queue item, immediately after logging "advancing".
        # `pause_source == "ui"` is the one reading we can be certain of, and
        # it must not be overridden by the near-end position. Pausing does not
        # cancel the timer, so someone who pauses three seconds from the end
        # has the duration run out moments later and the position then looks
        # exactly like an ending — and we skipped their video for them. A
        # TV-remote pause at that range stays genuinely ambiguous (Lounge
        # reports it identically to a video parked on its last frame), but the
        # person who pressed Pause in our own UI told us plainly.
        if self.state.paused and (self.state.pause_source == "ui"
                                  or not self._lounge_says_finished()):
            # Clear without advancing. NOTE what happens next, because it
            # surprises people and it is deliberate rather than a gap:
            # `current` goes None while the queue keeps its items, and
            # `resume()` then starts the NEXT one — pinned by
            # test_unpause_with_idle_current_and_queued_item_starts_next.
            # Measured on hardware: paused at 634.9 of 635 with one item
            # queued, and the card emptied seven seconds later with the queue
            # intact but not moving until Play was pressed.
            log.info(
                "Timer fired while paused (source=%s) and not treating it as "
                "end of video — clearing current without advancing",
                self.state.pause_source,
            )
            await self._end_current(reason="paused_timer")
            return

        current_app = self._safe_current_app()
        # Truthiness, not `is not None`: androidtvremote2 initialises
        # current_app to "" and only fills it from an IME key-inject message,
        # so on any device that never negotiates IME it stays "" forever.
        # Reading that as "some other app" made every video end here instead
        # of advancing — auto-advance never worked at all on that hardware.
        if current_app and current_app != self._smarttube_package:
            log.info("Timer fired but SmartTube not foreground (%s); stopping", current_app)
            await self._end_current(reason="killswitch_at_fire")
            return

        log.info("Duration timer advancing the queue for %s", current_video_id)
        await self._advance(reason="timer")

    def _current_item_finished(self) -> bool:
        """Has the item we own played to its end?

        Two independent pieces of evidence, either of which is enough:

        * the duration timer has come due (`_duration_elapsed`) — the item's
          whole nominal length has passed; or
        * the Lounge position is within NEAR_END_SECONDS of the duration.

        Neither alone is sufficient in practice. The position is measurably
        stale right at the end (SmartTube stops pushing, and our poll is 3s
        apart), and the timer can be pending when a video ends early. Taken
        together they separate "the video finished" from "the user did
        something", which is the question both the timer and the kill-switch
        actually need answered.

        A FRESH position that positively disagrees wins over the flag. The
        flag is set before the deferral branch on purpose, but it is only
        cleared at item start — so once the timer has deferred it stays True
        for the rest of the item, and the gap between a scraped duration and
        the real one can be hours when the metadata fetch timed out and
        supplied its 600s default. Inside that gap the kill-switch read a
        half-watched video as finished and launched the next queue item at a
        TV the viewer had just walked away from.
        """
        if self._lounge_contradicts_finish():
            return False
        if self._duration_elapsed:
            return True
        return self._lounge_says_finished()

    def _lounge_contradicts_finish(self) -> bool:
        """Is Lounge telling us the length we believed was WRONG?

        Note what this does NOT ask. "The position is far from the end" is not
        a contradiction, because the cache is measurably stale at exactly the
        moment a video ends — measured at 10.0 of 19.0 as the kill-switch
        fired for a clip that had unambiguously finished, since SmartTube
        stops pushing near the end and our poll is 3s apart. Treating that as
        proof the video was still playing is the stranding this release exists
        to fix, from the other side.

        `_duration_elapsed` means "the duration we scraped has passed", so the
        only thing that undermines it is evidence the scrape was wrong. Lounge
        reporting a materially LONGER duration than the item carries is that
        evidence, and it is the failure that matters: `_fallback()` supplies
        600s for anything it could not scrape, so a three-hour video looks
        finished ten minutes in and any kill-switch after that launches the
        next queue item at a TV the viewer just walked away from.
        """
        cur = self.state.current
        lng = self.state.lounge or {}
        if cur is None or cur.duration_s is None or not lng.get("available"):
            return False
        if lng.get("video_id") != cur.video_id:
            return False
        ct = lng.get("current_time")
        dur = lng.get("duration")
        if ct is None or not dur or float(dur) <= 0:
            return False
        if self._observation_predates_current(float(ct)):
            return False
        if float(dur) <= float(cur.duration_s) + NEAR_END_SECONDS:
            # The two agree about the length, so a short position is the cache
            # lagging, not the video still running.
            return False
        return (float(dur) - float(ct)) > NEAR_END_SECONDS

    def _lounge_says_finished(self) -> bool:
        """Is the Lounge observation showing our item at its end?

        Requires the observation to be about OUR video and to carry a real
        position — the cloud cache reports dormant players indefinitely
        (invariant 4), so a video_id alone proves nothing.
        """
        cur = self.state.current
        lng = self.state.lounge or {}
        if cur is None or not lng.get("available"):
            return False
        if lng.get("video_id") != cur.video_id:
            return False
        ct = lng.get("current_time")
        dur = lng.get("duration")
        if ct is None or not dur or dur <= 0:
            return False
        if self._observation_predates_current(float(ct)):
            return False
        return (float(dur) - float(ct)) <= NEAR_END_SECONDS

    def _observation_predates_current(self, ct: float) -> bool:
        """Is this position from BEFORE the item we are now playing?

        Blanking `state.lounge` when an item begins is not enough: SmartTube
        keeps pushing about the PREVIOUS playback for several seconds after
        we have moved on, so the stale frame simply arrives again. The
        video_id guard separates them only when the ids differ, and the same
        clip queued twice is ordinary.

        Time is the discriminator that always works. An item that became
        current N seconds ago cannot honestly be at N + slack seconds in.
        Measured: a video 7s into its life reported at 17.7s, which is what
        declared it finished and skipped it.

        `current_started_at` is stamped when playback starts, so `elapsed`
        never under-counts; the slack then absorbs launch latency and clock
        skew, and `start_s` covers a deliberate offset.
        """
        cur = self.state.current
        started = self._playhead_origin_at or self.state.current_started_at
        if cur is None or started is None:
            return False
        try:
            elapsed = (self._clock() - started).total_seconds()
        except Exception:
            return False
        if elapsed >= OBSERVATION_TRUST_AFTER:
            # Long past the window in which a frame about the PREVIOUS item can
            # still arrive, so a position ahead of the wall clock is something
            # that moved the playhead — a SponsorBlock skip, a seek on the TV's
            # own remote, playback we adopted mid-way — not a stale frame. See
            # the constant for what calling these stale cost.
            return False
        offset = float(cur.start_s or 0)
        return ct > (elapsed + offset + OBSERVATION_STALENESS_SLACK)

    async def _advance(self, reason: str) -> None:
        """Pop next item if any, begin it; otherwise idle. Always emits an event."""
        async with self._lock:
            # Remember what we are advancing AWAY from. Whatever the reason,
            # the item we are leaving has stopped, and a `lounge.finished`
            # arriving for it in the next moment describes that — not the item
            # we are about to start, even when the two share a video_id.
            leaving = self.state.current.video_id if self.state.current else None
            if leaving is not None:
                self._finish_consumed_vid = leaving
                self._finish_consumed_at = self._clock()
            next_item = self.state.queue.pop(0) if self.state.queue else None
            if next_item:
                # A `paused` that described the item we are LEAVING must not
                # carry onto the one we are starting. The empty-queue branch
                # below has reset it since the 2026-08-16 hardware fix, but
                # this branch did not — so advancing out of a paused video
                # (a Skip while paused, or a BACK-out that fires FINISHED)
                # left paused=True over a video that is genuinely playing:
                # the page shows it frozen at 0:00 with Play lit, and the
                # next add parks in the queue because add() gates
                # should_start on `not paused`.
                self.state.paused = False
                self.state.pause_source = None
                self._begin_locked(next_item)
                event = "item_started"
            else:
                self.state.current = None
                self.state.current_started_at = None
                # Nothing is playing and nothing is queued, so a leftover
                # `paused` describes a player we no longer own — and it is
                # not inert. add() gates should_start on `not paused`, and
                # the replace-current branch can't rescue it because that
                # needs `current is not None`. Leaving it set wedges the
                # queue: every later add parks and never plays, silently.
                # Found on hardware — a lone video ends Paused, not Stopped,
                # so the mirror sets this on the way out of every video that
                # plays to its end with nothing behind it.
                self.state.paused = False
                self.state.pause_source = None
                # Same for `_duration_elapsed` and the playhead origin: both
                # describe the item we have just stopped owning, and a later
                # kill-switch reading the stale flag advances off it.
                self._duration_elapsed = False
                self._playhead_origin_at = None
                # Cancel any leftover timer state for the now-cleared current.
                self._timer_gen += 1
                self._timer_task = None
                event = "item_ended"
            snapshot = self.state.snapshot()
        if not next_item:
            # current cleared without a successor — also cancel any
            # in-flight tv_play. Otherwise a previous tv_play in its
            # WAKE_DELAY sleep would wake up later and fire launch
            # commands at a TV the user has since told us to leave alone.
            self._cancel_in_flight_sends()
        await self._broadcaster.publish(event, snapshot)
        if next_item:
            self._send_to_tv(next_item)

    async def _end_current(self, reason: str) -> None:
        """Clear `current` without advancing. Used for paused-timer-fire and kill-switch."""
        async with self._lock:
            if self.state.current is None:
                return
            self.state.current = None
            self.state.current_started_at = None
            self._playhead_origin_at = None
            # Same reasoning as _advance's empty-queue branch: a `paused`
            # that outlives the item it described wedges every later add. So
            # does `_duration_elapsed` — a later kill-switch asks "did the
            # current item finish?", gets True from a flag belonging to an
            # item that no longer exists, and advances the queue off it.
            self.state.paused = False
            self.state.pause_source = None
            self._duration_elapsed = False
            self._timer_gen += 1
            self._timer_task = None
            snapshot = self.state.snapshot()
        # current cleared — cancel any in-flight tv_play so a previous
        # WAKE_DELAY sleep doesn't fire stale launch commands at the TV.
        self._cancel_in_flight_sends()
        await self._broadcaster.publish("item_ended", snapshot)

    async def _cancel_timer(self) -> None:
        async with self._lock:
            self._timer_gen += 1     # invalidate any sleeping timer body
            t = self._timer_task
            self._timer_task = None
        if t and not t.done():
            t.cancel()
            with contextlib.suppress(BaseException):
                await t

    def _safe_current_app(self) -> Optional[str]:
        try:
            return self._get_current_app()
        except Exception:
            return None

    def _cancel_in_flight_sends(self) -> None:
        """Cancel any in-flight tv_play tasks.

        tv_play has a 15s WAKE_DELAY sleep on cold boot. If a previous
        tv_play is mid-sleep when state.current changes (new item
        replaces it, or skip clears it), the previous tv_play would
        wake up and fire commands against state that no longer
        applies — observable as a "double play" (PlaybackActivity
        launched twice), or as a stale tv_play waking the TV during
        the next one's setup. The
        cancelled tv_play raises CancelledError out of its sleep;
        partial side-effects (wake key already sent, deep link already
        fired) are idempotent.
        """
        for prior in list(self._send_tasks):
            if not prior.done():
                prior.cancel()

    def has_pending_sends(self) -> bool:
        """Is a tv_play still in flight?

        Exposed for the beta self-test, which must refuse to start while one
        is running: _cancel_in_flight_sends only cancels tasks the controller
        created, so it cannot see the self-test, and a probe firing alongside
        an in-flight tv_play is two senders — the double-play shape.
        """
        return any(not t.done() for t in self._send_tasks)

    def _send_to_tv(self, item: QueueItem) -> None:
        """Fire-and-forget TV send. Failures are logged; state has already moved on."""
        self._cancel_in_flight_sends()

        async def _runner() -> None:
            try:
                # A float back means the sender ADOPTED playback already in
                # progress at that position rather than starting the item
                # fresh — tv_play's skip-redundant and resume-in-place
                # branches. None (or any callable that returns nothing) means
                # playback starts from the item's own beginning.
                adopted_at = await self._play(item.video_id, item.start_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TV send failed for %s", item.video_id)
                return
            # Playback has, as far as we can tell, only just started. The
            # duration timer has been counting since the item was QUEUED —
            # through WAKE_DELAY (15s) and the whole launch — so without this
            # it comes due that much early. A healthy Lounge hides it: the
            # defer branch sees time remaining and reschedules. An
            # out-of-sync Lounge has nothing to defer on, so the timer
            # advanced while the video was still playing — a live production
            # failure on v1.0.
            await self._anchor_timer_to_playback_start(
                item,
                position_s=adopted_at if isinstance(adopted_at, (int, float))
                else None,
            )

        task = asyncio.create_task(_runner())
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)

    async def _maybe_advance_at_end_of_video(self, observation: dict) -> None:
        """Start the next video the moment Lounge shows this one finished.

        A video with nothing behind it ends **Paused**, not Stopped, so
        `lounge.finished` never fires for it and we would otherwise wait out
        the duration timer — which may itself have deferred on a stale
        position, adding seconds more.

        Reaching the end is the signal; the duration timer is a fallback, and
        REQUIRING it here shut this path exactly when it was needed. Measured
        on hardware with the playhead moved on the device (a SponsorBlock
        skip): the video played out to 634.42 of 635, parked Paused, and the
        queue sat there over a minute with the next item waiting. The timer
        had not come due because the video finished ten minutes early in
        wall-clock terms; `lounge.finished` never fired because a lone video
        ends Paused rather than Stopped; and the kill-switch never ran because
        SmartTube stayed foreground on its ended-video screen, so there was no
        transition to trigger it. Three independent paths, all shut at once.

        What must still be respected is an explicit pause — a user pausing
        three seconds from the end looks identical to an ending, and skipping
        their video for them would be worse than being late. `pause_source`
        answers that directly, and it is a far better discriminator than a
        clock a skip has already invalidated. A pause made with the TV's own
        remote at that range stays genuinely ambiguous; being a few seconds
        eager there is the cheaper error than stranding the queue on every
        video SponsorBlock has touched.
        """
        async with self._lock:
            cur = self.state.current
            if cur is None or not self.state.queue:
                return
            if self.state.pause_source == "ui":
                return
            parked_at_end = False
            if not self._duration_elapsed:
                ct0, dur0 = observation.get("current_time"), observation.get("duration")
                try:
                    parked_at_end = (
                        ct0 is not None and dur0
                        and (float(dur0) - float(ct0)) <= END_PARK_SLACK
                    )
                except (TypeError, ValueError):
                    parked_at_end = False
                if not parked_at_end:
                    return
            if not observation.get("available"):
                return
            if observation.get("video_id") != cur.video_id:
                return
            # One ending, two dispatches. `_on_playback_state` emits POSITION
            # and then STATE from a single device event carrying the same
            # observation, and both land here — so without this the second
            # re-applies the frame to the item the first just started, popping
            # and discarding it. Deterministic with two consecutive entries
            # sharing a clip, which is the case the release notes advertise as
            # fixed. `_on_lounge_finished` has had this guard since the same
            # day; this path was missed.
            if (self._finish_consumed_vid == observation.get("video_id")
                    and self._finish_consumed_at is not None):
                try:
                    since = (self._clock() - self._finish_consumed_at).total_seconds()
                except Exception:
                    since = None
                if since is not None and 0 <= since < FINISH_CONSUMED_WINDOW:
                    return
            ct = observation.get("current_time")
            dur = observation.get("duration")
            if ct is None or not dur or float(dur) <= 0:
                return
            # And a frame that predates the item cannot end it. This function
            # reads its ARGUMENT rather than `state.lounge`, so `_begin_locked`
            # blanking that field — which is what protects the other finish
            # checks — does not cover this one at all.
            if self._observation_predates_current(float(ct)):
                return
            if observation.get("state") not in ("Paused", "Stopped"):
                return
            if (float(dur) - float(ct)) > NEAR_END_SECONDS:
                return
            ended = cur.video_id
        log.info(
            "Lounge shows %s parked at its end (%.1f/%.1f) and its duration has "
            "elapsed — advancing now rather than waiting for the timer",
            ended, float(ct), float(dur),
        )
        await self._advance(reason="lounge_end_of_video")

    def _launch_failed(self, observation: dict) -> bool:
        """Has the current item been current a long while without ever playing?

        Positive evidence only: Lounge reporting OUR video, and reporting it
        not playing, long past any healthy launch. Silence proves nothing — a
        Lounge we cannot hear from says nothing about what the TV is doing,
        and guessing there would skip good videos.
        """
        cur = self.state.current
        started = self.state.current_started_at
        if cur is None or started is None or self._playback_confirmed:
            return False
        # Somebody pressed Pause. A dud parks itself Paused with nobody
        # touching anything, and only the Lounge mirror sets `pause_source` —
        # so this is what separates them. It matters because pausing while the
        # video is still buffering produces the dud signature exactly: ct is
        # 0.0, so the `> 0` clause below never latches `_playback_confirmed`.
        if self.state.pause_source is not None:
            return False
        if not observation.get("available"):
            return False
        if observation.get("video_id") != cur.video_id:
            return False
        if observation.get("state") == "Playing":
            return False
        # Still at the very start. A video that ran and stopped sits wherever
        # it got to; one that never began sits at zero — measured, a dud parks
        # at exactly 0.0 Paused. Requiring this keeps the check off anything
        # that did play, however it ended up.
        ct = observation.get("current_time")
        if ct is None or float(ct) > 0.5:
            return False
        try:
            elapsed = (self._clock() - started).total_seconds()
        except Exception:
            return False
        return elapsed >= LAUNCH_CONFIRM_TIMEOUT

    async def _confirm_launch_after(self, item: QueueItem, gen: int) -> None:
        """Check LAUNCH_CONFIRM_TIMEOUT after an item became current.

        Re-arms if the clock moved under it. `_anchor_timer_to_playback_start`
        re-stamps `current_started_at` once the launch completes, and the
        check measures elapsed from that — so a check scheduled at begin can
        come due a few seconds before its own test can pass. The first version
        of this returned False there and never ran again, which looked correct
        in a test and did nothing at all on the device.
        """
        wait = LAUNCH_CONFIRM_TIMEOUT
        for _ in range(4):
            try:
                await self._sleeper(wait)
            except asyncio.CancelledError:
                return
            if gen != self._launch_gen:
                return                      # a later item owns this check now
            cur = self.state.current
            if cur is None or cur.id != item.id:
                return
            started = self.state.current_started_at
            if started is None:
                return
            try:
                elapsed = (self._clock() - started).total_seconds()
            except Exception:
                return
            if elapsed >= LAUNCH_CONFIRM_TIMEOUT:
                break
            wait = max(0.5, LAUNCH_CONFIRM_TIMEOUT - elapsed)
        await self._maybe_skip_a_launch_that_never_started(
            dict(self.state.lounge or {})
        )

    async def _maybe_skip_a_launch_that_never_started(self, observation: dict) -> None:
        """Move on from a video that was never going to play.

        See LAUNCH_CONFIRM_TIMEOUT for what this costs when it is missing: a
        single unavailable link holds the queue for its whole scraped
        duration, and Play does not recover it.
        """
        async with self._lock:
            if not self._launch_failed(observation):
                return
            failed = self.state.current.video_id
            has_next = bool(self.state.queue)
        log.warning(
            "%s has been current for over %.0fs and Lounge has never reported "
            "it playing (state=%s @ %s) — treating the launch as failed and %s",
            failed, LAUNCH_CONFIRM_TIMEOUT, observation.get("state"),
            observation.get("current_time"),
            "moving to the next item" if has_next else "clearing it",
        )
        if has_next:
            await self._advance(reason="launch_never_started")
        else:
            await self._end_current(reason="launch_never_started")

    async def _anchor_timer_to_playback_start(
        self, item: QueueItem, position_s: Optional[float] = None,
    ) -> None:
        """Restart the duration timer now that the launch has completed.

        Only for the item we just launched, and only while it is still
        current — an add that superseded us mid-launch owns the timer now.

        `position_s` is where playback actually is. It is set when the sender
        ADOPTED playback already running rather than starting the item: an
        idle add of the video already on screen takes ownership on purpose and
        sends nothing at all, so treating that as "playback begins now" armed
        a full-length countdown for a video most of the way through. Left
        None, the item is starting from its own beginning.
        """
        async with self._lock:
            cur = self.state.current
            if cur is None or cur.id != item.id or item.duration_s is None:
                return
            # The clock restarts, so whatever the old timer concluded about
            # this item's length having elapsed no longer holds.
            self._duration_elapsed = False
            # Re-stamp the start. It was set in `_begin_locked`, i.e. before
            # the wake and the launch, which made the UI's wall-clock estimate
            # open at 0:15 or more on a cold start.
            self.state.current_started_at = self._clock()
            offset = float(cur.start_s or 0)
            if position_s is None:
                self._playhead_origin_at = None
                remaining = float(item.duration_s)
            else:
                # The playhead is where the sender found it, not at zero.
                self._playhead_origin_at = self._clock() - timedelta(
                    seconds=max(0.0, float(position_s) - offset)
                )
                remaining = max(0.0, float(item.duration_s) - float(position_s))
            self._timer_gen += 1
            gen = self._timer_gen
            self._timer_task = asyncio.create_task(
                self._timer_body(gen, remaining)
            )
        log.debug("Duration timer anchored for %s: %.1fs remaining%s",
                  item.video_id, remaining,
                  "" if position_s is None
                  else f" (adopted playback at {float(position_s):.1f}s)")

    async def reschedule_timer_for_position(self, position_s: float) -> None:
        """Re-anchor the duration timer after the playhead moved externally.

        No-op for livestreams (no duration) and when nothing is playing.
        """
        async with self._lock:
            cur = self.state.current
            if cur is None or cur.duration_s is None:
                return
            remaining = max(0.0, float(cur.duration_s) - float(position_s))
            # Move the PLAYHEAD ORIGIN with the playhead, not just the timer.
            # `_observation_predates_current` calls a position stale when it
            # runs ahead of the time since that origin, which after a forward
            # seek every truthful position does — permanently, since elapsed
            # can never catch up. That silently switched off the Lounge half
            # of both finish checks for the rest of the item, and with it the
            # `paused and not _lounge_says_finished()` guard in _timer_body:
            # a lone video ends Paused, so the timer then cleared `current`
            # and left everything behind it queued with nothing to start it —
            # the stranding this release exists to fix, reachable by seeking.
            #
            # Its OWN field, deliberately: `current_started_at` answers "when
            # did this item become current", and the kill-switch start grace,
            # the STARTING badge and the frontend's progress estimate all read
            # it for that. See the field's definition in __init__.
            offset = float(cur.start_s or 0)
            self._playhead_origin_at = self._clock() - timedelta(
                seconds=max(0.0, float(position_s) - offset)
            )
            # And the item's nominal length demonstrably has NOT elapsed at a
            # position the user just seeked back to. `_current_item_finished`
            # short-circuits on this flag ahead of any position check, so
            # leaving it set let the next kill-switch advance the queue off a
            # video that had just been rewound.
            self._duration_elapsed = False
            self._timer_gen += 1
            gen = self._timer_gen
            self._timer_task = asyncio.create_task(
                self._timer_body(gen, remaining)
            )
        log.info("Duration timer re-anchored to %.1fs remaining after seek to %.1fs",
                 remaining, position_s)

    def track_send(self, coro) -> asyncio.Task:
        """Run an externally-created TV send under the concurrency guard.

        `_cancel_in_flight_sends` can only cancel tasks it knows about, so
        any play signal created outside `_send_to_tv` is invisible to it —
        which is how the resume path came to sit outside invariant 1. Route
        such sends through here and they behave like any other: they cancel
        whatever was in flight, and a later send can cancel them.
        """
        self._cancel_in_flight_sends()
        task = asyncio.create_task(coro)
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)
        return task
