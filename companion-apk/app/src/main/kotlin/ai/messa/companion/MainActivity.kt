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

    private val overlayPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
            // Result is ignored -- Settings.canDrawOverlays() is re-checked
            // lazily whenever the killswitch overlay actually tries to
            // show itself (see TouchKillswitchOverlay.show()'s own
            // failure handling), so there's nothing to react to here.
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        deviceSecretManager = DeviceSecretManager(applicationContext)
        setContentView(buildUi())
        requestBatteryOptimizationExemptionIfNeeded()
        requestOverlayPermissionIfNeeded()
        observeBridgeStatus()
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

        root.addView(title)
        root.addView(statusText)
        root.addView(codeText)
        root.addView(toggleButton)
        return root
    }

    private var bridgeRunning = false

    private fun onToggleBridge() {
        val intent = Intent(this, BridgeForegroundService::class.java)
        if (bridgeRunning) {
            intent.action = BridgeForegroundService.ACTION_STOP
            startService(intent)
        } else {
            ContextCompat.startForegroundService(this, intent)
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
                            statusText.text = getString(R.string.status_idle)
                            codeText.text = ""
                            toggleButton.text = "Connect Messa Bridge"
                        }
                        is BridgeStatus.State.Error -> {
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
