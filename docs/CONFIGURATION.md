# Configuration

← back to the [README](../README.md)

Every setting is optional and set as an environment variable in `docker-compose.yml`. The defaults suit most setups — most people change none of these.

```yaml
services:
  smarttube-playlist:
    environment:
      LOG_LEVEL: "DEBUG"
```

## All variables

| Variable | Default | What it does |
|---|---|---|
| `CLIENT_NAME` | `SmartTube Playlist` | Name shown on the TV during pairing |
| `SMARTTUBE_PACKAGE` | `org.smarttube.stable` | Set to `org.smarttube.beta` for the beta build |
| `LOG_LEVEL` | `INFO` | Set `DEBUG` when diagnosing something. Case-insensitive; an unrecognised value falls back to `INFO` with a warning rather than failing to start |
| `RATE_LIMIT_SECONDS` | `10` | Per-IP cool-down between queue submissions |
| `WAKE_DELAY` | `15.0` | Minimum seconds to wait after `POWER` before launching. A floor, not a timeout — instant-on TVs report "on" in ~1s while still booting |
| `WAKE_TIMEOUT` | `30.0` | Give up waiting for the TV to report on |
| `WAKE_POLL` | `0.5` | How often to re-check while waking |
| `SCREENSAVER_PACKAGES` | `com.google.android.apps.tv.dreamx,com.google.android.backdrop` | Packages treated as screensavers; these swallow launch intents, so they get dismissed first |
| `SCREENSAVER_DISMISS_KEY` | `HOME` | Key that dismisses the screensaver. `BACK` also works. `DPAD_CENTER` and `WAKEUP` are **not** supported — the remote protocol drops them silently |
| `IDLE_KEYCODE` | `HOME,BACK` | Keys sent when Skip empties the queue; lands on the ambient screensaver. `POWER` turns the display off, `HOME` stops at the launcher, empty disables it |
| `IDLE_KEYCODE_DELAY` | `0.6` | Seconds between those keys |
| `DEFAULT_DURATION_S` | `600` | Assumed length when the metadata scrape fails |
| `METADATA_TIMEOUT_S` | `15.0` | YouTube watch-page fetch timeout. The page is 1.1–1.6 MiB; too low and the scrape fails, giving the video a wrong title and a 10-minute assumed length that cuts long videos short |
| `DATA_DIR` | `/data` | Where pairing state is stored **inside** the container. Change the volume mount instead |
| `ALLOWED_HOSTS` | (unset) | Comma-separated hostnames to accept in the `Host` header. Only needed behind a reverse proxy on a real domain — see [SECURITY.md](../SECURITY.md) |
| `RESET_PAIRING` | (unset) | Set to `1` to clear all pairing on the next start. Fires **once** — see [SECURITY.md](../SECURITY.md#resetting-the-pairing) |

## Volume and mute

The volume and mute buttons send `VOLUME_UP` / `VOLUME_DOWN` / `VOLUME_MUTE` over the same connection used to control the TV. What happens next depends on the hardware, and both outcomes have been verified:

- **HDMI-CEC.** The streaming device turns the keypress into a CEC volume command aimed at whatever is doing the audio — TV speakers, soundbar, or AV receiver. Brand-agnostic. Confirmed against a Denon receiver: each press moved it one step.
- **The device's own output volume.** With no CEC link available, the streaming device attenuates its own HDMI output instead. Confirmed on a Chromecast with Google TV plugged into a TV with no CEC at all — volume tracked exactly.

There is nothing to choose or configure. The device picks whichever applies.

### If the buttons do nothing

The usual cause is **HDMI-CEC volume control being switched off**. It's on by default, but not always — it was off on one of the two devices tested here, which is what made volume look impossible at first.

CEC is branded differently by nearly every manufacturer, which makes it hard to search for. It's the same feature under all these names:

| Brand | What they call it |
|---|---|
| Samsung | Anynet+ |
| LG | SIMPLINK |
| Sony | BRAVIA Sync |
| Panasonic | VIERA Link |
| Philips | EasyLink |
| Sharp | Aquos Link |
| Hisense | HDMI-CEC |
| TCL | T-Link |
| Toshiba | Regza Link / CE-Link |

Enable it on the TV **and** on the receiver or soundbar if you have one — the chain only works if every device in it has CEC on. On the streaming device, look under **Settings → Display & Sound → HDMI-CEC** (wording varies by device and Android version).

If your TV has no CEC at all, that isn't necessarily fatal: a streaming stick may still adjust its own output volume, which is exactly what happens on the Chromecast tested here.

## Behind a reverse proxy

Two things matter:

1. **Set `ALLOWED_HOSTS`** to the hostname the proxy terminates on, or state-changing requests will 403. See [SECURITY.md](../SECURITY.md).
2. **Don't buffer the event stream.** The live-updating UI is Server-Sent Events; a buffering proxy makes the page look frozen. In nginx that means `proxy_buffering off;` and a `proxy_read_timeout` well above the default on `/api/events`.

## Timezones

There's nothing to configure. Timestamps are stored in UTC and sent to the browser with the offset attached, so every viewer sees times in their own local zone. Container log lines are UTC; set the standard Docker `TZ` variable if you'd rather read them in local time.
