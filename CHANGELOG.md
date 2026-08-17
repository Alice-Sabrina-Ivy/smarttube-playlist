# What's new

Written for people using the app, not for people reading the code. Each release
here matches a version on the [releases page](https://github.com/Alice-Sabrina-Ivy/smarttube-playlist/releases).

---

## v1.02

**Queueing videos actually works now.** This is the big one. If you queued two videos,
the first would finish and the second would just… sit there. The now-playing card went
blank, your video stayed in the queue, and nothing you could see explained why — the
only way out was pressing Skip or Play. On a real TV this happened roughly two times in
five. It now hands off every time, tested over and over against an actual device.

**Videos that silently never started.** Sometimes adding a video returned success, the
card appeared, the progress bar ticked along — and nothing was playing on the TV at all.
This turned out to be the same fault that would later make the app stop responding
altogether, which is why it always seemed to work fine right up until it didn't.

**The app no longer needs restarting when it loses the TV.** Three separate problems
could leave it permanently stuck: it could lose its connection to the TV with nothing
left trying to reconnect; it could lose its link to SmartTube while still reporting
everything as fine; and its own attempt to repair that link never actually ended the
dead one, so the repair quietly did nothing for as long as the app kept running. They
looked identical from the outside — the page claimed to be connected and retrying while
neither was true, and only restarting it helped. It now notices and recovers on its own.

**The progress bar could show a different video's time.** After starting a new video the
bar sometimes kept showing the previous one's position and length, so a ten-minute video
could appear finished ten seconds in.

**Ad breaks no longer skip your video.** A long enough ad could be mistaken for the video
ending, moving the queue on while you were still watching.

**The next video no longer starts before the current one has finished.** If the app's link
to SmartTube had drifted out of sync, its countdown could run out early and move on while
you were still watching. The countdown now starts when the video actually starts playing,
rather than when you added it — previously it began counting during the wake-up and
loading, which on a sleeping device could be half a minute of the video's length.

**Longer queues play all the way through.** With three or more videos, the second handover
could quietly fail and leave the rest of the queue sitting there. Queuing the same video
twice in a row didn't work either — the second copy was mistaken for the first still
playing, and nothing was sent to the TV at all.

**Videos you start on the TV yourself now show a live position.** If someone picked
something in SmartTube directly, the app would show it but the time never moved. It now
keeps up, and pausing or leaving SmartTube is reflected properly.

**A link that can't play no longer stops everything.** Paste a video that has been
deleted, made private, blocked in your country, or simply mistyped, and the queue used
to stop dead on it for ten minutes — with Play doing nothing, so the only way out was
Skip, if you worked out that was the problem. It now gives the video about 45 seconds
to start and then moves on to the next one.

**Smaller fixes:**

- Pressing Play twice while a video was resuming could start it twice over, with the
  audible stutter that causes.
- Links with a start time (`?t=90`) used to be dropped without trace when the jump
  failed; the app now records it. The video still starts from the beginning in that
  case — restarting it to apply the offset would be worse than losing the offset.
- Seeking didn't tell the app when the video would now end, so it could move on late.
- Returning the TV to its screensaver could back out of a video someone had just added.
- On some devices auto-advance never worked at all, because the app couldn't read which
  app was on screen and assumed the worst.

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
