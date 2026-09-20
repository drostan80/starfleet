package com.drostan.starfleet

import android.content.Context
import android.content.Intent
import android.media.MediaMetadataRetriever
import android.net.Uri
import android.util.Log
import androidx.activity.result.ActivityResult
import androidx.core.content.FileProvider
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.ActivityCallback
import com.getcapacitor.annotation.CapacitorPlugin
import java.io.File

/**
 * A1 step 18 (DESIGN.md §8) — launches VLC to stream a media file straight
 * off /files/ (server path, nginx-served, no download involved). `path` is
 * the server-container path GraphQL returns (filePathSonarr/Radarr,
 * "/data/..."); MediaUrl.build() (shared with DownloadPlugin, A2) mirrors
 * calendar.js's own launchMpv() for desktop mpv.
 *
 * Every path segment is percent-encoded independently (Uri.encode() per
 * segment, not one encode of the whole path) — confirmed both necessary
 * and sufficient this session (step 17): a real deployed file with
 * spaces/parens/braces/brackets in its name, encoded this exact way,
 * returned a correct 206 Partial Content + Content-Range through the live
 * nginx.
 *
 * extra_position/extra_duration on the ActivityResult are VLC's own
 * convention for a video launched with startActivityForResult (DESIGN.md
 * §8, "Android hurdles") — only present that way, not via any other
 * broadcast/callback mechanism.
 *
 * A2 step 25 — `uri` (a local file:// path from DownloadDatabase, already
 * resolved by the JS caller via Download.getLocalUri()) plays offline
 * instead of streaming: handed to VLC as content:// via FileProvider with
 * FLAG_GRANT_READ_URI_PERMISSION, since Android 11+ hides app-private
 * files from other apps otherwise (same package-visibility class of
 * problem as A1's <queries> fix, different mechanism — this one's about
 * file access, not component visibility).
 */
@CapacitorPlugin(name = "Vlc")
class VlcPlugin : Plugin() {

    // 2026-09-20 — found live, with real device data: VLC's own
    // extra_duration is reliable for a manual back-press mid-playback
    // (confirmed correct across every such exit today) but comes back as
    // 0 specifically on genuine natural end-of-file completion — the
    // exact case step 37's >=90% watched check most needs, and the exact
    // case it was silently failing on. extra_position stays correct in
    // that same case (confirmed: 1211140ms against a real ~1253000ms
    // file, 96.7% through). Rather than trust VLC's own duration at all,
    // play() looks the real duration up itself via MediaMetadataRetriever
    // before launching and vlcResult() uses that as the source of truth,
    // falling back to VLC's own value only if the retriever couldn't
    // read it either. A single Plugin instance only ever has one launch
    // pending at a time (the user can't start a second video from this
    // app while one is already open in VLC), so a plain instance field is
    // enough — no need to thread this through PluginCall/Intent extras.
    private var reliableDurationMs: Long = 0L

    @PluginMethod
    fun play(call: PluginCall) {
        val localUri = call.getString("uri")
        val filePath = call.getString("path")
        val title = call.getString("title", "")
        val episodeId = call.getString("episodeId")

        val mediaUri: Uri
        var offline = false

        if (localUri != null) {
            val file = Uri.parse(localUri).path?.let { File(it) }
            if (file == null || !file.exists()) {
                call.reject("local file not found")
                return
            }
            mediaUri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
            offline = true
        } else if (filePath != null) {
            val prefs = context.getSharedPreferences("starfleet", Context.MODE_PRIVATE)
            val serverUrl = prefs.getString("server_url", null)
            if (serverUrl == null) {
                call.reject("no server configured")
                return
            }
            mediaUri = Uri.parse(MediaUrl.build(serverUrl, filePath))
        } else {
            call.reject("path or uri is required")
            return
        }

        // Found live (user, 2026-09-20): VLC's own resume-last-position
        // feature is great for continuing an in-progress episode, but for
        // an already-watched one it means every re-watch starts (and
        // often instantly ends) right near the end, with no time to even
        // seek back. Force from-start only for a deliberate re-watch;
        // leave VLC's own resume alone otherwise. Streaming callers (no
        // download row) must pass fromStart explicitly since there's
        // nothing here to look it up from; offline callers can omit it
        // and let it resolve from the download row's own watched flag,
        // the same "episodeId alone is enough" pattern reportWatched()
        // already uses.
        val fromStart = if (call.data.has("fromStart")) {
            call.getBoolean("fromStart", false) ?: false
        } else if (offline && episodeId != null) {
            DownloadDatabase(context).get(episodeId)?.watched ?: false
        } else {
            false
        }

        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(mediaUri, "video/*")
            setPackage("org.videolan.vlc")
            putExtra("title", title)
            if (fromStart) putExtra("from_start", true)
            if (offline) addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }

        if (intent.resolveActivity(context.packageManager) == null) {
            call.reject("VLC not installed")
            return
        }

