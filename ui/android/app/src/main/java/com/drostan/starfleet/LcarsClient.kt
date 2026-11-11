package com.drostan.starfleet

import android.content.Context
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/**
 * Shared A3/A4 (DESIGN.md §8) — one OkHttpClient instance and the
 * server address + bearer token, both read from the same SharedPreferences
 * MainActivity/TokenPlugin already use ("starfleet"/"server_url",
 * "lcars_token"). LcarsWsPlugin (A3) uses this for its WebSocket
 * handshake; AutoDownloadWorker (A4) uses backlog()/addWatchEvent() below.
 */
object LcarsClient {
    val http: OkHttpClient by lazy {
        OkHttpClient.Builder().build()
    }

    /** ws://<server-host>:<port>/ — LCARS answers GraphQL (HTTP and WS) at
     * exactly "/", not "/graphql" (confirmed live, A0). */
    fun wsUrl(context: Context): String? {
        val serverUrl = serverUrl(context) ?: return null
        return serverUrl.replaceFirst("http://", "ws://").replaceFirst("https://", "wss://") + "/"
    }

    fun serverUrl(context: Context): String? =
        prefs(context).getString("server_url", null)

    fun bearerToken(context: Context): String? =
        prefs(context).getString("lcars_token", null)

    private fun prefs(context: Context) =
        context.getSharedPreferences("starfleet", Context.MODE_PRIVATE)

    private val JSON_MEDIA_TYPE = "application/json".toMediaType()

    private fun graphql(context: Context, query: String, variables: JSONObject): JSONObject? {
        val serverUrl = serverUrl(context) ?: return null
        val token = bearerToken(context) ?: return null
        val body = JSONObject().apply {
            put("query", query)
            put("variables", variables)
        }
        val request = Request.Builder()
            .url("$serverUrl/")
            .addHeader("Authorization", "Bearer $token")
            .addHeader("X-LCARS-Client", "android")
            .post(body.toString().toRequestBody(JSON_MEDIA_TYPE))
            .build()
        return http.newCall(request).execute().use { response ->
            val text = response.body?.string() ?: return null
            if (!response.isSuccessful) return null
            JSONObject(text)
        }
    }

    // includePlanned: true (2026-09-20, user request) — a planning-status
    // show whose first episode has already aired is "started" in every
    // practical sense; auto-download shouldn't wait for a manual promotion
    // to watching. Paused shows are unaffected (backlog never included
    // them, still doesn't).
    // 2026-11-11, user: auto-download only what is unwatched AND grabbed by Sonarr in the last
    // N days (Settings → App Downloads, default 3). The server filters (Sonarr's grab history);
    // an older server rejects the argument and the worker downloads nothing, never everything.
    const val GRAB_WINDOW_PREF = "auto_download_grab_days"
    const val GRAB_WINDOW_DEFAULT_DAYS = 3
    const val GRAB_WINDOW_MAX_DAYS = 30

    fun grabWindowDays(context: Context): Int =
        prefs(context).getInt(GRAB_WINDOW_PREF, GRAB_WINDOW_DEFAULT_DAYS)
            .coerceIn(1, GRAB_WINDOW_MAX_DAYS)

    private const val BACKLOG_QUERY = """
        query Backlog(${'$'}first: Int, ${'$'}days: Int) {
          backlog(first: ${'$'}first, includePlanned: true, grabbedWithinDays: ${'$'}days) {
            edges { node {
              id season episode title
              filePathSonarr filePathRadarr availableLocally
              show { id displayTitle }
            } }
          }
        }
    """

    /**
     * A4 step 33 — the same `backlog` GraphQL query api.js's own
     * fetchBacklog() uses, trimmed to just the fields AutoDownloadWorker
     * needs. Only returns episodes that are actually available to
     * download (availableLocally — meaning Sonarr/Radarr already have the
     * file server-side, not that it's on this device); everything else in
     * the backlog is "airing/tracked but no file yet", nothing to enqueue.
     */
    fun fetchBacklog(context: Context, first: Int = 100): List<BacklogEpisode> {
        val json = try {
            graphql(
                context,
                BACKLOG_QUERY,
                JSONObject().put("first", first).put("days", grabWindowDays(context)),
            )
        } catch (e: Exception) {
            null
        } ?: return emptyList()

        val edges = json.optJSONObject("data")?.optJSONObject("backlog")?.optJSONArray("edges")
            ?: return emptyList()
        val result = mutableListOf<BacklogEpisode>()
        for (i in 0 until edges.length()) {
            val node = edges.getJSONObject(i).getJSONObject("node")
            if (!node.optBoolean("availableLocally", false)) continue
            val filePath = node.optStringOrNull("filePathSonarr")
                ?: node.optStringOrNull("filePathRadarr")
                ?: continue
            val show = node.getJSONObject("show")
            result.add(
                BacklogEpisode(
                    id = node.getString("id"),
                    showId = show.getString("id"),
                    showTitle = show.optString("displayTitle", ""),
                    season = if (node.isNull("season")) null else node.getInt("season"),
                    episode = if (node.isNull("episode")) null else node.getInt("episode"),
                    title = node.optStringOrNull("title"),
                    filePath = filePath,
                )
            )
        }
        return result
    }

    private const val GRABBED_MOVIES_QUERY = """
        query GrabbedMovies(${'$'}days: Int!) {
          grabbedMovies(withinDays: ${'$'}days) { id displayTitle filePathRadarr }
        }
    """

