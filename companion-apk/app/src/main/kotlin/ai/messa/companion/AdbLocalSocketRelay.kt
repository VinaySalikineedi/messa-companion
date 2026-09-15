package ai.messa.companion

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket

/**
 * The phone-side half of the byte-pipe (open-source-phone.md section 2).
 * Connects to `127.0.0.1:<port>` -- the phone's OWN local wireless-
 * debugging ADB daemon, discovered by [NsdAdbPortDiscovery] -- and relays
 * raw bytes in both directions against whatever [WebSocketBridgeClient]
 * hands it. Exactly mirrors messa/companion_bridge.py's
 * `CompanionDeviceBridge` on the server side: neither side ever parses a
 * single ADB protocol byte, they only ever move them.
 *
 * This is the piece that makes "preserve standard ADB/uiautomator2
 * unmodified" true on the PHONE side too -- from the local ADB daemon's
 * point of view, this looks exactly like any other ordinary local ADB
 * client (Android Studio, a USB debugging session, etc.) connecting on
 * loopback. It has no idea its bytes are then being forwarded across a
 * WebSocket to a server on the other side of the world.
 */
class AdbLocalSocketRelay(
    private val onLocalBytes: (ByteArray) -> Unit,
    private val onLocalDisconnected: () -> Unit,
) {
    private var socket: Socket? = null
    private var readJob: Job? = null
    private val scope = CoroutineScope(Dispatchers.IO + Job())

    @Volatile
    private var connected = false

    /**
     * Opens the local TCP connection to the phone's own ADB daemon.
     * Called once per bridge session (this app only ever needs ONE local
     * ADB connection open at a time, same as the server side only ever
     * has one adbutils client at a time) -- if the port turns out stale
     * (Wi-Fi reconnected since discovery), the caller ([BridgeForegroundService])
     * is expected to re-run [NsdAdbPortDiscovery] and retry.
     */
    suspend fun connect(port: Int): Boolean = withContext(Dispatchers.IO) {
        try {
            val s = Socket()
            s.connect(InetSocketAddress("127.0.0.1", port), CONNECT_TIMEOUT_MS)
            socket = s
            connected = true
            readJob = scope.launch { pumpLocalToCallback(s) }
            true
        } catch (e: IOException) {
            Log.w(TAG, "Local ADB connect failed on port $port: $e")
            false
        }
    }

    private suspend fun pumpLocalToCallback(s: Socket) {
        val buffer = ByteArray(65536)
        try {
            val input = s.getInputStream()
            while (connected && scope.isActive) {
                val n = input.read(buffer)
                if (n < 0) break
                onLocalBytes(buffer.copyOf(n))
            }
        } catch (e: IOException) {
            Log.i(TAG, "Local ADB read loop ended: $e")
        } finally {
            connected = false
            onLocalDisconnected()
        }
    }

    /** Writes one chunk of bytes that arrived over the WebSocket straight
     * to the local ADB socket -- called from [WebSocketBridgeClient]'s
     * onBinaryMessage callback, wired up by [BridgeForegroundService]. */
    fun writeToLocal(data: ByteArray) {
        val s = socket ?: return
        try {
            s.getOutputStream().write(data)
            s.getOutputStream().flush()
        } catch (e: IOException) {
            Log.w(TAG, "Write to local ADB socket failed: $e")
            connected = false
            onLocalDisconnected()
        }
    }

    fun isConnected(): Boolean = connected

    fun close() {
        connected = false
        readJob?.cancel()
        try {
            socket?.close()
        } catch (e: IOException) {
            // Already gone -- nothing to do.
        }
        socket = null
    }

    companion object {
        private const val TAG = "AdbLocalSocketRelay"
        private const val CONNECT_TIMEOUT_MS = 5_000
    }
}
