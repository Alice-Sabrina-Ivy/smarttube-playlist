# SmartTube Playlist

A small, LAN-only web page that lets anyone on your home network paste a YouTube link and have it play on **SmartTube** on your **Google TV**. Several people can add videos at once; the queue plays through them in order.

No Home Assistant. No ADB. No Google Cast. No accounts, no sign-in, no cloud service in the middle.

> **Not affiliated with Google, YouTube, or the SmartTube project.** This is an independent hobby project that talks to software you already run.

![The SmartTube Playlist web UI: a "Now playing" card showing the current video's thumbnail, title, channel, progress bar and elapsed/remaining time, with play, pause, skip, clear and seek controls; below it a field for pasting a YouTube URL, the upcoming queue, and a status panel showing the connected TV, its power state and playback sync](docs/screenshot.png)

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
- **Port binding.** `38420:8000` binds all interfaces. On a LAN-only box that's the point. If the host has anything internet-facing, pin it: `"192.168.1.50:38420:8000"`. There is no authentication — see [SECURITY.md](SECURITY.md).
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

Worth knowing before you commit:

- **Pair Lounge if you can.** Without it, auto-advance runs on a duration estimate that any pause or seek on the TV puts out of sync.
- **Livestreams never auto-advance.** No fixed duration, so the queue sits on one until you skip. Marked with a `● LIVE` badge.
- **Leaving SmartTube stops the queue.** Open Netflix or press Home and it stops sending videos. Nothing is lost — go back to SmartTube and press Skip.
- **The queue is in-memory.** Restart the container and it's empty. Your pairing is saved; the queue deliberately isn't.
- **One Lounge session per YouTube account.** Another signed-in TV or Chromecast on the same account can override what you sent.
- **No history, no accounts.** By design — it's a shared room, not a personal library.

For the two-protocol design, why Google Cast wasn't used, and how playback is actually driven, see [How it works](docs/ARCHITECTURE.md).

---

## Security in one paragraph

**The web UI has no authentication. Anyone who can reach the page can control your TV.** That's the point on a home network — guests shouldn't need an account to queue a song. So keep it on your LAN, and **don't port-forward it**. If you want access from outside, put it behind your existing reverse-proxy auth.

Cross-site requests and DNS rebinding are both blocked, pairing can't be hijacked or wiped remotely, and submissions are rate-limited per IP. The threat model, reverse-proxy setup and how to reset a pairing are all in **[SECURITY.md](SECURITY.md)**.

---

## Configuration

Everything is optional — the defaults work, and most people change nothing. The ones people actually touch:

| Variable | Default | What it does |
|---|---|---|
| `SMARTTUBE_PACKAGE` | `org.smarttube.stable` | Set to `org.smarttube.beta` for the beta build |
| `DENON_HOST` | (unset) | Denon/Marantz AVR IP — set it and volume buttons appear |
| `WAKE_DELAY` | `15.0` | Raise it if the TV wakes but the video doesn't start |
| `LOG_LEVEL` | `INFO` | Set `DEBUG` when something's wrong |
| `RESET_PAIRING` | (unset) | Set to `1` and restart to clear pairing and start over |

Every setting, volume control and reverse-proxy notes: **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**.

---

## Troubleshooting

**Other devices can't open the page (Docker Desktop).** Firewall. See [Letting phones and tablets reach it](#letting-phones-and-tablets-reach-it). Confirm it works at `http://localhost:38420` on the host first — if that fails, it's not the firewall.

**Pairing fails immediately.** Confirm the TV is on the same network and ports 6466/6467 are reachable. Some Google TV devices have the Remote Service enabled but firewalled until a power cycle.

**`InvalidAuth` on startup.** The pairing certificate was rejected — the TV revoked it, or the files got out of sync. Set `RESET_PAIRING: "1"` in `docker-compose.yml`, restart, pair again, then take the flag back out.

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

## More documentation

The README covers getting it running. Everything else lives here:

| | |
|---|---|
| **[SECURITY.md](SECURITY.md)** | Threat model, what the no-auth design means, DNS-rebinding and CSRF protection, reverse proxies, resetting a pairing |
| **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** | Every environment variable, volume control, timezones |
| **[docs/API.md](docs/API.md)** | HTTP endpoints and the SSE stream, for webhooks and Home Assistant |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | How it works internally, module layout, running from source, contributing |

---

## Roadmap

Rough order, no dates — this is a spare-time project.

- **Verified support for other streaming sticks and boxes** — Fire TV, Nvidia Shield, Chromecast with Google TV, onn./Xiaomi. Some may already work; none are tested.
- **More AVR brands** for volume control — Yamaha, Onkyo, Sony and friends.

Got one of the untested devices? Reports either way are genuinely useful — open an issue.

---

## License

This project's own source is **MIT** — see [LICENSE](LICENSE).

It depends on third-party packages under their own licenses:

- [tronikos/androidtvremote2](https://github.com/tronikos/androidtvremote2) — Apache-2.0
- [FabioGNR/pyytlounge](https://github.com/FabioGNR/pyytlounge) — **GPL-3.0**

A note on that last one: pyytlounge's PyPI classifier claims MIT, but the LICENSE file shipped inside the package *and* in the upstream repository is the GNU GPL v3. Where a classifier and a license file disagree, the license file governs. A container image built from this repository bundles that GPL-3.0 code, so the image as distributed is a combined work carrying GPL-3.0 obligations — even though this repository's own source stays MIT and you can use it under MIT terms.

Not affiliated with, endorsed by, or connected to Google, YouTube, or the SmartTube project. "SmartTube" and "YouTube" are the marks of their respective owners; they're used here only to describe what this software talks to.
