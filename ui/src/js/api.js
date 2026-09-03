/**
 * GraphQL HTTP client.
 *
 * WebSocket subscriptions (episodeAvailabilityChanged / showCreated) are
 * NOT implemented here — LCARS's BearerTokenMiddleware reads the bearer
 * token from the HTTP upgrade request headers, which browsers cannot set
 * on WebSocket connections. Phase 1 uses polling instead.
 *
 * TODO (future): LCARS could be extended to also validate the bearer token
 * from the graphql-transport-ws connection_init payload, which browsers
 * CAN set. Once that lands, implement subscriptions here.
 */

import { getConfig } from './config.js?v=2';

/**
 * Execute a GraphQL query or mutation against LCARS.
 * @param {string} query - GraphQL document
 * @param {object} variables - Variables map
 * @returns {Promise<object>} data portion of the response
 * @throws {Error} on HTTP error or GraphQL errors
 */
export async function gql(query, variables = {}) {
  const cfg = getConfig();
  if (!cfg) throw new Error('Not configured');

  // LCARS answers at / (not /graphql — confirmed from TUI client's _query)
  const res = await fetch(`${cfg.lcars_url}/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${cfg.lcars_token}`,
      'X-LCARS-Client': 'holodeck',
    },
    body: JSON.stringify({ query, variables }),
  });

  // Always try to parse JSON — LCARS returns GraphQL errors with 400 status
  const json = await res.json().catch(() => null);

  if (json?.errors?.length) {
    throw new Error(json.errors.map(e => e.message).join('; '));
  }
  if (!res.ok) {
    throw new Error(`LCARS HTTP ${res.status} ${res.statusText}`);
  }
  return json.data;
}

/* ── Queries ──────────────────────────────────────────────── */

const EPISODES_IN_RANGE_QUERY = `
  query CalendarRange($start: DateTime!, $end: DateTime!, $after: String) {
    episodesInRange(start: $start, end: $end, first: 200, after: $after) {
      edges { node {
        id
        season episode title absoluteNumber
        airDateUtc
        availableViaSonarr availableViaRadarr availableLocally
        filePathSonarr filePathRadarr
        state
        seasonEntity { malId status }
        watchEvents(first: 1) { edges { node { id } } }
        show {
          id displayTitle status score mediaShape trackingSpace tracked
          totalEpisodes posterUrl
          externalIds(first: 20) {
            edges { node { service externalId url } }
          }
        }
      }}
      pageInfo { hasNextPage endCursor }
    }
  }
`;

/**
 * Fetch all episodes in a UTC date range, following pagination cursors.
 * @param {string} start - ISO 8601 UTC string (inclusive)
 * @param {string} end   - ISO 8601 UTC string (exclusive)
 * @returns {Promise<object[]>} flat array of episode nodes
 */
export async function fetchEpisodesInRange(start, end) {
  const episodes = [];
  let after = null;

  while (true) {
    const data = await gql(EPISODES_IN_RANGE_QUERY, { start, end, after });
    const connection = data.episodesInRange;
    for (const edge of connection.edges) {
      episodes.push(edge.node);
    }
    if (!connection.pageInfo.hasNextPage) break;
    after = connection.pageInfo.endCursor;
  }

  return episodes;
}

/* ── Mutations ────────────────────────────────────────────── */

/**
 * Mark an episode as watched. Returns the new WatchEvent id.
 * watchedAt defaults to now on the server if null.
 */
export async function addWatchEvent(showId, season, episode) {
  const data = await gql(`
    mutation AddWatch($showId: ID!, $season: Int!, $episode: Int!, $watchedAt: DateTime) {
      addWatchEvent(showId: $showId, season: $season, episode: $episode, watchedAt: $watchedAt) {
        id
      }
    }
  `, { showId, season, episode, watchedAt: new Date().toISOString() });
  return data.addWatchEvent.id;
}

