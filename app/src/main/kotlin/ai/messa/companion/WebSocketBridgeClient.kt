package ai.messa.companion

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * The outbound half of the bridge (open-source-phone.md section 2): opens
 * and holds a single `wss://.../device/ws` connection to the Messa
 * server, authenticated by [DeviceSecretManager]'s bearer secret, and
 * exposes a tiny [Listener] callback surface for [BridgeForegroundService]
 * to wire up to [AdbLocalSocketRelay]. This class does no ADB protocol
 * work and knows nothing about local sockets -- it is purely "bytes in,
 * bytes out, plus the handful of JSON/text control messages the server
 * side (messa/companion_bridge.py) sends."
 *
 * Reconnection policy is intentionally simple and caller-driven: this
 * class reports disconnects via [Listener.onDisconnected] and does NOT
 * auto-retry itself -- [BridgeForegroundService] owns the retry/backoff
 * loop, since it's also the thing that knows whether the phone still has
 * network connectivity worth retrying against.
 */
class WebSocketBridgeClient(
    private val serverWsUrl: String,
    private val listener: Listener,
) {
    interface Listener {
        fun onPairingRequired(code: String)
        fun onBridgeActive()
        fun onBinaryMessage(data: ByteArray)
        fun onDisconnected(code: Int, reason: String)
        fun onError(message: String)
    }

    private val client = OkHttpClient.Builder()
        // No client-side ping interval here on purpose: the SERVER drives
        // the heartbeat (messa/companion_bridge.py's
        // COMPANION_BRIDGE_HEARTBEAT_SECONDS "ping" text frames), and this
        // client just replies "pong" the moment one arrives (see
        // onMessage below) -- a second, independent client-side pinger
        // would just be redundant traffic.
        .readTimeout(0, TimeUnit.MILLISECONDS) // WebSocket: no read timeout, the app manages liveness itself
        .build()

    @Volatile
    private var webSocket: WebSocket? = null

    @Volatile
    var isBridgeActive: Boolean = false
        private set

    fun connect(deviceSecret: String, deviceName: String) {
        val request = Request.Builder()
            .url(serverWsUrl)
            .addHeader("Authorization", "Bearer $deviceSecret")
            .addHeader("X-Device-Name", deviceName)
            .build()

        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "WebSocket open, awaiting server pairing/bridge state")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleTextMessage(webSocket, text)
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                isBridgeActive = true
                listener.onBinaryMessage(bytes.toByteArray())
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                isBridgeActive = false
                listener.onDisconnected(code, reason)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                isBridgeActive = false
                listener.onError(t.message ?: "WebSocket connection failed")
                listener.onDisconnected(-1, t.message ?: "connection failed")
            }
        })
    }

    private fun handleTextMessage(webSocket: WebSocket, text: String) {
        // The server's ping is a bare "ping" text frame, never JSON --
        // check that first since it's the by-far-most-frequent message
        // once a bridge is active (every COMPANION_BRIDGE_HEARTBEAT_SECONDS).
        if (text == "ping") {
            webSocket.send("pong")
            return
        }

        val parsed = runCatching { JSONObject(text) }.getOrNull() ?: return
        when (parsed.optString("type")) {
            "pairing_required" -> {
                val code = parsed.optString("code")
                if (code.isNotBlank()) listener.onPairingRequired(code)
            }
            "bridge_active" -> {
                isBridgeActive = true
                listener.onBridgeActive()
            }
            else -> Log.d(TAG, "Unrecognized control message: $text")
        }
    }

    /** Forwards one raw ADB-protocol byte chunk read from the local
     * loopback socket ([AdbLocalSocketRelay]) up to the server as a
     * binary WebSocket frame -- no framing/parsing of any kind. */
    fun sendBinary(data: ByteArray) {
        webSocket?.send(data.toByteString(0, data.size))
    }

    /** The touch-killswitch's one and only output (open-source-phone.md
     * section 3.4) -- see [TouchKillswitchOverlay] and messa/
     * companion_bridge.py's `_handle_text_control_message`. */
    fun sendTouchAbort() {
        webSocket?.send("""{"type":"touch_abort"}""")
    }

    fun disconnect() {
        isBridgeActive = false
        webSocket?.close(1000, "client shutdown")
        webSocket = null
    }

    companion object {
        private const val TAG = "WebSocketBridgeClient"
    }
}
