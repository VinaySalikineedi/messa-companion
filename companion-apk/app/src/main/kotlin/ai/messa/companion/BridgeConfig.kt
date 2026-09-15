package ai.messa.companion

import android.content.Context
import android.content.SharedPreferences

/**
 * The one thing this app needs configured that isn't per-device secret
 * state: which Messa server to dial. Defaults to the production
 * `wss://messa.ai/device/ws` endpoint, but is overridable (from
 * [MainActivity]'s advanced settings, hidden behind a long-press --
 * ordinary users never see this) for self-hosted/staging deployments,
 * matching this whole feature's "open-source, run-it-yourself" framing
 * (open-source-phone.md section 1).
 */
object BridgeConfig {
    private const val PREFS_NAME = "messa_companion_config"
    private const val KEY_SERVER_WS_URL = "server_ws_url"
    const val DEFAULT_WS_URL = "wss://messa.ai/device/ws"

    private fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun deviceWsUrl(context: Context): String =
        prefs(context).getString(KEY_SERVER_WS_URL, DEFAULT_WS_URL) ?: DEFAULT_WS_URL

    fun setDeviceWsUrl(context: Context, url: String) {
        prefs(context).edit().putString(KEY_SERVER_WS_URL, url).apply()
    }
}
