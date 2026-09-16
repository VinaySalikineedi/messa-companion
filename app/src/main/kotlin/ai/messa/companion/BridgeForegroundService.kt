package ai.messa.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * The persistent foreground service that IS the bridge, end to end
 * (open-source-phone.md section 2/3/4). Holds one [WebSocketBridgeClient]
 * (the outbound `/device/ws` connection) and one [AdbLocalSocketRelay]
 * (the local connection to this phone's own ADB daemon), and wires bytes
 * straight between them -- neither side is ever parsed, this class is
 * pure plumbing plus lifecycle/retry management.
 *
 * `START_STICKY` + a persistent foreground notification + `PowerManager.PARTIAL_WAKE_LOCK`
 * and `WifiManager.WifiLock` prevents Android Doze from suspending the process's networking
 * when the screen turns off, maintaining 24/7 responsiveness to incoming SMS commands.
 */
class BridgeForegroundService : Service() {

    private val scope = CoroutineScope(Dispatchers.IO + Job())
    private var wsClient: WebSocketBridgeClient? = null
    private var adbRelay: AdbLocalSocketRelay? = null
    private lateinit var deviceSecretManager: DeviceSecretManager
    private lateinit var nsdDiscovery: NsdAdbPortDiscovery
    private lateinit var killswitchOverlay: TouchKillswitchOverlay

    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    private val reconnectChannel = Channel<Unit>(Channel.CONFLATED)

    private var retryDelayMs = INITIAL_RETRY_DELAY_MS

    private val screenReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            val action = intent?.action
            if (action == Intent.ACTION_SCREEN_ON || action == Intent.ACTION_USER_PRESENT) {
                Log.d(TAG, "Screen active event ($action) -> triggering immediate bridge reconnect")
                triggerImmediateReconnect()
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        deviceSecretManager = DeviceSecretManager(applicationContext)
        nsdDiscovery = NsdAdbPortDiscovery(applicationContext)
        killswitchOverlay = TouchKillswitchOverlay(applicationContext) {
            wsClient?.sendTouchAbort()
            killswitchOverlay.hide()
        }
        createNotificationChannel()

        // Acquire WakeLock to keep CPU executing during sleep
        try {
            val powerManager = getSystemService(Context.POWER_SERVICE) as? PowerManager
            wakeLock = powerManager?.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MessaBridge:WakeLock")?.apply {
                setReferenceCounted(false)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Could not create WakeLock: ${e.message}")
        }

        // Acquire WifiLock to keep Wi-Fi radio alive during screen-off
        try {
            val wifiManager = applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
            wifiLock = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                wifiManager?.createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "MessaBridge:WifiLock")
            } else {
                @Suppress("DEPRECATION")
                wifiManager?.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "MessaBridge:WifiLock")
            }?.apply {
                setReferenceCounted(false)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Could not create WifiLock: ${e.message}")
        }

        // Register screen-on receiver
        try {
            val filter = IntentFilter().apply {
                addAction(Intent.ACTION_SCREEN_ON)
                addAction(Intent.ACTION_USER_PRESENT)
            }
            registerReceiver(screenReceiver, filter)
        } catch (e: Exception) {
            Log.w(TAG, "Could not register screenReceiver: ${e.message}")
        }

        // Register network callback for immediate reconnect when network restores
        try {
            val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            if (cm != null) {
                val callback = object : ConnectivityManager.NetworkCallback() {
                    override fun onAvailable(network: Network) {
                        Log.d(TAG, "Network became available -> triggering immediate reconnect")
                        triggerImmediateReconnect()
                    }
                }
                networkCallback = callback
                cm.registerDefaultNetworkCallback(callback)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Could not register network callback: ${e.message}")
        }
    }

    fun triggerImmediateReconnect() {
        retryDelayMs = INITIAL_RETRY_DELAY_MS
        reconnectChannel.trySend(Unit)
    }

    private var connectJob: Job? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val notification = buildNotification(getString(R.string.notification_title_connecting))
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val serviceType = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE or ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            } else {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
            }
            startForeground(NOTIFICATION_ID, notification, serviceType)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        if (intent?.action == ACTION_STOP) {
            BridgeWatchdogReceiver.cancel(applicationContext)
            stopBridge()
            stopSelf()
            return START_NOT_STICKY
        }
        startBridge()
        BridgeWatchdogReceiver.schedule(applicationContext)
        return START_STICKY
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        super.onTaskRemoved(rootIntent)
        Log.d(TAG, "onTaskRemoved called -> scheduling immediate watchdog revival")
        BridgeWatchdogReceiver.schedule(applicationContext, 1000L)
    }

    private fun startBridge() {
        try {
            if (wakeLock?.isHeld != true) wakeLock?.acquire()
            if (wifiLock?.isHeld != true) wifiLock?.acquire()
        } catch (e: Exception) {
            Log.w(TAG, "Could not acquire WakeLock/WifiLock: ${e.message}")
        }
        triggerImmediateReconnect()
        if (connectJob == null || connectJob?.isActive != true) {
            connectJob = scope.launch {
                connectLoop()
            }
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
            // Wait for either the retry delay or an immediate trigger (screen turned on, network available)
            withTimeoutOrNull(retryDelayMs) {
                reconnectChannel.receive()
            }
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
                    wakeScreenBriefly()
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

    private fun wakeScreenBriefly() {
        try {
            val powerManager = getSystemService(Context.POWER_SERVICE) as? PowerManager
            @Suppress("DEPRECATION")
            val screenLock = powerManager?.newWakeLock(
                PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP or PowerManager.ON_AFTER_RELEASE,
                "MessaBridge:CommandWake"
            )
            screenLock?.acquire(3000L)
        } catch (e: Exception) {
            Log.w(TAG, "Could not acquire screen wake lock: ${e.message}")
        }
    }

    private suspend fun ensureAdbConnected(): Boolean {
        val relay = adbRelay ?: return false
        if (relay.isConnected()) return true
        val port = nsdDiscovery.discoverAdbPort() ?: return false
        return relay.connect(port)
    }

    private fun stopBridge() {
        connectJob?.cancel()
        connectJob = null
        try {
            if (wakeLock?.isHeld == true) wakeLock?.release()
            if (wifiLock?.isHeld == true) wifiLock?.release()
        } catch (e: Exception) {
            Log.w(TAG, "Error releasing locks: ${e.message}")
        }
        wsClient?.disconnect()
        adbRelay?.close()
        killswitchOverlay.hide()
        BridgeStatus.post(BridgeStatus.State.Stopped)
    }

    override fun onDestroy() {
        stopBridge()
        try {
            unregisterReceiver(screenReceiver)
        } catch (_: Exception) {}
        try {
            val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            networkCallback?.let { cm?.unregisterNetworkCallback(it) }
        } catch (_: Exception) {}
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
        private const val TAG = "BridgeFGS"
        const val ACTION_STOP = "ai.messa.companion.action.STOP"
        const val ACTION_KEEP_ALIVE = "ai.messa.companion.action.KEEP_ALIVE"
        private const val CHANNEL_ID = "messa_bridge_channel"
        private const val NOTIFICATION_ID = 1001
        private const val INITIAL_RETRY_DELAY_MS = 2_000L
        private const val MAX_RETRY_DELAY_MS = 60_000L
    }
}
