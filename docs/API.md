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
POST   /api/pause                         pause playback and freeze auto-advance
POST   /api/resume                        resume
POST   /api/clear                         empty the queue, leave current playing
POST   /api/seek           {to|by}        `to`: "1:23" / "90s" / "1h30m"; `by`: ±seconds
POST   /api/volume/{up|down|mute}         relays an HDMI-CEC volume keycode; 503 if
                                          no TV is paired
GET    /api/events                        SSE stream of queue snapshots
POST   /api/play           {url|video_id} legacy: clear queue and replace current
GET    /healthz                           liveness probe

POST   /api/pair/start     {host}         begin TV-remote pairing
POST   /api/pair/finish    {code}         6-character code from the TV
POST   /api/pair/cancel                   abort an in-progress pairing
POST   /api/lounge/pair    {code}         12-digit code from SmartTube; 409 if already paired
```

### Beta build only

```
GET    /api/diagnostics                   passive report: reads state, sends nothing
POST   /api/selftest                      start a device self-test; 200 + run id
GET    /api/selftest                      progress while running, full report when done
POST   /api/selftest/answers   {answers}  fold the tester's answers into the report
POST   /api/tv/address         {host}     repoint an existing pairing at a new address
```

`POST /api/selftest` returns **200** immediately with `run_id`, `eta_s` and the probe list — the run itself takes about three minutes — so poll the `GET` for progress and, once `status` is `done`, the full report.

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
