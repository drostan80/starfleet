package com.drostan.starfleet

import android.content.Context
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
        }
        call.resolve(result)
    }

    /**
     * Accepts any subset of the four settings — a page only changing one
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
        }
        var autoDownloadChanged = false
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
}
