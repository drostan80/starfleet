package com.drostan.starfleet

import android.content.Intent
import android.os.Bundle
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * A1 step 15 (DESIGN.md §8) — the only screen the zero-config client has.
 * One text field: LAN or Tailscale IP, port 8888 (nginx). Saved to the same
 * SharedPreferences MainActivity reads ("starfleet"/"server_url") — see
 * MainActivity.kt's step-14 spike for why that key has to match exactly.
 *
 * Launched two ways: MainActivity itself, when no address is stored yet
 * (first run), and the "Change server" static app shortcut (step 16) at
 * any time after. The shortcut case pre-fills the existing address instead
 * of showing a blank field, since the user is here to edit it, not start
 * fresh.
 */
class ServerSetupActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_server_setup)

        val prefs = getSharedPreferences("starfleet", MODE_PRIVATE)
        val addressField = findViewById<EditText>(R.id.server_address)
        val errorMessage = findViewById<TextView>(R.id.error_message)
        val continueButton = findViewById<Button>(R.id.continue_button)

        prefs.getString("server_url", null)?.let { existing ->
            addressField.setText(existing.removePrefix("http://").removePrefix("https://"))
        }

        fun trySubmit() {
            val raw = addressField.text.toString().trim()
            val normalized = normalizeServerAddress(raw)
            if (normalized == null) {
                errorMessage.text = getString(R.string.server_setup_error)
                errorMessage.visibility = TextView.VISIBLE
                return
            }
            prefs.edit().putString("server_url", normalized).apply()
            startActivity(
                Intent(this, MainActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
                }
            )
            finish()
        }

        continueButton.setOnClickListener { trySubmit() }
        addressField.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_DONE) {
                trySubmit()
                true
            } else {
                false
            }
        }
    }
}

/**
 * Accepts a bare "host:port" or "host" (defaults to port 8888, the nginx
 * port every deployment uses — DESIGN.md §8's zero-config principle) as
 * well as an already-schemed "http://host:port". Always normalizes to
 * "http://..." — cleartext is the deployment's own reality (LAN/Tailscale,
 * plain HTTP), not something this screen should ask the user to know
 * about. Returns null for empty/whitespace-only input; deliberately not a
 * stricter host-format regex, since a Tailscale MagicDNS name (e.g.
 * "tiny.tailnet-name.ts.net") is a valid address here too, not just an IP.
 */
fun normalizeServerAddress(input: String): String? {
    val trimmed = input.trim().removeSuffix("/")
    if (trimmed.isEmpty()) return null
    val withScheme = if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) {
        trimmed
    } else {
        "http://$trimmed"
    }
    val hostAndPort = withScheme.substringAfter("://")
    if (hostAndPort.isEmpty()) return null
    return if (hostAndPort.contains(":")) withScheme else "$withScheme:8888"
}
