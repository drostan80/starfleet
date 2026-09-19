package com.drostan.starfleet

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.os.Handler
import android.os.Looper
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.annotation.CapacitorPlugin
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject

/**
 * A3 (DESIGN.md §8) — replaces calendar.js's 10s poll with a live
 * subscription. Connects to LCARS's graphql-transport-ws endpoint at
 * exactly "/" (confirmed live, A0 step 12's own end-to-end WS check —
 * same server, same subprotocol negotiation, this is that same chain
 * driven from Kotlin instead of Python).
 *
 * Auth is the bearer token on the WS handshake's Authorization header,
 * not inside the connection_init payload — BearerTokenMiddleware reads it
 * from the HTTP upgrade request headers regardless of scope type
 * (server.py), so connection_init itself carries no payload.
 *
 * JS side: `LcarsWs.addListener('episodeAvailabilityChanged', cb)` /
 * `addListener('showCreated', cb)` — Capacitor's own listener mechanism,
 * no extra native registration needed beyond calling notifyListeners().
 */
@CapacitorPlugin(name = "LcarsWs")
class LcarsWsPlugin : Plugin() {

    private var webSocket: WebSocket? = null
    private var connected = false
    private var backoffMs = INITIAL_BACKOFF_MS
    private val mainHandler = Handler(Looper.getMainLooper())
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    companion object {
        private const val INITIAL_BACKOFF_MS = 2_000L
        private const val MAX_BACKOFF_MS = 60_000L
    }

    override fun load() {
        connect()

        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        networkCallback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                // A network coming back (WiFi<->mobile switch, airplane
                // mode off, etc.) is exactly when a stale socket needs
                // replacing — reconnect unconditionally rather than
                // trying to guess whether the old one is still good.
                mainHandler.post { reconnect() }
            }
        }
        cm.registerDefaultNetworkCallback(networkCallback!!)
    }

    private fun connect() {
        val wsUrl = LcarsClient.wsUrl(context)
        val token = LcarsClient.bearerToken(context)
        if (wsUrl == null || token == null) {
            // No server/token yet (e.g. first run, before ServerSetupActivity
            // + login) — nothing to connect to. A later connectivity change
            // or app restart (once TokenPlugin.setToken() has run) retries.
            return
        }

        val request = Request.Builder()
            .url(wsUrl)
            .addHeader("Authorization", "Bearer $token")
            .addHeader("Sec-WebSocket-Protocol", "graphql-transport-ws")
            .build()

        webSocket = LcarsClient.http.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(ws: WebSocket, response: Response) {
                ws.send(JSONObject().put("type", "connection_init").toString())
            }

            override fun onMessage(ws: WebSocket, text: String) {
                handleMessage(ws, text)
            }

            override fun onClosing(ws: WebSocket, code: Int, reason: String) {
                connected = false
                scheduleReconnect()
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                connected = false
                scheduleReconnect()
            }
        })
    }

    private fun handleMessage(ws: WebSocket, text: String) {
        val msg = JSONObject(text)
        when (msg.optString("type")) {
            "connection_ack" -> {
                connected = true
                backoffMs = INITIAL_BACKOFF_MS
                subscribe(ws, "1", "episodeAvailabilityChanged")
                subscribe(ws, "2", "showCreated")
            }
            "next" -> {
                val id = msg.optString("id")
                val fieldName = if (id == "1") "episodeAvailabilityChanged" else "showCreated"
                val data = msg.optJSONObject("payload")?.optJSONObject("data")?.optJSONObject(fieldName)
                    ?: return
                notifyListeners(fieldName, JSObject(data.toString()))
            }
            // "error"/"complete" — best-effort subscriptions layered on top
            // of each client's own poll/refresh path (schema.graphql's own
            // module docstring); nothing here needs a hard failure surfaced
            // to JS, the existing poll (desktop) or next reconnect covers it.
        }
    }

    private fun subscribe(ws: WebSocket, id: String, fieldName: String) {
        val message = JSONObject().apply {
            put("id", id)
            put("type", "subscribe")
            put("payload", JSONObject().put("query", "subscription { $fieldName { id } }"))
        }
        ws.send(message.toString())
    }

    private fun scheduleReconnect() {
        mainHandler.postDelayed({ reconnect() }, backoffMs)
        backoffMs = (backoffMs * 2).coerceAtMost(MAX_BACKOFF_MS)
    }

    private fun reconnect() {
        if (connected) return
        webSocket?.cancel()
        connect()
    }
}