        // See reliableDurationMs's own comment: VLC's own extra_duration
        // can't be trusted on natural end-of-file completion, so get the
        // real duration ourselves before handing off to VLC. Best-effort:
        // a read failure here (a slow/unreachable stream, an unsupported
        // container for the retriever) must never block launching
        // playback — it just means vlcResult() falls back to whatever
        // VLC itself reports, same as before this fix existed.
        // MediaMetadataRetriever only implements AutoCloseable since API 29
        // (this project's minSdk is 24) — release() explicitly instead of
        // Kotlin's use{}, which would throw NoSuchMethodError on API 24-28.
        val retriever = MediaMetadataRetriever()
        reliableDurationMs = try {
            if (offline) {
                retriever.setDataSource(context, mediaUri)
            } else {
                retriever.setDataSource(mediaUri.toString(), emptyMap())
            }
            retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)
                ?.toLongOrNull() ?: 0L
        } catch (e: Exception) {
            Log.w("VlcPlugin", "MediaMetadataRetriever failed, falling back to VLC's own duration", e)
            0L
        } finally {
            retriever.release()
        }

        startActivityForResult(call, intent, "vlcResult")
    }

    @ActivityCallback
    private fun vlcResult(call: PluginCall?, result: ActivityResult) {
        if (call == null) return
        val data = result.data
        val position = data?.getLongExtra("extra_position", -1L) ?: -1L
        val vlcDuration = data?.getLongExtra("extra_duration", -1L) ?: -1L
        // Root-caused live (2026-09-20), real device data: VLC's own
        // extra_duration is reliable for a manual back-press mid-playback
        // but comes back as 0 specifically on genuine natural
        // end-of-file completion — confirmed via a real test (position
        // 1211140ms, ~96.7% through a real ~1253000ms file, yet
        // extra_duration read 0). extra_position stays correct in that
        // same case, which is exactly why this was so easy to miss: the
        // bug isn't "VLC returns nothing," it's "VLC returns a correct
        // position paired with a wrong duration," and the >=90% check
        // silently failed on the wrong number. Use the duration play()
        // looked up itself (reliableDurationMs) as the source of truth;
        // fall back to VLC's own value only if that lookup also failed.
        val duration = if (reliableDurationMs > 0) reliableDurationMs else vlcDuration
        Log.d(
            "VlcPlugin",
            "vlcResult resultCode=${result.resultCode} hasData=${data != null} " +
                "position=$position vlcDuration=$vlcDuration reliableDurationMs=$reliableDurationMs",
        )
        // Same >=90% threshold as the desktop mpv-helper.py and the
        // Android VLC client's own existing convention (DESIGN.md §8).
        val watched = duration > 0 && position.toDouble() / duration.toDouble() >= 0.9
        Log.d("VlcPlugin", "vlcResult watched=$watched")

        val ret = JSObject()
        ret.put("position", position)
        ret.put("duration", duration)
        ret.put("watched", watched)
        // Resolve immediately — reportWatched's network call must never
        // make the JS caller wait on it.
        call.resolve(ret)

        if (watched) reportWatched(call)
    }

    /**
     * A4 step 37 — native, not JS: the local SQLite write (via
     * DownloadDatabase.markWatched) always succeeds immediately and is
     * what eviction (A4 step 36) reads; addWatchEvent is a best-effort
     * network call queued for retry on failure, decoupled entirely from
     * that local write (DESIGN.md §8, "Eviction source of truth").
     *
     * episodeId alone is enough for offline playback — showId/season/
     * episode come from the download row itself (stored at download time,
     * DownloadDatabase v2) rather than needing the JS caller to pass them
     * again just to play back something already indexed. Streaming
     * callers (not downloaded) pass showId/season/episode directly since
     * there's no download row to look them up from.
     */
    private fun reportWatched(call: PluginCall) {
        val episodeId = call.getString("episodeId")
        var showId = call.getString("showId")
        var season = call.getInt("season")
        var episodeNum = call.getInt("episode")
        Log.d("VlcPlugin", "reportWatched episodeId=$episodeId showId=$showId season=$season episode=$episodeNum")

        if (episodeId != null) {
            val db = DownloadDatabase(context)
            db.markWatched(episodeId)
            if (showId == null) {
                val row = db.get(episodeId)
                showId = row?.showId
                season = row?.season
                episodeNum = row?.episode
            }
        }

        val resolvedShowId = showId ?: run {
            Log.d("VlcPlugin", "reportWatched: no showId resolvable, addWatchEvent skipped")
            return
        }
        val appContext = context.applicationContext
        Thread {
            val watchedAtIso = java.time.Instant.now().toString()
            val ok = LcarsClient.addWatchEvent(appContext, resolvedShowId, season, episodeNum, watchedAtIso)
            Log.d("VlcPlugin", "addWatchEvent ok=$ok showId=$resolvedShowId season=$season episode=$episodeNum")
            if (!ok) {
                WatchEventRetryQueue.enqueue(appContext, resolvedShowId, season, episodeNum, watchedAtIso)
            }
        }.start()
    }
}
