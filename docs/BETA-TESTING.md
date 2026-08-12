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

## The easy way: one button

The status card at the bottom of the page has a **🔬 Run device self-test** button. Press it and it works through everything below by itself — about five minutes, with a progress list and a countdown so you can see it moving. It waits properly on the slow parts rather than guessing. When it finishes, **📄 Show the report** reveals the JSON and **📋 Copy report** puts it on your clipboard.

Two things worth knowing before you press it:

- **It drives your TV.** It starts a 19-second test video, tries pause, resume and volume, then hands the TV back to the screensaver. Don't run it while someone's watching something — it skips those steps if the queue is busy, but it's simpler to run it on an idle TV.
- **To test waking, put the device to sleep first** — then run it from your phone. If you use a separate box (Shield, Chromecast, Apple TV…), **send the box to sleep, not just the TV**: switching off the picture leaves the box awake, the app sees it as on, and both wake checks skip themselves. On a Shield, hold the power button on its remote or use the sleep tile, and check the box's own light. Nothing in the self-test ever puts your device to sleep for you — that's deliberate, since a device that ignores wake commands is exactly what we're looking for and we'd have no way to bring it back.

When it finishes, the page shows **a few short questions** — things the app genuinely cannot see, like whether the volume actually changed on your speakers. Answer what you can before copying; your answers go into the report automatically. These matter more than they look: several results are ambiguous without them, and "the app saw no change" is equally consistent with "it worked perfectly" and "it can never work."

## The passive report

The **📋 Copy diagnostics** button next to it produces the same information without touching your TV at all. Use it if you'd rather not have anything moved, or to capture a moment — see step 2 below.

It contains **no credentials** — no pairing certificate, no private key, no YouTube token. It *does* include your TV's local address (like `192.168.1.42`), because that's useful for diagnosis. That's a private address that means nothing outside your own network, but if you'd rather not share it, delete the `host` line before sending.

The most useful thing in there is `events` — a log of what app was in the foreground over time. That's how we learn what your device calls its screensaver and its launcher, which is the single most common reason playback fails on unfamiliar hardware.

## What to do, in order

Three passes. Each one is useful on its own, so send what you have rather than waiting until the end.

**1. Pair it.** Enter the TV's address, then the 6-character code. If pairing fails outright, stop and say so — nothing else can work without it.

**1b. Then pair with SmartTube too.** On the TV: **SmartTube → Settings → Remote control**, which shows a 12-digit code. Type it into the *Pair with SmartTube* card on the page.

Please don't skip this. It's optional for daily use but not for testing: without it the app can't see the real playback position, so three of the twelve checks can't run at all and two more come back as "started it, no idea what happened next." It takes about a minute and roughly doubles what the report can tell us.

**2. Catch the screensaver.** Leave the device alone until the screensaver appears, don't touch the remote, then open the page from your phone and hit **📋 Copy diagnostics** (the passive one — it won't disturb the screensaver).

This is the single most valuable thing you can send. We need to know what package your screensaver reports as: screensavers silently swallow app-launch requests, so if the app doesn't recognise yours, videos never start and nothing errors.

**3. Run the self-test twice.**

- Once with the **device asleep** (send it to sleep with its own remote first — the box, not just the TV picture — then press the button from your phone). This is the only way to answer whether your device wakes over the network, and which keycode does it. It's also the slowest run, because it waits on each wake attempt for as long as the app itself would.
- Once with the **device on and idle**. This covers playback, pause/resume, volume, and where the device lands when the app hands the TV back.
- If you can spare a third: leave it untouched until the **screensaver** appears, then run the self-test (not Copy diagnostics) from your phone. That's the only way we learn what your screensaver is called, and an unrecognised one is the most common reason videos never start on a device we've never seen.

Send both reports. Then tell us anything you saw on the screen that the app couldn't — that's the half we're missing.

## Known device-specific settings

If something doesn't work, these are the usual culprits — all on the device, none in the app.

### NVIDIA Shield

- **The TV won't wake.** Settings → **Remotes & accessories** → **Simplified wake buttons**, and disable *"SHIELD 2019 Remote: Wake on power and Netflix buttons only"* and *"Controllers: Wake on NVIDIA or logo buttons only"*. With these on, the Shield ignores a power command sent over the network, so waking cannot work.

  If it still won't wake with those off, the Shield may be ignoring the key itself rather than blocking it. The wake key is configurable — add `WAKE_KEYCODE=WAKEUP` (or `TV_POWER`) to the `environment:` block in your compose file and restart. Please report which one worked, including "none of them"; that answer is the whole reason this is a setting rather than a constant.
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

Paste the reports, plus your device model and its software version. If something failed, say what you saw on the TV screen — that's the part the app can't observe, and usually the part that explains the rest.

A run where most probes say `skipped` is normal, not a failure: several only apply when the TV is off, and others stand down when something's already playing. `unmeasurable` is different — it means the app couldn't see well enough to judge, and it's worth mentioning.
