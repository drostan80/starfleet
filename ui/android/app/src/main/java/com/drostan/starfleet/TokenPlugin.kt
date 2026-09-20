package com.drostan.starfleet

import android.content.Context
import android.util.Log
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin

/**
 * A1 step 19 (DESIGN.md §8) — the web client calls Token.setToken(...)
 * right after syncConfig() succeeds (zero-config principle, step 5:
 * "A Capacitor bridge call copies lcars_token into SharedPreferences for
 * native code"). VlcPlugin doesn't need the token itself (it streams
 * straight off the ungated /files/ path, no bearer required — nginx.conf),
 * but A3/A4's LcarsClient.kt (native GraphQL calls for subscriptions and
 * auto-download) do, and can't read the web client's localStorage.
 *
 * A4 testing (2026-09-19) found this write intermittently not landing even
 * though the JS-side call resolved with no exception — cause not yet
 * identified. commit() (synchronous, returns a real success boolean)
 * replaces apply() (fire-and-forget) here so a failure is at least
 * observable instead of silently possible; Log.d calls bracket the write
 * so Logcat shows whether the native method was even reached, and a
 * same-thread readback confirms whether the write actually persisted
 * before this call resolves.
 */
@CapacitorPlugin(name = "Token")
class TokenPlugin : Plugin() {

    @PluginMethod
    fun setToken(call: PluginCall) {
        val token = call.getString("token")
        if (token == null) {
            call.reject("token is required")
            return
        }
        Log.d("TokenPlugin", "setToken called, token length=${token.length}")
        val prefs = context.getSharedPreferences("starfleet", Context.MODE_PRIVATE)
        val committed = prefs.edit().putString("lcars_token", token).commit()
        val readback = prefs.getString("lcars_token", null)
        Log.d(
            "TokenPlugin",
            "setToken commit()=$committed readbackMatches=${readback == token}"
        )
        if (!committed || readback != token) {
            call.reject("SharedPreferences commit failed or readback mismatch")
            return
        }
        call.resolve()
    }
}
