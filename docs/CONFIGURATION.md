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

## Volume control

On many TV devices the remote's volume buttons ride HDMI-CEC straight to the amplifier, so there's nothing for a LAN service to drive. The Google TV Streamer is one of these — it reports no usable volume range over the remote protocol at all. Some other Android TV hardware may respond to volume keycodes, but that path isn't implemented yet and nobody has tested it. Talking to the amplifier directly works regardless of the TV, which is what this does.

You set this up **in the web UI**, not here — during setup you're asked which receiver you have and for its address.

| Brand | Protocol | Port | Tested on real hardware? |
|---|---|---|---|
| Denon, Marantz | Legacy Telnet | 23 | **Yes** |
| Yamaha | YNCA | 50000 | No — 2010 or newer (RX-V, RX-A/Aventage, TSR, HTR) |
| Onkyo, Integra | eISCP | 60128 | No — 2011 or newer |
| Pioneer | eISCP | 60128 | No — **2016 or newer only** |
| Sony | Audio Control API | 10000 | No — STR-DN1080, HT-series soundbars |

Everything except Denon/Marantz was written from each manufacturer's protocol documentation and from the source of established open-source libraries, but has never been run against the real device. The commands are pinned byte-for-byte by unit tests, so they match the documentation — that is not the same as knowing they work. If you own one of these, please open an issue either way.

Two things cause most false reports:

- **Network standby must be enabled** on Yamaha, Onkyo and Sony, or the receiver won't answer while it's off.
- **Yamaha allows only one control connection at a time.** If you also run Home Assistant's `yamaha_ynca` integration, it holds that connection permanently and this app cannot connect at all. The Yamaha phone app uses a different protocol and does not conflict.

**Pioneer models older than 2016** use a different protocol on port 8102 and are deliberately not supported: the mute *query* command could not be verified, and shipping a guess would give you a mute button that half works.

The choice is stored in `config.json` in the data volume, next to the TV pairing. Answering "I don't have one" is recorded too, so you're only asked once. To change it later, re-answer via `POST /api/avr` (see [API.md](API.md)) or clear everything with `RESET_PAIRING`.

The address must be on a private network: the service opens a socket to whatever it's given and has no authentication, so public addresses are refused.

Until a receiver is set up, the volume buttons stay hidden.

Brands outside the table above aren't supported. NAD is the closest candidate and is queued; Rotel, Anthem, Cambridge Audio, Emotiva and Arcam were each looked at and set aside, because their relative volume or mute-query commands couldn't be verified from a reliable source and a guess is worse than an absence. If you'd like yours added, open an issue naming the model and how it accepts network commands.

## Behind a reverse proxy

Two things matter:

1. **Set `ALLOWED_HOSTS`** to the hostname the proxy terminates on, or state-changing requests will 403. See [SECURITY.md](../SECURITY.md).
2. **Don't buffer the event stream.** The live-updating UI is Server-Sent Events; a buffering proxy makes the page look frozen. In nginx that means `proxy_buffering off;` and a `proxy_read_timeout` well above the default on `/api/events`.

## Timezones

There's nothing to configure. Timestamps are stored in UTC and sent to the browser with the offset attached, so every viewer sees times in their own local zone. Container log lines are UTC; set the standard Docker `TZ` variable if you'd rather read them in local time.
