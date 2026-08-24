# API

← back to the [README](../README.md)

Everything the web UI does is a plain HTTP call, so webhooks, scripts and Home Assistant automations can drive it too. There's no authentication — see [SECURITY.md](../SECURITY.md).

## Endpoints

```
GET    /api/status                        connection state, TV power, pairing status
GET    /api/queue                         full queue state as JSON
POST   /api/queue          {url|video_id} add a video (rate-limited)
DELETE /api/queue/{id}                    remove a queued item
POST   /api/queue/{id}/move/{up|down}     reorder a queued item one slot
POST   /api/skip                          next video, or screensaver if empty
POST   /api/pause                         pause playback and freeze auto-advance. Returns ok whether or
                                          not the pause reached the TV (see below)
POST   /api/resume                        resume
POST   /api/clear                         empty the queue, leave current playing
POST   /api/seek           {to|by}        `to`: "1:23" / "90s" / "1h30m"; `by`: ±seconds. 503 without a
                                          Lounge session, and 503 when SmartTube is not in that session
                                          (`lounge_screen_online: false`) — the message names the fix.
                                          Also re-anchors auto-advance to the new position (see below)
POST   /api/volume/{up|down|mute}         sends a volume keycode over the paired remote; 503 if
                                          no TV is paired
GET    /api/events                        SSE stream of queue snapshots
POST   /api/play           {url|video_id} legacy: clear queue and replace current
GET    /healthz                           liveness probe

POST   /api/pair/start     {host}         begin TV-remote pairing
POST   /api/pair/finish    {code}         6-character code from the TV
POST   /api/pair/cancel                   abort an in-progress pairing
POST   /api/lounge/pair    {code}         12-digit code from SmartTube; 409 if already paired
```

### What `/api/status` reports

```json
{
  "version": "1.03",
  "configured": true,
  "credentials_present": true,
  "host": "192.168.1.42",
  "pairing_in_progress": false,
  "tv_on": true,
  "current_app": "org.smarttube.stable",
  "lounge_paired": true,
  "lounge_connected": true,
  "lounge_screen_online": true,
  "volume_available": true
}
```

- `version` — the running build, from the `VERSION` file baked into the image.
- `configured` — there is a **live connection** to the TV right now. Not "has this been set up": a paired install whose TV is unreachable reports `false` here.
- `credentials_present` — the TV-remote certificate, key and config are all on disk. `credentials_present && !configured` is the "paired but can't reach" state the page shows as **PAIRED · CAN'T REACH**; the reverse combination can't happen.
- `host` — the TV's address from config. Set even while the connection is down, since that's exactly when you need to know which address is failing.
- `pairing_in_progress` — a TV-remote pairing flow has started and is waiting for the code.
- `tv_on` — `true`, `false`, or `null` when the power state can't be read.
- `current_app` — the foreground package on the TV, or `null`/empty when it can't be read. Unreadable is not the same as "some other app".
- `lounge_paired` — a Lounge token exists on disk.
- `lounge_connected` — the service's session with YouTube's Lounge is bound. This is a fact about **our end**: YouTube binds the session and answers every command with 200 whether or not SmartTube is in it.
- `lounge_screen_online` — whether SmartTube is actually **in** that session, read from the device list YouTube sends with every bind. `true`, `false`, or `null` when nothing has been observed yet (no session, or no device list seen). Brief absences — the TV's own connection to YouTube recycling, measured at well under two minutes — are not reported; `false` means the TV has stayed gone for several minutes. `lounge_connected: true` with `lounge_screen_online: false` is the state the page calls **SMARTTUBE NOT LISTENING**: pause, seek and setPlaylist would all return success and reach nothing, so the service routes around the session — pause goes out as a keycode over the paired remote, adds stop waiting for a position report that cannot come (an add that finds SmartTube in front rebinds the session once, and goes to the launch Intent if the TV is still absent), and `/api/seek` refuses with 503. Only an observed `false` changes behaviour; `null` leaves everything as it was.
- `volume_available` — a TV is paired, so the volume keycodes have somewhere to go. Whether the device honours them depends on its CEC volume setting, which can't be read over this protocol.

