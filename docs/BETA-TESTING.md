# Beta testing on new hardware

← back to the [README](../README.md)

Thanks for helping. This app is verified on a Google TV Streamer and a Chromecast with Google TV, and on nothing else — so your device is genuinely new information, whether it works or not.

You don't need ADB or a terminal on the TV. The beta build collects most of the answers itself.

## Get the beta build

**Starting from scratch?** Follow [Option A in the README](../README.md#option-a--docker-desktop-windows-or-macos), changing the very end of the pasted command from `:latest` to `:beta`. That one word is the entire difference — the beta image is the same app with the diagnostics turned on.

**Already running the stable version?** Two ways, depending on how you installed it:

- *With a `docker-compose.yml`:* change its `image:` line to end in `:beta`, **and change `SELF_TEST: "0"` to `SELF_TEST: "1"`** — your compose file sets that explicitly, and an explicit setting overrides what the beta image would otherwise switch on for you. Then `docker compose pull && docker compose up -d`.
- *With the one-line command:* delete the container (Containers tab → **⋮** → **Delete**) and run the command again with `:beta` on the end. Your pairing survives — it lives in the `smarttube-data` storage, not in the container.

Either way your TV pairing is kept.

**How to tell it worked:** the bottom of the page gains a **🔬 Run device self-test** button. That button is the only visible difference, so if it isn't there, the switch didn't take — check the two things above rather than assuming the feature is broken. (Don't go by the version number: stable and beta both report `1.01`.)

## The easy way: one button

The status card at the bottom of the page has a **🔬 Run device self-test** button. Press it and it works through everything below by itself, with a progress list and a countdown. The countdown shows the worst case — a healthy device finishes well ahead of it. Expect ~3 minutes with the device awake; the asleep run can take up to ~8 because it waits out every wake attempt for as long as the app itself would, and it also measures how long your device stays deaf after waking (a real Shield firmware bug). When it finishes, **📄 Show the report** reveals the JSON and **📋 Copy report** puts it on your clipboard.

Two things worth knowing before you press it:

- **It drives your TV.** It starts a 19-second test video, tries pause, resume and volume, then hands the TV back to the screensaver. Don't run it while someone's watching something — it skips those steps if the queue is busy, but it's simpler to run it on an idle TV.
- **To test waking, put the device to sleep first** — then run it from your phone. If you use a separate box (Shield, Chromecast, Apple TV…), **send the box to sleep, not just the TV**: switching off the picture leaves the box awake, the app sees it as on, and both wake checks skip themselves. On a Shield, hold the power button on its remote or use the sleep tile, and check the box's own light. Nothing in the self-test ever puts your device to sleep for you — that's deliberate, since a device that ignores wake commands is exactly what we're looking for and we'd have no way to bring it back.

When it finishes, the page shows **a few short questions** — things the app genuinely cannot see, like whether the volume actually changed on your speakers. Answer what you can before copying; your answers go into the report automatically. These matter more than they look: several results are ambiguous without them, and "the app saw no change" is equally consistent with "it worked perfectly" and "it can never work."

## The passive report

The **📋 Copy diagnostics** button next to it reports what the app can see without touching your TV at all. Use it if you'd rather not have anything moved.

It contains **no credentials** — no pairing certificate, no private key, no YouTube token. It *does* include your TV's local address (like `192.168.1.42`), because that's useful for diagnosis. That's a private address that means nothing outside your own network, but if you'd rather not share it, delete the `host` line before sending.

The most useful thing in there is `events` — a log of what app was in the foreground over time. That's how we learn what your device calls its screensaver and its launcher, which is the single most common reason playback fails on unfamiliar hardware.

## What to do

One run is enough to start. Send it, and we'll tell you whether anything else is worth your time — the report says plainly what it did and didn't establish, so we won't ask you to repeat things blind.

**1. Pair the TV.** Enter its address, then the 6-character code that appears on screen.

This is also the moment of truth. It uses the same channel everything else does, so if pairing works, the rest can. If it fails outright, stop right there and tell us — that's a real answer, and it saves you the evening.

**2. Pair with SmartTube.** On the TV: **SmartTube → Settings → Remote control**, which shows a 12-digit code. Type it into the *Pair with SmartTube* card on the page.

Worth the minute it takes. Without it the app can't see the real playback position, so three of the thirteen checks can't run at all and two more come back as "started it, no idea what happened next."

**3. Run the self-test once**, with the device **on and idle**. Pick your device from the dropdown, wait about three minutes, answer whatever questions you can be bothered with, then **📋 Copy report** and send it.

That's the ask. Everything below is only if we come back and say it would help.

<details>
<summary>The other two runs, if we ask for them</summary>

**With the device asleep.** Send the box itself to sleep — not just the TV picture; on a separate streaming box those are different things and the app sees straight through the second one. Then press the button from your phone.

This is the only way to answer whether your device wakes over the network, and which key does it. It's also the slow one: it waits on each attempt for as long as the app itself would, so budget up to eight minutes.

**With the screensaver up.** Leave the device untouched until the screensaver appears, then run the self-test from your phone — the self-test, not Copy diagnostics. It will try to start a video while the screensaver is on screen, which is how we learn whether your screensaver swallows launch requests. That's the most common reason videos silently fail to start on hardware we've never seen.

</details>

**4. Optional, if you have another five minutes.** Two things no automated check can cover:

- Add **two** short videos from the page and let the first play to its end. Did the second start by itself? That's the whole auto-advance mechanism, and it can only be seen from the sofa.
- With the device asleep, add one video the ordinary way — not the self-test. Did it play, and roughly how long from pressing Add to the picture appearing? Anything over a minute tells us a timing default is wrong for your hardware.

Neither is required. The report is worth sending without them.

## Known device-specific settings

If something doesn't work, these are the usual culprits — all on the device, none in the app.

### NVIDIA Shield

- **The Shield won't wake.** Settings → **Remotes & accessories** → **Simplified wake buttons**, and disable *"SHIELD 2019 Remote: Wake on power and Netflix buttons only"* and *"Controllers: Wake on NVIDIA or logo buttons only"*. With these on, the Shield ignores a power command sent over the network, so waking cannot work.

  Every reported Shield wake failure with this protocol was fixed by those two toggles — none needed a different keycode, and Home Assistant wakes Shields with the same plain POWER we use. So if it still won't wake with both off, that's genuinely new information: run the self-test again and send the report rather than experimenting with `WAKE_KEYCODE`.

- **It wakes, but the video doesn't start.** SHIELD Experience before 9.2 has a firmware bug NVIDIA fixed in early 2025: *"remote stops responding for 60 seconds after wake from sleep."* The self-test measures this window (`current_app_readable_after_wake_s`) and will suggest a `WAKE_DELAY` to bridge it — but updating the firmware is the real fix. Either way, tell us your version from Settings → Device Preferences → **About**.
- **The connection drops every ~15 seconds right after pairing.** Known Shield quirk with this protocol. Fully reboot the Shield once after pairing the remote; it doesn't come back.
- **Volume does nothing.** Settings → Device Preferences → **Display & Sound → Volume control**. The Shield offers three modes, and only two can work here:
  - **HDMI-CEC** (the default on 2019 models) — works, provided the TV or receiver honours CEC volume. Many TVs don't.
  - **Digital** (the default on 2015/2017 models) — works; the Shield attenuates its own output.
  - **IR** — unreliable rather than impossible. We previously said this could never work; NVIDIA's own docs corrected us — the Shield relays network volume out through the remote's IR blaster. It only works when the remote physically faces your amplifier, so if volume is flaky in IR mode, that's why. CEC or Digital are still the modes to prefer.
- **Long-time SmartTube install?** SmartTube's signing key was compromised around November 2025 and the app was re-released under new application IDs. The in-app updater **cannot cross that rename**, so an install from before then still runs the legacy ID (`com.teamsmart.videomanager.tv`) — and this app won't find it. The report detects this (`smarttube_package_candidate`), but the right fix is a fresh install of current stable (32.10s or later) rather than a config change: builds before 31.94s also played link-launched videos without your account, which breaks age-restricted videos and watch history.
- **Paired but nothing syncs after a reboot.** SmartTube's remote-control registration can silently die when the Shield reboots. On the TV: SmartTube → Settings → **Remote control** — toggle it off and back on.
- **The Shield wakes up by itself at night.** That's SmartTube, not this app: any phone whose YouTube app is still linked to it can open the connection that self-launches SmartTube and wakes the device. Unlink old devices in SmartTube's Remote control settings.

### Any device

- **Volume does nothing** — HDMI-CEC volume control is usually switched off somewhere in the chain. See [CONFIGURATION.md](CONFIGURATION.md#volume-and-mute); note your TV probably calls CEC something else (*Anynet+*, *SIMPLINK*, *BRAVIA Sync*).
- **The report shows a `current_app is empty` warning** — foreground detection isn't working, which quietly breaks several things at once. Worth reporting immediately.

## Sending it back

Paste the reports, plus your device model and its software version. If something failed, say what you saw on the TV screen — that's the part the app can't observe, and usually the part that explains the rest.

A run where most probes say `skipped` is normal, not a failure: several only apply when the TV is off, and others stand down when something's already playing. `unmeasurable` is different — it means the app couldn't see well enough to judge, and it's worth mentioning.

## When you're done

**Send your report first.** It only lives in the page — recreating the container throws it away, and after switching back the button is gone, so re-running would mean reinstalling the beta.

Then, to go back to stable:

- *With a `docker-compose.yml`:* change `image:` back to end in `:latest` **and change `SELF_TEST: "1"` back to `"0"`** — that second half is the important one. Left on, the self-test button stays pressable by anyone who can reach the page. Then `docker compose pull && docker compose up -d`.
- *With the one-line command:* delete the container and run the original command again (the one ending `:latest`). Nothing to undo — the setting lived in the beta image, not in your command.

Your pairing survives either way.
