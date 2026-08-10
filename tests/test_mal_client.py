"""MyAnimeList OAuth2/PKCE + score/status push — SCOPE.md §5.5/§6.1/§6.9,
BUILD_PLAN.md B.10.

Pure client-layer coverage: no real network calls, an injected fake
httpx.Client stands in throughout — same pattern
test_anilist_client.py already established, adapted for MAL's real
REST + form-encoded (not GraphQL/JSON) shape.
"""

import httpx
import pytest

from lcars import mal_client


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class _FakeClient:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = 0
        self.last_data = None
        self.last_headers = None
        self.last_url = None

    def post(self, url, data=None, headers=None):
        return self._call(url, data, headers)

    def patch(self, url, data=None, headers=None):
        return self._call(url, data, headers)

    def _call(self, url, data, headers):
        self.calls += 1
        self.last_url = url
        self.last_data = data
        self.last_headers = headers
        if self._error is not None:
            raise self._error
        return self._response

    def close(self):
        pass


# --- PKCE helpers --------------------------------------------------------------


def test_generate_code_verifier_is_within_the_43_to_128_char_range():
    verifier = mal_client.generate_code_verifier()
    assert 43 <= len(verifier) <= 128


def test_generate_code_verifier_is_different_every_call():
    assert mal_client.generate_code_verifier() != mal_client.generate_code_verifier()


def test_authorize_url_uses_plain_challenge_method_and_the_verifier_as_challenge():
    url = mal_client.authorize_url("cid", "the-verifier")
    assert "client_id=cid" in url
    assert "code_challenge=the-verifier" in url
    assert "code_challenge_method=plain" in url
    assert "redirect_uri=http://localhost:1701/" in url


# --- exchange_code ---------------------------------------------------------------


def test_exchange_code_returns_access_and_refresh_tokens():
    fake = _FakeClient(
        response=_FakeResponse(payload={"access_token": "acc-1", "refresh_token": "ref-1"})
    )
    access, refresh = mal_client.exchange_code(
        "cid", "csecret", "authcode", "verifier", client=fake
    )
    assert (access, refresh) == ("acc-1", "ref-1")
    assert fake.last_data["code"] == "authcode"
    assert fake.last_data["code_verifier"] == "verifier"
    assert fake.last_data["client_secret"] == "csecret"


def test_exchange_code_omits_client_secret_when_not_set():
    # The real-world case, confirmed live 2026-08-10: MAL's "Other" app type
    # is a PKCE public client and issues no client_secret at all.
    fake = _FakeClient(
        response=_FakeResponse(payload={"access_token": "acc-1", "refresh_token": "ref-1"})
    )
    mal_client.exchange_code("cid", None, "authcode", "verifier", client=fake)
    assert "client_secret" not in fake.last_data


def test_exchange_code_raises_mal_auth_error_on_rejection():
    fake = _FakeClient(response=_FakeResponse(status_code=400, payload={"error": "invalid_grant"}))
    with pytest.raises(mal_client.MALAuthError, match="invalid_grant"):
        mal_client.exchange_code("cid", None, "badcode", "verifier", client=fake)


def test_exchange_code_raises_if_no_access_token_in_response():
    fake = _FakeClient(response=_FakeResponse(payload={"refresh_token": "ref-1"}))
    with pytest.raises(mal_client.MALError, match="access_token"):
        mal_client.exchange_code("cid", None, "code", "verifier", client=fake)


def test_exchange_code_raises_on_connect_error():
    fake = _FakeClient(error=httpx.ConnectError("boom"))
    with pytest.raises(mal_client.MALError, match="Could not connect"):
        mal_client.exchange_code("cid", None, "code", "verifier", client=fake)


def test_exchange_code_raises_on_timeout():
    fake = _FakeClient(error=httpx.TimeoutException("boom"))
    with pytest.raises(mal_client.MALError, match="Timed out"):
        mal_client.exchange_code("cid", None, "code", "verifier", client=fake)


# --- refresh_access_token ---------------------------------------------------------


