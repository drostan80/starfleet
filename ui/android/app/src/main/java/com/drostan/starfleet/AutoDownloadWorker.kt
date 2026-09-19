package com.drostan.starfleet

import android.app.DownloadManager
import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

/**
 * A4 (DESIGN.md §8) — periodic backlog -> diff -> enqueue, gated to
 * NetworkType.UNMETERED unconditionally: this is the one part of the
 * download story with no user-facing toggle (unlike the manual-download
 * Wi-Fi-only checkbox) — an automatic background job has no business
 * spending someone's cellular data on its own initiative, unlimited plan
 * or not.
 *
 * Also flushes WatchEventRetryQueue (step 37) on the same cadence —
 * reusing this periodic tick rather than inventing a second schedule for
 * what's fundamentally the same "phone home when a good connection shows
 * up" concern.
 *
 * Notification: no dedicated channel/manager built for this — A2's own
 * DownloadManager.Request already sets
 * VISIBILITY_VISIBLE_NOTIFY_COMPLETED, and DownloadManager posts that
 * notification itself from the system's own notification channel,
 * regardless of who enqueued the request. Nothing extra needed here.
 */
class AutoDownloadWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    companion object {
        private const val WORK_NAME = "auto_download"
        private const val INTERVAL_MINUTES = 30L

        fun schedule(context: Context) {
            val constraints = Constraints.Builder()
                .setRequiredNetworkType(NetworkType.UNMETERED)
                .build()
            val request = PeriodicWorkRequestBuilder<AutoDownloadWorker>(INTERVAL_MINUTES, TimeUnit.MINUTES)
                .setConstraints(constraints)
                .build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                WORK_NAME,
                ExistingPeriodicWorkPolicy.KEEP,
                request,
            )
        }
    }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        WatchEventRetryQueue.flush(applicationContext)

        val db = DownloadDatabase(applicationContext)

        // Found by testing, not designed in: a "pending" row whose file
        // actually finished downloading, because the completion broadcast
        // never reached DownloadPlugin's receiver — the app process had
        // already died (OOM kill, force-stop, whatever) before it could
        // fire. DownloadManager's own record of the download survives
        // process death regardless (it's a separate system service), so
        // this reconciles our index against that authoritative source
        // before doing anything else — the same "poller as safety net"
        // pattern the server side already uses for Sonarr/Radarr
        // availability, applied here to our own local index.
        reconcilePending(applicationContext, db)

        // Eviction before enqueueing new downloads — if the user just
        // lowered the storage limit, a fresh batch shouldn't immediately
        // blow past it again.
        evict(applicationContext, db)

        val backlog = LcarsClient.fetchBacklog(applicationContext)
        val existingIds = db.listAll().map { it.episodeId }.toSet()
        for (ep in backlog) {
            if (ep.id in existingIds) continue
            DownloadPlugin.enqueue(
                applicationContext,
                ep.filePath,
                ep.id,
                ep.showId,
                ep.season,
                ep.episode,
                ep.showTitle,
                ep.title ?: "",
            )
        }

        Result.success()
    }

    /**
     * Reconciles any "pending" row against DownloadManager's own record by
     * dm_id — the completion receiver (DownloadPlugin) only fires while
     * the app process is alive; DownloadManager itself keeps going and
     * keeps its own record regardless. Confirmed live: a file that
     * genuinely finished (present on disk) stayed "pending" in our index
     * after the app process was killed mid-download-cycle, until this
     * reconciliation caught it back up on the next tick.
     */
    private fun reconcilePending(context: Context, db: DownloadDatabase) {
        val dm = context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
        for (row in db.listAll().filter { it.status == "pending" && it.dmId != null }) {
            dm.query(DownloadManager.Query().setFilterById(row.dmId!!)).use { c ->
                if (!c.moveToFirst()) return@use
                when (c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS))) {
                    DownloadManager.STATUS_SUCCESSFUL -> {
                        val localUri = c.getString(c.getColumnIndexOrThrow(DownloadManager.COLUMN_LOCAL_URI)) ?: ""
                        val size = c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_TOTAL_SIZE_BYTES))
                        db.markComplete(row.dmId, localUri, size)
                    }
                    DownloadManager.STATUS_FAILED -> db.markFailed(row.dmId)
                }
            }
        }
    }

    /**
     * A4 step 36 — "eviction source of truth" (user, 2026-09-19): reads
     * the local SQLite `watched` flag only, never waits on server
     * confirmation. Watched-oldest-first, then unwatched-oldest, until
     * under the limit. `storage_limit_gb` <= 0 (including unset/default
     * 0f) means no limit configured — nothing to evict.
     */
    private fun evict(context: Context, db: DownloadDatabase) {
        val limitGb = context.getSharedPreferences("starfleet", Context.MODE_PRIVATE)
            .getFloat("storage_limit_gb", 0f)
        if (limitGb <= 0f) return

        val limitBytes = (limitGb * 1_000_000_000L).toLong()
        val completed = db.listAll().filter { it.status == "complete" }
        var totalBytes = completed.sumOf { it.sizeBytes ?: 0L }
        if (totalBytes <= limitBytes) return

        // watched=true sorts first (compareByDescending on a Boolean puts
        // true before false), oldest-downloaded-first within each group.
        val ordered = completed.sortedWith(
            compareByDescending<DownloadRow> { it.watched }.thenBy { it.downloadedAt }
        )

        for (row in ordered) {
            if (totalBytes <= limitBytes) break
            // Shared with DownloadPlugin's own delete methods — see its
            // cancelAndDeleteFile() for why this needs both
            // DownloadManager.remove() (job cancellation) and a manual
            // file delete (remove() alone doesn't reliably delete files
            // under setDestinationInExternalFilesDir, confirmed live).
            DownloadPlugin.cancelAndDeleteFile(context, row)
            db.markEvicted(row.episodeId)
            totalBytes -= (row.sizeBytes ?: 0L)
        }
    }
}
