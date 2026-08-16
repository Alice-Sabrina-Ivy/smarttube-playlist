# What's new

Written for people using the app, not for people reading the code. Each release
here matches a version on the [releases page](https://github.com/Alice-Sabrina-Ivy/smarttube-playlist/releases).

---

## v1.01

**If your TV's address changes, you can now fix it from the page.** Routers hand out a
new address after a reboot, and when that happened the page said the app wasn't set up
— the only way out was pairing with your TV all over again. Now it says what's actually
wrong and gives you a box to type the new address into. Nothing is re-paired, so it
takes a few seconds.

**Fixes for the queue getting stuck or losing videos:**

- After a video finished, adding another one sometimes did nothing — it sat in the
  queue and never started. Found by testing against a real TV.
- A brief network hiccup while the TV was off could throw away a video you'd just
  added.
- The queue could stop moving on to the next video and sit on one indefinitely.
- The queue could jump ahead when something *else* finished playing on the TV.
- If a video's details failed to load, the app assumed it was ten minutes long and cut
  longer videos short.

**A privacy fix.** The code that links this app to YouTube was being written into the
app's own logs. Anyone you sent those logs to — when asking for help, say — could have
used it to control what played on your TV. It's now hidden before anything is written
down.

**A setting for devices that won't wake up.** A few streaming boxes ignore the normal
power-on command and stay asleep. `WAKE_KEYCODE` lets you try an alternative instead of
being stuck. See [CONFIGURATION.md](docs/CONFIGURATION.md).

**The setup guide has been rewritten** — shorter, with a proper update path for the
one-line install, and a thirty-second test that tells you whether your device will work
before you install anything.

---

## v1.0

First release. Paste a YouTube link on a page anyone on your network can open, and it
plays on SmartTube on your TV. Several people can add videos at once and the queue
plays through them in order.
