package com.goodol.tracker

import android.app.AppOpsManager
import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Process

data class AppSession(val pkg: String, val label: String, val start: Long, val end: Long)

data class UsageData(val sessions: List<AppSession>, val unlocks: List<Long>)

/**
 * Turns Android's app-usage history (the same data Digital Wellbeing uses) into
 * "app X was in front from .. to .." sessions plus unlock times.
 */
object Usage {
    // Event types as plain numbers so they work on every Android version we support.
    private const val RESUMED = 1          // ACTIVITY_RESUMED / MOVE_TO_FOREGROUND
    private const val PAUSED = 2           // ACTIVITY_PAUSED / MOVE_TO_BACKGROUND
    private const val SCREEN_OFF = 16      // SCREEN_NON_INTERACTIVE
    private const val UNLOCKED = 18        // KEYGUARD_HIDDEN
    private const val SHUTDOWN = 26        // DEVICE_SHUTDOWN

    private const val MERGE_GAP_MS = 3_000L  // same app again within 3 s = one session
    private const val MIN_MS = 1_000L

    fun hasAccess(context: Context): Boolean {
        val ops = context.getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
        val mode = if (Build.VERSION.SDK_INT >= 29)
            ops.unsafeCheckOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), context.packageName)
        else
            @Suppress("DEPRECATION")
            ops.checkOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), context.packageName)
        return mode == AppOpsManager.MODE_ALLOWED
    }

    /** Home screens, the status bar and this app itself aren't "using an app". */
    private fun ignored(context: Context): Set<String> {
        val pm = context.packageManager
        val home = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME)
        val launchers = pm.queryIntentActivities(home, PackageManager.MATCH_ALL).map { it.activityInfo.packageName }
        return launchers.toSet() + setOf(
            context.packageName, "android", "com.android.systemui", "com.miui.home",
            "com.mi.android.globallauncher", "com.android.permissioncontroller",
            // Screens that stay in front without real use: clock/alarm, Xiaomi App vault, share sheet.
            "com.android.deskclock", "com.google.android.deskclock", "com.mi.globalminusscreen",
            "com.android.intentresolver",
        )
    }

    private val labelCache = HashMap<String, String>()

    fun label(context: Context, pkg: String): String = labelCache.getOrPut(pkg) {
        try {
            val pm = context.packageManager
            pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
        } catch (e: Exception) {
            pkg
        }
    }

    fun collect(context: Context, fromMs: Long, toMs: Long): UsageData {
        val usm = context.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
        val events = usm.queryEvents(fromMs, toMs)
        val skip = ignored(context)
        val raw = ArrayList<AppSession>()
        val unlocks = ArrayList<Long>()
        var curPkg: String? = null
        var curStart = 0L

        fun close(ts: Long) {
            val pkg = curPkg ?: return
            if (ts - curStart >= MIN_MS) raw.add(AppSession(pkg, "", curStart, ts))
            curPkg = null
        }

        val e = UsageEvents.Event()
        while (events.hasNextEvent()) {
            events.getNextEvent(e)
            val ts = e.timeStamp
            when (e.eventType) {
                RESUMED -> {
                    val pkg = e.packageName ?: continue
                    if (pkg == curPkg) continue  // another screen of the same app
                    close(ts)
                    if (pkg !in skip) { curPkg = pkg; curStart = ts }
                }
                PAUSED -> if (e.packageName == curPkg) close(ts)
                SCREEN_OFF, SHUTDOWN -> close(ts)
                UNLOCKED -> unlocks.add(ts)
            }
        }
        // Still open now: sent as running until now; the PC extends it on the next sync.
        if (curPkg != null) close(toMs)

        val merged = ArrayList<AppSession>()
        for (s in raw.sortedBy { it.start }) {
            val last = merged.lastOrNull()
            if (last != null && last.pkg == s.pkg && s.start - last.end <= MERGE_GAP_MS) {
                merged[merged.size - 1] = last.copy(end = maxOf(last.end, s.end))
            } else merged.add(s)
        }
        return UsageData(merged.map { it.copy(label = label(context, it.pkg)) }, unlocks)
    }

    /** Today's totals for the app's own screen. */
    fun today(context: Context): Triple<Long, Int, List<Pair<String, Long>>> {
        val cal = java.util.Calendar.getInstance().apply {
            set(java.util.Calendar.HOUR_OF_DAY, 0); set(java.util.Calendar.MINUTE, 0)
            set(java.util.Calendar.SECOND, 0); set(java.util.Calendar.MILLISECOND, 0)
        }
        val data = collect(context, cal.timeInMillis, System.currentTimeMillis())
        val byApp = data.sessions.groupBy { it.label }.mapValues { (_, v) -> v.sumOf { it.end - it.start } }
        val top = byApp.entries.sortedByDescending { it.value }.take(5).map { it.key to it.value }
        return Triple(byApp.values.sum(), data.unlocks.size, top)
    }
}
