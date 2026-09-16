package ai.messa.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * The persistent foreground service that IS the bridge, end to end
 * (open-source-phone.md section 2/3/4). Holds one [WebSocketBridgeClient]
 * (the outbound `/device/ws` connection) and one [AdbLocalSocketRelay]
 * (the local connection to this phone's own ADB daemon), and wires bytes
 * straight between them -- neither side is ever parsed, this class is
 * pure plumbing plus lifecycle/retry management.
 *
 * `START_STICKY` + a persistent foreground notification is roadblock #3's
 * answer (section 4): without this, Android's battery optimization/Doze
 * would suspend the process's networking within minutes of the screen
 * turning off, silently dropping the bridge. `REQUEST_IGNORE_BATTERY_
 * OPTIMIZATIONS` is requested once from [MainActivity] for the same
 * reason, before this service ever starts.
 */
class BridgeForegroundService : Service() {

    private val scope = CoroutineScope(Dispatchers.IO + Job())
    private var wsClient: WebSocketBridgeClient? = null
    private var adbRelay: AdbLocalSocketRelay? = null
    private lateinit var deviceSecretManager: DeviceSecretManager
    private lateinit var nsdDiscovery: NsdAdbPortDiscovery
    private lateinit var killswitchOverlay: TouchKillswitchOverlay

    private var retryDelayMs = INITIAL_RETRY_DELAY_MS

    override fun onCreate() {
        super.onCreate()
        deviceSecretManager = DeviceSecretManager(applicationContext)
        nsdDiscovery = NsdAdbPortDiscovery(applicationContext)
        killswitchOverlay = TouchKillswitchOverlay(applicationContext) {
            // A genuine touch always sends touch_abort immediately AND
            // hides the overlay itself -- see this class's own doc for
            // why an overlay bug must never be able to trap the user
            // behind an invisible, unresponsive layer.
            wsClient?.sendTouchAbort()
            killswitchOverlay.hide()
        }
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val notification = buildNotification(getString(R.string.notification_title_connecting))
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        if (intent?.action == ACTION_STOP) {
            stopBridge()
            stopSelf()
            return START_NOT_STICKY
        }
        startBridge()
        // START_STICKY: if the OS kills this process under memory pressure,
        // it's restarted (with a null Intent) and startBridge() runs again
        // -- the WebSocket/local-ADB connections themselves are never
        // durable across a process death, only this restart behavior is.
        return START_STICKY
    }

    private fun startBridge() {
        scope.launch {
            connectLoop()
        }
    }

    private suspend fun connectLoop() {
        val deviceSecret = deviceSecretManager.getOrCreateSecret()
        val deviceName = deviceSecretManager.getOrCreateDeviceName(Build.MODEL ?: "Android Phone")

        while (true) {
            val serverUrl = BridgeConfig.deviceWsUrl(applicationContext)
            val connected = connectOnce(serverUrl, deviceSecret, deviceName)
            if (connected) {
                retryDelayMs = INITIAL_RETRY_DELAY_MS
            }
            // connectOnce only returns after the connection has ended one
            // way or another (disconnected, error, or a local ADB relay
            // failure) -- back off before trying again so a persistent
            // outage (no network, server down) doesn't spin this loop.
            delay(retryDelayMs)
            retryDelayMs = (retryDelayMs * 2).coerceAtMost(MAX_RETRY_DELAY_MS)
        }
    }

    /** Runs one full connection attempt; suspends until it ends. Returns
     * true if the bridge became fully active at some point during this
     * attempt (used only to reset the backoff delay). */
    private suspend fun connectOnce(serverUrl: String, deviceSecret: String, deviceName: String): Boolean {
        var becameActive = false
        val doneSignal = kotlinx.coroutines.CompletableDeferred<Unit>()

        adbRelay = AdbLocalSocketRelay(
            onLocalBytes = { data -> wsClient?.sendBinary(data) },
            onLocalDisconnected = {
                // A local ADB client (adbutils, mid-task) disconnecting is
                // NORMAL and expected between tasks -- the WS bridge stays
                // open (mirrors messa/companion_bridge.py's own "local
                // disconnect must not end the bridge" design). The one
                // thing this DOES end is the touch killswitch's watch
                // window -- nothing is actively driving the screen between
                // tasks, so there's nothing to guard against a touch for.
                killswitchOverlay.hide()
            },
        )

        wsClient = WebSocketBridgeClient(
            serverWsUrl = serverUrl,
            listener = object : WebSocketBridgeClient.Listener {
                override fun onPairingRequired(code: String) {
                    updateNotification(getString(R.string.notification_title_pairing), code)
                    BridgeStatus.post(BridgeStatus.State.PairingRequired(code))
                }

                override fun onBridgeActive() {
                    becameActive = true
                    updateNotification(getString(R.string.notification_title_connected))
                    BridgeStatus.post(BridgeStatus.State.Connected)
                    scope.launch {
                        ensureAdbConnected()
                    }
                }

                private val pendingChunks = ArrayList<ByteArray>()
                @Volatile private var isConnectingAdb = false

                override fun onBinaryMessage(data: ByteArray) {
                    val relay = adbRelay ?: return
                    if (relay.isConnected()) {
                        relay.writeToLocal(data)
                        return
                    }
                    synchronized(pendingChunks) {
                        if (relay.isConnected()) {
                            relay.writeToLocal(data)
                            return
                        }
                        pendingChunks.add(data)
                        if (!isConnectingAdb) {
                            isConnectingAdb = true
                            killswitchOverlay.show()
                            scope.launch {
                                try {
                                    ensureAdbConnected()
                                    synchronized(pendingChunks) {
                                        for (chunk in pendingChunks) {
                                            relay.writeToLocal(chunk)
                                        }
                                        pendingChunks.clear()
                                    }
                                } finally {
                                    isConnectingAdb = false
                                }
                            }
                        }
                    }
                }

                override fun onDisconnected(code: Int, reason: String) {
                    BridgeStatus.post(BridgeStatus.State.Disconnected(reason))
                    if (!doneSignal.isCompleted) doneSignal.complete(Unit)
                }

                override fun onError(message: String) {
                    BridgeStatus.post(BridgeStatus.State.Error(message))
                }
            },
        )

        wsClient?.connect(deviceSecret, deviceName)
        doneSignal.await()
        adbRelay?.close()
        return becameActive
    }

    private suspend fun ensureAdbConnected(): Boolean {
        val relay = adbRelay ?: return false
        if (relay.isConnected()) return true
        val port = nsdDiscovery.discoverAdbPort() ?: return false
        return relay.connect(port)
    }

    private fun stopBridge() {
        wsClient?.disconnect()
        adbRelay?.close()
        killswitchOverlay.hide()
        BridgeStatus.post(BridgeStatus.State.Stopped)
    }

    override fun onDestroy() {
        stopBridge()
        scope.coroutineContext[Job]?.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID, getString(R.string.notification_channel_name), NotificationManager.IMPORTANCE_LOW,
            )
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }

    private fun buildNotification(title: String, contentText: String? = null): Notification {
        val stopIntent = Intent(this, BridgeForegroundService::class.java).apply { action = ACTION_STOP }
        val stopPendingIntent = PendingIntent.getService(
            this, 0, stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(contentText)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setOngoing(true)
            .addAction(0, getString(R.string.notification_action_stop), stopPendingIntent)
            .build()
    }

    private fun updateNotification(title: String, contentText: String? = null) {
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, buildNotification(title, contentText))
    }

    companion object {
        const val ACTION_STOP = "ai.messa.companion.action.STOP"
        private const val CHANNEL_ID = "messa_bridge_channel"
        private const val NOTIFICATION_ID = 1001
        private const val INITIAL_RETRY_DELAY_MS = 2_000L
        private const val MAX_RETRY_DELAY_MS = 60_000L
    }
}
