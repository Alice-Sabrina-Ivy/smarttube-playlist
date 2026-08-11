# How it works

← back to the [README](../README.md)

## Two channels to the TV

Everything runs over two connections, and the difference between them explains most of the project's behaviour:

1. **Android TV Remote v2** (TCP 6466) — the same protocol the official Google TV mobile app uses. Wakes the TV, foregrounds SmartTube, and reports which app is in front. It cannot see playback state, position, or media metadata. Always required.
2. **YouTube Lounge** (HTTPS to youtube.com) — the protocol behind "play on TV" in the YouTube mobile app; SmartTube implements the receiver side. Pushes videos, reports real position and play/pause state, and signals end-of-video. Optional but strongly recommended.

Without Lounge the service is flying blind: it knows what it asked the TV to do, but not what actually happened.

## Starting a video

1. If the TV reports off, send `POWER` and wait for it to come up. Instant-on TVs claim to be awake within about a second while the OS is still booting, so a minimum delay is enforced rather than trusting the "on" signal.
2. If a screensaver is in the foreground, dismiss it first — screensavers silently swallow app-launch intents.
3. If SmartTube isn't in the foreground, launch it via `market://launch`.
4. Send **exactly one** play signal: `setPlaylist` over Lounge when it's connected, otherwise a `vnd.youtube.launch://` deep link through the remote.

That last point is load-bearing. Sending both makes SmartTube load the video twice, which is audible.

## Auto-advance

With Lounge connected, the queue advances on the actual end-of-video signal. Without it, an `asyncio` timer sized to the scraped duration drives advancement instead — which is why a failed metadata scrape is more than cosmetic, and why livestreams (no fixed duration) never auto-advance at all.

## Metadata

Queuing a video fetches its YouTube watch page server-side and pulls title, channel, duration and livestream status out of the embedded `ytInitialPlayerResponse` JSON. The page is 1.1–1.6 MiB, so the fetch timeout needs real headroom; when the scrape fails the item falls back to the video ID as its title and an assumed duration.

## Leaving SmartTube

If the foreground app changes away from SmartTube, the queue stops sending videos. Queued items aren't lost — return to SmartTube and press Skip.

## State and events

The queue lives in memory and is deliberately not persisted; pairing is. Every mutation is broadcast as a full snapshot to all connected SSE clients, so every open browser stays in step without polling.

## Why not Google Cast?

Earlier versions used pychromecast. On current Google TV firmware, any unauthenticated cast client makes the TV launch its Default Media Receiver — the blue cast screen — interrupting whatever's playing. The Google Home app avoids this with account authentication that third-party tools can't replicate. Lounge never triggers a cast UI, which is why it won.

---

# Development

Running it directly, without Docker:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
DATA_DIR=./data .venv/bin/python -m uvicorn app:app --port 8000 --reload
```

Python 3.12. The frontend is a single `index.html` — vanilla HTML/CSS/JS, no build step, no bundler. Edit and reload.

## Module layout

| File | Responsibility |
|---|---|
| `app.py` | FastAPI app, HTTP endpoints, TV launch sequence, Lounge bridge |
| `playlist.py` | Queue state machine (named to avoid shadowing stdlib `queue`) |
| `lounge.py` | YouTube Lounge client wrapper and playback observation |
| `metadata.py` | YouTube watch-page scraper |
| `events.py` | SSE fan-out to connected browsers |
| `ratelimit.py` | Per-IP rate limiting |
| `denon.py` | Denon/Marantz AVR volume backend |
| `index.html` | The entire frontend |

## Contributing

Bug reports are welcome, especially with logs at `LOG_LEVEL=DEBUG` and a note about which TV hardware you're on — device coverage beyond Google TV is the biggest open question in the project.

This is a personal project maintained in spare time. Issues and pull requests may sit for a while.
