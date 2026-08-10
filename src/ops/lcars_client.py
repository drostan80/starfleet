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

    async def _walk_connection(
        self, query: str, field_name: str, variables: dict | None = None
    ) -> list[dict]:
        """Generic Relay-cursor page walker — every flat top-level
        `dueForX`/`shows` query shares this exact shape (B.1/B.2); the
        nested `show { seasons }` walk in all_seasons() below needs its
        own inline loop instead, since its connection lives one level
        deeper than `data[field_name]`."""
        variables = dict(variables or {})
        items: list[dict] = []
        after = None
        while True:
            variables["after"] = after
            data = await self._query(query, variables)
            connection = data[field_name]
            items.extend(edge["node"] for edge in connection["edges"])
            if not connection["pageInfo"]["hasNextPage"]:
                break
            after = connection["pageInfo"]["endCursor"]
        return items

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
        return await self._walk_connection(query, "dueForMetadataRefresh")

    async def due_for_season_reconciliation(self) -> list[dict]:
        """§5.5/B.2 — the weekly tier: every season of a watching+
        actively-airing show not Fribb-reconciled in the last 7 days
        (Query.dueForSeasonReconciliation, already filtered server-side
        — self-gating, so Ops can check this as often as convenient)."""
        query = """
        query($after: String) {
          dueForSeasonReconciliation(first: 50, after: $after) {
            edges { node { id seasonNumber show { id } } }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        return await self._walk_connection(query, "dueForSeasonReconciliation")

    async def all_seasons(self) -> list[dict]:
        """§5.5/B.2 — the monthly tier: every season of every show,
        unconditional. No due-query exists for this tier (SCOPE.md
        §5.5's B.2 note: Ops's own monthly timer is the correctness
        boundary, not a stored ceiling) — walks the full library
        directly: every show, then every one of its seasons."""
        shows_query = """
        query($after: String) {
          shows(first: 50, after: $after) {
            edges { node { id } }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        seasons_query = """
        query($id: ID!, $after: String) {
          show(id: $id) {
            seasons(first: 50, after: $after) {
              edges { node { id seasonNumber show { id } } }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        shows = await self._walk_connection(shows_query, "shows")
        seasons: list[dict] = []
        for show in shows:
            after = None
            while True:
                data = await self._query(seasons_query, {"id": show["id"], "after": after})
                connection = data["show"]["seasons"]
                seasons.extend(edge["node"] for edge in connection["edges"])
                if not connection["pageInfo"]["hasNextPage"]:
                    break
                after = connection["pageInfo"]["endCursor"]
        return seasons

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

    async def reconcile_season_mapping(self, show_id: str, season_number: int) -> dict:
        """The exact same mutation A.4 built (`reconcileSeasonMapping`)
        — both B.2 tiers (weekly and monthly) call this one mutation, no
        Ops-specific variant, same precedent refresh_show_metadata()
        above already established."""
        query = """
        mutation($id: ID!, $season: Int!) {
          reconcileSeasonMapping(showId: $id, seasonNumber: $season) { id }
        }
        """
        data = await self._query(query, {"id": show_id, "season": season_number})
        return data["reconcileSeasonMapping"]

    async def poll_file_availability(self) -> dict:
        """§5.2/§6.7, B.3 — the global availability sweep
        (pollFileAvailability): no per-item argument, since one call
        covers every tracked show at once (LCARS's own checkpoint state
        keeps repeat calls cheap, not Ops filtering anything itself)."""
        query = """
        mutation { pollFileAvailability { episodesUpdated showsUpdated } }
        """
        data = await self._query(query)
        return data["pollFileAvailability"]

    async def backfill_file_availability(self) -> dict:
        """§5.2/§6.7, B.3 — the manual, one-time counterpart to
        poll_file_availability() above: walks each configured service's
        entire history, ignoring any existing checkpoint. Never called
        by scheduler.py's own automatic loop — only by the explicit
        `ops backfill-availability` CLI command (cli.py), run
        deliberately by a human, since it blocks LCARS's single
        request-handling thread for real seconds-to-minutes while it
        runs (schema.graphql's own backfillFileAvailability docstring
        has the full rationale)."""
        query = """
        mutation { backfillFileAvailability { episodesUpdated showsUpdated } }
        """
        data = await self._query(query)
        return data["backfillFileAvailability"]

    async def audit_local_files(self) -> dict:
        """§5.2/§6.10, B.3b — the local file audit: two passes per
        configured service (a pure-API current-state reconciliation,
        plus a filesystem-reading orphan/untracked-show discovery pass
        — see schema.graphql's auditLocalFiles docstring for the full
        rationale). Never called by scheduler.py's own automatic loop —
        only by the explicit `ops audit-local-files` CLI command
        (cli.py), run deliberately by a human."""
        query = """
        mutation {
          auditLocalFiles {
            episodesCorrected
            showsCorrected
            orphanFiles { showId path parsedSeason parsedEpisode }
            untrackedShows { service title externalId path }
          }
        }
        """
        data = await self._query(query)
        return data["auditLocalFiles"]

    async def poll_anime_schedule(self) -> dict:
        """§6.7, B.5 — the global animeschedule.net RSS sweep
        (pollAnimeSchedule): no per-item argument, one call matches the
        whole feed against every watching+airing anime show at once.
        Called by scheduler.py's own automatic loop, riding the same
        hourly tick as run_daily_and_weekly_once — the feed's own
        rolling window rotates faster than a day (animeschedule.py's
        own docstring), so it can't wait for B.1/B.4's daily cadence."""
        query = """
        mutation { pollAnimeSchedule { episodesUpdated flagged } }
        """
        data = await self._query(query)
        return data["pollAnimeSchedule"]

    async def poll_local_service_presence(self) -> dict:
        """§5.4/§6.7, B.7 — the `local` pseudo-service rollup
        (pollLocalServicePresence): pure SQL aggregate, no external
        dependency, negligible cost. Rides the same hourly tick as
        run_daily_and_weekly_once, unlike poll_catalog_service_presence
        below."""
        query = """
        mutation { pollLocalServicePresence { showsUpdated } }
        """
        data = await self._query(query)
        return data["pollLocalServicePresence"]

    async def poll_catalog_service_presence(self) -> dict:
        """§5.4/§6.7, B.7 — the Sonarr/Radarr catalog-matching sweep
        (pollCatalogServicePresence): real N×M cost, confirmed with the
        user — rides B.2's own monthly cadence instead of the hourly
        tick, no new interval."""
        query = """
        mutation { pollCatalogServicePresence { showsUpdated } }
        """
        data = await self._query(query)
        return data["pollCatalogServicePresence"]

    async def reconcile_episode_movie_links(self) -> dict:
        """§5.1/§6.7, B.8b — episode_movie_link.py's own automatic
        tmdb_match derivation: no external HTTP call anywhere in the
        sweep (pure internal SQL reconciliation), so it rides the same
        hourly tick as run_daily_and_weekly_once, alongside
        poll_local_service_presence above."""
        query = """
        mutation {
          reconcileEpisodeMovieLinks {
            matched
            flagged
            unmatched
            availabilitySynced
          }
        }
        """
        data = await self._query(query)
        return data["reconcileEpisodeMovieLinks"]

    async def recommended_availability_poll_interval_seconds(self) -> int:
        """§5.2/§6.7, B.3 — LCARS computes Ops's own adaptive cadence
        (300s/900s/3600s) server-side, from data only it holds (watching
        shows' episode air dates) — Ops just asks and acts on the
        answer, same "server decides, client acts" split B.1/B.2's own
        dueForX queries already established."""
        query = """
        query { recommendedAvailabilityPollIntervalSeconds }
        """
        data = await self._query(query)
        return data["recommendedAvailabilityPollIntervalSeconds"]
