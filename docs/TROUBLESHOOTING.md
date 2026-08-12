# Troubleshooting

← back to the [README](../README.md)

Symptoms are grouped roughly in the order you'd hit them: installing, pairing, then playing.

**`manifest unknown` or `denied` when starting.** Docker couldn't download the image. Check you're online and try `docker compose pull`. If it still fails, [open an issue](https://github.com/Alice-Sabrina-Ivy/smarttube-playlist/issues).

**`no configuration file provided: not found`.** Either the terminal isn't in the folder holding `docker-compose.yml`, or the file is really called `docker-compose.yml.txt` — Windows hides the extension. Type `dir` (Windows) or `ls` (Mac) to see the real filenames.

**`error during connect` or `port is already allocated`.** The first means Docker Desktop hasn't finished starting — wait for **Engine running** and re-run. The second means something else is using port 38420 — change the left-hand number in `docker-compose.yml` to e.g. `38421:8000` and re-run.

**Other devices can't open the page (Docker Desktop).** Firewall. See [Letting phones and tablets reach it](../README.md#letting-phones-and-tablets-reach-it). Confirm it works at `http://localhost:38420` on the host first — if that fails, it's not the firewall.

**Pairing fails immediately.** Is the TV actually on and awake? Then confirm it's on the same network and ports 6466/6467 are reachable. Some Google TV devices have the Remote Service enabled but firewalled until a power cycle.

**`InvalidAuth` on startup.** The pairing certificate was rejected — the TV revoked it, or the files got out of sync. Set `RESET_PAIRING: "1"` in `docker-compose.yml`, restart, pair again, then take the flag back out.

**HTTP 500 with `PermissionError: '/data/cert.pem'`.** The data directory isn't writable by the container's user. The entrypoint chowns it to UID 1000 at startup, so this normally self-heals — unless you added a `user:` line to `docker-compose.yml`, which prevents the chown. Either remove that line or pre-create the directory with the right owner: `sudo chown -R 1000:1000 ./data`.

**The video opens in stock YouTube instead of SmartTube.** Either SmartTube isn't installed, or stock YouTube is registered as the default handler for YouTube links. Open SmartTube once manually and pick "always" if Android offers.

**TV wakes but nothing plays.** Raise `WAKE_DELAY`. SmartTube has to be foregrounded *after* the TV is genuinely awake, and some TVs report themselves ready well before they are.

**The queue stops advancing.** Check SmartTube is still the foreground app. Backing out of it stops the queue by design. Re-open SmartTube and hit Skip.

**Auto-advance is early or late.** Nothing is linked to SmartTube, so the app is working off an estimated video length. Pair with SmartTube to fix it properly, or use Skip to realign.

**The volume and mute buttons do nothing.** HDMI-CEC volume control is switched off somewhere in the chain. Check it on the TV, on the receiver or soundbar if you have one, and on the streaming device (**Settings → Display & Sound → HDMI-CEC**). Your manufacturer probably calls CEC something else entirely — see [docs/CONFIGURATION.md](CONFIGURATION.md#volume-and-mute).

**A video shows a 10:00 duration that's obviously wrong,** or its title shows as a jumble of letters. The lookup to YouTube failed, so it fell back to an assumed 10 minutes. This isn't cosmetic: that fake length drives auto-advance, so a long video gets skipped 10 minutes in. Look for `metadata fetch failed` in the logs; if it happens often, your connection is slow to reach YouTube — raise `METADATA_TIMEOUT_S` (see [docs/CONFIGURATION.md](CONFIGURATION.md)).

**Reading the logs:**

```bash
docker compose logs -f
```

On Docker Desktop you can also click the container and open the **Logs** tab.

Still stuck? [Open an issue](https://github.com/Alice-Sabrina-Ivy/smarttube-playlist/issues) with your logs at `LOG_LEVEL=DEBUG` and which TV device you're using.
