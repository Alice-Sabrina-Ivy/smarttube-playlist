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
POST   /api/volume/{up|down|mute}         requires DENON_HOST; 503 otherwise
GET    /api/events                        SSE stream of queue snapshots
POST   /api/play           {url|video_id} legacy: clear queue and replace current
GET    /healthz                           liveness probe

POST   /api/pair/start     {host}         begin TV-remote pairing
POST   /api/pair/finish    {code}         6-character code from the TV
POST   /api/pair/cancel                   abort an in-progress pairing
POST   /api/lounge/pair    {code}         12-digit code from SmartTube; 409 if already paired
```

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
