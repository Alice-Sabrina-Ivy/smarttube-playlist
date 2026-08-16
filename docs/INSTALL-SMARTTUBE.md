# Installing SmartTube

← back to the [README](../README.md)

[SmartTube](https://smarttubeapp.github.io/) is a free, ad-free YouTube app for TV devices — the same YouTube, without the adverts. It's a separate project, and it's what this app sends your videos to, so it needs to be on the TV first.

## Which devices can run it

SmartTube runs on Android TV and Google TV devices. But **this app needs more than SmartTube does** — it needs Google's Android TV Remote Service, which Fire OS doesn't ship. The quickest check is in the README: [Will it work with my device?](../README.md#will-it-work-with-my-device)

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

> **Fire TV is the awkward one.** SmartTube itself runs on models released before October 2025 — but this app still can't drive it, because Fire OS ships Amazon's remote stack instead of Google's. SmartTube working there is not enough.

## Installing SmartTube

SmartTube is **not on the Play Store** — you install it yourself, and only from the official source: its developer warns that copies on app stores and APK sites may contain malware.

Easiest route, done entirely on the TV:

1. From the Play Store on your TV, install **Downloader by AFTVnews**.
2. Open Downloader and type this into its address box:

   ```
   kutt.to/stn_stable
   ```

3. It downloads the official APK. Accept the prompts — Android asks you to allow installs from Downloader — then install.
4. Open SmartTube once and play something, to confirm it works.

Other methods (USB stick, "Send Files to TV", ADB) are at [smarttubeapp.github.io](https://smarttubeapp.github.io/).
