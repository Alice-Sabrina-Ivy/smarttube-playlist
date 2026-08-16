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
from datetime import datetime, timezone
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
        if self._cast_play is not None:
            await self._cast_play()
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

    async def _kill_switch_after_debounce(self) -> None:
        """Wait KILL_SWITCH_DEBOUNCE seconds and re-check the foreground.
        Fire the kill-switch only if SmartTube is genuinely no longer
        foreground. Library callback flickers (mid-playback transient
        reports of launcher) get filtered out."""
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
            snapshot = self.state.snapshot()

        # Routine position updates — emit a lightweight snapshot, no state-machine action.
        if event_type == "lounge.position":
            await self._broadcaster.publish("lounge_update", snapshot)
            return

        await self._broadcaster.publish("lounge_update", snapshot)

        if event_type == "lounge.finished":
            await self._on_lounge_finished()
        elif event_type == "lounge.state":
            await self._sync_paused_from_lounge(observation)
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
        self._schedule_timer_locked(item)

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
            log.info(
                "Duration timer fired for %s; Lounge ct=%.1f within 5s of "
                "duration=%.1f — advancing (Lounge.finished did not fire)",
                current_video_id, lng_ct, lng_dur_f,
            )

        # Timer fired legitimately; behavior depends on paused + foreground state.
        if self.state.paused:
            await self._end_current(reason="paused_timer")
            return

        current_app = self._safe_current_app()
        if current_app is not None and current_app != self._smarttube_package:
            log.info("Timer fired but SmartTube not foreground (%s); stopping", current_app)
            await self._end_current(reason="killswitch_at_fire")
            return

        await self._advance(reason="timer")

    async def _advance(self, reason: str) -> None:
        """Pop next item if any, begin it; otherwise idle. Always emits an event."""
        async with self._lock:
            next_item = self.state.queue.pop(0) if self.state.queue else None
            if next_item:
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
            # Same reasoning as _advance's empty-queue branch: a `paused`
            # that outlives the item it described wedges every later add.
            self.state.paused = False
            self.state.pause_source = None
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
                await self._play(item.video_id, item.start_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TV send failed for %s", item.video_id)

        task = asyncio.create_task(_runner())
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)
