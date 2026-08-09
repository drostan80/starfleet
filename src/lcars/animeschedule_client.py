"""animeschedule.net polling — SCOPE.md §6.7/§10.1, BUILD_PLAN.md B.5.

**Resolved 2026-08-09 (B.5), route settled by live testing, not
doc-reading** — the REST API v3 originally preferred by BUILD_PLAN.md's
own B.5 text splits into two endpoints with two different access
levels, confirmed by real unauthenticated calls: `/api/v3/anime`
(including its `anilist-ids` filter) returns 200 with no token at all,
but `/api/v3/timetables/{airType}` — the one actually carrying
per-episode `episodeDate`/`episodeNumber` — returns 401 "Unauthorized.
Use private endpoint." No self-serve registration page exists
(`/api`, `/api/v3`, `/about/api`, `/api-docs` all 404), so getting
access isn't a simple token-request; asked the user directly rather
than guess at an undocumented process. Decision: fall back to the RSS
feeds outright (BUILD_PLAN.md's own documented fallback), accepting
the free-text/fuzzy-matching tradeoff §10.1 originally flagged RSS for.

Only the **raw** feed (`/jpnrss.xml`) is fetched — its own item text
("has been released *natively*") is the native/Japan broadcast
release, matching `episode.air_date_utc`'s existing semantics (the
same real-world event AniList's `airingSchedule` and Sonarr's raw date
both describe). `/subrss.xml`/`/dubrss.xml` describe a fansub/dub
group's own release timing, a different, later event this project
doesn't model — not fetched.

Confirmed live: the raw feed holds a fixed ~25-item rolling window that
covered barely 16 hours of real releases in one snapshot — global
release volume rotates the window faster than a day, so `animeschedule.py`
polls this every scheduler tick (hourly default), not on B.1/B.4's daily
cadence; see that module's own docstring.
"""

import re
from datetime import datetime
from xml.etree import ElementTree

import httpx

RAW_FEED_URL = "https://animeschedule.net/jpnrss.xml"

# "Episode 42 of Digimon BeatBreak is out!" — confirmed live against the
# real feed (module docstring). `.+?` non-greedy so a title itself
# containing " is out!" (unlikely, but titles are free text) doesn't
# swallow past the real boundary.
_TITLE_RE = re.compile(r"^Episode\s+(\d+)\s+of\s+(.+?)\s+is out!$")


class AnimeScheduleError(Exception):
    pass


def fetch_raw_feed(client: httpx.Client | None = None) -> list[dict]:
    """Fetches and parses `/jpnrss.xml`, returns a list of
    `{"guid": str, "title": str, "episode": int, "air_date_utc": str}`
    — `air_date_utc` already converted to this project's standard
    Z-suffixed UTC string (`util.now_utc_iso()`'s own format), parsed
    from the feed's RFC 2822 `pubDate`. An item whose `<title>` doesn't
    match the "Episode N of TITLE is out!" shape (unexpected feed
    change) is skipped, not raised on — best-effort, same reasoning as
    every other Phase B poller (a malformed item shouldn't blank a
    whole sweep)."""
    owns_client = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        try:
            response = client.get(RAW_FEED_URL)
        except httpx.ConnectError as e:
            raise AnimeScheduleError("Could not connect to animeschedule.net") from e
        except httpx.TimeoutException as e:
            raise AnimeScheduleError("Timed out talking to animeschedule.net") from e

        if response.status_code != 200:
            raise AnimeScheduleError(
                f"animeschedule.net returned an error: HTTP {response.status_code}"
            )

        try:
            root = ElementTree.fromstring(response.text)
        except ElementTree.ParseError as e:
            raise AnimeScheduleError("animeschedule.net returned unparseable RSS") from e

        items = []
        for item in root.iterfind("./channel/item"):
            title_el = item.find("title")
            guid_el = item.find("guid")
            pub_date_el = item.find("pubDate")
            if title_el is None or title_el.text is None:
                continue
            if guid_el is None or guid_el.text is None:
                continue
            if pub_date_el is None or pub_date_el.text is None:
                continue
            match = _TITLE_RE.match(title_el.text.strip())
            if match is None:
                continue
            try:
                air_date_utc = _parse_pub_date(pub_date_el.text)
            except ValueError:
                continue
            items.append(
                {
                    "guid": guid_el.text.strip(),
                    "title": match.group(2),
                    "episode": int(match.group(1)),
                    "air_date_utc": air_date_utc,
                }
            )
        return items
    finally:
        if owns_client:
            client.close()


def _parse_pub_date(raw: str) -> str:
    """RFC 2822 (`email.utils`'s own format, e.g. "Sun, 09 Aug 2026
    16:00:00 UTC") -> this project's standard `YYYY-MM-DDTHH:MM:SSZ`.
    `datetime.strptime` over `email.utils.parsedate_to_datetime` —
    the feed's own `UTC` literal (not a numeric `+0000` offset)
    confirmed live isn't accepted by the latter."""
    dt = datetime.strptime(raw.strip(), "%a, %d %b %Y %H:%M:%S %Z")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
