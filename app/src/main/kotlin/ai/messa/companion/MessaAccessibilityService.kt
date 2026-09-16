@file:Suppress("NewApi")
package ai.messa.companion

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.app.KeyguardManager
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.util.Base64
import android.util.Log
import android.view.Display
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import androidx.annotation.RequiresApi
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.ConcurrentHashMap

/**
 * Native Android Accessibility Service for Messa AI (open-source-phone.md).
 *
 * Provides persistent 24/7 automation with zero developer options, no Wi-Fi ports,
 * and no sleep disconnects:
 * - Native hierarchy dumping directly from memory (150ms round-trip).
 * - Hardware gestures (tap, double-tap, swipe, scroll) via dispatchGesture.
 * - Native text setting via ACTION_SET_TEXT.
 * - Global actions (Home, Back, Recents, Notifications).
 * - High-speed screenshot capture via takeScreenshot (API 30+).
 */
class MessaAccessibilityService : AccessibilityService() {

    private val mainHandler = Handler(Looper.getMainLooper())
    private val cachedNodeMap = ConcurrentHashMap<String, AccessibilityNodeInfo>()

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(TAG, "MessaAccessibilityService connected and ready")
        BridgeStatus.post(BridgeStatus.State.Connected)
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Events are observed in real time; the service queries rootInActiveWindow on demand
    }

    override fun onInterrupt() {
        Log.w(TAG, "MessaAccessibilityService interrupted")
    }

    override fun onDestroy() {
        if (instance == this) instance = null
        cachedNodeMap.clear()
        super.onDestroy()
    }

    /**
     * Traverses the active window and produces a pruned, structured JSON element tree
     * directly compatible with AndroidPhoneAgent's prompt schema.
     */
    fun dumpHierarchy(): JSONObject {
        cachedNodeMap.clear()
        val root = rootInActiveWindow
        val elementsArray = JSONArray()
        val currentPackage = root?.packageName?.toString() ?: ""

        if (root != null) {
            var counter = 1
            fun traverse(node: AccessibilityNodeInfo) {
                val bounds = Rect()
                node.getBoundsInScreen(bounds)

                val text = node.text?.toString()?.trim() ?: ""
                val desc = node.contentDescription?.toString()?.trim() ?: ""
                val className = node.className?.toString() ?: ""
                val resourceId = node.viewIdResourceName ?: ""
                val isClickable = node.isClickable
                val isEditable = node.isEditable
                val isScrollable = node.isScrollable

                // Include node if it is interactive, editable, or has informative text/description
                val isMeaningful = isClickable || isEditable || isScrollable || text.isNotEmpty() || desc.isNotEmpty()
                val isVisible = bounds.width() > 0 && bounds.height() > 0

                if (isMeaningful && isVisible) {
                    val elemId = "e$counter"
                    counter++
                    cachedNodeMap[elemId] = AccessibilityNodeInfo.obtain(node)

                    val elemObj = JSONObject().apply {
                        put("id", elemId)
                        put("class", className)
                        put("text", text)
                        put("content_desc", desc)
                        put("resource_id", resourceId)
                        put("clickable", isClickable)
                        put("editable", isEditable)
                        put("scrollable", isScrollable)
                        put("bounds", JSONArray(listOf(bounds.left, bounds.top, bounds.right, bounds.bottom)))
                        put("center", JSONArray(listOf(bounds.centerX(), bounds.centerY())))
                    }
                    elementsArray.put(elemObj)
                }

                for (i in 0 until node.childCount) {
                    val child = node.getChild(i) ?: continue
                    traverse(child)
                    child.recycle()
                }
            }

            traverse(root)
            root.recycle()
        }

        return JSONObject().apply {
            put("package", currentPackage)
            put("elements", elementsArray)
        }
    }

    /**
     * Dispatches a single tap at (x, y) coordinates via native GestureDescription.
     */
    suspend fun tap(x: Int, y: Int): Boolean {
        val deferred = CompletableDeferred<Boolean>()
        val path = Path().apply {
            moveTo(x.toFloat(), y.toFloat())
            lineTo(x.toFloat(), y.toFloat())
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, 50)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()

        mainHandler.post {
            dispatchGesture(gesture, object : GestureResultCallback() {
                override fun onCompleted(gestureDescription: GestureDescription?) {
                    deferred.complete(true)
                }

                override fun onCancelled(gestureDescription: GestureDescription?) {
                    Log.w(TAG, "Tap at ($x, $y) cancelled")
                    deferred.complete(false)
                }
            }, mainHandler)
        }

        return deferred.await()
    }

    /**
     * Dispatches a swipe from (x1, y1) to (x2, y2).
     */
    suspend fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Long): Boolean {
        val deferred = CompletableDeferred<Boolean>()
        val path = Path().apply {
            moveTo(x1.toFloat(), y1.toFloat())
            lineTo(x2.toFloat(), y2.toFloat())
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs.coerceAtLeast(100))
        val gesture = GestureDescription.Builder().addStroke(stroke).build()

        mainHandler.post {
            dispatchGesture(gesture, object : GestureResultCallback() {
                override fun onCompleted(gestureDescription: GestureDescription?) {
                    deferred.complete(true)
                }

                override fun onCancelled(gestureDescription: GestureDescription?) {
                    Log.w(TAG, "Swipe cancelled")
                    deferred.complete(false)
                }
            }, mainHandler)
        }

        return deferred.await()
    }

    /**
     * Sets text directly into an editable node or target element.
     */
    fun setText(targetId: String?, text: String): Boolean {
        var targetNode: AccessibilityNodeInfo? = null
        if (!targetId.isNullOrBlank()) {
            targetNode = cachedNodeMap[targetId]
        }

        if (targetNode == null) {
            val root = rootInActiveWindow
            targetNode = root?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
        }

        if (targetNode == null) {
            Log.w(TAG, "No editable target found for setText")
            return false
        }

        val arguments = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        val ok = targetNode.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)
        return ok
    }

    /**
     * Dispatches global system keys (Home, Back, Recents, Notifications).
     */
    fun pressKey(key: String): Boolean {
        return when (key.lowercase().trim()) {
            "home" -> performGlobalAction(GLOBAL_ACTION_HOME)
            "back" -> performGlobalAction(GLOBAL_ACTION_BACK)
            "recents" -> performGlobalAction(GLOBAL_ACTION_RECENTS)
            "notifications" -> performGlobalAction(GLOBAL_ACTION_NOTIFICATIONS)
            else -> false
        }
    }

    /**
     * Takes a screen capture on Android 11+ (API 30+) natively without user prompts.
     */
    @RequiresApi(Build.VERSION_CODES.R)
    suspend fun takeScreenshot(): ByteArray? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            Log.w(TAG, "takeScreenshot requires Android 11+ (API 30)")
            return null
        }

        val deferred = CompletableDeferred<ByteArray?>()
        takeScreenshot(
            Display.DEFAULT_DISPLAY,
            ContextCompat.getMainExecutor(applicationContext),
            object : TakeScreenshotCallback {
                override fun onSuccess(screenshotResult: ScreenshotResult) {
                    val hardwareBitmap = Bitmap.wrapHardwareBuffer(
                        screenshotResult.hardwareBuffer, screenshotResult.colorSpace
                    )
                    if (hardwareBitmap == null) {
                        screenshotResult.hardwareBuffer.close()
                        deferred.complete(null)
                        return
                    }

                    val softwareBitmap = hardwareBitmap.copy(Bitmap.Config.ARGB_8888, false)
                    screenshotResult.hardwareBuffer.close()
                    hardwareBitmap.recycle()

                    val outputStream = ByteArrayOutputStream()
                    softwareBitmap.compress(Bitmap.CompressFormat.JPEG, 75, outputStream)
                    softwareBitmap.recycle()
                    deferred.complete(outputStream.toByteArray())
                }

                override fun onFailure(errorCode: Int) {
                    Log.w(TAG, "takeScreenshot failed with error code $errorCode")
                    deferred.complete(null)
                }
            }
        )

        return deferred.await()
    }

    /**
     * Wakes the display and attempts non-secure keyguard dismissal.
     */
    fun wakeAndUnlock() {
        try {
            val powerManager = getSystemService(Context.POWER_SERVICE) as? PowerManager
            @Suppress("DEPRECATION")
            val wakeLock = powerManager?.newWakeLock(
                PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP or PowerManager.ON_AFTER_RELEASE,
                "MessaAccessibility:WakeLock"
            )
            wakeLock?.acquire(3000L)

            val km = getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                km?.requestDismissKeyguard(null, null)
            }
        } catch (e: Exception) {
            Log.w(TAG, "wakeAndUnlock error: ${e.message}")
        }
    }

    companion object {
        private const val TAG = "MessaAccessibility"

        @Volatile
        var instance: MessaAccessibilityService? = null
            private set

        fun isAvailable(): Boolean = instance != null
    }
}
