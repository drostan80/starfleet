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
        val showTitle = call.getString("showTitle", "") ?: ""
        val label = call.getString("label", "") ?: ""

        if (filePath == null || episodeId == null) {
            call.reject("path and episodeId are required")
            return
        }

        val prefs = context.getSharedPreferences("starfleet", Context.MODE_PRIVATE)
        val serverUrl = prefs.getString("server_url", null)
        if (serverUrl == null) {
            call.reject("no server configured")
            return
        }

        val mediaUrl = MediaUrl.build(serverUrl, filePath)
        val filename = filePath.substringAfterLast('/')
        // User setting (ServerSetupActivity), default false — most of the
        // user's data is unlimited; this is the flip-it-before-travelling
        // escape hatch, not the default posture.
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
        db.insertPending(episodeId, filePath, showTitle, label, dmId, Instant.now().toString())

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
        deleteFileFor(db.get(episodeId)?.localUri)
        db.delete(episodeId)
        call.resolve()
    }

    @PluginMethod
    fun deleteAllDownloads(call: PluginCall) {
        for (row in db.listAll()) deleteFileFor(row.localUri)
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

    private fun deleteFileFor(localUri: String?) {
        if (localUri == null) return
        try {
            Uri.parse(localUri).path?.let { File(it).delete() }
        } catch (e: Exception) {
            // Best-effort — the DB row is removed either way by the caller.
        }
    }
}
