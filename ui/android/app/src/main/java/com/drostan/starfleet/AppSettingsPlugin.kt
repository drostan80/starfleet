package com.drostan.starfleet

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin

/**
 * 2026-09-20 (user request) — the app's own download/storage settings
 * (Wi-Fi-only, storage limit, auto-download on/off, auto-delete-after-
 * watched) previously lived only in ServerSetupActivity, reached via the
 * "Change server" shortcut. The user wants them reachable from inside the
 * app itself too — settings.html gets a native-only section that reads
 * and writes through this plugin. ServerSetupActivity's own fields are
 * left in place (same SharedPreferences keys, so both stay in sync
 * automatically) — this is an additional access point, not a replacement.
 */
@CapacitorPlugin(name = "AppSettings")
class AppSettingsPlugin : Plugin() {

    private fun prefs() = context.getSharedPreferences("starfleet", Context.MODE_PRIVATE)

    @PluginMethod
    fun getSettings(call: PluginCall) {
        val p = prefs()
        val result = JSObject().apply {
            put("wifiOnly", p.getBoolean("downloads_wifi_only", false))
            put("storageLimitGb", p.getFloat("storage_limit_gb", 0f).toDouble())
            put("autoDownloadEnabled", p.getBoolean("auto_download_enabled", true))
            put("autoDeleteWatchedHours", p.getFloat("auto_delete_watched_hours", 0f).toDouble())
            put("autoDownloadGrabDays", LcarsClient.grabWindowDays(context))
        }
        call.resolve(result)
    }

    /**
     * Accepts any subset of the settings — a page only changing one
     * field doesn't need to resend the others. autoDownloadEnabled going
     * through here (not just ServerSetupActivity's own listener) is why
     * this immediately calls AutoDownloadWorker.schedule()/cancel() rather
     * than just writing the pref — otherwise toggling it off from
     * settings.html wouldn't actually stop the periodic work until the
     * next app restart.
     */
    @PluginMethod
    fun updateSettings(call: PluginCall) {
        val editor = prefs().edit()
        var autoDownloadChanged = false
        if (call.data.has("wifiOnly")) {
            editor.putBoolean("downloads_wifi_only", call.getBoolean("wifiOnly", false) ?: false)
        }
        if (call.data.has("storageLimitGb")) {
            editor.putFloat("storage_limit_gb", (call.getDouble("storageLimitGb") ?: 0.0).toFloat())
        }
        if (call.data.has("autoDeleteWatchedHours")) {
            editor.putFloat(
                "auto_delete_watched_hours",
                (call.getDouble("autoDeleteWatchedHours") ?: 0.0).toFloat(),
            )
            autoDownloadChanged = true  // the tick also runs for the auto-delete alone
        }
        if (call.data.has("autoDownloadGrabDays")) {
            editor.putInt(
                LcarsClient.GRAB_WINDOW_PREF,
                (call.getInt("autoDownloadGrabDays") ?: LcarsClient.GRAB_WINDOW_DEFAULT_DAYS)
                    .coerceIn(1, LcarsClient.GRAB_WINDOW_MAX_DAYS),
            )
        }
        if (call.data.has("autoDownloadEnabled")) {
            editor.putBoolean(
                "auto_download_enabled",
                call.getBoolean("autoDownloadEnabled", true) ?: true,
            )
            autoDownloadChanged = true
        }
        editor.apply()
        if (autoDownloadChanged) {
            AutoDownloadWorker.schedule(context)
        }
        call.resolve()
    }

    /**
     * 2026-11-11 (user): a link to another site (Sonarr, AniList …) opened inside the WebView and
     * the only way back was closing the app. config.js sends such links here instead; the system
     * browser opens them and the app stays as it was.
     */
    @PluginMethod
    fun openExternal(call: PluginCall) {
        val url = call.getString("url")
        if (url.isNullOrBlank() || !(url.startsWith("http://") || url.startsWith("https://"))) {
            call.reject("a http(s) url is required")
            return
        }
        try {
            context.startActivity(
                Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
            call.resolve()
        } catch (e: ActivityNotFoundException) {
            call.reject("no browser found")
        }
    }
}
