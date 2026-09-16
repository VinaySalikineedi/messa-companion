package ai.messa.companion

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.net.InetSocketAddress
import java.net.Socket

/**
 * Roadblock #1 (open-source-phone.md section 4): Resolves the local ADB
 * daemon port. Fast-probes standard ADB port 5555 (and common ports 5556-5585)
 * on loopback first (taking ~2ms), then falls back to Android's [NsdManager]
 * mDNS service resolution if neither responds.
 */
class NsdAdbPortDiscovery(context: Context) {

    private val nsdManager = context.applicationContext
        .getSystemService(Context.NSD_SERVICE) as NsdManager

    /**
     * Resolves the current local wireless-debugging connect port, or null
     * if nothing answers within [timeoutMs].
     */
    suspend fun discoverAdbPort(timeoutMs: Long = 6_000L): Int? = withContext(Dispatchers.IO) {
        // 1. Fast probe standard ADB daemon port 5555 (takes ~2ms on loopback)
        if (isPortListening(5555, 100)) {
            Log.i(TAG, "Standard ADB port 5555 is open on loopback")
            return@withContext 5555
        }

        // 2. Fast probe common alternate loopback ADB ports
        for (candidate in 5556..5585) {
            if (isPortListening(candidate, 50)) {
                Log.i(TAG, "Found listening ADB loopback port: $candidate")
                return@withContext candidate
            }
        }

        // 3. Fall back to mDNS discovery
        discoverViaMdns(timeoutMs)
    }

    private fun isPortListening(port: Int, timeoutMs: Int): Boolean {
        return try {
            Socket().use { s ->
                s.connect(InetSocketAddress("127.0.0.1", port), timeoutMs)
                true
            }
        } catch (_: Exception) {
            false
        }
    }

    private suspend fun discoverViaMdns(timeoutMs: Long): Int? {
        val result = CompletableDeferred<Int?>()

        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {
                Log.i(TAG, "mDNS discovery started for $serviceType")
            }

            override fun onServiceFound(service: NsdServiceInfo) {
                if (!service.serviceType.contains(SERVICE_TYPE_BARE)) return
                resolveService(service, result)
            }

            override fun onServiceLost(service: NsdServiceInfo) {}

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
