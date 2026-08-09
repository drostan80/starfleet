"""animeschedule.net raw RSS feed fetch/parse — SCOPE.md §6.7/§10.1,
BUILD_PLAN.md B.5. Pure client-layer coverage: no real network call, an
injected fake httpx.Client stands in throughout — same pattern
tests/test_anilist_client.py's own _FakeClient already uses.
"""

import httpx
import pytest

from lcars import animeschedule_client

_FEED_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0">
  <channel>
    <title>AnimeSchedule.net Anime Times RAW</title>
    {items}
  </channel>
</rss>"""

_ITEM_TEMPLATE = """
    <item>
      <title>Episode {episode} of {title} is out!</title>
      <link>https://AnimeSchedule.net/anime/whatever</link>
      <description>Episode {episode} of {title} has been released natively!</description>
      <guid>{title} Episode {episode}</guid>
      <pubDate>{pub_date}</pubDate>
    </item>"""


class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = 0
        self.last_url = None

    def get(self, url):
        self.calls += 1
        self.last_url = url
        if self._error is not None:
            raise self._error
        return self._response

    def close(self):
        pass


def _feed(*items: str) -> str:
    return _FEED_TEMPLATE.format(items="".join(items))


def test_fetch_raw_feed_parses_title_episode_and_air_date():
    xml = _feed(
        _ITEM_TEMPLATE.format(
            episode=42, title="Digimon BeatBreak", pub_date="Sun, 09 Aug 2026 00:00:00 UTC"
        )
    )
    fake = _FakeClient(response=_FakeResponse(text=xml))
    items = animeschedule_client.fetch_raw_feed(client=fake)
    assert items == [
        {
            "guid": "Digimon BeatBreak Episode 42",
            "title": "Digimon BeatBreak",
            "episode": 42,
            "air_date_utc": "2026-08-09T00:00:00Z",
        }
    ]
    assert fake.last_url == animeschedule_client.RAW_FEED_URL


def test_fetch_raw_feed_parses_multiple_items_in_order():
    xml = _feed(
        _ITEM_TEMPLATE.format(episode=1, title="Show A", pub_date="Sun, 09 Aug 2026 00:00:00 UTC"),
        _ITEM_TEMPLATE.format(episode=2, title="Show B", pub_date="Sun, 09 Aug 2026 01:00:00 UTC"),
    )
    fake = _FakeClient(response=_FakeResponse(text=xml))
    items = animeschedule_client.fetch_raw_feed(client=fake)
    assert [i["title"] for i in items] == ["Show A", "Show B"]


def test_fetch_raw_feed_is_empty_with_no_items():
    fake = _FakeClient(response=_FakeResponse(text=_feed()))
    assert animeschedule_client.fetch_raw_feed(client=fake) == []


def test_fetch_raw_feed_skips_a_title_not_matching_the_expected_shape():
    xml = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0">
      <channel>
        <item>
          <title>Some unrelated announcement</title>
          <guid>whatever</guid>
          <pubDate>Sun, 09 Aug 2026 00:00:00 UTC</pubDate>
        </item>
      </channel>
    </rss>"""
    fake = _FakeClient(response=_FakeResponse(text=xml))
    assert animeschedule_client.fetch_raw_feed(client=fake) == []


def test_fetch_raw_feed_raises_on_http_error_status():
    fake = _FakeClient(response=_FakeResponse(status_code=500, text=""))
    with pytest.raises(animeschedule_client.AnimeScheduleError, match="HTTP 500"):
        animeschedule_client.fetch_raw_feed(client=fake)


def test_fetch_raw_feed_raises_on_connect_error():
    fake = _FakeClient(error=httpx.ConnectError("boom"))
    with pytest.raises(animeschedule_client.AnimeScheduleError, match="Could not connect"):
        animeschedule_client.fetch_raw_feed(client=fake)


def test_fetch_raw_feed_raises_on_timeout():
    fake = _FakeClient(error=httpx.TimeoutException("boom"))
    with pytest.raises(animeschedule_client.AnimeScheduleError, match="Timed out"):
        animeschedule_client.fetch_raw_feed(client=fake)


def test_fetch_raw_feed_raises_on_unparseable_xml():
    fake = _FakeClient(response=_FakeResponse(text="not xml at all <<<"))
    with pytest.raises(animeschedule_client.AnimeScheduleError, match="unparseable"):
        animeschedule_client.fetch_raw_feed(client=fake)
