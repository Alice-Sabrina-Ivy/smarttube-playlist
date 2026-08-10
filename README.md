# SmartTube Playlist

A small, LAN-only web page that lets anyone on your home network paste a YouTube link and have it play on **SmartTube** on your **Google TV**. Several people can add videos at once; the queue plays through them in order.

No Home Assistant. No ADB. No Google Cast. No accounts, no sign-in, no cloud service in the middle.

> **Not affiliated with Google, YouTube, or the SmartTube project.** This is an independent hobby project that talks to software you already run.

---

## Project status — work in progress

This works, and it's used daily on the setup it was built for. It is not finished, and it has only ever been proven on one kind of hardware.

**What's supported today:**

- **Google TV devices only.** That's the only hardware this has been developed and tested against. The underlying Android TV Remote v2 protocol is common to Android TV generally, so other Android TV boxes and sticks — Nvidia Shield, Fire TV, Chromecast with Google TV, onn. and Xiaomi boxes — may well work. Nobody has verified it. If you try one, please open an issue and say what happened, working or not.
- **Volume control on Denon and Marantz receivers only**, via their legacy port-23 protocol.

**Coming in future versions:**

- Support for other streaming sticks and boxes, tested rather than assumed.
- Volume support for more AVR brands beyond Denon/Marantz.

Expect rough edges, occasional breaking changes between versions, and issues that take a while to get answered.

---

## Why this exists

