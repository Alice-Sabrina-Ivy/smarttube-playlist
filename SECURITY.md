# Security

← back to the [README](README.md)

## The short version

**The web UI has no authentication. Anyone who can reach the page can control your TV.**

That is the design, not an oversight — guests shouldn't need an account to queue a song. It's safe on a home network and unsafe anywhere else. If you only ever reach it from your own LAN, you can stop reading here.

## Threat model

This is built for a trusted home network. It assumes everyone who can reach the page is someone you'd hand the remote to. Within that assumption the worst anyone can do is play an annoying video, and the guards below exist so that *only* people actually on your network can do even that.

It is **not** built to be exposed to the internet, and no amount of configuration makes it safe to do so on its own.

### What an unauthenticated caller can do

Anyone who can reach the page can queue, skip, pause, reorder and clear videos, seek, change the volume, wake the TV, and read what's playing. They can also point the service at a different TV address — deliberately reversible, since the same screen stays on display until a working address is entered, which is what makes it a recovery tool rather than a way to lock someone out. They cannot read files, run commands, or reach anything on your network beyond this service.

With `SELF_TEST=1` they can additionally start the device self-test, which drives the TV for about three minutes — a short clip, pause/resume, volume — then hands the TV back to its screensaver, refusing everyone else's playback requests while it runs. It never powers the device off. The same setting exposes `/api/diagnostics`, which reports this host's LAN address, your device model and firmware, how old the SmartTube pairing is, and a recent event log.

**Both are off by default.** The `:beta` image turns them on, because producing that report is what it exists for.

### Don't port-forward it

No auth means anyone on the internet could take over your TV. If you want access from outside, put it behind your existing reverse-proxy auth — Authelia, Tailscale, basic auth, anything — and add the proxy's hostname to `ALLOWED_HOSTS` (see below).

### Pin the bind interface

The default `ports:` line binds `38420` on every interface, which is what makes the page reachable from phones on your LAN. If the host has any internet-routable address, pin the bind to your LAN IP instead:

```yaml
ports:
  - "192.168.1.50:38420:8000"
```

### Guest Wi-Fi counts as "reachable"

If your guest network can route to your main LAN, guests can reach this. That's usually the point here — but decide it deliberately rather than discovering it.

## Built-in protections

### Cross-site requests are blocked

A CSRF check rejects state-changing requests whose `Origin` doesn't match the service's own, so a random website you visit can't quietly drive your TV in the background. Non-browser clients (curl, webhooks, Home Assistant) don't send `Origin` and pass through unaffected.

### DNS rebinding is blocked

A malicious page can re-point its own domain at a LAN address and then talk to services there. Comparing `Origin` against `Host` is no defence at all, because a rebinding attacker controls **both** headers and simply sends a matching pair.

So the `Host` header itself has to look like a LAN identity before anything else is checked. Accepted:

- bare IP addresses — `192.168.1.50`, the normal way anyone reaches this
- single-label names — `mynas`, which can't be a registrable public domain
- `.local`, `.localhost`, `.internal`, `.home.arpa` suffixes

Anything else — that is, a real registrable domain — is refused with 403 unless you list it in `ALLOWED_HOSTS`. Rebinding requires a domain the attacker owns, and those always contain a dot, which is what makes the distinction work.

**If you use a reverse proxy on a real domain**, set `ALLOWED_HOSTS` to that hostname or every state-changing request will 403:

```yaml
ALLOWED_HOSTS: "tv.example.com"
```

Comma-separate multiple names. `GET` requests are unaffected either way.

### Pairing can't be hijacked or wiped

There is deliberately no unpair or reset endpoint, so nobody on the LAN can destroy your pairing as a prank or a denial of service.

The same reasoning covers Lounge: once a token exists, `POST /api/lounge/pair` returns **409** instead of overwriting it. Without that, anyone on the network could pair their own session and take over playback control while silently breaking yours.

Re-pairing therefore requires access to the container's configuration — see below.

### Rate limiting

Queue submissions are limited to one per IP every `RATE_LIMIT_SECONDS` (default 10), so one person can't flood the queue.

### Stored credentials

The TV pairing certificate and the Lounge token live in the `data` volume and are written `0600` where the filesystem supports it. Anyone who can read that directory can impersonate your client to the TV, so treat it like any other secret — and note that most NAS bind-mounts don't enforce Unix permissions.

## Resetting the pairing

Set `RESET_PAIRING` and restart:

```yaml
environment:
  RESET_PAIRING: "1"
```

On the next start the TV-remote certificate and the Lounge token are deleted and you're back on the setup screen.

**It fires once.** A container can't rewrite its own environment, so the flag stays set across restarts — unguarded, it would wipe your freshly-created pairing on every single reboot. Instead a marker file records that the reset ran, and later starts skip it and log a reminder to take the flag out. Clearing `RESET_PAIRING` removes the marker and arms it again for next time.

You can also just delete `cert.pem`, `key.pem`, `config.json` (TV remote) and `lounge.json` (Lounge) from the `data` folder by hand.

## Reporting a problem

This is a hobby project with no security guarantees and no SLA. If you find something, open an issue — or, for anything you'd rather not post publicly, use GitHub's **Report a vulnerability** button on the Security tab.

Please don't expect a fast response, and don't use this software anywhere its compromise would matter.
