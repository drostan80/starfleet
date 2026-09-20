package com.drostan.starfleet

import android.content.Intent
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
 *
 * Step 15 — no address stored at all (first run) means ServerSetupActivity
 * instead. This overrides load(), not onCreate(): BridgeActivity.onCreate()
 * must still call through to AppCompatActivity's own onCreate() (skipping
 * it crashes immediately with SuperNotCalledException, thrown by the
 * framework right after onCreate() returns — checked and confirmed against
 * real BridgeActivity behavior before shipping this, not assumed), but
 * super.onCreate() is what calls load() in the first place, and load() is
 * the one thing that actually builds the Bridge/WebView. Redirecting from
 * inside our load() override, before ever calling super.load(), means the
 * unconfigured case never spins up a WebView pointed at nothing.
 */
class MainActivity : BridgeActivity() {
    private var serverConfigured = true

    override fun onCreate(savedInstanceState: Bundle?) {
        // Must run before super.onCreate() -> load() builds the Bridge
        // (BridgeActivity.registerPlugin() just appends to bridgeBuilder,
        // read once at Bridge construction time).
        registerPlugin(VlcPlugin::class.java)
        registerPlugin(TokenPlugin::class.java)
        registerPlugin(DownloadPlugin::class.java)
        registerPlugin(LcarsWsPlugin::class.java)
        registerPlugin(AppSettingsPlugin::class.java)

        val prefs = getSharedPreferences("starfleet", MODE_PRIVATE)
        val serverUrl = prefs.getString("server_url", null)
        if (serverUrl != null) {
            config = CapConfig.Builder(this)
                .setServerUrl(serverUrl)
                .create()
            // A4 step 33 — enqueueUniquePeriodicWork + KEEP is idempotent,
            // safe to call on every launch; only actually schedules once.
            // Deliberately no separate one-time trigger alongside this:
            // found by testing that a plain (non-unique)
            // OneTimeWorkRequestBuilder call here could race the periodic
            // work's own first execution and double-enqueue every episode
            // (confirmed live — every file ended up downloaded twice,
            // DownloadManager auto-renaming the second copy "-1"). The
            // periodic work alone doesn't have this problem: WorkManager
            // guarantees serialized execution for a single uniquely-named
            // periodic work, which is exactly why enqueueUniquePeriodicWork
            // (not a plain enqueue) is used here in the first place.
            AutoDownloadWorker.schedule(this)
        } else {
            serverConfigured = false
        }
        super.onCreate(savedInstanceState)
    }

    override fun load() {
        if (!serverConfigured) {
            startActivity(Intent(this, ServerSetupActivity::class.java))
            finish()
            return
        }
        super.load()
    }
}
