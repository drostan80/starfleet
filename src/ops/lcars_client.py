"""Ops's own GraphQL client for LCARS — B.1, SCOPE.md §11.2's "Resolved
2026-08-09 (B.1)" note. Ops holds no database connection at all; every
responsibility it has acts through LCARS's own API, same as Data/
Holodeck/Captain's Log (§3 principle 8's peer-client framing).

Close port of Data's own real, working `lcars_client.py`
(`~/repos/data/src/data/lcars_client.py`) — same bearer-token +
`X-LCARS-Client` header shape, `httpx.AsyncClient`, the same
401-vs-network-vs-GraphQL-error split. `X-LCARS-Client: ops` here
instead of `"data"` — a free-text value, not constrained by the
`ResolvedByClient` GraphQL enum (that enum is specific to
`resolvePendingReview`, §5.6, a human-facing action Ops doesn't
perform) — §5.7 already names `sonarr_sync`/`anilist_sync`-shaped
process actors as legitimate `changed_by` values distinct from the
four human-facing clients, so "ops" fits the same, already-anticipated
shape.
"""

import httpx


class LcarsError(Exception):
    pass


class LcarsAuthError(LcarsError):
    """Specifically a 401 — the bearer token is missing/wrong, as
    opposed to a network blip or a GraphQL validation error."""


class LcarsClient:
    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {bearer_token}", "X-LCARS-Client": "ops"},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> "LcarsClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _query(self, query: str, variables: dict | None = None) -> dict:
        try:
            response = await self._client.post(
                "/", json={"query": query, "variables": variables or {}}
            )
        except httpx.ConnectError as e:
            raise LcarsError("Could not connect to LCARS") from e
        except httpx.TimeoutException as e:
            raise LcarsError("Timed out talking to LCARS") from e

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.status_code == 401:
            raise LcarsAuthError("LCARS rejected the bearer token (401 Unauthorized)")
        if payload and payload.get("errors"):
            messages = "; ".join(e.get("message", "unknown error") for e in payload["errors"])
            raise LcarsError(f"LCARS GraphQL error: {messages}")
        if response.status_code >= 400:
            raise LcarsError(f"LCARS returned an error: HTTP {response.status_code}")
        # A malformed 200 (non-JSON body, or JSON with neither "data" nor
        # "errors") — checked explicitly rather than left to raise a raw
        # TypeError/KeyError below. Ops is an unattended daemon
        # (scheduler.py's run_forever), not Data's TUI — an uncaught
        # non-LcarsError here would silently kill the whole process rather
        # than being caught by run_once/run_forever's own LcarsError
        # handling, exactly the failure mode this client exists to guard
        # against. Data's own real lcars_client.py has this same gap
        # (unfixed here — a different repo, out of this step's scope).
        if not payload or "data" not in payload:
            raise LcarsError(f"LCARS returned an unexpected response: {response.text!r}")

        return payload["data"]

    async def due_for_metadata_refresh(self) -> list[dict]:
        """§6.7/B.1 — every show the daily pass owes a refresh, walked
        across every page (Query.dueForMetadataRefresh, itself already
        filtered server-side to watching-status + actively-airing +
        not-yet-refreshed-today — Ops does no filtering of its own)."""
        query = """
        query($after: String) {
          dueForMetadataRefresh(first: 50, after: $after) {
            edges { node { id displayTitle } }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        shows: list[dict] = []
        after = None
        while True:
            data = await self._query(query, {"after": after})
            connection = data["dueForMetadataRefresh"]
            shows.extend(edge["node"] for edge in connection["edges"])
            if not connection["pageInfo"]["hasNextPage"]:
                break
            after = connection["pageInfo"]["endCursor"]
        return shows

    async def refresh_show_metadata(self, show_id: str) -> dict:
        """The exact same manual-retry mutation A.8 built
        (`refreshShowMetadata`) — Ops's daily pass and a client's own
        on-open trigger both call this one mutation, no Ops-specific
        variant (SCOPE.md §11.2's B.1 note)."""
        query = """
        mutation($id: ID!) { refreshShowMetadata(showId: $id) { id } }
        """
        data = await self._query(query, {"id": show_id})
        return data["refreshShowMetadata"]
