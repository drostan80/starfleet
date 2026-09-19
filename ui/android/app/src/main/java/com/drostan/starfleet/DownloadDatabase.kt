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
        private const val DB_VERSION = 1
        const val TABLE = "downloads"
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE $TABLE (
                episode_id TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                show_title TEXT,
                label TEXT,
                local_uri TEXT,
                size_bytes INTEGER,
                downloaded_at TEXT NOT NULL,
                watched INTEGER NOT NULL DEFAULT 0,
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

    fun insertPending(episodeId: String, filePath: String, showTitle: String, label: String, dmId: Long, downloadedAt: String) {
        val values = ContentValues().apply {
            put("episode_id", episodeId)
            put("file_path", filePath)
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

    fun markWatched(episodeId: String) {
        val values = ContentValues().apply { put("watched", 1) }
        writableDatabase.update(TABLE, values, "episode_id = ?", arrayOf(episodeId))
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
    val showTitle: String?,
    val label: String?,
    val localUri: String?,
    val sizeBytes: Long?,
    val downloadedAt: String,
    val watched: Boolean,
    val dmId: Long?,
    val status: String,
) {
    companion object {
        fun from(c: android.database.Cursor): DownloadRow = DownloadRow(
            episodeId = c.getString(c.getColumnIndexOrThrow("episode_id")),
            filePath = c.getString(c.getColumnIndexOrThrow("file_path")),
            showTitle = c.getStringOrNull("show_title"),
            label = c.getStringOrNull("label"),
            localUri = c.getStringOrNull("local_uri"),
            sizeBytes = c.getLongOrNull("size_bytes"),
            downloadedAt = c.getString(c.getColumnIndexOrThrow("downloaded_at")),
            watched = c.getInt(c.getColumnIndexOrThrow("watched")) != 0,
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
