package com.drostan.starfleet

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.activity.result.ActivityResult
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.ActivityCallback
import com.getcapacitor.annotation.CapacitorPlugin

/**
 * A1 step 18 (DESIGN.md §8) — launches VLC to stream a media file straight
 * off /files/ (server path, nginx-served, no download involved). `path` is
 * the server-container path GraphQL returns (filePathSonarr/Radarr,
 * "/data/..."); stripping the /data prefix and rebuilding the public
 * /files/ URL mirrors calendar.js's own launchMpv() for desktop mpv.
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
 */
@CapacitorPlugin(name = "Vlc")
class VlcPlugin : Plugin() {

    @PluginMethod
    fun play(call: PluginCall) {
        val filePath = call.getString("path")
        val title = call.getString("title", "")

        if (filePath == null) {
            call.reject("path is required")
            return
        }

        val prefs = context.getSharedPreferences("starfleet", Context.MODE_PRIVATE)
        val serverUrl = prefs.getString("server_url", null)
        if (serverUrl == null) {
            call.reject("no server configured")
            return
        }

        val mediaPath = if (filePath.startsWith("/data")) filePath.substring("/data".length) else filePath
        val encodedPath = mediaPath.split("/").joinToString("/") { Uri.encode(it) }
        val mediaUrl = "$serverUrl/files$encodedPath"

        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(Uri.parse(mediaUrl), "video/*")
            setPackage("org.videolan.vlc")
            putExtra("title", title)
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
