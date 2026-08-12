# A QR code for your guests

← back to the [README](../README.md)


Nobody wants an IP address read out to them at a party. Make a QR code of the page and guests scan it with their camera — no app, no typing.

Encode the LAN address, **not** `localhost`:

```
http://192.168.1.50:38420
```

⚠️ **Example IP** — use your own, from [Letting phones and tablets reach it](../README.md#letting-phones-and-tablets-reach-it).

**Easiest:** [goqr.me](https://goqr.me/) → pick **URL** → paste → download the PNG. Free, no sign-up. Any generator works; a `192.168.x.x` address means nothing outside your home.

**Offline instead**, replacing the address with yours:

```bash
qrencode -o queue.png "http://192.168.1.50:38420"    # brew/apt install qrencode

pip install "qrcode[pil]"                            # any OS
qr "http://192.168.1.50:38420" > queue.png
qr --ascii "http://192.168.1.50:38420"               # preview in the terminal
```

Print it and stick it by the TV or on the fridge. Two things break it:

- **The host's address changing.** Give that machine a DHCP reservation, as [suggested for the TV](../README.md#what-youll-need). Or encode a name — `http://mynas:38420` survives address changes, and single-label names are accepted.
- **Guests not on your Wi-Fi.** Mobile data or an isolated guest network won't reach it, which may be deliberate — see [Security](../README.md#security-in-one-paragraph).
