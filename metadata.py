"""YouTube watch-page metadata scraper.

Server-side scrape of the YouTube watch page to pull title, channel, duration,
and live-broadcast flag for a given video ID. Used by the queue to decide how
long the auto-advance timer should run and to render the now-playing card.

The scrape can fail (network blip, age-restricted, region-locked, page layout
change). On failure the caller still gets a usable Metadata object filled with
defaults — only the *video ID extraction* upstream is allowed to hard-fail.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger("smarttube-playlist.metadata")

DEFAULT_DURATION_S = int(os.environ.get("DEFAULT_DURATION_S", "600"))
# The watch page is 1.1-1.6 MiB and its size varies run to run, so this needs
# real headroom — a NAS on a slow link routinely needs more than a few seconds.
# Overrunning it isn't a cosmetic failure: the fallback duration feeds the
# auto-advance timer, so a timed-out scrape cuts long videos short.
FETCH_TIMEOUT_S = float(os.environ.get("METADATA_TIMEOUT_S", "15.0"))

# A recent desktop Chrome UA. YouTube serves a different (lighter) page to
# obviously-bot UAs, sometimes without ytInitialPlayerResponse.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# The watch page sets `var ytInitialPlayerResponse = {...};` inside a <script>.
# We grab the JSON object using a brace-matching scan rather than a greedy
# regex, since the value contains nested braces and strings.
_PR_PREFIX = re.compile(r"var\s+ytInitialPlayerResponse\s*=\s*")


@dataclass
class Metadata:
    video_id: str
    title: str
    channel: str
    duration_s: Optional[int]   # None => livestream, no auto-advance
    is_live: bool
    thumbnail_url: str
    scrape_ok: bool             # False if we fell back to defaults


def _thumbnail_for(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def _fallback(video_id: str) -> Metadata:
    return Metadata(
        video_id=video_id,
        title=video_id,
        channel="unknown",
        duration_s=DEFAULT_DURATION_S,
        is_live=False,
        thumbnail_url=_thumbnail_for(video_id),
        scrape_ok=False,
    )


_SEARCH_URL = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
_SEARCH_CONTEXT = {
    "context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00",
                           "hl": "en", "gl": "US"}},
}


def _parse_length_text(text: str) -> Optional[int]:
    """'10:35' -> 635, '1:02:03' -> 3723. None for anything else (LIVE, '')."""
    if not text or ":" not in text:
        return None
    parts = text.strip().split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total if total > 0 else None


def _find_video_renderer(node, video_id: str) -> Optional[dict]:
    """Depth-first search of an InnerTube response for OUR videoRenderer.

    Renderer nesting differs between clients and changes over time; matching
    on the videoId wherever it sits is what survives that."""
    if isinstance(node, dict):
        vr = node.get("videoRenderer")
        if isinstance(vr, dict) and vr.get("videoId") == video_id:
            return vr
        for v in node.values():
            found = _find_video_renderer(v, video_id)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_video_renderer(v, video_id)
            if found is not None:
                return found
    return None


def _runs_text(obj) -> Optional[str]:
    runs = (obj or {}).get("runs") if isinstance(obj, dict) else None
    if runs and isinstance(runs, list) and runs[0].get("text"):
        return runs[0]["text"]
    simple = (obj or {}).get("simpleText") if isinstance(obj, dict) else None
    return simple or None


async def _search_rescue(
    video_id: str, client: httpx.AsyncClient,
) -> Optional[Metadata]:
    """Full metadata from the InnerTube SEARCH endpoint, keyed by video id.

    Measured live 2026-08-31 while the bot wall was up: the watch page and
    every InnerTube PLAYER client identity (web, TV, Android, iOS, SmartTube's
    own Quest identity) answered "Sign in to confirm you're not a bot" — the
    wall is applied per address on the player surface. The SEARCH surface was
    not walled, needs no API key, and a query that is just the video id puts
    the video's own renderer in the results: title, owner, `lengthText` and
    a LIVE overlay for livestreams. Unlike oEmbed that includes the DURATION,
    which is what a QUEUED video shows on its card and what arms the
    auto-advance timer — a queued video has no Lounge frame to correct a
    600s guess, and the wrong length was reported from the queue within
    minutes of the wall going up.
    """
    try:
        resp = await client.post(
            _SEARCH_URL,
            json={**_SEARCH_CONTEXT, "query": video_id},
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            timeout=FETCH_TIMEOUT_S,
        )
        resp.raise_for_status()
        vr = _find_video_renderer(resp.json(), video_id)
        if vr is None:
            return None
        title = _runs_text(vr.get("title"))
        if not title:
            return None
        channel = _runs_text(vr.get("ownerText")) or "unknown"
        live = False
        for overlay in vr.get("thumbnailOverlays") or []:
            status = (overlay or {}).get("thumbnailOverlayTimeStatusRenderer") or {}
            if status.get("style") == "LIVE":
                live = True
        duration = None if live else _parse_length_text(
            _runs_text(vr.get("lengthText")) or "")
        if not live and duration is None:
            # A renderer with neither a length nor a live badge is not enough
            # to trust; let the next tier try.
            return None
        return Metadata(
            video_id=video_id,
            title=title,
            channel=channel,
            duration_s=duration,
            is_live=live,
            thumbnail_url=_thumbnail_for(video_id),
            scrape_ok=True,
        )
    except Exception:
        log.info("search rescue failed for %s", video_id, exc_info=True)
        return None


_OEMBED_URL = "https://www.youtube.com/oembed"


async def _oembed_rescue(
    video_id: str, client: httpx.AsyncClient,
) -> Optional[Metadata]:
    """Title and uploader from the oEmbed endpoint, when the watch page fails.

    Hit live 2026-08-31: after a day of heavy fetching from one address,
    YouTube served the watch page with HTTP 200 and no videoDetails ("Sign in
    to confirm" — the bot wall), and every card fell back to title=video_id /
    channel=unknown. oEmbed is not walled the same way and carries exactly
    the two fields a guest actually reads on the card. It has no duration and
    no live flag, so the result keeps the documented fallback duration and
    `scrape_ok=False` — the Lounge-reported duration corrects the
    auto-advance timer at runtime, as it already does for a wrong scrape.
    """
    try:
        resp = await client.get(
            _OEMBED_URL,
            params={"url": f"https://www.youtube.com/watch?v={video_id}",
                    "format": "json"},
            timeout=FETCH_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        title = data.get("title")
        if not title:
            return None
        return Metadata(
            video_id=video_id,
            title=title,
            channel=data.get("author_name") or "unknown",
            duration_s=DEFAULT_DURATION_S,
            is_live=False,
            thumbnail_url=_thumbnail_for(video_id),
            scrape_ok=False,
        )
    except Exception:
        log.info("oEmbed rescue failed for %s", video_id, exc_info=True)
        return None


def _extract_player_response(html: str) -> Optional[dict]:
    """Find `var ytInitialPlayerResponse = {...}` and return the parsed JSON."""
    m = _PR_PREFIX.search(html)
    if not m:
        return None
    start = m.end()
    # The value should begin with `{`. Walk the string, tracking brace depth
    # while respecting JSON string literals and escapes, until we find the
    # matching closing brace.
    if start >= len(html) or html[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(html)):
        c = html[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                blob = html[start : i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    log.warning("ytInitialPlayerResponse JSON decode failed")
                    return None
    return None


def parse_metadata(video_id: str, html: str) -> Metadata:
    """Pure parser — separated from the network call so tests can hit it directly."""
    pr = _extract_player_response(html)
    if not pr:
        log.warning("No ytInitialPlayerResponse found for %s", video_id)
        return _fallback(video_id)

    details = pr.get("videoDetails")
    if not isinstance(details, dict):
        log.warning("videoDetails missing for %s", video_id)
        return _fallback(video_id)

    is_live = bool(details.get("isLive", False))
    title = details.get("title") or video_id
    channel = details.get("author") or "unknown"

    if is_live:
        duration_s: Optional[int] = None
    else:
        raw_len = details.get("lengthSeconds")
        try:
            duration_s = int(raw_len) if raw_len is not None else DEFAULT_DURATION_S
            if duration_s <= 0:
                duration_s = DEFAULT_DURATION_S
        except (TypeError, ValueError):
            duration_s = DEFAULT_DURATION_S

    return Metadata(
        video_id=video_id,
        title=title,
        channel=channel,
        duration_s=duration_s,
        is_live=is_live,
        thumbnail_url=_thumbnail_for(video_id),
        scrape_ok=True,
    )


async def fetch_metadata(
    video_id: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Metadata:
    """Fetch the YouTube watch page and parse out metadata.

    Returns a Metadata object on every non-catastrophic outcome — fields fall
    back to sensible defaults if the scrape fails. Only an unparseable input
    upstream (no extractable video ID) should ever surface as an error.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=True,
            max_redirects=3,
        )
    try:
        try:
            # Pass the timeout per-request rather than relying on the client's
            # own. Callers share one long-lived AsyncClient (app.py builds it
            # with a much tighter default), and without this override that
            # client's timeout silently wins and METADATA_TIMEOUT_S does
            # nothing on the only path the app actually takes.
            resp = await client.get(url, headers=headers, timeout=FETCH_TIMEOUT_S)
            resp.raise_for_status()
            md = parse_metadata(video_id, resp.text)
            if not md.scrape_ok:
                # The page came back but the parse fell back — the bot wall,
                # a layout change, an age gate. Search still carries the full
                # card including the duration; oEmbed at least names it.
                rescued = (await _search_rescue(video_id, client)
                           or await _oembed_rescue(video_id, client))
                if rescued is not None:
                    return rescued
            return md
        except httpx.HTTPError as e:
            log.warning("metadata fetch failed for %s: %s", video_id, e)
            rescued = (await _search_rescue(video_id, client)
                       or await _oembed_rescue(video_id, client))
            if rescued is not None:
                return rescued
            return _fallback(video_id)
    finally:
        if own_client:
            await client.aclose()
