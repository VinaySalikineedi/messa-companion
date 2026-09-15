package ai.messa.companion

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import java.security.SecureRandom

/**
 * Owns the one credential this whole bridge depends on: a 256-bit random
 * "device secret" generated once on first launch, stored ONLY in Android
 * Keystore-backed [EncryptedSharedPreferences] (open-source-phone.md
 * section 3.2), and presented on every `/device/ws` connection as
 * `Authorization: Bearer <secret>`.
 *
 * Deliberately the smallest possible surface: this class never sends the
 * secret anywhere itself (that's [WebSocketBridgeClient]'s job) and never
 * logs it (see every call site in this app -- none of them Log.d the
 * return value of [getOrCreateSecret]). The server never learns the raw
 * secret either, past the single TLS-protected Authorization header on
 * each connect -- it stores only a sha256 hash (messa/companion_bridge.py's
 * `hash_device_secret`, migration 044's `device_secret_hash` column) --
 * so a compromised database read alone can never be replayed as a working
 * credential.
 */
class DeviceSecretManager(context: Context) {

    private val prefs = run {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            PREFS_FILE_NAME,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    /**
     * Returns this device's secret, generating and persisting a fresh
     * 256-bit random one on first call. Every subsequent call (including
     * across app restarts/reboots) returns the SAME value -- rotating it
     * would silently unpair the phone from the server's point of view
     * (its `device_secret_hash` would no longer match anything), so
     * rotation is a deliberate user action ([regenerateSecret]), never
     * implicit.
     */
    fun getOrCreateSecret(): String {
        val existing = prefs.getString(KEY_DEVICE_SECRET, null)
        if (existing != null) return existing
        val fresh = generateRandomSecretHex()
        prefs.edit().putString(KEY_DEVICE_SECRET, fresh).apply()
        return fresh
    }

    /**
     * Explicit, user-initiated secret rotation (e.g. "this phone was
     * reset / I no longer trust the old secret"). The old secret's hash
     * on the server becomes permanently unmatchable, and the app must
     * re-pair (a fresh 6-digit code, a fresh SMS) with the new one --
     * exactly like reinstalling the app.
     */
    fun regenerateSecret(): String {
        val fresh = generateRandomSecretHex()
        prefs.edit().putString(KEY_DEVICE_SECRET, fresh).apply()
        return fresh
    }

    fun hasSecret(): Boolean = prefs.contains(KEY_DEVICE_SECRET)

    /** device_name is purely cosmetic (shown on the server's device list
     * and in SMS confirmations) -- defaults to the device's own model
     * name, editable later without affecting the secret/pairing at all. */
    fun getOrCreateDeviceName(defaultName: String): String {
        val existing = prefs.getString(KEY_DEVICE_NAME, null)
        if (existing != null) return existing
        prefs.edit().putString(KEY_DEVICE_NAME, defaultName).apply()
        return defaultName
    }

    fun setDeviceName(name: String) {
        prefs.edit().putString(KEY_DEVICE_NAME, name).apply()
    }

    private fun generateRandomSecretHex(): String {
        val bytes = ByteArray(32) // 256 bits
        SecureRandom().nextBytes(bytes)
        return bytes.joinToString(separator = "") { "%02x".format(it) }
    }

    companion object {
        private const val PREFS_FILE_NAME = "messa_companion_secure_prefs"
        private const val KEY_DEVICE_SECRET = "device_secret_hex"
        private const val KEY_DEVICE_NAME = "device_name"
    }
}
