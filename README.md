# SmartTube Playlist

A small, LAN-only web page that lets anyone on your home network paste a YouTube link and have it play on **SmartTube** on your **Google TV**. Several people can add videos at once; the queue plays through them in order.

No accounts to create and no third-party service to sign up for.

> ⚠️ **This is the `beta` branch.** It carries a device diagnostics report and
> exists for testing on hardware nobody here owns. Asked to help test?
> Start with **[docs/BETA-TESTING.md](docs/BETA-TESTING.md)**.
> For the stable version, use [`main`](https://github.com/Alice-Sabrina-Ivy/smarttube-playlist/tree/main).
>
> **Installing fresh from this branch?** Follow Option A below — the command
> it gives you already points at the `:beta` image. If you're using a
> `docker-compose.yml` instead, check its `image:` line ends in `:beta`, or
> you'll be testing the stable build and the self-test button won't be there.

> **Not affiliated with Google, YouTube, or the SmartTube project.** This is an independent hobby project that talks to software you already run.

![The web UI: a "Now playing" card with thumbnail, progress bar and playback controls, a box for pasting a YouTube link, the upcoming queue, and a TV status panel](docs/screenshot.png)

*Volume and mute work on both tested devices — see [Volume and mute](#volume-and-mute).*

---

## Project status — work in progress

Used daily on the setup it was built for, but not finished, and only proven on the two devices below.

**Verified on real hardware:**

| Device | Pairing | Playback | Wake from off | Volume / mute |
|---|---|---|---|---|
| Google TV Streamer (4K) | ✓ | ✓ | ✓ | ✓ (via HDMI-CEC) |
| Chromecast with Google TV (4K) | ✓ | ✓ | ✓ | ✓ (device's own output — that TV has no CEC) |

Each exercised end to end: pairing, Lounge, pause/resume, auto-advance, wake-from-sleep, screensaver return.

### Will it work with my device?

**One test answers it, in about thirty seconds, before you install anything.** Open Google's own **Google TV** app on your phone, tap the remote icon, and try to control your device with it. That app speaks the exact protocol this service uses. If it can drive your device, so can this. If it can't, nothing here will help.

The reason is a single component: **Android TV Remote Service**, which Google pre-installs on Android TV and Google TV devices. It's the whole requirement.

| Device | Works? | |
|---|---|---|
| **Google TV Streamer (4K)** | ✅ | Verified end to end |
| **Chromecast with Google TV (4K)** | ✅ | Verified end to end |
| **NVIDIA Shield** (all models) | ✅ probably | Same protocol; Home Assistant documents Shield-specific remote behaviour, which only makes sense if it works. Not yet confirmed by us |
| **Android TV / Google TV** with the Play Store — onn., Xiaomi, Sony, Philips, TCL, Hisense, Nokia | ✅ probably | Carries the Remote Service by default |
| **Amazon Fire TV** (any model) | ❌ | SmartTube runs fine there, but Fire OS has **no Android TV Remote Service**, so this app can't drive it. Amazon's own LAN remote protocol exists — it just can't launch a *specific video* into SmartTube, or tell us which app is on screen, and both are load-bearing here. Own one and want to help? [Open an issue](https://github.com/Alice-Sabrina-Ivy/smarttube-playlist/issues) — a couple of measurements would settle it for good |
| **Roku**, incl. Roku TVs | ❌ | Not Android. Can't install SmartTube at all |
| **Apple TV, Samsung (Tizen), LG (webOS)** | ❌ | Not Android |
| **Older Chromecast dongles** (1st–3rd gen, Ultra, Audio) | ❌ | Cast receivers, not Android TV — only *Chromecast with Google TV* qualifies |
| **Carrier boxes** (Bell, Sky, Rogers…) | ❓ | Depends. Many are Android TV underneath and should work; do the phone-app test |
| **AOSP boxes with no Play Store** | ❓ | Probably not, and this is the trap below |

> **Careful:** SmartTube advertises that it works *without* Google services. This app needs more than SmartTube does — it needs Google's Remote Service specifically. So "SmartTube runs on my box" does **not** mean this will. The phone-app test is the one that settles it.

Tried a device that isn't listed? [Open an issue](https://github.com/Alice-Sabrina-Ivy/smarttube-playlist/issues) either way — a No is as useful as a Yes.

Where this is heading: see [Roadmap](#roadmap).

Expect rough edges, occasional breaking changes, and slow replies to issues.

---

## Why this exists

SmartTube is the ad-free YouTube client for Android TV, but it doesn't support Google Cast — guests can't just hit the cast button. The official workaround (TV-code linking inside each guest's YouTube app) is a per-guest setup chore, awkward enough that most people give up and hand over the remote.

This is the easier alternative: one page on your LAN, anyone pastes a link, the video plays. Paste more links and they queue up behind it.

---

## What you'll need

- A **Google TV** device — verified on the Google TV Streamer and Chromecast with Google TV. Other Android TV hardware most likely works but is unconfirmed; see [Project status](#project-status--work-in-progress).
- **[SmartTube](https://smarttubeapp.github.io/)** installed on it. It's a separate project from this one, it isn't on the Play Store, and you install it yourself — **[docs/INSTALL-SMARTTUBE.md](docs/INSTALL-SMARTTUBE.md)** walks you through it.
- A computer that can run Docker and stays on while you're watching — a PC, a Mac, a NAS, a Raspberry Pi (64-bit OS), whatever you have.
- Both on the **same network** as the TV.
- **An internet connection** on that computer — video titles and lengths come from YouTube, and playback commands relay through YouTube's servers. Only the web page itself is LAN-only.
- The TV's IP address (Settings → Network, or your router's device list).

**Give the TV a permanent address** if you can — most routers call this a *DHCP reservation* or *static lease*, usually in the router's device list. Optional, but if the TV's address changes this app stops finding it, and re-pairing means setting `RESET_PAIRING: "1"` in `docker-compose.yml` and restarting (the pairing screen is hidden once a TV is paired).

## Setup

Pick the path that matches you. Both end at the same place.

| | **Option A — Docker Desktop** | **Option B — Docker Engine** |
|---|---|---|
| **For** | Windows or Mac, point-and-click | Linux, NAS, homelab — terminal required |
| **Runs on** | Your everyday computer | An always-on box |
| **Catch** | Only works while that computer is awake and Docker Desktop is running | You're expected to know your way around a shell |

If you just want to try it, start with **Option A**. You can move it to a NAS later — copy the `data` folder across and you won't even need to re-pair.

---

### Option A — Docker Desktop (Windows or macOS)

**1. Install Docker Desktop.**

- [Download for Windows](https://docs.docker.com/desktop/install/windows-install/)
- [Download for Mac](https://docs.docker.com/desktop/install/mac-install/) — pick the Apple Silicon or Intel build to match your Mac

Run the installer and launch it. Wait until the bottom-left says **Engine running**. On Windows it may ask to install WSL 2 and reboot — let it.

> **Stuck on "Docker Engine starting…"?** (Windows) Give it five minutes — the first launch is the slow one. Then, in order:
>
> 1. **Restart the PC.** The installer asks for this and it's easy to skip. It fixes this more often than everything else combined.
> 2. **Check virtualization is on:** Task Manager → Performance → CPU, where **Virtualization** should read *Enabled*. If it says disabled, you'll need to turn it on in your BIOS/UEFI — Intel calls it **VT-x** or *Virtualization Technology*, AMD calls it **SVM Mode**. Docker can't turn it on for you.
> 3. Run `wsl --update` in PowerShell, then restart Docker Desktop.

**2. What you're actually looking at.**

Docker Desktop runs apps in their own sealed box — a *container* — so nothing gets installed into Windows or macOS proper. Its window is mostly a viewer for what's running; there's no "install an app" button in it, which is the bit that trips most people up. You create the container once, by pasting a single line, and from then on it just sits in the **Containers** tab.

**3. Open Docker Desktop's built-in terminal.**

Look in the **bottom-right** of the Docker Desktop window for this button:

<img src="docs/docker-terminal-button.png" alt="A small dark button showing a greater-than sign followed by an underscore" height="26">

Click it. The first time, it asks whether to turn the terminal on — say yes. Then a panel slides up from the bottom.

You don't need to go looking for any folder in it. The next step works from wherever it opens.

**4. Paste this one line and press Enter.**

```
docker run -d --name smarttube-playlist --restart unless-stopped -p 38420:8000 -v smarttube-data:/data ghcr.io/alice-sabrina-ivy/smarttube-playlist:beta
```

It downloads for a minute or two, then prints a long line of letters and numbers. That's the container's ID, and it means it worked. Check the **Containers** tab — `smarttube-playlist` should be there, green, marked *Running*.

That's the whole install.

<details>
<summary>What that line actually says (optional)</summary>

- `-d` — run in the background
- `--name smarttube-playlist` — what it's called in the Containers tab
- `--restart unless-stopped` — bring it back after a reboot
- `-p 38420:8000` — reach it at port 38420 on this computer
- `-v smarttube-data:/data` — keep the TV pairing in storage Docker manages, so it survives updates. Deliberately not a folder on your PC: Windows paths are the single most common thing to get wrong here.
- the last part is the app itself, the `:beta` build

</details>

**5. Open the page.**

<http://localhost:38420>

It should load.

**6. One tick box so it survives a restart.**

Gear icon (**Settings**, top right) → **General** → tick **Start Docker Desktop when you sign in to your computer**.

The app is already set to restart itself, but it can't if Docker Desktop isn't running — and Docker Desktop doesn't start on its own until you tick this. Skip it and everything works perfectly until your next reboot, then quietly doesn't.

Two short things before you pair.

#### Letting phones and tablets reach it

`localhost` only works on the computer running it. Everyone else needs that computer's address on your network:

- **Windows 11:** Settings → **Network & internet** → click **Wi-Fi** (or **Ethernet**) → click your network's name → scroll to **IPv4 address**
- **Windows 10:** Settings → **Network & Internet** → **Status** → **Properties** under your connection → **IPv4 address**
- **Mac:** System Settings → **Network** → your connection → **Details** → **TCP/IP**

Then browse to `http://<that-address>:38420` from any device on the network.

> **Why not `ipconfig`?** Because Docker Desktop itself adds virtual network adapters, and their addresses look completely legitimate in that output — `172.x.x.x`, or `192.168.56.1` if you've ever had VirtualBox. Picking one gives you an address that works on that PC and nowhere else, with nothing to explain why. The Settings path above only ever shows the connection you're actually using.
>
> **Sanity check:** the first three numbers should match your phone's. Check on the phone under Wi-Fi settings — tap your network's name. If yours reads `192.168.1.x`, the computer's should too.

**Windows only:** on first run, Windows Defender Firewall asks whether to allow Docker Desktop — tick **Private networks** and allow it. Missed the prompt and nothing else can connect? *Windows Security → Firewall & network protection → Allow an app through firewall*, find Docker Desktop, tick **Private**.

#### The honest catch with Docker Desktop

It's only alive while that computer is on, awake, and running Docker Desktop. If the machine sleeps mid-video, the TV keeps playing but the queue stops advancing and the page goes dead until it wakes.

Fine for trying it out, or movie night on a desktop that's already on. For something always available, move it to a NAS or Pi — Option B.

#### Changing a setting later

Everything in [docs/CONFIGURATION.md](docs/CONFIGURATION.md) is optional, and you can ignore it until something needs fixing. When it does, add the setting to the same command and run it again:

1. **Containers** tab → the **⋮** menu next to `smarttube-playlist` → **Delete**. Your TV pairing is safe; it lives in the `smarttube-data` storage, not in the container.
2. Paste the command again with the setting added — `-e NAME=VALUE`, before the last line. For example, to wait longer for a slow TV to wake:

```
docker run -d --name smarttube-playlist --restart unless-stopped -p 38420:8000 -v smarttube-data:/data -e WAKE_DELAY=30 ghcr.io/alice-sabrina-ivy/smarttube-playlist:beta
```

Prefer keeping settings in a file? That's what `docker-compose.yml` is for — see [Option B](#option-b--linux-nas-or-homelab). It works on Docker Desktop too; it just needs a terminal opened in the right folder, which is the step this quick path exists to avoid.

**Option A is done — skip Option B entirely and go to [Pair with your TV](#pair-with-your-tv).**

---

### Option B — Linux, NAS, or homelab

Comfortable in a terminal, or putting this on a NAS or Raspberry Pi? The whole path — Docker Engine, Portainer, choosing where data lives, reverse proxies, building from source — is in **[docs/ADVANCED-SETUP.md](docs/ADVANCED-SETUP.md)**.

Come back here for [Pair with your TV](#pair-with-your-tv) once it's running; that part is the same either way.

---

### Pair with your TV

Same for both options. Open the page and work through the cards.

**1. Pair the TV remote** *(required)*

**Turn the TV on first** and leave it on the home screen — pairing can't wake a sleeping TV; that only works once paired. Enter your TV's IP address and submit. A **6-character code** appears on the TV screen — type it into the web UI.

This is the same protocol the official Google TV mobile app uses. The TV will remember this client under *Settings → Apps → See all apps → Show system apps → Android TV Remote Service* if you ever want to revoke it.

**2. Pair with SmartTube** *(optional, strongly recommended)*

On the TV: **SmartTube → Settings → Remote control**. A **12-digit code** appears — paste it into the web UI's *Pair with SmartTube* card. (Older guides call this "Link with TV code"; it moved. Verified on SmartTube 32.10.)

This uses the same YouTube Lounge protocol the phone app's "play on TV" button uses. You'll see the name in logs and the API; the UI just calls it SmartTube, since that's the app you're actually linking to.

This is the same mechanism as "play on TV" in the YouTube mobile app; SmartTube implements the receiver side. Skipping it still works, but you lose real playback position and precise end-of-video detection — see [Honest limitations](#honest-limitations).

**3. Done.** Paste a YouTube URL, hit **Add to queue**.

Your answers persist to the `data` folder, so this is a one-time job.

---

## Using it

- Add a video while nothing's playing → it starts immediately.
- Add while something's playing → it queues behind it (first come, first served).
- When a video ends, the next one starts on its own.
- Reorder anything in the queue with the up/down arrows. Anyone can reorder anything — it's a shared queue on purpose.
- **Skip** advances to the next video, or returns the TV to its screensaver if the queue is empty.
- **Pause/Resume** works whether or not Lounge is paired.
- The page updates live for everyone with it open — adds, removes, skips, and advances show up without refreshing.

If your TV is asleep, adding a video wakes it, waits for it to boot, foregrounds SmartTube, and plays — about 15–20 seconds on most TVs.

**Guests coming over?** Stick a QR code of the page on the fridge and they can scan straight to it: [docs/QR-CODE.md](docs/QR-CODE.md).

---

## Honest limitations

Worth knowing before you commit:

- **Pair with SmartTube if you can.** Without it, auto-advance runs on a duration estimate that any pause or seek on the TV puts out of sync.
- **Livestreams never auto-advance.** No fixed duration, so the queue sits on one until you skip. Marked with a `● LIVE` badge.
- **Leaving SmartTube stops the queue.** Open Netflix or press Home and it stops sending videos. Nothing is lost — go back to SmartTube and press Skip.
- **The queue is in-memory.** Restart the container and it's empty. Your pairing is saved; the queue deliberately isn't.
- **One Lounge session per YouTube account.** Another signed-in TV or Chromecast on the same account can override what you sent.
- **No history, no accounts.** By design — it's a shared room, not a personal library.

For the two-protocol design, why Google Cast wasn't used, and how playback is actually driven, see [How it works](docs/ARCHITECTURE.md).

---

## Volume and mute

The volume and mute buttons reach your TV, soundbar or amplifier over HDMI — nothing to configure, and it worked on both tested devices.

If they do nothing, HDMI-CEC volume control is switched off somewhere. Note your TV probably calls CEC something else entirely (*Anynet+*, *SIMPLINK*, *BRAVIA Sync*): [docs/CONFIGURATION.md](docs/CONFIGURATION.md#volume-and-mute).

---

## Security in one paragraph

**The web UI has no authentication. Anyone who can reach the page can control your TV.** That's the point on a home network — guests shouldn't need an account to queue a song. So keep it on your LAN, and **don't port-forward it**. If you want access from outside, put it behind your existing reverse-proxy auth.

Cross-site requests and DNS rebinding are both blocked, pairing can't be hijacked or wiped remotely, and submissions are rate-limited per IP. The threat model, reverse-proxy setup and how to reset a pairing are all in **[SECURITY.md](SECURITY.md)**.

---

## Configuration

Everything is optional and the defaults work — most people change nothing. Set anything you do want as an environment variable in `docker-compose.yml`.

Every setting, with what it's for: **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**.

---

## Updating

In the folder with `docker-compose.yml`:

```bash
docker compose pull && docker compose up -d
```

Your pairing is kept. Stopping and removing it: [docs/ADVANCED-SETUP.md](docs/ADVANCED-SETUP.md#updating-stopping-removing).

---

## Something not working?

**[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** covers the common ones — the image not downloading, pairing failing, the page not opening on phones, volume buttons doing nothing, and wrong video lengths.

---

## More documentation

The README covers getting it running. Everything else lives here:

| | |
|---|---|
| **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** | Symptoms and fixes, from install errors to playback oddities |
| **[docs/BETA-TESTING.md](docs/BETA-TESTING.md)** | Testing on hardware nobody here owns — what to run, what to report |
| **[docs/INSTALL-SMARTTUBE.md](docs/INSTALL-SMARTTUBE.md)** | Installing SmartTube on the TV, and which devices can run it |
| **[docs/QR-CODE.md](docs/QR-CODE.md)** | Make a QR code so guests scan straight to the page |
| **[docs/ADVANCED-SETUP.md](docs/ADVANCED-SETUP.md)** | Linux, NAS and homelab installs: Docker Engine, Portainer, where data lives, port binding, reverse proxies, building from source |
| **[SECURITY.md](SECURITY.md)** | Threat model, what the no-auth design means, DNS-rebinding and CSRF protection, reverse proxies, resetting a pairing |
| **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** | Every environment variable, volume control, timezones |
| **[docs/API.md](docs/API.md)** | HTTP endpoints and the SSE stream, for webhooks and Home Assistant |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | How it works internally, module layout, running from source, contributing |
| **[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)** | Licences of the bundled dependencies — read before redistributing the image |

---

## Roadmap

No dates — spare-time project.

- **Verified support for other sticks and boxes** — Nvidia Shield, onn./Xiaomi, carrier boxes. (Not Fire TV: Fire OS has no Android TV Remote Service, so it is out of reach regardless of SmartTube supporting it.)
- **Volume where HDMI-CEC isn't available** — those users currently get none.

Got one of the untested devices? [Open an issue](https://github.com/Alice-Sabrina-Ivy/smarttube-playlist/issues) either way.

---

## License

This project's own source is **MIT** — see [LICENSE](LICENSE). Built on [tronikos/androidtvremote2](https://github.com/tronikos/androidtvremote2) and [FabioGNR/pyytlounge](https://github.com/FabioGNR/pyytlounge).

The prebuilt container image bundles pyytlounge (GPL-3.0), so **the image as a whole is distributed under GPL-3.0-or-later**, even though this repository's source is MIT. Redistributing the image? Read [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) first.

Not affiliated with, endorsed by, or connected to Google, YouTube, or the SmartTube project. "SmartTube" and "YouTube" are the marks of their respective owners; they're used here only to describe what this software talks to.
