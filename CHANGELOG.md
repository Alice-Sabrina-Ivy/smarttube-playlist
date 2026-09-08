# What's new

Written for people using the app, not for people reading the code. Each release
here matches a version on the [releases page](https://github.com/Alice-Sabrina-Ivy/smarttube-playlist/releases).

---

## v1.05

A fix for a queue that could stop advancing on some devices — the NVIDIA
Shield most of all.

**With several videos queued, the next one sometimes never started.** When one
video ended and the app started the next, some devices quietly dropped that
start and stayed on the finished video's screen. The app then mistook the
video still showing for one you had picked yourself on the TV remote, cleared
what it was trying to play, and left the rest of the queue waiting with nothing
running. It now recognises a hand-off the device didn't follow and sends it
again, so the next video starts; if a device keeps refusing, the app stops
after a few tries and keeps your queue, so pressing Play picks it back up. This
turned up on a Shield with four or more videos queued — the number only
mattered because more videos mean more hand-offs for one to be dropped.

---

## v1.04

A reliability release. Every change below is something that used to go
wrong while using the page — mostly around pause, play and seek, and around the hand-off from one
queued video to the next — found over a day of hands-on testing and fixed. If you only read one
line: pause, play and the skip buttons now do what you press, every time, and the next video in
the queue starts when the current one ends.

**The progress bar now stops the moment you press Pause, and picks up correctly when you resume.**
It used to keep counting for a few seconds after you paused — the app was waiting for the TV to
confirm, and said nothing until it did — and then came back several seconds out of step. It now
stops immediately, holds the position you actually paused at rather than jumping back to the TV's
last report, and carries on from there. Measured within about a second of the TV throughout.

**Fast-forwarding to the end with the TV remote no longer strands the next video.** Skipping ahead
with the physical remote to the last few seconds of a video left the queue stuck: the app was still
waiting out the runtime the skip had removed, and the TV stopped reporting a position for it, so
nothing told the app the video had finished. It now recognises a player that has gone away even when
it never reports stopping, and moves on.

**Pressing Play after a pause no longer makes the video stutter.** The app asked the TV to resume and
then waited to be told it had — but it had stopped listening for that answer while paused, so when
the TV was slow to volunteer it, the app assumed the resume had failed and relaunched the video,
which restarted it. It now asks the TV directly instead of waiting, so Play resumes in place.

**A video already playing when you open the page now shows up.** The app only heard about playback
when it started, so anything already on the TV — because you opened the page late, or because the
app restarted — stayed invisible until the TV happened to mention it. You could be looking at a
paused video with the page insisting nothing was playing. It now asks the TV what is on screen as
soon as it connects.

**The page recovers if its live connection dies.** It reconnects a stream that has closed for good
rather than sitting frozen on whatever it last drew, and re-syncs when you come back to a phone that
had locked its screen.

**Adding a video no longer cuts off one that is already playing.** If something was playing on the
TV that you had not added through the page — you started it in SmartTube yourself, say — adding a
video used to take over immediately and cut it off. It now takes the playing video as the current
one and puts yours next, so the first one finishes and yours follows. If the app cannot actually
tell that a video is playing (YouTube keeps reporting videos that stopped long ago), it still starts
yours straight away rather than leaving it waiting behind something that is not running.

**A video playing on the TV that you didn't add through the page now appears on it.** If someone
started something from SmartTube directly, or paused it, the page said "Nothing playing" over a
video that was plainly on screen — and greyed out Pause, Play, Skip and Seek, so there was no way to
do anything about it from your phone. It now shows the video, keeps the controls live, and says
"PAUSED ON TV" with the time held still when it is paused.

**Seek buttons now do what you press.** Pressing the same skip button repeatedly did nothing after
the first press, and pressing different ones quickly sent the video jumping back and forth between
two points. Both had the same cause: each press worked out where to jump from YouTube's last report
of the position, and that report does not update between quick presses — so every press started from
the same stale place. Presses now build on each other until the TV actually reports back.

**The next video no longer gets stranded when the current one ends a few seconds "early".** When
the app had picked up a video already playing on the TV, its countdown was based on a position
report that lags the real playhead — so the video could end a few seconds before the countdown, and
the app read SmartTube dropping back to its home screen as you leaving, cleared the card, and left
the next video sitting in Up next. SmartTube leaving within a few seconds of the countdown is now
treated as the video ending, and the next one starts.

**Resuming — from the page or the TV remote — no longer leaves the page stuck on "paused".** Two
causes. Pressing Play on the page for a video the app did not start itself resumed the TV
correctly but never updated the page, which kept showing PAUSED ON TV; the page now reflects the
command it just sent. And a resume made with the TV remote could go unnoticed for up to five
minutes, because the app stopped asking the TV for its state while paused; it now keeps asking,
so a remote resume shows up within about a minute even when SmartTube is not volunteering it.

**Pausing with the TV remote no longer makes the video vanish from the page.** After about
fifteen seconds paused, the page used to hide the Now playing card — it could not tell a pause on
the remote from you backing out of the player, and chose to hide. The card now stays, marked
"PAUSED ON TV", for as long as the video is paused; if you really did back out, Play resumes where
you were and Skip clears it.

**Video titles, uploaders and lengths keep working when YouTube gets suspicious.** YouTube
sometimes answers the page the app reads video details from with a "sign in to confirm you're not
a bot" version that carries none — after that, every added video showed its raw ID instead of its
name, "unknown" as the uploader, and a made-up 10:00 length in the queue (a length that also
drives auto-advance). The app now falls back to YouTube's search results for the same video,
which still serve the title, uploader and real length, and to a lighter title-only endpoint after
that.

**A video that had just started can no longer be mistaken for one that ended.** For its first
seconds a freshly launched video may report no playback position at all, and the safety net that
detects a vanished player could read that as the video being gone and skip ahead. It now only
treats a missing position as a vanished player when the TV does not claim to be playing.

**Pausing and then resuming no longer restarts the video.** When YouTube's channel could not
confirm a pause, the app paused the TV with a remote-control keycode instead — but Play still asked
YouTube to resume, waited seconds for an answer that channel could not give, and then relaunched
the video from the beginning. Play now resumes the same way Pause paused: immediately, in place.

**Rapid pause/play presses no longer leave the page and the TV disagreeing.** Pressing one while
the other was still being delivered could land the two commands on the TV a millisecond apart — the
video restarting while the page said paused. Whichever you pressed last now wins, and the older
in-flight command stands down. Commands are also delivered one at a time now, and a report that
predates your press no longer counts as the TV answering it — toggling pause and play faster than
the TV reports back used to produce a pause that silently did nothing, and then a play that
restarted the video from scratch.

**Skipping around while paused now steps properly, and a skip press after resuming no longer
rewinds the video.** Seeking with more than a few seconds between presses could jump to the same
spot every time instead of moving further, and a skip pressed shortly after resuming from a pause
could throw the video back more than a minute. Both came from trusting a position report that was
out of date — one the TV had no way of updating while paused, or one it sent just after resuming
that still described where it had been. Presses now keep building on each other until the TV
reports something that can actually be true.

**The skip buttons work right after a video starts.** For the first stretch of a freshly started
video the TV may not have volunteered a playback position yet, and a skip press was refused with
"no current playback position". The app now asks for the position instead of waiting to be told.

**Seeking no longer occasionally answers "Lounge not connected."** The app's link to YouTube
recycles routinely — YouTube itself closes it every few minutes, and the app rebuilds it far more
often than that while a video is playing, because rebuilding is how it keeps the position fresh.
Each rebuild leaves a gap of about a fifth of a second, and a seek that happened to arrive inside
one was refused with an error toast for a link that was already back by the time you read it. Seeks
now wait out the blip and go through.

**A video could finish and leave the next one stuck in Up next.** If the last position report before
a video ended was a few seconds short of the end — which it often is, because the TV stops sending
updates near the end — the app decided the video had not finished, cleared the Now playing card and
stopped, with the queue still full. It now also trusts the video's own length having run out, while
still leaving alone a video you paused and walked away from.

**Waking a sleeping device is about ten seconds faster.** The app waited a fixed sixteen seconds
after sending the wake command before starting the video — a number that had never actually been
tested against a failure. It has now: on the reference device, five out of five cold starts played
just as reliably at six seconds as at sixteen, and the device reports itself awake in well under a
second every time. Most of that wait was doing nothing. There is still a floor, and it is now
measured from the moment the device reports itself awake rather than from when the command was sent,
so a device that answers slowly still gets its full settling time. If you have hardware that needs
longer, `WAKE_DELAY` is still yours to raise.

**Pressing Play no longer restarts a long video from the beginning.** If you paused, backed out of
the player, and came back, the app had to relaunch the video — and relaunched it from the start,
because the only position it passed along was the one from the original link. It now resumes where
you actually paused.

**The link to YouTube recovers itself if it goes quiet.** There was a state it could not get out of:
the connection stays open, YouTube stops sending anything, and nothing in the app was in a position
to notice — playback position stayed blank and pause and seek stayed unavailable until the container
was restarted. The app now stops believing in a connection that has delivered nothing for several
minutes and rebuilds it. (The approach is borrowed from a Rust client for the same protocol.)

**A video added while the device was asleep sometimes never played.** Found the cause, and it was
not what it looked like. If you put the device to sleep part-way through a video, YouTube's servers
carry on reporting that video as still playing — sometimes with the position still ticking up, on a
device that is switched off. The app believed it, decided SmartTube must have a player running, and
handed the new video straight to it. YouTube accepted that and it reached nothing: no video started,
the page showed it playing anyway, and about forty seconds later it quietly disappeared. The app now
takes the view that a device it has only just woken cannot have anything playing, whatever the
servers claim, and starts the video the way it starts one on a cold TV — which works. As a
side-effect this also made waking-and-playing about three seconds quicker, because the app no longer
waits on the servers before giving up on them.

**A queued video could fail to start when the one before it ended.** The same false "still playing"
report, arriving at a different moment. When a video finishes, YouTube's report of where it was can
be several seconds out of date, so the app would try to slide the next video into a player that had
already closed — accepted, and silently did nothing. The app now asks where playback actually is
before deciding, and when it cannot get a straight answer it starts the next video the way it starts
one on a sleeping TV.

**Up to nine seconds of dead air between queued videos.** Measured: a video's picture and sound
genuinely ended, and the next one did not begin for another nine seconds, with SmartTube's home
screen in between. The app was waiting on a stale report and then sleeping for as long as that
report claimed was left — compounding an error rather than re-checking it. It now looks again after
three seconds when the remaining time is short enough to be a stale reading. Videos whose length was
genuinely mis-read are unaffected.

**A video that never starts now says so.** A link that cannot play — deleted, private,
region-blocked, or just mistyped — has always been skipped after about 45 seconds so it does not
hold the queue. But it happened in silence: whoever pasted it watched their video get replaced by
someone else's with nothing to suggest the URL was the problem. The now-playing card now says which
video was dropped. If several in a row fail to start, it says that instead, and the queue is left
alone rather than marched through — that is usually the device, not the videos.

**A rejected paste no longer costs you ten seconds.** Paste a playlist link, a channel link, or a
typo, and the app refuses it — but it also used to start your ten-second cooldown, so the corrected
paste came straight back with "too many requests" from a service that had done nothing for you. The
cooldown is now spent only by a video the app actually accepts.

**Seek no longer tells you to go and fix your TV when nothing is wrong.** SmartTube's link to
YouTube drops out for up to a minute or so at a time, several times an hour, entirely normally.
Seeking genuinely cannot work during those gaps and is still refused — but it used to answer with
instructions to go and toggle a setting on your TV, for a fault that fixes itself within the minute.
It now says the link is recycling and to try again shortly, and keeps the TV instructions for a
connection that has genuinely stayed down.

**The progress bar no longer runs ahead of a video that hasn't started.** On a sleeping device the
card would give up waiting part-way through the wake, and start showing a progress bar climbing from
sixteen seconds for a video whose first frame was still several seconds away.

**Not a change, but worth knowing: SmartTube itself sometimes returns to the Google TV home screen
when a video ends.** Measured while investigating the above — the app sent nothing at all in the
twenty seconds before it happened. SmartTube backgrounds itself and then reopens on its own browse
screen; whatever was behind it shows through for about a quarter of a second in between. If a video
is queued behind, the app brings SmartTube straight back and you will not notice. That is
SmartTube's own behaviour and this app can neither cause nor prevent it.

---

## v1.03

**Pause did nothing, or a video vanished from the page while it kept playing.** SmartTube's
link to YouTube — the *Remote control* setting this app relies on for playback position, pause
and seek — can quietly die on the TV while the video plays on, and YouTube keeps accepting the
app's commands as if nothing were wrong. So the app believed them: Pause reported success while
the TV played on; every few minutes the app lost sight of the video, read that as you having
closed it, and took the now-playing card away with anything queued behind it left sitting
there; seeks did nothing; and a video started on the TV itself couldn't be shown at all. The
app now checks whether SmartTube is actually on the other end of that link. When it isn't, the
header says **SMARTTUBE NOT LISTENING** and the SmartTube link line reads **PAIRED · NOT
LISTENING**, with a note underneath saying what to do — and only once the link has genuinely
stayed gone for a few minutes, because the TV's own connection also takes brief dips that fix
themselves and those aren't worth an alarm; Pause goes over the TV-remote connection
instead, which does reach the TV, and Play after such a pause resumes where you paused rather
than starting the video over. Pause doesn't just take YouTube's word for it any more, either:
if the TV itself doesn't confirm within a few seconds, the TV-remote pause goes out as well —
so a pause works even in the minutes before the app can notice the link has died. The paused
position also now freezes where you paused it, instead of showing 0:00, when the exact position
isn't available. Adding a video no longer waits on a link that cannot answer;
and seeking says plainly why it can't. The video keeps playing and stays on the page.

**How to fix it — on the TV, not here.** Open **SmartTube → Settings → Remote control** and
switch it off and on again. If that doesn't clear it, force-stop SmartTube and reopen it; then
reboot the streaming device. The page catches up by itself within a few minutes, or the moment
anyone adds a video while SmartTube is still up on the TV. Restarting this app will *not* help —
it reconnects to the same dead link — and empties the queue. If Remote control now shows a fresh
12-digit code, SmartTube has a new identity and needs pairing again: set `RESET_LOUNGE=1` and
restart (below), then use the *Pair with SmartTube* card.

**Pressing Play with videos queued but nothing playing started two things at once.** It resumed
whatever SmartTube had left parked and launched the queued video in the same instant. Now only
the queued video starts.

**A pause pressed on the page could be recorded as one made with the TV remote**, if SmartTube's
own "paused" notice reached the app first. That mattered: a TV-remote pause near the end of a
video lets the queue move on, and lets another guest's add replace the paused video — neither of
which should happen to a video you paused yourself.

**`RESET_LOUNGE` re-pairs SmartTube without touching the TV pairing.** Set it to `1` and restart
to clear just the SmartTube link; the TV-remote pairing stays. Like `RESET_PAIRING`, it fires once
and then ignores itself, so it's safe to leave set. See [CONFIGURATION.md](docs/CONFIGURATION.md).

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
keeps up, and pausing or leaving SmartTube is reflected properly. It needs your device to
be awake and the app to be able to tell what is on screen; where it cannot, the position
stays still rather than risk pulling SmartTube to the front — or waking the device — just
to ask.

**Adding a video to a sleeping device that last played something else.** The app would
start the video and then immediately stop tracking it: it played, but with no now-playing
card, no countdown, and anything queued behind it left sitting there. YouTube's servers go
on reporting the last thing you watched as still playing, and several parts of the app read
that as "somebody has picked something else on the TV" while the new video was still being
launched.

**The same thing could skip a video you had queued twice in a row.** When two entries are
the same video, the app cannot tell a late report about the first copy from one about the
second — they carry the same id — so on a slow start the second copy was treated as already
finished and skipped before it had played.

Six different parts of the app can decide it no longer owns the current video. All six now
wait for the video to finish starting before drawing that conclusion.

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
- One more route to the same stall: if the countdown ran out at the very moment the video
  ended and SmartTube dropped to its home screen, the queue could still stop dead with
  everything still in it.
- A video shorter than the time it takes to start one — a Short, on a device waking from
  sleep — could be skipped before it had played at all.
- Pause could report success after the link to SmartTube had quietly expired, leaving the
  TV playing on under a page that said it was paused, with only Resume able to recover it.

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