SmartTube is the ad-free YouTube client for Android TV, but it doesn't support Google Cast — guests can't just hit the cast button. The official workaround (TV-code linking inside each guest's YouTube app) is a per-guest setup chore, awkward enough that most people give up and hand over the remote.

This is the easier alternative: one page on your LAN, anyone pastes a link, the video plays. Paste more links and they queue up behind it.

---

## What you'll need

- A **Google TV** device with **[SmartTube](https://github.com/yuliskov/SmartTube)** installed. Other Android TV hardware is untested — see [Project status](#project-status--work-in-progress).
- A computer that can run Docker and stays on while you're watching — a PC, a Mac, a NAS, a Raspberry Pi, whatever you have.
- Both on the **same network** as the TV.
- The TV's IP address (Settings → Network, or your router's device list).

**Pin the TV's IP** with a DHCP reservation in your router if you can. If the TV's address changes you'll have to pair again.

---

## Setup

Pick the path that matches you. Both end at the same place.

| | **Option A — Docker Desktop** | **Option B — Docker Engine** |
|---|---|---|
| **For** | Windows or Mac, point-and-click | Linux, NAS, homelab, terminal-comfortable |
| **Runs on** | Your everyday computer | An always-on box |
| **Catch** | Only works while that computer is awake and Docker Desktop is running | You're expected to know your way around a shell |

If you just want to try it, start with **Option A**. You can move it to a NAS later — copy the `data` folder across and you won't even need to re-pair.

---

### Option A — Docker Desktop (Windows or macOS)

**1. Install Docker Desktop.**

- [Download for Windows](https://docs.docker.com/desktop/install/windows-install/)
- [Download for Mac](https://docs.docker.com/desktop/install/mac-install/) — pick the Apple Silicon or Intel build to match your Mac

Run the installer, then launch Docker Desktop. Wait until the whale icon settles and the bottom-left corner says **Engine running**. On Windows it may ask to install WSL 2 and reboot — let it.

**2. Make a folder for it.**

Anywhere you like, e.g. `Documents\smarttube-playlist`. Everything lives here, including your TV pairing.

**3. Save the configuration file into that folder.**

Download [`docker-compose.yml`](docker-compose.yml) (click **Raw**, then save) into the folder you just made.

> **Windows:** if you save from Notepad, set *Save as type* to **All Files** so you don't end up with `docker-compose.yml.txt`. Docker will not find a file with the extra `.txt`.

You don't need to edit it. The defaults work as-is.

**4. Open a terminal in that folder.**

- **Windows:** open the folder in File Explorer, right-click a blank area, choose **Open in Terminal**.
- **Mac:** right-click the folder, choose **Services → New Terminal at Folder**. (If it's not there: enable it in System Settings → Keyboard → Keyboard Shortcuts → Services.)

**5. Start it.**

```bash
docker compose up -d
```

First run downloads the image — a minute or two. `-d` means it keeps running in the background, and restarts automatically whenever Docker Desktop starts.

**6. Open the page.**

<http://localhost:38420>

Now skip to [Pair with your TV](#pair-with-your-tv).

#### Letting phones and tablets reach it

`localhost` only works on the computer running it. For everyone else on the network you need that computer's LAN IP:

- **Windows:** `ipconfig` → *IPv4 Address* under your active adapter
- **Mac:** System Settings → Network → your connection → *Details*

Then browse to `http://<that-ip>:38420` from any device on the LAN — e.g. `http://192.168.1.50:38420`.

**Windows only:** the first time you run it, Windows Defender Firewall pops up asking whether to allow Docker Desktop. Tick **Private networks** and allow it. If you missed the prompt and other devices can't connect, go to *Windows Security → Firewall & network protection → Allow an app through firewall*, find Docker Desktop, and make sure **Private** is ticked.

#### The honest catch with Docker Desktop

The service is only alive while that computer is powered on, awake, and running Docker Desktop. If the machine sleeps mid-video, playback on the TV continues but the queue stops advancing and the web page goes dead until it wakes.

Fine for trying it out or for movie night on a desktop that's already on. For something that just works whenever guests are over, move it to a NAS or a Pi using Option B.

---

### Option B — Docker Engine (Linux, NAS, homelab)

Assumes Docker Engine and the Compose plugin are already installed.

```bash
mkdir -p /opt/smarttube-playlist && cd /opt/smarttube-playlist
curl -O https://raw.githubusercontent.com/Alice-Sabrina-Ivy/smarttube-playlist/main/docker-compose.yml
# optional: edit docker-compose.yml to set the bind address or DENON_HOST
docker compose up -d
```

Then open `http://<host-ip>:38420/` and continue to [Pair with your TV](#pair-with-your-tv).

The image is multi-arch — `linux/amd64` and `linux/arm64` both work, so Pi and ARM NAS boxes are covered.

#### Notes for this path

- **Data lives in `./data`** next to the compose file: pairing certificate, TV address, Lounge token. Back it up and you never re-pair. It's created on first run and chowned to UID 1000 by the entrypoint, which runs as root just long enough to do that and then drops privileges via `gosu`.
- **Port binding.** `38420:8000` binds all interfaces. On a LAN-only box that's the point. If the host has anything internet-facing, pin it: `"192.168.1.50:38420:8000"`. There is no authentication — see [Security](#security).
- **Reverse proxy.** If you front it with Caddy/nginx/Traefik, pass through `text/event-stream` unbuffered or the live-updating UI will stall. In nginx that means `proxy_buffering off;` and `proxy_read_timeout` well above the default on `/api/events`.

#### Deploying with Portainer

Portainer flags CLI-deployed stacks as "limited" because it has no record of the compose source, so deploy through its own UI if you want full control:

**Stacks → Add stack →** name it `smarttube-playlist` → build method **Web editor** → paste the contents of `docker-compose.yml` → **Deploy**.

Because the compose file references a published image, Portainer pulls it directly — no source tree needed on the host. To update later, hit **Pull and redeploy** on the stack.

If a CLI-deployed container is already running, remove it first so the stack doesn't fight it for the name:

```bash
docker stop smarttube-playlist && docker rm smarttube-playlist
```

#### Build from source

Only needed if you're modifying the code or want to avoid the prebuilt image:

```bash
git clone https://github.com/Alice-Sabrina-Ivy/smarttube-playlist
cd smarttube-playlist
# in docker-compose.yml: comment out `image:`, uncomment `build: .`
docker compose up -d --build
```

---

### Pair with your TV

Same for both options. Open the page and work through the two cards.

**1. Pair the TV remote** *(required)*

Enter your TV's IP address and submit. A **6-character code** appears on the TV screen — type it into the web UI.

This is the same protocol the official Google TV mobile app uses. The TV will remember this client under *Settings → Apps → See all apps → Show system apps → Android TV Remote Service* if you ever want to revoke it.

**2. Pair YouTube Lounge** *(optional, strongly recommended)*

On the TV: **SmartTube → Settings → "Link with TV code"**. A **12-digit code** appears. Paste it into the web UI's *Pair YouTube Lounge* card.

This is the same mechanism as "play on TV" in the YouTube mobile app; SmartTube implements the receiver side. Skipping it still works, but you lose real playback position and precise end-of-video detection — see [Honest limitations](#honest-limitations).

**3. Done.** Paste a YouTube URL, hit **Add to queue**.

Both pairings persist to the `data` folder, so this is a one-time job.

---

## Using it

- Add a video while nothing's playing → it starts immediately.
- Add while something's playing → it queues behind it (first come, first served).
- When a video ends, the next one starts on its own.
- Reorder anything in the queue with the up/down arrows. Anyone can reorder anything — it's a shared queue on purpose.
- **Skip** advances to the next video, or returns the TV to its screensaver if the queue is empty.
- **Pause/Resume** works whether or not Lounge is paired.
- The page updates live for everyone with it open — adds, removes, skips, and advances show up without refreshing.

If your TV is asleep, adding a video wakes it, waits for it to boot, foregrounds SmartTube, and plays. That whole sequence takes about 15–20 seconds on most TVs.

---

## Honest limitations

The service talks to the TV over two channels:

1. **Android TV Remote v2** (port 6466) — waking the TV, foregrounding SmartTube, watching which app is in front. Always required.
2. **YouTube Lounge** (HTTPS to youtube.com) — pushing videos, reading real playback position, pause/play. Optional but strongly recommended.

Things worth knowing before you commit:

- **With Lounge paired:** auto-advance fires on the actual end of the video, the progress bar shows real position, and pausing on the TV remote is mirrored in the web UI within a second or two.
- **Without Lounge:** auto-advance runs off a duration estimate scraped from the YouTube page. Any pause or seek on the TV puts it out of sync. Pause/play fall back to the remote's `MEDIA_PAUSE`/`MEDIA_PLAY` keys.
- **Livestreams never auto-advance.** They have no fixed duration, so the queue sits on them until you skip manually. The UI marks them with a `● LIVE` badge.
- **Leaving SmartTube stops the queue.** Open Netflix or hit Home and the queue stops sending videos. Nothing is lost — go back to SmartTube and press Skip to pick up again.
- **The queue is in-memory.** Restart the container and it's empty. Pairing is persisted; the queue deliberately isn't.
- **One Lounge session per YouTube account.** If another signed-in TV or Chromecast on the same account starts something, it can override what we sent.
- **No history and no accounts.** By design — it's a shared room, not a personal library.
- **Why not Cast?** Earlier versions used pychromecast. On current Google TV firmware any unauthenticated cast client makes the TV launch its Default Media Receiver (the blue cast screen), interrupting whatever's playing. The Google Home app dodges this with account auth that third-party tools can't replicate. Lounge never triggers a cast UI.

---

## Security

**The web UI has no authentication. Anyone who can reach the page can control your TV.** On a home LAN that's the whole point — guests shouldn't need an account to queue a song.

What that means in practice:

- **Don't port-forward it.** No auth means anyone on the internet could take over your TV. If you want remote access, put it behind your existing reverse-proxy auth (Authelia, Tailscale, basic auth — anything).
- **Pin the bind interface** if the host has any internet-routable address. See the `ports:` comment in `docker-compose.yml`.
- **Guest Wi-Fi counts as "reachable."** If your guest network can route to your main LAN, guests can reach this. That is usually what you want here, but decide deliberately.
- **Re-pairing needs filesystem access, on purpose.** There's no unpair or reset endpoint, so nobody on the LAN can wipe your pairing as a prank or a denial of service. To re-pair, delete `cert.pem`, `key.pem`, and `config.json` (TV remote) or `lounge.json` (Lounge) from the `data` folder and restart.
- **Cross-origin POSTs are blocked** by a CSRF check, so a random website you visit can't quietly drive your TV in the background. Non-browser clients (curl, webhooks) are unaffected.
- **Requests are rate-limited per IP** (10s between submissions by default) so one person can't flood the queue.

---

## Configuration

All optional, all set as environment variables in `docker-compose.yml`. Defaults are sensible; most people change none of these.

| Variable | Default | What it does |
|---|---|---|
| `CLIENT_NAME` | `SmartTube Playlist` | Name shown on the TV during pairing |
| `SMARTTUBE_PACKAGE` | `org.smarttube.stable` | Set to `org.smarttube.beta` for the beta build |
| `LOG_LEVEL` | `INFO` | Set `DEBUG` when diagnosing something |
| `RATE_LIMIT_SECONDS` | `10` | Per-IP cool-down between queue submissions |
| `WAKE_DELAY` | `15.0` | Minimum seconds to wait after `POWER` before launching. A floor, not a timeout — instant-on TVs report "on" in ~1s while still booting |
| `WAKE_TIMEOUT` | `30.0` | Give up waiting for the TV to report on |
| `WAKE_POLL` | `0.5` | How often to re-check while waking |
| `SCREENSAVER_PACKAGES` | `com.google.android.apps.tv.dreamx,com.google.android.backdrop` | Packages treated as screensavers; these swallow launch intents, so they get dismissed first |
| `SCREENSAVER_DISMISS_KEY` | `HOME` | Key that dismisses the screensaver. `BACK` also works. `DPAD_CENTER` and `WAKEUP` are **not** supported — the remote protocol drops them silently |
| `IDLE_KEYCODE` | `HOME,BACK` | Keys sent when Skip empties the queue; lands on the ambient screensaver. `POWER` turns the display off, `HOME` stops at the launcher, empty disables it |
| `IDLE_KEYCODE_DELAY` | `0.6` | Seconds between those keys |
| `DEFAULT_DURATION_S` | `600` | Assumed length when the metadata scrape fails |
| `METADATA_TIMEOUT_S` | `5.0` | YouTube watch-page fetch timeout |
| `DENON_HOST` | (unset) | Denon/Marantz AVR IP. Set it and volume buttons appear in the UI |
| `DATA_DIR` | `/data` | Where pairing state is stored inside the container. Change the volume mount instead |

### Volume control

Google TV devices don't expose usable volume over the remote protocol — the physical remote's volume buttons ride HDMI-CEC straight to your amp, which a LAN service can't imitate. The way around that is to talk to the amp directly.

**Denon and Marantz receivers** are supported today: set `DENON_HOST` to the receiver's IP and the UI grows volume up/down/mute buttons that speak its legacy port-23 protocol (present on essentially every Denon since the early 2010s). Without it, the volume buttons stay hidden.

Other AVR brands aren't supported yet — that's on the roadmap. If you'd like yours added, open an issue naming the model and how it takes network commands.

---

## Troubleshooting

**Other devices can't open the page (Docker Desktop).** Firewall. See [Letting phones and tablets reach it](#letting-phones-and-tablets-reach-it). Confirm it works at `http://localhost:38420` on the host first — if that fails, it's not the firewall.

**Pairing fails immediately.** Confirm the TV is on the same network and ports 6466/6467 are reachable. Some Google TV devices have the Remote Service enabled but firewalled until a power cycle.

**`InvalidAuth` on startup.** The pairing certificate was rejected — the TV revoked it, or the files got out of sync. Delete `cert.pem`, `key.pem`, and `config.json` from the `data` folder, restart, and pair again.

**HTTP 500 with `PermissionError: '/data/cert.pem'`.** The data directory isn't writable by the container's user. The entrypoint chowns it to UID 1000 at startup, so this normally self-heals — unless you added a `user:` line to `docker-compose.yml`, which prevents the chown. Either remove that line or pre-create the directory with the right owner: `sudo chown -R 1000:1000 ./data`.

**The video opens in stock YouTube instead of SmartTube.** Either SmartTube isn't installed, or stock YouTube is registered as the default handler for YouTube links. Open SmartTube once manually and pick "always" if Android offers.

**TV wakes but nothing plays.** Raise `WAKE_DELAY`. SmartTube has to be foregrounded *after* the TV is genuinely awake, and some TVs report themselves ready well before they are.

**The queue stops advancing.** Check SmartTube is still the foreground app. Backing out of it stops the queue by design. Re-open SmartTube and hit Skip.

**Auto-advance is early or late.** You're in the no-Lounge fallback, running on a duration estimate. Pair Lounge to fix it properly, or use Skip to realign.

**A video shows a 10:00 duration that's obviously wrong.** The metadata scrape failed and fell back to `DEFAULT_DURATION_S`. Look for `metadata fetch failed` in the logs — usually transient. Playback is fine; only the auto-advance timing is off.

**Reading the logs:**

```bash
docker compose logs -f
```

On Docker Desktop you can also click the container and open the **Logs** tab.

---

## API

Everything the web UI does is a plain HTTP call, so webhooks and Home Assistant automations can drive it too.

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
POST   /api/lounge/pair    {code}         12-digit code from SmartTube
```

Every SSE event carries a complete snapshot under `state`, plus a `type` naming the transition. Clients replace their whole view from each message — there's no diffing to implement.

Queue a video from anywhere on the LAN:

```bash
curl -X POST http://<host>:38420/api/queue \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

---

## How it works

1. A persistent TLS connection to the TV on port 6466 (Android TV Remote v2) handles waking, launching, and app-foreground observation. When Lounge is paired, an HTTPS session to YouTube's Lounge endpoint runs alongside it.
2. Queuing a video fetches its YouTube watch page server-side and pulls title, channel, duration, and livestream status out of the embedded `ytInitialPlayerResponse` JSON.
3. Starting a video: if the TV is off, send `POWER` and wait for it to come up. If a screensaver is in front, dismiss it. If SmartTube isn't foreground, launch it via `market://launch`. Then send **exactly one** play signal — `setPlaylist` over Lounge when it's connected, otherwise a `vnd.youtube.launch://` deep link through the remote. Sending both makes SmartTube load the video twice, audibly.
4. Lounge pushes state changes (now playing, position, play/pause, end of video). Those drive auto-advance, mirror pause state into the UI, and feed the progress bar.
5. Without Lounge, an `asyncio` timer sized to the scraped duration drives auto-advance instead.
6. If the foreground app changes away from SmartTube, the queue stops sending videos.
7. Every queue mutation is broadcast as a full snapshot to all connected SSE clients.

---

## Roadmap

Rough order, no dates — this is a spare-time project.

- **Verified support for other streaming sticks and boxes** — Fire TV, Nvidia Shield, Chromecast with Google TV, onn./Xiaomi. Some may already work; none are tested.
- **More AVR brands** for volume control — Yamaha, Onkyo, Sony and friends.

Got one of the untested devices? Reports either way are genuinely useful — open an issue.

---

## Development

Running it directly, without Docker:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
DATA_DIR=./data .venv/bin/python -m uvicorn app:app --port 8000 --reload
```

Python 3.12. The frontend is a single `index.html` — vanilla HTML/CSS/JS, no build step, no bundler. Edit and reload.

Module layout:

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

Bug reports are welcome, especially with logs at `LOG_LEVEL=DEBUG`. This is a personal project maintained in spare time — issues and PRs may sit for a while.

---

## License

This project's own source is **MIT** — see [LICENSE](LICENSE).

It depends on third-party packages under their own licenses:

- [tronikos/androidtvremote2](https://github.com/tronikos/androidtvremote2) — Apache-2.0
- [FabioGNR/pyytlounge](https://github.com/FabioGNR/pyytlounge) — **GPL-3.0**

A note on that last one: pyytlounge's PyPI classifier claims MIT, but the LICENSE file shipped inside the package *and* in the upstream repository is the GNU GPL v3. Where a classifier and a license file disagree, the license file governs. A container image built from this repository bundles that GPL-3.0 code, so the image as distributed is a combined work carrying GPL-3.0 obligations — even though this repository's own source stays MIT and you can use it under MIT terms.

Not affiliated with, endorsed by, or connected to Google, YouTube, or the SmartTube project. "SmartTube" and "YouTube" are the marks of their respective owners; they're used here only to describe what this software talks to.
