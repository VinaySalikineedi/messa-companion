package ai.messa.companion

import android.content.Context
import android.graphics.PixelFormat
import android.util.Log
import android.view.InputDevice
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager

/**
 * Physical touch killswitch (open-source-phone.md section 3.4): a fully
 * transparent, full-screen, touch-ONLY overlay ([FLAG_NOT_TOUCHABLE] is
 * deliberately never set) sitting above everything else while Messa is
 * mid-task. A genuine finger touch anywhere on it immediately fires
 * [onGenuineTouch], which [BridgeForegroundService]/[WebSocketBridgeClient]
 * wires to send `{"type": "touch_abort"}` up the WebSocket -- the server
 * side (messa/companion_bridge.py's touch_abort handling, wired to
 * server.py's `_press_home_and_abort_phone_task`) does the actual Home-key
 * press and task cancellation, so this overlay's own job is narrowly
 * "detect the touch and say so," nothing more.
 *
 * FLAGGED FOR HARDWARE VERIFICATION (same spirit as this codebase's other
 * FLAG_FOR_GO_LIVE_VERIFICATION comments, e.g. channels/vapi.py /
 * server.py's call-audio relay): distinguishing a genuine finger touch
 * from uiautomator2's own synthetic input injection is done here via
 * [MotionEvent.getDeviceId] -- events from Messa's own automation
 * typically report a non-positive/virtual device id (no real touchscreen
 * `InputDevice` behind them), while a real finger touch reports the
 * device id of the actual touchscreen hardware. This heuristic is
 * well-established but NOT formally guaranteed by the Android platform
 * across every OEM/Android version, so it should be verified against a
 * REAL device running uiautomator2 before this is trusted as the sole
 * safety mechanism -- see open-source-phone.md section 8's own "ship on
 * mocks, flag hardware verification" precedent for this whole feature.
 */
class TouchKillswitchOverlay(
    private val context: Context,
    private val onGenuineTouch: () -> Unit,
) {
    private val windowManager = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private var overlayView: View? = null

    fun show() {
        if (overlayView != null) return

        val view = object : View(context) {
            override fun onTouchEvent(event: MotionEvent): Boolean {
                if (event.action == MotionEvent.ACTION_DOWN && isGenuineTouch(event)) {
                    Log.i(TAG, "Genuine touch detected -- firing killswitch")
                    onGenuineTouch()
                }
                // Always consume and pass through visually (this view is
                // fully transparent) -- returning true just keeps this
                // overlay receiving the gesture stream; it never blocks
                // the user's OWN interaction with their phone, only
                // observes it.
                return true
            }
        }

        val layoutParams = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.MATCH_PARENT,
            overlayWindowType(),
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT,
        )

        runCatching { windowManager.addView(view, layoutParams) }
            .onFailure { e -> Log.w(TAG, "Failed to add killswitch overlay (permission missing?): $e") }
            .onSuccess { overlayView = view }
    }

    fun hide() {
        val view = overlayView ?: return
        runCatching { windowManager.removeView(view) }
        overlayView = null
    }

    private fun isGenuineTouch(event: MotionEvent): Boolean {
        val deviceId = event.deviceId
        if (deviceId <= 0) return false // synthetic/virtual input source -- see class doc
        val device = InputDevice.getDevice(deviceId) ?: return false
        return (device.sources and InputDevice.SOURCE_TOUCHSCREEN) == InputDevice.SOURCE_TOUCHSCREEN
    }

    private fun overlayWindowType(): Int =
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        } else {
            @Suppress("DEPRECATION")
            WindowManager.LayoutParams.TYPE_PHONE
        }

    companion object {
        private const val TAG = "TouchKillswitchOverlay"
    }
}
