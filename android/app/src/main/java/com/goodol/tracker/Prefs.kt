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

    // ---- focus mode (state comes from the PC on every sync; "local" = started while the PC
    // was unreachable, only blocks on the phone)
    var focusId: Long
        get() = p.getLong("focus_id", 0L)
        set(v) = p.edit().putLong("focus_id", v).apply()
    var focusStartSec: Double
        get() = java.lang.Double.longBitsToDouble(p.getLong("focus_start", 0L))
        set(v) = p.edit().putLong("focus_start", java.lang.Double.doubleToRawLongBits(v)).apply()
    var focusEndSec: Double
        get() = java.lang.Double.longBitsToDouble(p.getLong("focus_end", 0L))
        set(v) = p.edit().putLong("focus_end", java.lang.Double.doubleToRawLongBits(v)).apply()
    var focusLabel: String
        get() = p.getString("focus_label", "") ?: ""
        set(v) = p.edit().putString("focus_label", v).apply()
    var focusLocal: Boolean
        get() = p.getBoolean("focus_local", false)
        set(v) = p.edit().putBoolean("focus_local", v).apply()

    val focusActive get() = focusEndSec > System.currentTimeMillis() / 1000.0

    fun setFocus(id: Long, start: Double, end: Double, label: String, local: Boolean) {
        p.edit().putLong("focus_id", id)
            .putLong("focus_start", java.lang.Double.doubleToRawLongBits(start))
            .putLong("focus_end", java.lang.Double.doubleToRawLongBits(end))
            .putString("focus_label", label).putBoolean("focus_local", local).apply()
    }

    fun clearFocus() = setFocus(0L, 0.0, 0.0, "", false)

    /** Packages to block during focus (from the PC; defaults until the first sync). */
    var blocked: Set<String>
        get() = p.getStringSet("blocked", null) ?: DEFAULT_BLOCKED
        set(v) = p.edit().putStringSet("blocked", v).apply()

    /** Blocked-app attempts not yet reported to the PC, as a JSON array string. */
    var pendingBlocks: String
        get() = p.getString("pending_blocks", "[]") ?: "[]"
        set(v) = p.edit().putString("pending_blocks", v).apply()

    /** Limits, bedtime and schedules from the PC (JSON), kept so they work offline. */
    var wellbeing: String
        get() = p.getString("wellbeing", null) ?: DEFAULT_WELLBEING
        set(v) = p.edit().putString("wellbeing", v).apply()

    /** Today's PC numbers + goals from the last sync (JSON), for the screen and widget. */
    var today: String
        get() = p.getString("today", "{}") ?: "{}"
        set(v) = p.edit().putString("today", v).apply()
    var todayAtMs: Long
        get() = p.getLong("today_at", 0L)
        set(v) = p.edit().putLong("today_at", v).apply()

    /** One-off things already done, e.g. "warn:com.facebook.katana:2026-11-08" (kept 3 days). */
    fun once(key: String): Boolean {
        val set = p.getStringSet("once", emptySet())!!.toMutableSet()
        if (key in set) return false
        val today = java.time.LocalDate.now()
        set.removeAll { k -> k.substringAfterLast('|').let { d -> runCatching { java.time.LocalDate.parse(d) }.getOrNull()?.isBefore(today.minusDays(3)) ?: true } }
        set.add(key)
        p.edit().putStringSet("once", set).apply()
        return true
    }

    fun graceUntil(key: String): Long = p.getLong("grace:$key", 0L)
    fun setGrace(key: String, until: Long) = p.edit().putLong("grace:$key", until).apply()

    /** {"date": "...", "used": {package: seconds}}: time on the app's website on the PC today. */
    var pcUsed: String
        get() = p.getString("pc_used", "{}") ?: "{}"
        set(v) = p.edit().putString("pc_used", v).apply()

    /** The Week tab's data from the PC (JSON), kept for when the PC isn't reachable. */
    var week: String
        get() = p.getString("week", "") ?: ""
        set(v) = p.edit().putString("week", v).apply()

    var dnsWasOn: Boolean
        get() = p.getBoolean("dns_was_on", false)
        set(v) = p.edit().putBoolean("dns_was_on", v).apply()

    var lastNudgeId: Long
        get() = p.getLong("last_nudge", 0L)
        set(v) = p.edit().putLong("last_nudge", v).apply()

    companion object {
        const val DEFAULT_WELLBEING = """{"limits":{"com.facebook.katana":60,"com.instagram.android":30},
            "bedtime":{"enabled":true,"start":"23:30","end":"06:00","wind_down":30},"schedules":[]}"""
        val DEFAULT_BLOCKED = setOf(
            "com.facebook.katana", "com.facebook.lite", "com.instagram.android", "com.zhiliaoapp.musically",
            "com.ss.android.ugc.trill", "com.twitter.android", "com.snapchat.android", "com.reddit.frontpage",
            "com.google.android.youtube", "com.netflix.mediaclient",
        )
    }

    fun forget() {
        p.edit().remove("token").remove("pc_id").remove("pc_name").remove("last_host")
            .remove("cursor_ms").remove("last_sync_ms").remove("last_error").apply()
    }

    private fun randomId(): String {
        val bytes = ByteArray(12).also { SecureRandom().nextBytes(it) }
        return "android-" + bytes.joinToString("") { "%02x".format(it) }
    }
}
