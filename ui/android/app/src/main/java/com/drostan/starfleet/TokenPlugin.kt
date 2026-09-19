package com.drostan.starfleet

import android.content.Context
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
        context.getSharedPreferences("starfleet", Context.MODE_PRIVATE)
            .edit()
            .putString("lcars_token", token)
            .apply()
        call.resolve()
    }
}