def test_refresh_access_token_returns_the_new_pair():
    fake = _FakeClient(
        response=_FakeResponse(payload={"access_token": "acc-2", "refresh_token": "ref-2"})
    )
    access, refresh = mal_client.refresh_access_token("cid", None, "ref-1", client=fake)
    assert (access, refresh) == ("acc-2", "ref-2")
    assert fake.last_data["grant_type"] == "refresh_token"
    assert fake.last_data["refresh_token"] == "ref-1"


def test_refresh_access_token_falls_back_to_the_old_refresh_token_if_response_omits_one():
    # Module docstring: MAL's own docs don't clearly state whether the refresh
    # grant rotates refresh_token — handle both readings correctly.
    fake = _FakeClient(response=_FakeResponse(payload={"access_token": "acc-2"}))
    access, refresh = mal_client.refresh_access_token("cid", None, "ref-1", client=fake)
    assert (access, refresh) == ("acc-2", "ref-1")


def test_refresh_access_token_raises_mal_auth_error_when_the_refresh_token_itself_has_expired():
    fake = _FakeClient(response=_FakeResponse(status_code=401, payload={"error": "invalid_token"}))
    with pytest.raises(mal_client.MALAuthError, match="mal-login"):
        mal_client.refresh_access_token("cid", None, "expired-ref", client=fake)


def test_refresh_access_token_omits_client_secret_when_not_set():
    fake = _FakeClient(
        response=_FakeResponse(payload={"access_token": "acc-2", "refresh_token": "ref-2"})
    )
    mal_client.refresh_access_token("cid", None, "ref-1", client=fake)
    assert "client_secret" not in fake.last_data


# --- update_my_list_status (the push) ---------------------------------------------


def test_update_my_list_status_sends_both_status_and_score():
    fake = _FakeClient(response=_FakeResponse(payload={"status": "watching", "score": 8}))
    result = mal_client.update_my_list_status("tok", 123, status="watching", score=8, client=fake)
    assert result == {"status": "watching", "score": 8}
    assert fake.last_data == {"status": "watching", "score": 8}
    assert fake.last_headers == {"Authorization": "Bearer tok"}
    assert fake.last_url == "https://api.myanimelist.net/v2/anime/123/my_list_status"


def test_update_my_list_status_score_only_omits_status_field():
    fake = _FakeClient(response=_FakeResponse(payload={"score": 8}))
    mal_client.update_my_list_status("tok", 123, score=8, client=fake)
    assert "status" not in fake.last_data
    assert fake.last_data["score"] == 8


def test_update_my_list_status_status_only_omits_score_field():
    fake = _FakeClient(response=_FakeResponse(payload={"status": "dropped"}))
    mal_client.update_my_list_status("tok", 123, status="dropped", client=fake)
    assert "score" not in fake.last_data
    assert fake.last_data["status"] == "dropped"


def test_update_my_list_status_never_sends_is_rewatching_or_num_times_rewatched():
    # §6.8/§6.9 — rewatching never auto-toggles either MAL field.
    fake = _FakeClient(response=_FakeResponse(payload={"status": "watching"}))
    mal_client.update_my_list_status("tok", 123, status="watching", client=fake)
    assert "is_rewatching" not in fake.last_data
    assert "num_times_rewatched" not in fake.last_data


def test_update_my_list_status_raises_mal_auth_error_on_401():
    fake = _FakeClient(response=_FakeResponse(status_code=401, payload={"message": "bad token"}))
    with pytest.raises(mal_client.MALAuthError, match="mal-login"):
        mal_client.update_my_list_status("bad-tok", 123, score=8, client=fake)


def test_update_my_list_status_raises_mal_error_on_other_http_errors():
    fake = _FakeClient(response=_FakeResponse(status_code=500, payload=None))
    with pytest.raises(mal_client.MALError, match="HTTP 500"):
        mal_client.update_my_list_status("tok", 123, score=8, client=fake)


def test_update_my_list_status_raises_on_connect_error():
    fake = _FakeClient(error=httpx.ConnectError("boom"))
    with pytest.raises(mal_client.MALError, match="Could not connect"):
        mal_client.update_my_list_status("tok", 123, score=8, client=fake)