/**
 * Delete a watch event (unmark watched). Returns true on success.
 */
export async function deleteWatchEvent(watchEventId) {
  const data = await gql(`
    mutation DeleteWatch($id: ID!) {
      deleteWatchEvent(watchEventId: $id)
    }
  `, { id: watchEventId });
  return data.deleteWatchEvent;
}

/**
 * Change the status of a show.
 * status: 'WATCHING' | 'COMPLETED' | 'PLANNED' | 'PAUSED' | 'DROPPED'
 */
export async function setStatus(showId, status, confirmed = false) {
  const vars = { showId, status };
  if (confirmed) vars.confirmed = true;
  const data = await gql(`
    mutation SetStatus($showId: ID!, $status: ShowStatus!, $confirmed: Boolean) {
      setStatus(showId: $showId, status: $status, confirmed: $confirmed) {
        id status
      }
    }
  `, vars);
  return data.setStatus;
}

/**
 * Set a numeric score for a show (LCARS scale 0–20, quarter-point).
 */
export async function setScore(showId, score) {
  const data = await gql(`
    mutation SetScore($showId: ID!, $score: Float!) {
      setScore(showId: $showId, score: $score) {
        id score
      }
    }
  `, { showId, score });
  return data.setScore;
}

/**
 * Set a numeric score for a season (LCARS scale 0–20, quarter-point).
 */
export async function setSeasonScore(seasonId, score) {
  const data = await gql(`
    mutation SetSeasonScore($seasonId: ID!, $score: Float!) {
      setSeasonScore(seasonId: $seasonId, score: $score) {
        id score
      }
    }
  `, { seasonId, score });
  return data.setSeasonScore;
}

export async function setSeasonStatus(seasonId, status, confirmed = false) {
  const vars = { seasonId, status };
  if (confirmed) vars.confirmed = true;
  const data = await gql(`
    mutation SetSeasonStatus($seasonId: ID!, $status: ShowStatus, $confirmed: Boolean) {
      setSeasonStatus(seasonId: $seasonId, status: $status, confirmed: $confirmed) {
        id status
      }
    }
  `, vars);
  return data.setSeasonStatus;
}

/**
 * Link (upsert) a show's external ID for a given service.
 * Identity is (showId, service) — calling with an existing service overwrites.
 */
export async function linkShowExternalId(showId, service, externalId, url) {
  const data = await gql(`
    mutation LinkExternalId($showId: ID!, $service: String!, $externalId: String!, $url: String!) {
      linkShowExternalId(showId: $showId, service: $service, externalId: $externalId, url: $url) {
        service externalId url
      }
    }
  `, { showId, service, externalId, url });
  return data.linkShowExternalId;
}

/**
 * Remove a show's external ID for a given service.
 */
export async function unlinkShowExternalId(showId, service) {
  const data = await gql(`
    mutation UnlinkExternalId($showId: ID!, $service: String!) {
      unlinkShowExternalId(showId: $showId, service: $service)
    }
  `, { showId, service });
  return data.unlinkShowExternalId;
}

/**
 * Set (or create) a season mapping's AniList/MAL IDs.
 * Upserts by (showId, seasonNumber). Pass null to clear a field.
 */
export async function setSeasonMapping(showId, seasonNumber, anilistId, malId) {
  const data = await gql(`
    mutation SetSeasonMapping($showId: ID!, $seasonNumber: Int!, $anilistId: Int, $malId: Int) {
      setSeasonMapping(showId: $showId, seasonNumber: $seasonNumber, anilistId: $anilistId, malId: $malId) {
        id seasonNumber anilistId malId score
        startedAt completedAt absStart absEnd source
      }
    }
  `, { showId, seasonNumber, anilistId, malId });
  return data.setSeasonMapping;
}

/* ── Show detail query ────────────────────────────────────── */

