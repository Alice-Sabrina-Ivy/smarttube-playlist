# Advanced setup

← back to the [README](../README.md)

For Linux, a NAS, a homelab box, or anywhere you'd rather use Docker Engine directly than Docker Desktop. The [README](../README.md) covers the point-and-click route; everything here assumes you're comfortable in a shell.

The end state is identical either way — same image, same pairing flow, same data directory.

## Docker Engine + Compose

Assumes Docker Engine and the Compose plugin are already installed.

```bash
mkdir -p /opt/smarttube-playlist && cd /opt/smarttube-playlist
curl -O https://raw.githubusercontent.com/Alice-Sabrina-Ivy/smarttube-playlist/main/docker-compose.yml
docker compose up -d
```

Then open `http://<host-ip>:38420/` and work through [Pair with your TV](../README.md#pair-with-your-tv).

Nothing in `docker-compose.yml` needs editing to get started. Every setting is optional — see [CONFIGURATION.md](CONFIGURATION.md).

### Architectures

The image is multi-arch: `linux/amd64` and `linux/arm64`.

A Raspberry Pi needs a **64-bit** OS. Check with `uname -m` — you want `aarch64`, not `armv7l`. On 32-bit Raspberry Pi OS the container fails with `exec format error`.

### Where state lives

`./data`, next to the compose file: the pairing certificate, the TV's address, the Lounge token, and your AV receiver choice. Back that directory up and you never have to pair again — including when moving to a different host.

It's created on first run and chowned to UID 1000 by the entrypoint, which runs as root just long enough to do that before dropping privileges via `gosu`. If you add a `user:` directive to the compose file the chown can't happen, and you'll need to pre-create the directory with the right ownership yourself.

To put it somewhere else, change the left-hand side of the volume mapping:

```yaml
volumes:
  - /srv/appdata/smarttube-playlist:/data
```

Leave the `:/data` side alone — that's the path inside the container, and the app expects it.

### Port binding

`38420:8000` binds all interfaces, which is what makes the page reachable from phones on your LAN. If the host has any internet-facing address, pin the bind to your LAN IP:

```yaml
ports:
  - "192.168.1.50:38420:8000"
```

There is no authentication by design — read [SECURITY.md](../SECURITY.md) before exposing this anywhere.

### Behind a reverse proxy

Two things to set, both covered in [CONFIGURATION.md](CONFIGURATION.md#behind-a-reverse-proxy):

1. `ALLOWED_HOSTS` — without it, every add/skip/pause returns 403.
2. Unbuffered `text/event-stream` — without it, the live-updating UI stalls.

## Deploying with Portainer

Portainer marks CLI-deployed stacks as "limited" because it has no record of the compose source, so deploy through its own UI if you want full management from there:

**Stacks → Add stack →** name it `smarttube-playlist` → build method **Web editor** → paste the contents of `docker-compose.yml` → **Deploy**.

Because the compose file references a published image, Portainer pulls it directly — no source tree needed on the host. To update later, hit **Pull and redeploy** on the stack.

If a CLI-deployed container is already running, remove it first so the stack doesn't fight it for the container name:

```bash
docker stop smarttube-playlist && docker rm smarttube-playlist
```

Note that environment variables set in Portainer's stack editor live in Portainer, not in any file on disk — a `docker-compose.yml` sitting on the host is not the source of truth for what's actually running.

## Building from source

Only needed if you're modifying the code, or you'd rather not use the prebuilt image:

```bash
git clone https://github.com/Alice-Sabrina-Ivy/smarttube-playlist
cd smarttube-playlist
# in docker-compose.yml: comment out `image:`, uncomment `build: .`
docker compose up -d --build
```

For running it without Docker at all, plus the module layout, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Updating, stopping, removing

```bash
docker compose pull && docker compose up -d
```

Your `data` directory is untouched, so no re-pairing. The same command applies any setting you changed in `docker-compose.yml`. Portainer users: **Pull and redeploy** on the stack instead.

To stop it: `docker compose down`. To remove it entirely, stop it and delete the folder — nothing was ever installed on the TV, though you can revoke the pairing under *Settings → Apps → See all apps → Show system apps → Android TV Remote Service*.

The compose file tracks `:latest`, which follows the newest build from `main` — so a redeploy picks up whatever has landed since. That's the intended default.

If you'd rather only move when you choose to, pin a tag instead:

```yaml
image: ghcr.io/alice-sabrina-ivy/smarttube-playlist:1.0   # exactly this release
image: ghcr.io/alice-sabrina-ivy/smarttube-playlist:1     # newest 1.x
```

Every published version stays available, so a pin can sit unchanged for as long as you like, and you can always drop back to an older one. See the [releases](https://github.com/Alice-Sabrina-Ivy/smarttube-playlist/releases).