    /**
     * 2026-11-11 (user): films Radarr grabbed in the same window are auto-downloaded too. A film
     * has no episode: its download is keyed by the film's (show) id, season/episode stay null (a
     * VLC watch then reports a film watch). An older server without `grabbedMovies` = none.
     */
    fun fetchGrabbedMovies(context: Context): List<BacklogEpisode> {
        val json = try {
            graphql(context, GRABBED_MOVIES_QUERY, JSONObject().put("days", grabWindowDays(context)))
        } catch (e: Exception) {
            null
        } ?: return emptyList()
        val films = json.optJSONObject("data")?.optJSONArray("grabbedMovies") ?: return emptyList()
        val result = mutableListOf<BacklogEpisode>()
        for (i in 0 until films.length()) {
            val film = films.getJSONObject(i)
            val filePath = film.optStringOrNull("filePathRadarr") ?: continue
            result.add(
                BacklogEpisode(
                    id = film.getString("id"),
                    showId = film.getString("id"),
                    showTitle = film.optString("displayTitle", ""),
                    season = null,
                    episode = null,
                    title = "Film",  // the Downloads page's label
                    filePath = filePath,
                )
            )
        }
        return result
    }

    private const val ADD_WATCH_EVENT_MUTATION = """
        mutation AddWatch(${'$'}showId: ID!, ${'$'}season: Int, ${'$'}episode: Int, ${'$'}watchedAt: DateTime, ${'$'}platform: String) {
          addWatchEvent(showId: ${'$'}showId, season: ${'$'}season, episode: ${'$'}episode, watchedAt: ${'$'}watchedAt, platform: ${'$'}platform) { id }
        }
    """

    /**
     * 2026-11-11 (user): a download counts as watched for the auto-delete also when it was marked
     * watched another way (✓ by hand, another device, Jellyfin). For each episode id LCARS holds as
     * WATCHED: its latest watch time. Ids LCARS has as unwatched are left out. Null = the server
     * could not be asked (the caller then changes nothing). `films`: ids that are films (their own
     * show id), asked as a show — any watch of the film counts.
     */
    fun fetchWatchTimes(
        context: Context, ids: List<String>, films: Set<String> = emptySet(),
    ): Map<String, java.time.Instant>? {
        val out = mutableMapOf<String, java.time.Instant>()
        for (chunk in ids.chunked(40)) {
            val vars = JSONObject()
            val params = mutableListOf<String>()
            val fields = StringBuilder()
            chunk.forEachIndexed { i, id ->
                vars.put("id$i", id)
                params.add("\$id$i: ID!")
                val field = if (id in films) "show" else "episode"
                val state = if (id in films) "" else "state "
                fields.append("e$i: $field(id: \$id$i) { ${state}watchEvents(first: 50) { edges { node { watchedAt } } } } ")
            }
            val query = "query DownloadWatched(${params.joinToString(", ")}) { $fields}"
            val res = try { graphql(context, query, vars) } catch (e: Exception) {
                android.util.Log.d("LcarsClient", "fetchWatchTimes failed: $e")
                null
            } ?: return null
            val data = res.optJSONObject("data") ?: return null
            chunk.forEachIndexed { i, id ->
                val ep = data.optJSONObject("e$i") ?: return@forEachIndexed
                if (id !in films && ep.optString("state") != "WATCHED") return@forEachIndexed
                val edges = ep.optJSONObject("watchEvents")?.optJSONArray("edges") ?: return@forEachIndexed
                var latest: java.time.Instant? = null
                for (k in 0 until edges.length()) {
                    val node = edges.optJSONObject(k)?.optJSONObject("node") ?: continue
                    val t = parseInstant(node.optStringOrNull("watchedAt")) ?: continue
                    if (latest == null || t.isAfter(latest)) latest = t
                }
                latest?.let { out[id] = it }
            }
        }
        return out
    }

    /** An ISO time as LCARS or this app writes it ("…Z", an offset, or naive = UTC); null if unreadable. */
    fun parseInstant(s: String?): java.time.Instant? {
        if (s.isNullOrBlank()) return null
        return try { java.time.Instant.parse(s) } catch (e: Exception) {
            try { java.time.OffsetDateTime.parse(s).toInstant() } catch (e: Exception) {
                try { java.time.LocalDateTime.parse(s).toInstant(java.time.ZoneOffset.UTC) } catch (e: Exception) { null }
            }
        }
    }

    /**
     * A4 step 37 — fired from VlcPlugin's own onActivityResult, independent
     * of the local SQLite `watched` write eviction (step 36) reads. Returns
     * whether the network call succeeded; VlcPlugin queues a retry (via
     * WatchEventRetryQueue) on false rather than blocking or losing it.
     */
    fun addWatchEvent(context: Context, showId: String, season: Int?, episode: Int?, watchedAtIso: String): Boolean {
        val variables = JSONObject().apply {
            put("showId", showId)
            put("season", season ?: JSONObject.NULL)
            put("episode", episode ?: JSONObject.NULL)
            put("watchedAt", watchedAtIso)
            put("platform", "android-vlc")
        }
        val json = try {
            graphql(context, ADD_WATCH_EVENT_MUTATION, variables)
        } catch (e: Exception) {
            null
        } ?: return false
        return json.has("data") && !json.has("errors")
    }
}

data class BacklogEpisode(
    val id: String,
    val showId: String,
    val showTitle: String,
    val season: Int?,
    val episode: Int?,
    val title: String?,
    val filePath: String,
)

private fun JSONObject.optStringOrNull(key: String): String? =
    if (isNull(key) || !has(key)) null else getString(key)
