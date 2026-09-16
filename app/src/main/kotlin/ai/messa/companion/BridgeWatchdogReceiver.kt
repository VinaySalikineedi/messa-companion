package ai.messa.companion

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * Keepalive watchdog that periodically wakes up via AlarmManager to ensure
 * BridgeForegroundService is active and healthy even during deep Doze or
 * when killed by OEM aggressive power managers (e.g. Motorola BatteryCare).
 */
class BridgeWatchdogReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent?) {
        Log.d(TAG, "Watchdog alarm received: verifying bridge service is running")

        // Only keep alive if bridge is meant to be running
        if (BridgeStatus.state.value !is BridgeStatus.State.Stopped) {
            val serviceIntent = Intent(context, BridgeForegroundService::class.java).apply {
                action = BridgeForegroundService.ACTION_KEEP_ALIVE
            }
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    ContextCompat.startForegroundService(context, serviceIntent)
                } else {
                    context.startService(serviceIntent)
                }
            } catch (e: Exception) {
                Log.e(TAG, "Failed to start BridgeForegroundService from watchdog: ${e.message}")
            }
        }

        // Schedule next check
        schedule(context)
    }

    companion object {
        private const val TAG = "BridgeWatchdog"
        private const val WATCHDOG_INTERVAL_MS = 5 * 60 * 1000L // 5 minutes
        private const val REQUEST_CODE = 9921

        fun schedule(context: Context, delayMs: Long = WATCHDOG_INTERVAL_MS) {
            try {
                val alarmManager = context.getSystemService(Context.ALARM_SERVICE) as? AlarmManager ?: return
                val intent = Intent(context, BridgeWatchdogReceiver::class.java)
                val pendingIntent = PendingIntent.getBroadcast(
                    context,
                    REQUEST_CODE,
                    intent,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
                )

                val triggerAtMillis = SystemClock.elapsedRealtime() + delayMs
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                    alarmManager.setExactAndAllowWhileIdle(
                        AlarmManager.ELAPSED_REALTIME_WAKEUP,
                        triggerAtMillis,
                        pendingIntent
                    )
                } else {
                    alarmManager.setExact(
                        AlarmManager.ELAPSED_REALTIME_WAKEUP,
                        triggerAtMillis,
                        pendingIntent
                    )
                }
                Log.d(TAG, "Scheduled watchdog alarm in ${delayMs / 1000}s")
            } catch (e: Exception) {
                Log.w(TAG, "Could not schedule watchdog alarm: ${e.message}")
            }
        }

        fun cancel(context: Context) {
            try {
                val alarmManager = context.getSystemService(Context.ALARM_SERVICE) as? AlarmManager ?: return
                val intent = Intent(context, BridgeWatchdogReceiver::class.java)
                val pendingIntent = PendingIntent.getBroadcast(
                    context,
                    REQUEST_CODE,
                    intent,
                    PendingIntent.FLAG_NO_CREATE or PendingIntent.FLAG_IMMUTABLE
                )
                if (pendingIntent != null) {
                    alarmManager.cancel(pendingIntent)
                    pendingIntent.cancel()
                    Log.d(TAG, "Cancelled watchdog alarm")
                }
            } catch (e: Exception) {
                Log.w(TAG, "Could not cancel watchdog alarm: ${e.message}")
            }
        }
    }
}
