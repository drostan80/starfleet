package com.drostan.starfleet

import android.content.Context
import okhttp3.OkHttpClient

/**
 * Shared A3/A4 (DESIGN.md §8) — one OkHttpClient instance and the
 * server address + bearer token, both read from the same SharedPreferences
 * MainActivity/TokenPlugin already use ("starfleet"/"server_url",
 * "lcars_token"). LcarsWsPlugin (A3) uses this for its WebSocket
 * handshake; AutoDownloadWorker (A4) will add its own `backlog`/
 * `addWatchEvent` HTTP methods here once that phase actually needs them —
 * not built ahead of that need.
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
}