const SHOW_DETAIL_QUERY = `
  query ShowDetail($id: ID!, $epAfter: String) {
    show(id: $id) {
      id displayTitle displayTitleOverride
      titleRomaji titleEnglish titleNative synonyms
      status score totalEpisodes mediaShape trackingSpace tracked
      posterUrl bannerUrl synopsis genresRaw durationMinutes
      availableViaRadarr filePathRadarr
      studioCredits(first: 5) {
        edges { node { roleType studio { name } } }
      }
      artAssets { id seasonId kind source url width height selected }
      seasons(first: 100) {
        edges { node {
          id seasonNumber anilistId malId status score posterUrl
          startedAt completedAt absStart absEnd source
        }}
      }
      externalIds(first: 20) {
        edges { node { service externalId url } }
      }
      watchEvents(first: 1) {
        edges { node { id watchedAt } }
      }
      episodes(first: 200, after: $epAfter) {
        edges { node {
          id season episode absoluteNumber kind title synopsis
          airDateUtc state
          availableViaSonarr availableViaRadarr availableLocally
          filePathSonarr filePathRadarr
          seasonEntity { malId status }
          linkedMovieShow { id posterUrl displayTitle }
          watchEvents(first: 1) { edges { node { id } } }
        }}
        pageInfo { hasNextPage endCursor }
      }
    }
  }
`;

/**
 * Fetch a single show with all seasons and episodes (paginated).
 * @param {string} id - Show id
 * @returns {Promise<object>} show node with flat episodes array
 */
export async function fetchShow(id) {
  let show = null;
  const episodes = [];
  let epAfter = null;

  while (true) {
    const data = await gql(SHOW_DETAIL_QUERY, { id, epAfter });
    const s = data.show;
    if (!s) throw new Error('Show not found');
    if (!show) {
      show = { ...s };
    }
    for (const edge of s.episodes.edges) {
      episodes.push(edge.node);
    }
    if (!s.episodes.pageInfo.hasNextPage) break;
    epAfter = s.episodes.pageInfo.endCursor;
  }

  // Flatten relay connections
  show.episodes = episodes;
  show.seasons = (show.seasons?.edges || []).map(e => e.node);
  show.externalIds = (show.externalIds?.edges || []).map(e => e.node);
  show.studioCredits = (show.studioCredits?.edges || []).map(e => e.node);
  show.watchEvents = (show.watchEvents?.edges || []).map(e => e.node);
  return show;
}

/**
 * Trigger lazy-fetch of episode synopses from TVDB/TMDB.
 * @param {string} showId
 * @returns {Promise<object>} updated show
 */
export async function fetchEpisodeSynopses(showId) {
  const data = await gql(`
    mutation FetchEpisodeSynopses($showId: ID!) {
      fetchEpisodeSynopses(showId: $showId) { id }
    }
  `, { showId });
  return data.fetchEpisodeSynopses;
}

/**
 * Set (overwrite) a show's synopsis text.
 */
export async function setShowSynopsis(showId, synopsis) {
  const data = await gql(`
    mutation SetShowSynopsis($showId: ID!, $synopsis: String!) {
      setShowSynopsis(showId: $showId, synopsis: $synopsis) { id synopsis }
    }
  `, { showId, synopsis });
  return data.setShowSynopsis;
}

/**
 * Set (overwrite) an episode's synopsis text.
 */
export async function setEpisodeSynopsis(episodeId, synopsis) {
  const data = await gql(`
    mutation SetEpisodeSynopsis($episodeId: ID!, $synopsis: String!) {
      setEpisodeSynopsis(episodeId: $episodeId, synopsis: $synopsis) { id synopsis }
    }
  `, { episodeId, synopsis });
  return data.setEpisodeSynopsis;
}

/**
 * Fetch synopsis candidates from external sources (read-only, no DB writes).
 * @param {string} showId
 * @param {string|null} episodeId - pass for episode-level candidates
 * @returns {Promise<{source: string, text: string|null}[]>}
 */