### Pause is not confirmed

`POST /api/pause` freezes auto-advance and returns `ok: true` regardless of what happened on the TV. The pause itself is **sent** — over Lounge when SmartTube is in the session, otherwise as the `MEDIA_PAUSE` keycode over the paired remote. A Lounge pause is only trusted once the TV itself confirms it within a few seconds (YouTube answers success regardless of whether anything received the command); an unconfirmed one falls back to the keycode as well. The endpoint still returns `ok` either way — including when the keycode was withheld because another app was in front (next paragraph). Read the playback state from `/api/events` if you need to know.

The keycode fallback is only sent when SmartTube is the foreground app, or the foreground can't be read — media keys reach whatever holds the media session, and a pause from a webhook must not pause Netflix.

### Seeking moves the auto-advance countdown too

`/api/seek` is not purely a playback command. Auto-advance is backed by a
timer sized to the video's length when it started, and a seek moves the
playhead without telling it — so before v1.02 a jump forward left the queue
advancing late by the size of the jump, and "late" is precisely the window in
which the video ends first and the next item never starts. The endpoint now
re-anchors that timer, and the app's idea of where the playhead is, to the
position you seeked to.

Nothing to pass for it; it happens whenever the seek succeeds **for a video the queue owns**. Seeking playback somebody started on the TV itself still works — the endpoint only needs a live Lounge session with SmartTube in it — but there is no countdown of ours behind it to move, so nothing is re-anchored. A seek the
device rejects returns **502** and changes nothing, which matters because
acting on a seek that never landed is worse than not seeking at all.

### Recovering a TV that moved

```
POST   /api/tv/address         {host}     repoint an existing pairing at a new address
```

Always available. The pairing certificate binds to the *device*, not its
address, so a DHCP lease change breaks the connection while leaving the
credentials perfectly valid. This repoints them without re-pairing.

### Device diagnostics

Present on every build, but **off unless `SELF_TEST=1`** (see
[CONFIGURATION.md](CONFIGURATION.md)). While disabled the first three return
**503**:

```
GET    /api/diagnostics                   passive report: reads state, sends nothing
POST   /api/selftest                      start a device self-test; 200 + run id
POST   /api/selftest/answers   {answers}  fold the tester's answers into the report
```

`GET /api/selftest` is the exception — it always answers, reporting
`enabled: false`, because that is the field the page reads to decide whether
to show the button at all.

`POST /api/selftest` returns **200** immediately with `run_id`, `eta_s` and the probe list — the run itself takes up to about eight minutes — so poll the `GET` for progress and, once `status` is `done`, the full report.

It returns **409** while a run is already in flight, and so does **every endpoint that moves the TV**: `/api/queue`, `/api/play`, `/api/skip`, `/api/pause`, `/api/resume`, `/api/seek` and `/api/volume/*`. The self-test sends its own commands, and two senders at once is the double-play failure this project guards hardest against. `/api/clear` stays available — it only empties the queue and sends nothing to the TV.

It also returns 409 if a video is already mid-launch when you press the button, since that launch is itself a sender.

Set `SELF_TEST=0` to remove the button and make `POST` return 503.

## Queue a video

```bash
curl -X POST http://<host>:38420/api/queue \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

Accepts any YouTube URL form or a bare 11-character video ID. Returns 429 if the caller's IP is inside the rate-limit window.

## Live updates

`GET /api/events` is a Server-Sent Events stream. Every event carries a **complete** snapshot under `state`, plus a `type` naming the transition — so clients replace their whole view from each message and there's no diffing to implement.

```bash
curl -N http://<host>:38420/api/events
```

## Notes for automation

- Non-browser clients don't send an `Origin` header and so aren't affected by the CSRF check.
- Requests still have to pass the `Host` check — use the IP or a LAN name, or set `ALLOWED_HOSTS`. See [SECURITY.md](../SECURITY.md).
- `/healthz` returns 200 whenever the event loop is responsive, regardless of pairing or TV state. It's a liveness probe, not a readiness probe.
