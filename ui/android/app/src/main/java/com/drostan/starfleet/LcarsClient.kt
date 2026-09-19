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

    private const val BACKLOG_QUERY = """
        query Backlog(${'$'}first: Int) {
          backlog(first: ${'$'}first) {
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
            graphql(context, BACKLOG_QUERY, JSONObject().put("first", first))
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

    private const val ADD_WATCH_EVENT_MUTATION = """
        mutation AddWatch(${'$'}showId: ID!, ${'$'}season: Int, ${'$'}episode: Int, ${'$'}watchedAt: DateTime, ${'$'}platform: String) {
          addWatchEvent(showId: ${'$'}showId, season: ${'$'}season, episode: ${'$'}episode, watchedAt: ${'$'}watchedAt, platform: ${'$'}platform) { id }
        }
    """

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