export async function fetchSynopsisCandidates(showId, episodeId = null) {
  const data = await gql(`
    mutation FetchSynopsisCandidates($showId: ID!, $episodeId: ID) {
      fetchSynopsisCandidates(showId: $showId, episodeId: $episodeId) {
        source text
      }
    }
  `, { showId, episodeId });
  return data.fetchSynopsisCandidates;
}

/**
 * Run Fribb reconciliation for a season mapping.
 * Returns the updated Season with reconciliation result.
 * @param {string} showId
 * @param {number} seasonNumber
 * @returns {Promise<object>} Season node
 */
export async function reconcileSeasonMapping(showId, seasonNumber) {
  const data = await gql(`
    mutation Reconcile($showId: ID!, $seasonNumber: Int!) {
      reconcileSeasonMapping(showId: $showId, seasonNumber: $seasonNumber) {
        id seasonNumber anilistId malId score
        startedAt completedAt absStart absEnd source
      }
    }
  `, { showId, seasonNumber });
  return data.reconcileSeasonMapping;
}

/* ── List / Phase 2 queries ───────────────────────────────── */

/**
 * Fetch all tracked shows, following pagination cursors.
 * @returns {Promise<object[]>} flat array of Show nodes
 */
export async function fetchAllShows() {
  const shows = [];
  let after = null;
  const QUERY = `
    query AllShows($first: Int, $after: String) {
      shows(first: $first, after: $after) {
        edges { node {
          id displayTitle status score mediaShape trackingSpace tracked
          totalEpisodes posterUrl
          externalIds(first: 20) {
            edges { node { service externalId url } }
          }
        }}
        pageInfo { hasNextPage endCursor }
      }
    }
  `;
  while (true) {
    const data = await gql(QUERY, { first: 100, after });
    for (const edge of data.shows.edges) shows.push(edge.node);
    if (!data.shows.pageInfo.hasNextPage) break;
    after = data.shows.pageInfo.endCursor;
  }
  return shows;
}

/**
 * Fetch recent grab events for a service ('sonarr' | 'radarr').
 * @param {string} service - 'sonarr' or 'radarr'
 * @param {number} page - 1-based page number
 * @param {number} pageSize - items per page
 * @returns {Promise<object[]>} array of GrabEvent objects
 */
export async function fetchRecentGrabs(service, page = 1, pageSize = 20) {
  const data = await gql(`
    query RecentGrabs($service: String!, $page: Int, $pageSize: Int) {
      recentGrabs(service: $service, page: $page, pageSize: $pageSize) {
        service title releaseTitle date quality
        seasonNumber episodeNumber airDate filePath
      }
    }
  `, { service, page, pageSize });
  return data.recentGrabs;
}

/**
 * Fetch all available-but-unwatched episodes (backlog).
 * Follows pagination cursors.
 * @returns {Promise<object[]>} flat array of Episode nodes
 */
export async function fetchBacklog(first = 100) {
  const episodes = [];
  let after = null;

  // Re-use the same fragment shape as episodesInRange so backlog.js can
  // share the card rendering logic from calendar.js.
  const BACKLOG_QUERY = `
    query Backlog($first: Int, $after: String) {
      backlog(first: $first, after: $after) {
        edges { node {
          id
          season episode title absoluteNumber
          airDateUtc
          availableViaSonarr availableViaRadarr availableLocally
          filePathSonarr filePathRadarr
          state
          seasonEntity { malId status }
          watchEvents(first: 1) { edges { node { id } } }
          show {
            id displayTitle status score mediaShape trackingSpace tracked
            totalEpisodes posterUrl
            externalIds(first: 20) {
              edges { node { service externalId url } }
            }
          }
        }}
        pageInfo { hasNextPage endCursor }
      }
    }
  `;

  while (true) {
    const data = await gql(BACKLOG_QUERY, { first, after });
    const conn = data.backlog;
    for (const edge of conn.edges) episodes.push(edge.node);
    if (!conn.pageInfo.hasNextPage) break;
    after = conn.pageInfo.endCursor;
  }

  return episodes;
}

