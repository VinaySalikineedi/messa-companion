package ai.messa.companion

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.launch

/**
 * The APK's entire UI surface (open-source-phone.md section 2/3): show
 * the current bridge status, the pairing code when one is pending, and a
 * start/stop toggle for [BridgeForegroundService]. Deliberately minimal
 * -- built as a single screen with no navigation, since the actual "using
 * Messa" experience happens over SMS/the live-view page, never inside
 * this app itself. Built programmatically (no XML layout) to keep this
 * whole scaffold in as few files as possible; a real production build can
 * freely replace this with a proper Compose/XML UI without touching any
 * of the bridge engine classes.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var deviceSecretManager: DeviceSecretManager
    private lateinit var statusText: TextView
    private lateinit var codeText: TextView
    private lateinit var toggleButton: Button
    private lateinit var a11yStatusText: TextView
    private lateinit var a11yButton: Button

    private val overlayPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
            // Result is ignored
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        deviceSecretManager = DeviceSecretManager(applicationContext)
        setContentView(buildUi())
        requestBatteryOptimizationExemptionIfNeeded()
        requestOverlayPermissionIfNeeded()
        observeBridgeStatus()
    }

    override fun onResume() {
        super.onResume()
        updateAccessibilityUi()
    }

    private fun updateAccessibilityUi() {
        val isA11y = MessaAccessibilityService.isAvailable()
        if (isA11y) {
            a11yStatusText.text = getString(R.string.accessibility_status_enabled)
            a11yStatusText.setTextColor(0xFF2E7D32.toInt()) // Green
            a11yButton.visibility = android.view.View.GONE
        } else {
            a11yStatusText.text = getString(R.string.accessibility_status_disabled)
            a11yStatusText.setTextColor(0xFFD32F2F.toInt()) // Red
            a11yButton.visibility = android.view.View.VISIBLE
        }
    }

    private fun buildUi(): LinearLayout {
        val padding = (24 * resources.displayMetrics.density).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(padding, padding * 2, padding, padding)
        }

        val title = TextView(this).apply {
            text = getString(R.string.app_name)
            textSize = 24f
        }
        statusText = TextView(this).apply {
            text = getString(R.string.status_idle)
            textSize = 16f
            setPadding(0, padding, 0, 0)
        }
        codeText = TextView(this).apply {
            text = ""
            textSize = 32f
            setPadding(0, padding / 2, 0, padding / 2)
        }
        toggleButton = Button(this).apply {
            text = "Connect Messa Bridge"
            setOnClickListener { onToggleBridge() }
        }

        a11yStatusText = TextView(this).apply {
            text = getString(R.string.accessibility_status_disabled)
            textSize = 14f
            setTextColor(0xFFD32F2F.toInt())
            setPadding(0, padding, 0, padding / 4)
        }
        a11yButton = Button(this).apply {
            text = getString(R.string.action_open_accessibility)
            textSize = 13f
            setOnClickListener {
                try {
                    startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
                } catch (e: Exception) {
                    try {
                        startActivity(Intent(Settings.ACTION_SETTINGS))
                    } catch (ignored: Exception) {}
                }
            }
        }

        val wirelessBanner = TextView(this).apply {
            text = "Optional Fallback: Settings > Developer options > Wireless debugging > ON"
            textSize = 11f
            setTextColor(0xFF888888.toInt())
            setPadding(0, padding / 2, 0, padding / 6)
        }
        val devSettingsButton = Button(this).apply {
            text = "Open Developer Options"
            textSize = 12f
            setOnClickListener {
                try {
                    startActivity(Intent(Settings.ACTION_APPLICATION_DEVELOPMENT_SETTINGS))
                } catch (e: Exception) {
                    try {
                        startActivity(Intent(Settings.ACTION_SETTINGS))
                    } catch (ignored: Exception) {}
                }
            }
        }

        root.addView(title)
        root.addView(statusText)
        root.addView(codeText)
        root.addView(toggleButton)
        root.addView(a11yStatusText)
        root.addView(a11yButton)
        root.addView(wirelessBanner)
        root.addView(devSettingsButton)
        return root
    }

    private var bridgeRunning = false

    private fun onToggleBridge() {
        val intent = Intent(this, BridgeForegroundService::class.java)
        if (bridgeRunning) {
            BridgeWatchdogReceiver.cancel(this)
            intent.action = BridgeForegroundService.ACTION_STOP
            startService(intent)
        } else {
            ContextCompat.startForegroundService(this, intent)
            BridgeWatchdogReceiver.schedule(this)
        }
    }

    private fun observeBridgeStatus() {
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                BridgeStatus.state.collect { state ->
                    when (state) {
                        is BridgeStatus.State.Idle -> {
                            bridgeRunning = false
                            statusText.text = getString(R.string.status_idle)
                            codeText.text = ""
                            toggleButton.text = "Connect Messa Bridge"
                        }
                        is BridgeStatus.State.PairingRequired -> {
                            bridgeRunning = true
                            statusText.text = getString(R.string.status_pairing)
                            codeText.text = "PAIR ${state.code}"
                            toggleButton.text = "Disconnect Messa Bridge"
                        }
                        is BridgeStatus.State.Connected -> {
                            bridgeRunning = true
                            statusText.text = getString(R.string.status_connected)
                            codeText.text = ""
                            toggleButton.text = "Disconnect Messa Bridge"
                        }
                        is BridgeStatus.State.Disconnected -> {
                            bridgeRunning = false
                            statusText.text = getString(R.string.status_idle)
                            codeText.text = ""
                            toggleButton.text = "Connect Messa Bridge"
                        }
                        is BridgeStatus.State.Error -> {
                            bridgeRunning = false
                            statusText.text = getString(R.string.status_error)
                            codeText.text = state.message
                            toggleButton.text = "Reconnect Messa Bridge"
                        }
                        is BridgeStatus.State.Stopped -> {
                            bridgeRunning = false
                            statusText.text = getString(R.string.status_idle)
                            codeText.text = ""
                            toggleButton.text = "Connect Messa Bridge"
                        }
                    }
                }
            }
        }
    }

    /**
     * Roadblock #3 (open-source-phone.md section 4): without this
     * exemption, Android's battery optimization/Doze will suspend the
     * foreground service's networking within minutes of the screen
     * turning off, silently dropping the bridge. Requested once, up
     * front -- the system dialog this launches is the standard OS one,
     * never a custom/misleading prompt.
     */
    private fun requestBatteryOptimizationExemptionIfNeeded() {
        val powerManager = getSystemService(POWER_SERVICE) as PowerManager
        if (!powerManager.isIgnoringBatteryOptimizations(packageName)) {
            runCatching {
                startActivity(
                    Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                        data = Uri.parse("package:$packageName")
                    },
                )
            }
        }
    }

    /** SYSTEM_ALERT_WINDOW for the touch killswitch overlay (section 3.4)
     * -- the one permission in this app that needs an explicit trip to
     * Settings on modern Android; everything else is a normal manifest
     * permission. */
    private fun requestOverlayPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !Settings.canDrawOverlays(this)) {
            val intent = Intent(
                Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName"),
            )
            overlayPermissionLauncher.launch(intent)
        }
    }
}
