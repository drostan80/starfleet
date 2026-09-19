package com.drostan.starfleet

import android.os.Bundle
import com.getcapacitor.BridgeActivity
import com.getcapacitor.CapConfig

/**
 * A1 step 14 spike (DESIGN.md §8) — dynamic server.url from SharedPreferences.
 *
 * BridgeActivity.onCreate() reads the protected `config` field inside its own
 * super.onCreate() (via load(), called at the end of that method) — verified
 * against the real @capacitor/android 8.5.2 source, not assumed. So setting
 * `config` here before calling super.onCreate() is enough: no override of
 * load()/onCreate() internals needed, no reflection.
 *
 * CapConfig.Builder(context) starts from hardcoded defaults, not from the
 * static capacitor.config.json — confirmed in source (no loadDefault()-as-
 * Builder factory exists). Harmless today since our static config carries
 * nothing else CapConfig reads (appId/appName/webDir aren't CapConfig
 * fields; server.cleartext isn't either — see AndroidManifest.xml's
 * usesCleartextTraffic instead, which is the real mechanism in this
 * Capacitor version). Revisit if a later step adds e.g. allowNavigation to
 * capacitor.config.json — it would need repeating here too.
 */
class MainActivity : BridgeActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        val prefs = getSharedPreferences("starfleet", MODE_PRIVATE)
        val serverUrl = prefs.getString("server_url", null)
        if (serverUrl != null) {
            config = CapConfig.Builder(this)
                .setServerUrl(serverUrl)
                .create()
        }
        super.onCreate(savedInstanceState)
    }
}