/**
 * Fetch pending reviews queue.
 * @param {boolean} includeResolved - include already-resolved reviews
 * @param {number} first - page size
 * @param {string|null} after - pagination cursor
 * @returns {Promise<{nodes: object[], pageInfo: object}>}
 */
export async function fetchPendingReviews(includeResolved = false, first = 50, after = null) {
  const data = await gql(`
    query PendingReviews($includeResolved: Boolean, $first: Int, $after: String) {
      pendingReviews(includeResolved: $includeResolved, first: $first, after: $after) {
        edges { node {
          id entityType entityId field
          previousValue proposedValueChain
          source createdAt resolvedAt resolvedByClient resolutionNote
        }}
        pageInfo { hasNextPage endCursor }
      }
    }
  `, { includeResolved, first, after });
  return {
    nodes: data.pendingReviews.edges.map(e => e.node),
    pageInfo: data.pendingReviews.pageInfo,
  };
}

/**
 * Resolve a pending review (mark as acknowledged/applied).
 * @param {string} id - PendingReview id
 * @param {string} resolutionNote - optional note
 * @returns {Promise<object>} the resolved PendingReview
 */
export async function resolvePendingReview(id, resolutionNote = '') {
  const data = await gql(`
    mutation Resolve($id: ID!, $resolutionNote: String) {
      resolvePendingReview(id: $id, resolutionNote: $resolutionNote) {
        id resolvedAt resolutionNote resolvedByClient
      }
    }
  `, { id, resolutionNote: resolutionNote || null });
  return data.resolvePendingReview;
}

/**
 * Fetch artwork from all sources (AniList per-season, TVDB) for a show.
 */
export async function fetchShowArt(showId) {
  const data = await gql(`
    mutation FetchArt($showId: ID!) {
      fetchShowArt(showId: $showId) {
        id artAssets { id seasonId kind source url width height selected }
      }
    }
  `, { showId });
  return data.fetchShowArt;
}

/**
 * Select an art asset as the active choice for its slot.
 */
export async function selectArtAsset(id) {
  const data = await gql(`
    mutation Select($id: ID!) {
      selectArtAsset(id: $id) { id selected }
    }
  `, { id });
  return data.selectArtAsset;
}

/**
 * Deselect an art asset, reverting to fallback.
 */
export async function deselectArtAsset(id) {
  const data = await gql(`
    mutation Deselect($id: ID!) {
      deselectArtAsset(id: $id) { id selected }
    }
  `, { id });
  return data.deselectArtAsset;
}

/**
 * Resolve entity labels for pending reviews in a single batched query.
 * Uses GraphQL aliases to fetch show/episode/season by entityId in one round trip.
 * @param {object[]} reviews - array of {entityType, entityId}
 * @returns {Promise<Map<string, object>>} keyed by entityId → {label, context}
 */
