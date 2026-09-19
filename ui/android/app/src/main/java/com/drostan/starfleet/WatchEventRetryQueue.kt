package com.drostan.starfleet

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * A4 step 37 (DESIGN.md §8) — "eviction source of truth" decision (user,
 * 2026-09-19): VlcPlugin's onActivityResult writes `watched` to the local
 * SQLite index immediately (always succeeds — that's what eviction reads,
 * DownloadDatabase.markWatched), and separately fires addWatchEvent to
 * LCARS, which may fail (offline, server down). This queue is where that
 * failed call goes instead of being lost — flushed on AutoDownloadWorker's
 * own periodic tick, decoupled from eviction timing entirely: an unflushed
 * queue entry never blocks or reverses a local watched flag.
 *
 * Plain SharedPreferences-backed JSON array, not another SQLite table —
 * expected volume is at most a few entries between successful syncs.
 */
object WatchEventRetryQueue {
    private const val PREFS_NAME = "starfleet_watch_retry"
    private const val KEY = "pending"

    fun enqueue(context: Context, showId: String, season: Int?, episode: Int?, watchedAtIso: String) {
        val prefs = prefs(context)
        val arr = JSONArray(prefs.getString(KEY, "[]"))
        arr.put(
            JSONObject().apply {
                put("showId", showId)
                put("season", season ?: JSONObject.NULL)
                put("episode", episode ?: JSONObject.NULL)
                put("watchedAt", watchedAtIso)
            }
        )
        prefs.edit().putString(KEY, arr.toString()).apply()
    }

    /** Attempts every queued event; entries that still fail stay queued. */
    fun flush(context: Context) {
        val prefs = prefs(context)
        val arr = JSONArray(prefs.getString(KEY, "[]"))
        val remaining = JSONArray()
        for (i in 0 until arr.length()) {
            val item = arr.getJSONObject(i)
            val ok = LcarsClient.addWatchEvent(
                context,
                item.getString("showId"),
                if (item.isNull("season")) null else item.getInt("season"),
                if (item.isNull("episode")) null else item.getInt("episode"),
                item.getString("watchedAt"),
            )
            if (!ok) remaining.put(item)
        }
        prefs.edit().putString(KEY, remaining.toString()).apply()
    }

    private fun prefs(context: Context) =
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
}
