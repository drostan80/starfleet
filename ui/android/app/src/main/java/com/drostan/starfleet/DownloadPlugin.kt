package com.drostan.starfleet

import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.Uri
import android.os.Environment
import androidx.core.content.ContextCompat
import com.getcapacitor.JSArray
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import java.io.File
import java.time.Instant

/**
 * A2 (DESIGN.md §8) — manual download via the system DownloadManager, not
 * Capacitor's own Filesystem.downloadFile (dies when the WebView is
 * backgrounded — DESIGN.md's own "Android hurdles"). DownloadManager gives
 * background/resume/notifications for free; A4's AutoDownloadWorker will
 * enqueue through this same path later.
 *
 * The local index (DownloadDatabase) is keyed by GraphQL episode id, not
 * dm_id — dm_id is only the transient handle DownloadManager itself uses
 * to report completion; the web client only ever thinks in episode ids.
 */
@CapacitorPlugin(name = "Download")
class DownloadPlugin : Plugin() {

    companion object {
        /**
         * A4 — shared by this plugin's own @PluginMethod (JS-triggered) and
         * AutoDownloadWorker (background, no Plugin/PluginCall in scope):
         * the actual DownloadManager request + index write is identical
         * either way, only where the params come from differs. Returns
         * null if no server is configured (caller decides how to surface
         * that — a rejected PluginCall vs. just skipping in the worker).
         */
        fun enqueue(
            context: Context,
            filePath: String,
            episodeId: String,
            showId: String?,
            season: Int?,
            episodeNum: Int?,
            showTitle: String,
            label: String,
        ): Long? {
            val prefs = context.getSharedPreferences("starfleet", Context.MODE_PRIVATE)
            val serverUrl = prefs.getString("server_url", null) ?: return null

            val mediaUrl = MediaUrl.build(serverUrl, filePath)
            val filename = filePath.substringAfterLast('/')
            // User setting (ServerSetupActivity), default false — most of
            // the user's data is unlimited; this is the flip-it-before-
            // travelling escape hatch, not the default posture. Applies to
            // auto-downloads too (A4) — same setting either path.
            val wifiOnly = prefs.getBoolean("downloads_wifi_only", false)

            val request = DownloadManager.Request(Uri.parse(mediaUrl)).apply {
                setTitle(label.ifEmpty { filename })
                setDescription(showTitle)
                setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                setDestinationInExternalFilesDir(context, Environment.DIRECTORY_DOWNLOADS, filename)
                setAllowedOverMetered(!wifiOnly)
                setAllowedOverRoaming(!wifiOnly)
            }

            val dm = context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
            val dmId = dm.enqueue(request)
            DownloadDatabase(context).insertPending(
                episodeId, filePath, showId, season, episodeNum, showTitle, label, dmId, Instant.now().toString(),
            )
            return dmId
        }

        /**
         * Shared by DownloadPlugin's own deleteDownload/deleteAllDownloads
         * and AutoDownloadWorker's eviction — same reasoning as enqueue()
         * above for why this lives here instead of being duplicated.
         *
         * Found by testing, not designed in, in two layers:
         * 1. A hard row-delete alone left an in-progress ("pending", no
         *    local_uri yet) download's underlying DownloadManager job
         *    completely untouched — it kept downloading regardless,
         *    eventually finishing as a file our index knew nothing about
         *    (visible as DownloadManager's own auto-renamed "-1" duplicate
         *    the next time something tried to write the same filename).
         *    dm.remove(dmId) cancels that.
         * 2. dm.remove() alone then turned out NOT to reliably delete the
         *    file for a destination set via setDestinationInExternalFilesDir
         *    (confirmed live: called it on a fully completed download,
         *    the file was still sitting there afterward) — Android's own
         *    "if there is a downloaded file... it is deleted" guarantee is
         *    reliable for the public Downloads directory DownloadManager
         *    genuinely owns, not for an app-private external dir. So this
         *    also deletes the file itself directly: row.localUri when
         *    known (a completed download), else the same filename
         *    enqueue() itself derived (a pending one that may have
         *    finished writing without us knowing it yet).
         */
        fun cancelAndDeleteFile(context: Context, row: DownloadRow?) {
            if (row == null) return
            row.dmId?.let { dmId ->
                (context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager).remove(dmId)
            }
            val path = row.localUri?.let { Uri.parse(it).path }
                ?: File(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS), row.filePath.substringAfterLast('/')).path
            try {
                File(path).delete()
            } catch (e: Exception) {
                // Best-effort — the DB row is removed either way by the caller.
            }
        }
    }

    private lateinit var db: DownloadDatabase
    private var receiver: BroadcastReceiver? = null

    override fun load() {
        db = DownloadDatabase(context)
        receiver = object : BroadcastReceiver() {
            override fun onReceive(ctx: Context, intent: Intent) {
                val dmId = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1L)
                if (dmId == -1L) return
                val dm = ctx.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
                dm.query(DownloadManager.Query().setFilterById(dmId)).use { c ->
                    if (!c.moveToFirst()) return@use
                    when (c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS))) {
                        DownloadManager.STATUS_SUCCESSFUL -> {
                            val localUri = c.getString(c.getColumnIndexOrThrow(DownloadManager.COLUMN_LOCAL_URI)) ?: ""
                            val size = c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_TOTAL_SIZE_BYTES))
                            db.markComplete(dmId, localUri, size)
                        }
                        DownloadManager.STATUS_FAILED -> db.markFailed(dmId)
                    }
                }
            }
        }
        // RECEIVER_EXPORTED, not RECEIVER_NOT_EXPORTED: this broadcast comes
        // from DownloadManager, a system service running under a different
        // UID than this app — "not exported" blocks exactly that cross-UID
        // delivery. Confirmed the hard way: with NOT_EXPORTED, even a
        // manually-fired `adb shell am broadcast -p <pkg>` targeted directly
        // at this package never reached onReceive, despite `dumpsys activity
        // broadcasts` showing the filter genuinely registered. Safe to widen:
        // onReceive re-queries DownloadManager itself for the real status by
        // id rather than trusting anything else in the broadcast, so a
        // spoofed broadcast from another app can at most trigger a redundant
        // (but accurate) re-check, never inject fake data.
        ContextCompat.registerReceiver(
            context,
            receiver,
            IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE),
            ContextCompat.RECEIVER_EXPORTED,
        )
    }

    @PluginMethod
    fun download(call: PluginCall) {
        val filePath = call.getString("path")
        val episodeId = call.getString("episodeId")
        val showId = call.getString("showId")
        val season = call.getInt("season")
        val episodeNum = call.getInt("episode")
        val showTitle = call.getString("showTitle", "") ?: ""
        val label = call.getString("label", "") ?: ""

        if (filePath == null || episodeId == null) {
            call.reject("path and episodeId are required")
            return
        }

        val dmId = enqueue(context, filePath, episodeId, showId, season, episodeNum, showTitle, label)
        if (dmId == null) {
            call.reject("no server configured")
            return
        }
        call.resolve(JSObject().apply { put("downloadId", dmId) })
    }

    @PluginMethod
    fun listDownloads(call: PluginCall) {
        val arr = JSArray()
        for (row in db.listAll()) {
            arr.put(JSObject().apply {
                put("episodeId", row.episodeId)
                put("filePath", row.filePath)
                put("showTitle", row.showTitle ?: "")
                put("label", row.label ?: "")
                put("localUri", row.localUri)
                put("sizeBytes", row.sizeBytes ?: 0L)
                put("downloadedAt", row.downloadedAt)
                put("watched", row.watched)
                put("status", row.status)
            })
        }
        call.resolve(JSObject().apply { put("downloads", arr) })
    }

    @PluginMethod
    fun getLocalUri(call: PluginCall) {
        val episodeId = call.getString("episodeId")
        if (episodeId == null) {
            call.reject("episodeId is required")
            return
        }
        val row = db.get(episodeId)
        call.resolve(JSObject().apply {
            put("localUri", if (row?.status == "complete") row.localUri else null)
        })
    }

    @PluginMethod
    fun deleteDownload(call: PluginCall) {
        val episodeId = call.getString("episodeId")
        if (episodeId == null) {
            call.reject("episodeId is required")
            return
        }
        cancelAndDeleteFile(context, db.get(episodeId))
        db.delete(episodeId)
        call.resolve()
    }

    @PluginMethod
    fun deleteAllDownloads(call: PluginCall) {
        for (row in db.listAll()) cancelAndDeleteFile(context, row)
        db.deleteAll()
        call.resolve()
    }

    @PluginMethod
    fun markWatched(call: PluginCall) {
        val episodeId = call.getString("episodeId")
        if (episodeId == null) {
            call.reject("episodeId is required")
            return
        }
        db.markWatched(episodeId)
        call.resolve()
    }
}