export async function resolveReviewLabels(reviews) {
  if (!reviews.length) return new Map();

  // Group by entityType, deduplicate entityIds
  const byType = { show: new Set(), episode: new Set(), season: new Set() };
  for (const r of reviews) {
    if (byType[r.entityType]) byType[r.entityType].add(r.entityId);
  }

  // Build aliased query fragments
  const parts = [];
  const showIds = [...byType.show];
  const episodeIds = [...byType.episode];
  const seasonIds = [...byType.season];

  showIds.forEach((id, i) => {
    parts.push(`s${i}: show(id: "${id}") { id displayTitle posterUrl }`);
  });
  episodeIds.forEach((id, i) => {
    parts.push(`e${i}: episode(id: "${id}") { id season episode title airDateUtc show { id displayTitle posterUrl } }`);
  });
  seasonIds.forEach((id, i) => {
    parts.push(`z${i}: season(id: "${id}") { id seasonNumber anilistId malId show { id displayTitle posterUrl } }`);
  });

  if (!parts.length) return new Map();

  const data = await gql(`query ResolveLabels { ${parts.join('\n')} }`);
  const result = new Map();

  showIds.forEach((id, i) => {
    const s = data[`s${i}`];
    if (s) result.set(id, { label: s.displayTitle, posterUrl: s.posterUrl, showId: s.id });
  });
  episodeIds.forEach((id, i) => {
    const e = data[`e${i}`];
    if (e) {
      const ctx = `S${String(e.season).padStart(2, '0')}E${String(e.episode).padStart(2, '0')}`;
      result.set(id, {
        label: `${e.show.displayTitle} — ${ctx}`,
        showTitle: e.show.displayTitle,
        episodeCtx: ctx,
        episodeTitle: e.title,
        airDateUtc: e.airDateUtc,
        posterUrl: e.show.posterUrl,
        showId: e.show.id,
        season: e.season,
        episode: e.episode,
      });
    }
  });
  seasonIds.forEach((id, i) => {
    const z = data[`z${i}`];
    if (z) {
      result.set(id, {
        label: `${z.show.displayTitle} — Season ${z.seasonNumber}`,
        showTitle: z.show.displayTitle,
        seasonNumber: z.seasonNumber,
        anilistId: z.anilistId,
        malId: z.malId,
        posterUrl: z.show.posterUrl,
        showId: z.show.id,
      });
    }
  });

  return result;
}

/**
 * Set an episode's air date (sets air_date_source = MANUAL).
 * @param {string} episodeId
 * @param {string} airDateUtc - ISO 8601 DateTime
 * @returns {Promise<object>} updated Episode
 */
export async function setEpisodeAirDate(episodeId, airDateUtc) {
  const data = await gql(`
    mutation SetAirDate($episodeId: ID!, $airDateUtc: DateTime!) {
      setEpisodeAirDate(episodeId: $episodeId, airDateUtc: $airDateUtc) {
        id airDateUtc airDateSource
      }
    }
  `, { episodeId, airDateUtc });
  return data.setEpisodeAirDate;
}

/**
 * Reassign an episode to a different season/episode slot.
 * Rejects if the target slot is already occupied by a different episode.
 * Ensure the target season row exists first (via setSeasonMapping) to
 * prevent seasonEntity reverting on the next Sonarr sync.
 * @param {string} episodeId
 * @param {number} season - target season number
 * @param {number} episode - target episode number
 * @returns {Promise<object>} updated Episode
 */
export async function setEpisodeNumber(episodeId, season, episode) {
  const data = await gql(`
    mutation SetEpNum($episodeId: ID!, $season: Int!, $episode: Int!) {
      setEpisodeNumber(episodeId: $episodeId, season: $season, episode: $episode) {
        id season episode absoluteNumber
      }
    }
  `, { episodeId, season, episode });
  return data.setEpisodeNumber;
}

/* ── Add-show queries & mutations ────────────────────────── */

/**
 * Search Sonarr/Radarr for candidates by title (read-only, no DB writes).
 * EPISODIC → Sonarr (returns tvdbId), MOVIE → Radarr (returns tmdbId).
 * @param {'EPISODIC'|'MOVIE'} mediaShape
 * @param {string} title - search term
 * @returns {Promise<object[]>} array of ArrCandidate {title, year, tvdbId, tmdbId, overview}
 */
export async function searchArrCandidates(mediaShape, title) {
  const data = await gql(`
    query SearchArr($mediaShape: MediaShape!, $title: String!) {
      searchArrCandidates(mediaShape: $mediaShape, title: $title) {
        title year tvdbId tmdbId overview
      }
    }
  `, { mediaShape, title });
  return data.searchArrCandidates;
}

