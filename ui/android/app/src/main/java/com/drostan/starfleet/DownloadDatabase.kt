package com.drostan.starfleet

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

/**
 * A2 step 23 (DESIGN.md §8) — local download index.
 *
 * The design doc named `@capacitor-community/sqlite` (a third-party JS+
 * native bridge giving the web client raw SQL access) for this. Used a
 * plain Android SQLiteOpenHelper instead: the actual surface this index
 * needs is small and fixed (list, check-one, mark-watched, delete-one —
 * all exposed as ordinary DownloadPlugin @PluginMethods returning JSON),
 * not ad-hoc SQL from JS, so pulling in a third-party native dependency
 * (which itself bundles SQLCipher/requery) for this one hobby app's
 * download list isn't worth the added surface. Revisit only if a real
 * need for arbitrary queries from JS shows up.
 */
class DownloadDatabase(context: Context) : SQLiteOpenHelper(context, DB_NAME, null, DB_VERSION) {

    companion object {
        private const val DB_NAME = "downloads.db"
        // v2 (A4): show_id/season/episode — needed so VlcPlugin can report
        // addWatchEvent for offline playback without JS threading them
        // through separately (episodeId alone is enough to look the rest
        // up from this row).
        // v3 (2026-09-20, user request): watched_at — powers a new
        // time-based auto-delete ("delete X hours after marked watched"),
        // independent of the existing size-based eviction in
        // AutoDownloadWorker.evict(). Dropping and recreating on upgrade is
        // fine — this is a re-downloadable local cache, not data of record.
        private const val DB_VERSION = 3
        const val TABLE = "downloads"
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE $TABLE (
                episode_id TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                show_id TEXT,
                season INTEGER,
                episode INTEGER,
                show_title TEXT,
                label TEXT,
                local_uri TEXT,
                size_bytes INTEGER,
                downloaded_at TEXT NOT NULL,
                watched INTEGER NOT NULL DEFAULT 0,
                watched_at TEXT,
                dm_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """.trimIndent()
        )
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        db.execSQL("DROP TABLE IF EXISTS $TABLE")
        onCreate(db)
    }

    fun insertPending(
        episodeId: String,
        filePath: String,
        showId: String?,
        season: Int?,
        episodeNum: Int?,
        showTitle: String,
        label: String,
        dmId: Long,
        downloadedAt: String,
    ) {
        val values = ContentValues().apply {
            put("episode_id", episodeId)
            put("file_path", filePath)
            put("show_id", showId)
            put("season", season)
            put("episode", episodeNum)
            put("show_title", showTitle)
            put("label", label)
            put("dm_id", dmId)
            put("downloaded_at", downloadedAt)
            put("status", "pending")
        }
        writableDatabase.insertWithOnConflict(TABLE, null, values, SQLiteDatabase.CONFLICT_REPLACE)
    }

    fun markComplete(dmId: Long, localUri: String, sizeBytes: Long) {
        val values = ContentValues().apply {
            put("local_uri", localUri)
            put("size_bytes", sizeBytes)
            put("status", "complete")
        }
        writableDatabase.update(TABLE, values, "dm_id = ?", arrayOf(dmId.toString()))
    }

    fun markFailed(dmId: Long) {
        val values = ContentValues().apply { put("status", "failed") }
        writableDatabase.update(TABLE, values, "dm_id = ?", arrayOf(dmId.toString()))
    }

    /**
     * A4 step 36 — a soft delete, not `delete()`: found by testing that a
     * hard delete let AutoDownloadWorker's own diff step (existingIds =
     * every episode_id in this table) immediately re-download the exact
     * episode it just evicted, on the very same tick — it's still
     * "unwatched but available" in the LCARS backlog by definition, since
     * eviction never touches LCARS's own watch_event state. Keeping the
     * row with status='evicted' (and clearing local_uri/size_bytes, since
     * the file itself really is gone) means the existing diff logic skips
     * it without needing a separate "recently evicted" cooldown mechanism.
     */
    fun markEvicted(episodeId: String) {
        val values = ContentValues().apply {
            put("status", "evicted")
            putNull("local_uri")
            putNull("size_bytes")
        }
        writableDatabase.update(TABLE, values, "episode_id = ?", arrayOf(episodeId))
    }

    fun markWatched(episodeId: String) {
        val values = ContentValues().apply {
            put("watched", 1)
            put("watched_at", java.time.Instant.now().toString())
        }
        writableDatabase.update(TABLE, values, "episode_id = ?", arrayOf(episodeId))
    }

    /**
     * Rows watched at least `hours` ago and still holding a file
     * (status='complete') — candidates for the time-based auto-delete
     * setting (2026-09-20, user request), independent of the size-based
     * limit in AutoDownloadWorker.evict().
     */
    fun listWatchedOlderThan(hours: Double): List<DownloadRow> {
        val cutoff = java.time.Instant.now().minusSeconds((hours * 3600).toLong()).toString()
        return listAll().filter {
            it.status == "complete" && it.watched && it.watchedAt != null && it.watchedAt < cutoff
        }
    }

    fun get(episodeId: String): DownloadRow? {
        writableDatabase.query(
            TABLE, null, "episode_id = ?", arrayOf(episodeId), null, null, null
        ).use { c ->
            if (!c.moveToFirst()) return null
            return DownloadRow.from(c)
        }
    }

    fun listAll(): List<DownloadRow> {
        val rows = mutableListOf<DownloadRow>()
        writableDatabase.query(
            TABLE, null, null, null, null, null, "downloaded_at DESC"
        ).use { c ->
            while (c.moveToNext()) rows.add(DownloadRow.from(c))
        }
        return rows
    }

    fun delete(episodeId: String) {
        writableDatabase.delete(TABLE, "episode_id = ?", arrayOf(episodeId))
    }

    fun deleteAll() {
        writableDatabase.delete(TABLE, null, null)
    }
}

data class DownloadRow(
    val episodeId: String,
    val filePath: String,
    val showId: String?,
    val season: Int?,
    val episode: Int?,
    val showTitle: String?,
    val label: String?,
    val localUri: String?,
    val sizeBytes: Long?,
    val downloadedAt: String,
    val watched: Boolean,
    val watchedAt: String?,
    val dmId: Long?,
    val status: String,
) {
    companion object {
        fun from(c: android.database.Cursor): DownloadRow = DownloadRow(
            episodeId = c.getString(c.getColumnIndexOrThrow("episode_id")),
            filePath = c.getString(c.getColumnIndexOrThrow("file_path")),
            showId = c.getStringOrNull("show_id"),
            season = c.getIntOrNull("season"),
            episode = c.getIntOrNull("episode"),
            showTitle = c.getStringOrNull("show_title"),
            label = c.getStringOrNull("label"),
            localUri = c.getStringOrNull("local_uri"),
            sizeBytes = c.getLongOrNull("size_bytes"),
            downloadedAt = c.getString(c.getColumnIndexOrThrow("downloaded_at")),
            watched = c.getInt(c.getColumnIndexOrThrow("watched")) != 0,
            watchedAt = c.getStringOrNull("watched_at"),
            dmId = c.getLongOrNull("dm_id"),
            status = c.getString(c.getColumnIndexOrThrow("status")),
        )
    }
}

private fun android.database.Cursor.getStringOrNull(col: String): String? {
    val i = getColumnIndexOrThrow(col)
    return if (isNull(i)) null else getString(i)
}

private fun android.database.Cursor.getLongOrNull(col: String): Long? {
    val i = getColumnIndexOrThrow(col)
    return if (isNull(i)) null else getLong(i)
}

private fun android.database.Cursor.getIntOrNull(col: String): Int? {
    val i = getColumnIndexOrThrow(col)
    return if (isNull(i)) null else getInt(i)
}
