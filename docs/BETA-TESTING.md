# Beta testing on new hardware

← back to the [README](../README.md)

Thanks for helping. This app is verified on a Google TV Streamer and a Chromecast with Google TV, and on nothing else — so your device is genuinely new information, whether it works or not.

You don't need ADB or a terminal on the TV. The beta build collects most of the answers itself.

## Get the beta build

Change one line in your `docker-compose.yml`:

```yaml
image: ghcr.io/alice-sabrina-ivy/smarttube-playlist:beta
```

Then `docker compose pull && docker compose up -d`. Your pairing is kept.

Beta builds report their version as `1.01` so it's obvious which one you're running.

## The report

The status card at the bottom of the page has a **📋 Copy diagnostics** button. It produces a JSON blob describing what your device reported.

It contains **no credentials** — no pairing certificate, no private key, no YouTube token. It *does* include your TV's local address (like `192.168.1.42`), because that's useful for diagnosis. That's a private address that means nothing outside your own network, but if you'd rather not share it, delete the `host` line before sending.

The most useful thing in there is `events` — a log of what app was in the foreground over time. That's how we learn what your device calls its screensaver and its launcher, which is the single most common reason playback fails on unfamiliar hardware.

## What to do, in order

Each step is useful even if a later one fails, so please report as you go rather than waiting until the end.

**1. Pair it.** Enter the TV's address, then the 6-character code. If pairing fails outright, stop and say so — that's the load-bearing step.

**2. Let it go idle.** Leave the device alone until the screensaver appears. Don't touch the remote. Then open the page from your phone or another computer and hit **Copy diagnostics**.

This is the important one: we need to see what package your screensaver reports as. Screensavers silently swallow app-launch requests, so if the app doesn't recognise yours, videos won't start.

**3. Play something.** Paste a YouTube link and hit Add to queue. Report whether it plays, and roughly how long it took.

**4. Try the controls.** Pause, resume, skip. Then volume up, down, and mute — report whether the volume actually changed, since the app can't tell.

**5. Let the TV sleep, then queue a video.** It should wake by itself and start playing. Report whether it woke.

**6. Copy diagnostics one last time** and send it over.

## Known device-specific settings

If something doesn't work, these are the usual culprits — all on the device, none in the app.

### NVIDIA Shield

- **The TV won't wake.** Settings → **Remotes & accessories** → **Simplified wake buttons**, and disable *"SHIELD 2019 Remote: Wake on power and Netflix buttons only"* and *"Controllers: Wake on NVIDIA or logo buttons only"*. With these on, the Shield ignores a power command sent over the network, so waking cannot work.
- **Volume does nothing.** Settings → Device Preferences → **Display & Sound → Volume control**. The Shield offers three modes, and only two can work here:
  - **HDMI-CEC** (the default on 2019 models) — works, provided the TV or receiver honours CEC volume. Many TVs don't.
  - **Digital** (the default on 2015/2017 models) — works; the Shield attenuates its own output.
  - **IR** — **cannot work.** The infrared emitter is in the physical remote, so a command sent over the network has no way to reach your amplifier. Switch to CEC or digital.
- **Older firmware.** On SHIELD Experience before 9.2, the remote can stop responding for about 60 seconds after waking from sleep, which is longer than this app waits. Report your version from Settings → Device Preferences → About.
- **Wrong SmartTube package.** Older SmartTube installs used a different application ID. If the report's `device.current_app` shows something like `com.teamsmart.videomanager.tv` while SmartTube is open, tell us — it means the app is looking for the wrong package name, and it's a one-line fix in your compose file.

### Any device

- **Volume does nothing** — HDMI-CEC volume control is usually switched off somewhere in the chain. See [CONFIGURATION.md](CONFIGURATION.md#volume-and-mute); note your TV probably calls CEC something else (*Anynet+*, *SIMPLINK*, *BRAVIA Sync*).
- **The report shows a `current_app is empty` warning** — foreground detection isn't working, which quietly breaks several things at once. Worth reporting immediately.

## Sending it back

Paste the report, plus your device model and its software version. If something failed, say what you saw on the TV screen — that's the part the app can't observe.