/**
 * Add a show via arr (Sonarr/Radarr) — creates in LCARS and adds to arr.
 * Pass tvdbId/tmdbId from the chosen ArrCandidate to bypass arr's own
 * auto-pick-first-result behavior.
 * @param {object} input - AddShowWithArrInput fields
 * @returns {Promise<object>} AddShowResult {show, sonarrSeriesCreated, radarrMovieCreated, matchedTitle, matchedTvdbId, matchedTmdbId}
 */
export async function addShowWithArr(input) {
  const data = await gql(`
    mutation AddShowWithArr($input: AddShowWithArrInput!) {
      addShowWithArr(input: $input) {
        show {
          id displayTitle status score mediaShape trackingSpace tracked
          posterUrl
          externalIds(first: 20) { edges { node { service externalId url } } }
        }
        sonarrSeriesCreated radarrMovieCreated
        matchedTitle matchedTvdbId matchedTmdbId
      }
    }
  `, { input });
  return data.addShowWithArr;
}

/**
 * Add a show directly (no arr interaction).
 * Used for COMPLETED/DROPPED where arr is not wanted.
 * @param {object} input - AddShowInput fields
 * @returns {Promise<object>} Show node
 */
export async function addShow(input) {
  const data = await gql(`
    mutation AddShow($input: AddShowInput!) {
      addShow(input: $input) {
        id displayTitle status score mediaShape trackingSpace tracked
        posterUrl
        externalIds(first: 20) { edges { node { service externalId url } } }
      }
    }
  `, { input });
  return data.addShow;
}

/**
 * Soft-delete a show (sets tracked=0, unmonitors in arr).
 * @param {string} showId
 * @returns {Promise<object>} the soft-deleted Show
 */
export async function softDeleteShow(showId) {
  const data = await gql(`
    mutation SoftDelete($showId: ID!) {
      softDeleteShow(showId: $showId) {
        id displayTitle tracked
      }
    }
  `, { showId });
  return data.softDeleteShow;
}

/**
 * Re-run on-demand metadata fetch for a show (retry after failure).
 * @param {string} showId
 * @returns {Promise<object>} refreshed Show
 */
export async function refreshShowMetadata(showId) {
  const data = await gql(`
    mutation RefreshMeta($showId: ID!) {
      refreshShowMetadata(showId: $showId) {
        id displayTitle metadataLastRefreshedAt
        externalIds { edges { node { service externalId url } } }
      }
    }
  `, { showId });
  return data.refreshShowMetadata;
}

/**
 * Batch-check an array of search candidates against LCARS's
 * showByExternalId query, using aliased fields to send one request.
 * Returns a Map of candidate index → { showId, displayTitle } for
 * candidates that are already tracked.
 *
 * @param {Array<{tvdbId?: number, tmdbId?: number}>} candidates
 * @returns {Promise<Map<number, {showId: string, displayTitle: string}>>}
 */
export async function batchCheckTracked(candidates) {
  const parts = [];
  const mapping = []; // [{idx, alias}]

  candidates.forEach((c, i) => {
    if (c.tvdbId) {
      const alias = `t${i}`;
      parts.push(`${alias}: showByExternalId(service: "tvdb", externalId: "${c.tvdbId}") { id displayTitle }`);
      mapping.push({ idx: i, alias });
    } else if (c.tmdbId) {
      const alias = `m${i}`;
      parts.push(`${alias}: showByExternalId(service: "tmdb", externalId: "${c.tmdbId}") { id displayTitle }`);
      mapping.push({ idx: i, alias });
    }
  });

  if (!parts.length) return new Map();

  const data = await gql(`query CheckTracked { ${parts.join('\n')} }`);
  const result = new Map();

  for (const { idx, alias } of mapping) {
    const show = data[alias];
    if (show) {
      result.set(idx, { showId: show.id, displayTitle: show.displayTitle });
    }
  }

  return result;
}
