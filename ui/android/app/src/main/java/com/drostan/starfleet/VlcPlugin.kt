package com.drostan.starfleet

import android.content.Context
import android.content.Intent
import android.net.Uri
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

    @PluginMethod
    fun play(call: PluginCall) {
        val localUri = call.getString("uri")
        val filePath = call.getString("path")
        val title = call.getString("title", "")

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

        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(mediaUri, "video/*")
            setPackage("org.videolan.vlc")
            putExtra("title", title)
            if (offline) addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }

        if (intent.resolveActivity(context.packageManager) == null) {
            call.reject("VLC not installed")
            return
        }

        startActivityForResult(call, intent, "vlcResult")
    }

    @ActivityCallback
    private fun vlcResult(call: PluginCall?, result: ActivityResult) {
        if (call == null) return
        val data = result.data
        val position = data?.getLongExtra("extra_position", -1L) ?: -1L
        val duration = data?.getLongExtra("extra_duration", -1L) ?: -1L

        val ret = JSObject()
        ret.put("position", position)
        ret.put("duration", duration)
        // Same >=90% threshold as the desktop mpv-helper.py and the
        // Android VLC client's own existing convention (DESIGN.md §8).
        ret.put("watched", duration > 0 && position.toDouble() / duration.toDouble() >= 0.9)
        call.resolve(ret)
    }
}
