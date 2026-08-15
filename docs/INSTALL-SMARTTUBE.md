# Installing SmartTube

← back to the [README](../README.md)

[SmartTube](https://smarttubeapp.github.io/) is a free, ad-free YouTube app for TV devices — the same YouTube, without the adverts. It's a separate project, and it's what this app sends your videos to, so it needs to be on the TV first.

**Runs on:** Android TV and Google TV devices — Chromecast with Google TV, Nvidia Shield, Xiaomi Mi Box, onn. boxes, and most Android TV boxes and built-in Android TVs.

**Doesn't run on:** phones and tablets, Samsung (Tizen) and LG (webOS) TVs, Apple TV, Roku.

**Fire TV is the awkward one.** SmartTube itself runs on Fire TV models released before October 2025 — but *this* app still can't drive it, because Fire OS ships Amazon's remote stack instead of Google's Android TV Remote Service, and that service is what we connect to. So SmartTube working there is not enough. See [Will it work with my device?](../README.md#will-it-work-with-my-device).

**Installing it.** SmartTube is **not on the Play Store** — you install it yourself, and only from the official source: its developer warns that copies on app stores and APK sites may contain malware.

Easiest route, done entirely on the TV:

1. From the Play Store on your TV, install **Downloader by AFTVnews**.
2. Open Downloader and type this into its address box:

   ```
   kutt.to/stn_stable
   ```

3. It downloads the official APK. Accept the prompts — Android asks you to allow installs from Downloader — then install.
4. Open SmartTube once and play something, to confirm it works.

Other methods (USB stick, "Send Files to TV", ADB) are at [smarttubeapp.github.io](https://smarttubeapp.github.io/).
