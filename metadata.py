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


_VIDEO_ID_IN_SEARCH = re.compile(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"')

# Bound on watch-page verification calls per lookup. The cost of a wrong-thumbnail
# bug is mostly cosmetic, but going past ~5 candidates rarely improves the result.
MAX_VERIFY_CANDIDATES = 5


def _normalize(s: str) -> str:
    """Casefold + collapse whitespace + strip surrounding punct/space."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.casefold()).strip()


def _title_channel_matches(md: "Metadata", title: str, channel: str) -> bool:
    """Bidirectional substring match on normalized strings.

    'bidirectional' because either side may be truncated in real data: Cast
    sometimes truncates long titles, YouTube sometimes appends/strips
    suffixes. Channel names are usually exact but we relax the match the
    same way for safety.
    """
    a_t, b_t = _normalize(md.title), _normalize(title)
    a_c, b_c = _normalize(md.channel), _normalize(channel)
    title_ok = bool(a_t and b_t) and (a_t in b_t or b_t in a_t)
    chan_ok = bool(a_c and b_c) and (a_c in b_c or b_c in a_c)
    return title_ok and chan_ok


async def find_video_id_by_title(
    title: str,
    *,
    expected_channel: Optional[str] = None,
    max_candidates: int = MAX_VERIFY_CANDIDATES,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[str]:
    """Look up a YouTube video ID by title via the search-results page.

    Two-layer accuracy:
    1. **Disambiguate the search.** If `expected_channel` is given, it's
       added to the query (e.g. 'Live Stream HANA' instead of just
       'Live Stream'). This dramatically improves the first-page hit rate
       for generic titles.
    2. **Verify each candidate.** With `expected_channel`, we fetch the
       top N candidates' watch pages and compare title+channel before
       accepting one. Without `expected_channel` (no verification signal)
       we trust the first search result — best-effort.

    Returns None if no candidate verifies, in which case callers should
    show a placeholder rather than a possibly-wrong thumbnail.
    """
    if not title:
        return None

    query = f"{title} {expected_channel}".strip() if expected_channel else title
    url = "https://www.youtube.com/results"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    params = {"search_query": query}

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=True,
            max_redirects=3,
        )
    try:
        try:
            resp = await client.get(
                url, headers=headers, params=params, timeout=FETCH_TIMEOUT_S,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("YouTube search failed for %r: %s", title, e)
            return None

        # Dedupe while preserving order — same video may appear multiple times
        # (in main results + a "from same channel" rail).
        candidates = list(dict.fromkeys(_VIDEO_ID_IN_SEARCH.findall(resp.text)))
        candidates = candidates[:max_candidates]
        if not candidates:
            return None

        # No channel signal → trust the first result, no verification possible.
        if not expected_channel:
            return candidates[0]

        # Verify by fetching each candidate's watch page until one matches.
        for vid in candidates:
            try:
                md = await fetch_metadata(vid, client=client)
            except Exception:
                log.debug("Verify fetch raised for %s; skipping", vid, exc_info=True)
                continue
            if md.scrape_ok and _title_channel_matches(md, title, expected_channel):
                return vid
        log.info("No candidate verified for title=%r channel=%r", title, expected_channel)
        return None
    finally:
        if own_client:
            await client.aclose()


def thumbnail_url_for(video_id: str) -> str:
    return _thumbnail_for(video_id)


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
            return parse_metadata(video_id, resp.text)
        except httpx.HTTPError as e:
            log.warning("metadata fetch failed for %s: %s", video_id, e)
            return _fallback(video_id)
    finally:
        if own_client:
            await client.aclose()
