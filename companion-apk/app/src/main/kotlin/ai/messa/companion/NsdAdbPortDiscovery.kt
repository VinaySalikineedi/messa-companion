package ai.messa.companion

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Roadblock #1 (open-source-phone.md section 4): Android's wireless-
 * debugging local ADB port changes every time Wi-Fi reconnects, so a
 * hardcoded/user-typed port goes stale constantly. Rather than asking the
 * user to hunt for it in Developer Options, this uses Android's own
 * [NsdManager] to resolve the `_adb-tls-connect._tcp` mDNS service the
 * OS's own wireless-debugging daemon already advertises on the loopback
 * interface -- the exact same mechanism Android Studio's "auto-connect"
 * relies on internally.
 *
 * This is the ONLY thing on the phone side that ever needs to know the
 * local ADB port; once discovered, [AdbLocalSocketRelay] just opens a
 * plain TCP socket to `127.0.0.1:<port>` like any other ADB client would.
 */
class NsdAdbPortDiscovery(context: Context) {

    private val nsdManager = context.applicationContext
        .getSystemService(Context.NSD_SERVICE) as NsdManager

    /**
     * Resolves the current local wireless-debugging connect port, or null
     * if nothing answers within [timeoutMs] (e.g. wireless debugging is
     * off, or this Android version/OEM doesn't advertise the service --
     * callers fall back to asking the user to enable Developer Options >
     * Wireless debugging and try again).
     */
    suspend fun discoverAdbPort(timeoutMs: Long = 8_000L): Int? {
        val result = CompletableDeferred<Int?>()

        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {
                Log.i(TAG, "mDNS discovery started for $serviceType")
            }

            override fun onServiceFound(service: NsdServiceInfo) {
                if (!service.serviceType.contains(SERVICE_TYPE_BARE)) return
                resolveService(service, result)
            }

            override fun onServiceLost(service: NsdServiceInfo) {
                // Not actionable here -- AdbLocalSocketRelay's own connect
                // attempt is what actually surfaces a now-stale port, via
                // its usual translated connection-error path.
            }

            override fun onDiscoveryStopped(serviceType: String) {}

            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                Log.w(TAG, "mDNS discovery failed to start (code $errorCode)")
                result.complete(null)
            }

            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
        }

        return try {
            nsdManager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
            withTimeoutOrNull(timeoutMs) { result.await() }.also {
                runCatching { nsdManager.stopServiceDiscovery(listener) }
            }
        } catch (e: Exception) {
            Log.w(TAG, "mDNS discovery threw: $e")
            null
        }
    }

    private fun resolveService(service: NsdServiceInfo, result: CompletableDeferred<Int?>) {
        val resolveListener = object : NsdManager.ResolveListener {
            override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                Log.w(TAG, "mDNS resolve failed for ${serviceInfo.serviceName} (code $errorCode)")
            }

            override fun onServiceResolved(serviceInfo: NsdServiceInfo) {
                // The wireless-debugging daemon only ever advertises this
                // on the loopback interface -- the port is what matters,
                // the host is always 127.0.0.1 from this same device.
                if (!result.isCompleted) {
                    result.complete(serviceInfo.port)
                }
            }
        }
        runCatching { nsdManager.resolveService(service, resolveListener) }
    }

    companion object {
        private const val TAG = "NsdAdbPortDiscovery"
        private const val SERVICE_TYPE_BARE = "_adb-tls-connect"
        private const val SERVICE_TYPE = "_adb-tls-connect._tcp."
    }
}
