package ai.messa.companion

import android.content.Context
import android.content.Intent
import android.util.Base64
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
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
 * The outbound WebSocket client connecting to Messa's `/device/ws` endpoint.
 *
 * Supports two simultaneous protocols over the single connection:
 * 1. Native Accessibility JSON RPC: high-speed async commands (dump_tree, tap,
 *    swipe, type, press_key, screenshot, wake, app_start) executed directly
 *    by [MessaAccessibilityService].
 * 2. Binary ADB loopback forwarding: legacy fallback for uninstrumented devices.
 */
class WebSocketBridgeClient(
    private val serverWsUrl: String,
    private val listener: Listener,
    private val context: Context? = null,
) {
    interface Listener {
        fun onPairingRequired(code: String)
        fun onBridgeActive()
        fun onBinaryMessage(data: ByteArray)
        fun onDisconnected(code: Int, reason: String)
        fun onError(message: String)
    }

    private val scope = CoroutineScope(Dispatchers.IO)
    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS)
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
                // Announce native accessibility capabilities to server
                val isA11y = MessaAccessibilityService.isAvailable()
                val caps = JSONObject().apply {
                    put("type", "client_capabilities")
                    put("accessibility_enabled", isA11y)
                }
                webSocket.send(caps.toString())
                Log.i(TAG, "Announced client capabilities: accessibility_enabled=$isA11y")
            }
            "rpc_request" -> {
                scope.launch {
                    handleRpcRequest(parsed)
                }
            }
            else -> Log.d(TAG, "Unrecognized control message: $text")
        }
    }

    private suspend fun handleRpcRequest(req: JSONObject) {
        val reqId = req.optString("id")
        val action = req.optString("action")
        val service = MessaAccessibilityService.instance

        if (service == null) {
            sendRpcResponse(reqId, "error", "Accessibility service not enabled", null)
            return
        }

        try {
            when (action) {
                "dump_tree" -> {
                    val result = service.dumpHierarchy()
                    sendRpcResponse(reqId, "ok", null, result)
                }
                "tap" -> {
                    val x = req.optInt("x")
                    val y = req.optInt("y")
                    val ok = service.tap(x, y)
                    sendRpcResponse(reqId, if (ok) "ok" else "failed", null, null)
                }
                "swipe" -> {
                    val x1 = req.optInt("x1")
                    val y1 = req.optInt("y1")
                    val x2 = req.optInt("x2")
                    val y2 = req.optInt("y2")
                    val durationMs = req.optLong("duration_ms", 300L)
                    val ok = service.swipe(x1, y1, x2, y2, durationMs)
                    sendRpcResponse(reqId, if (ok) "ok" else "failed", null, null)
                }
                "type" -> {
                    val text = req.optString("text")
                    val targetId = req.optString("target_id").takeIf { it.isNotBlank() }
                    val ok = service.setText(targetId, text)
                    sendRpcResponse(reqId, if (ok) "ok" else "failed", null, null)
                }
                "press_key" -> {
                    val key = req.optString("key")
                    val ok = service.pressKey(key)
                    sendRpcResponse(reqId, if (ok) "ok" else "failed", null, null)
                }
                "screenshot" -> {
                    val bytes = service.takeScreenshot()
                    if (bytes != null) {
                        val b64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
                        val res = JSONObject().apply { put("image_base64", b64) }
                        sendRpcResponse(reqId, "ok", null, res)
                    } else {
                        sendRpcResponse(reqId, "failed", "Screenshot capture failed", null)
                    }
                }
                "wake" -> {
                    service.wakeAndUnlock()
                    sendRpcResponse(reqId, "ok", null, null)
                }
                "app_start" -> {
                    val pkg = req.optString("package")
                    val ctx = context ?: service.applicationContext
                    val launchIntent = ctx.packageManager.getLaunchIntentForPackage(pkg)
                    if (launchIntent != null) {
                        launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                        ctx.startActivity(launchIntent)
                        sendRpcResponse(reqId, "ok", null, null)
                    } else {
                        sendRpcResponse(reqId, "failed", "Package not found: $pkg", null)
                    }
                }
                else -> {
                    sendRpcResponse(reqId, "unknown_action", "Action $action not supported", null)
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "RPC $action error: ${e.message}", e)
            sendRpcResponse(reqId, "error", e.message ?: "RPC execution error", null)
        }
    }

    private fun sendRpcResponse(reqId: String, status: String, error: String? = null, result: JSONObject? = null) {
        val resp = JSONObject().apply {
            put("type", "rpc_response")
            put("id", reqId)
            put("status", status)
            if (error != null) put("error", error)
            if (result != null) put("result", result)
        }
        webSocket?.send(resp.toString())
    }

    /** Forwards one raw ADB-protocol byte chunk up to the server. */
    fun sendBinary(data: ByteArray) {
        webSocket?.send(data.toByteString(0, data.size))
    }

    /** The touch-killswitch abort message. */
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
