package com.drostan.starfleet

import android.net.Uri

/**
 * Shared by VlcPlugin (A1) and DownloadPlugin (A2) — both need the same
 * server-container-path -> public /files/ URL translation. Per-segment
 * Uri.encode() (not one encode of the whole path) is load-bearing: verified
 * against a real deployed file with spaces/parens/braces/brackets in its
 * name (A1 step 17) — a Range request through the live nginx came back a
 * correct 206/Content-Range only with this exact encoding shape.
 */
object MediaUrl {
    fun build(serverUrl: String, filePath: String): String {
        val mediaPath = if (filePath.startsWith("/data")) filePath.substring("/data".length) else filePath
        val encodedPath = mediaPath.split("/").joinToString("/") { Uri.encode(it) }
        return "$serverUrl/files$encodedPath"
    }
}
