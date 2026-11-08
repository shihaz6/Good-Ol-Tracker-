package com.goodol.tracker

import android.content.Context
import java.security.SecureRandom

/** Small persistent settings: pairing, where the PC was last seen, how far we've synced. */
class Prefs(context: Context) {
    private val p = context.getSharedPreferences("got", Context.MODE_PRIVATE)

    val deviceId: String
        get() = p.getString("device_id", null) ?: randomId().also { p.edit().putString("device_id", it).apply() }

    var token: String?
        get() = p.getString("token", null)
        set(v) = p.edit().putString("token", v).apply()

    var pcId: String?
        get() = p.getString("pc_id", null)
        set(v) = p.edit().putString("pc_id", v).apply()

    var pcName: String?
        get() = p.getString("pc_name", null)
        set(v) = p.edit().putString("pc_name", v).apply()

    var lastHost: String?
        get() = p.getString("last_host", null)
        set(v) = p.edit().putString("last_host", v).apply()

    var lastPort: Int
        get() = p.getInt("last_port", 47323)
        set(v) = p.edit().putInt("last_port", v).apply()

    /** Usage history before this (ms) is confirmed received by the PC. */
    var cursorMs: Long
        get() = p.getLong("cursor_ms", 0L)
        set(v) = p.edit().putLong("cursor_ms", v).apply()

    var lastSyncMs: Long
        get() = p.getLong("last_sync_ms", 0L)
        set(v) = p.edit().putLong("last_sync_ms", v).apply()

    var lastError: String?
        get() = p.getString("last_error", null)
        set(v) = p.edit().putString("last_error", v).apply()

    val paired get() = !token.isNullOrEmpty()

    fun forget() {
        p.edit().remove("token").remove("pc_id").remove("pc_name").remove("last_host")
            .remove("cursor_ms").remove("last_sync_ms").remove("last_error").apply()
    }

    private fun randomId(): String {
        val bytes = ByteArray(12).also { SecureRandom().nextBytes(it) }
        return "android-" + bytes.joinToString("") { "%02x".format(it) }
    }
}
