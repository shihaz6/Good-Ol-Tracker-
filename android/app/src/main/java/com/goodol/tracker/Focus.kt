package com.goodol.tracker

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Settings
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

/** Focus state + distraction alerts on the phone. The PC is the source of truth. */
object Focus {
    private const val CHANNEL = "alerts"
    const val ACTION_START = "com.goodol.tracker.START_FOCUS"
    const val ACTION_SNOOZE = "com.goodol.tracker.SNOOZE"

    /** Apply what the PC said in a sync / focus reply. */
    fun applyReply(context: Context, res: JSONObject) {
        val prefs = Prefs(context)
        if (res.has("focus")) {
            val f = res.optJSONObject("focus")
            if (f != null) {
                if (prefs.focusLocal && prefs.focusActive) endLocal(prefs)  // the PC's session takes over
                prefs.setFocus(f.optLong("id"), f.optDouble("start"), f.optDouble("end"), f.optString("label"), false)
            } else if (!(prefs.focusLocal && prefs.focusActive)) {
                prefs.clearFocus()  // the PC ended it (or it ran out); a phone-only session keeps going
            }
        }
        res.optJSONArray("phone_block")?.let { arr ->
            prefs.blocked = (0 until arr.length()).map { arr.getString(it) }.toSet()
        }
        res.optJSONObject("nudge")?.let { showNudge(context, it) }
        res.optJSONObject("wellbeing")?.let { prefs.wellbeing = it.toString() }
        res.optJSONObject("limit_pc_used")?.let { prefs.pcUsed = it.toString() }
        res.optJSONObject("today")?.let { prefs.today = it.toString(); prefs.todayAtMs = System.currentTimeMillis() }
        TodayWidget.refresh(context)
    }

    /** A plain notification (limit warnings, wind-down, filter off). */
    fun notify(context: Context, id: Int, title: String, text: String) {
        if (!canNotify(context)) return
        channel(context)
        val open = PendingIntent.getActivity(context, 4, Intent(context, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val note = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        try { NotificationManagerCompat.from(context).notify(id, note) } catch (e: SecurityException) { }
    }

    /** Start (or end) focus: on the PC when reachable, else just on the phone. */
    fun start(context: Context, minutes: Int, label: String = ""): String {
        val prefs = Prefs(context)
        return try {
            if (prefs.stoppedFocusId != 0L) {
                // An old session you ended here offline is still running on the PC: the PC
                // would just hand that one back. End it there first, then start the new one.
                Net.focusAction(context, prefs, JSONObject().put("action", "stop"))
                prefs.stoppedFocusId = 0L
            }
            val res = Net.focusAction(context, prefs, JSONObject().put("action", "start").put("minutes", minutes).put("label", label))
            applyReply(context, res)
            "Focus started for $minutes min."
        } catch (e: Exception) {
            val now = System.currentTimeMillis() / 1000.0
            if (prefs.focusLocal && prefs.focusActive) endLocal(prefs)
            prefs.setFocus(-1L, now, now + minutes * 60, label, true)
            // Remembered so the PC gets it later: it counts in your focus stats, and the apps
            // blocked during it have a session to belong to (they used to be dropped).
            val arr = localSessions(prefs)
            if (arr.length() < 50) {
                arr.put(JSONObject().put("id", localId(prefs)).put("start", (now * 1000).toLong())
                    .put("planned_end", ((now + minutes * 60) * 1000).toLong()).put("label", label))
                prefs.pendingLocalFocus = arr.toString()
            }
            "PC not reachable: blocking apps on the phone only for $minutes min."
        }
    }

    fun stop(context: Context): String {
        val prefs = Prefs(context)
        if (prefs.focusLocal) { endLocal(prefs); prefs.clearFocus(); return "Focus ended." }
        return try {
            applyReply(context, Net.focusAction(context, prefs, JSONObject().put("action", "stop")))
            prefs.clearFocus()
            prefs.stoppedFocusId = 0L
            "Focus ended."
        } catch (e: Exception) {
            // Never lock you out: end it here, and remember it so the next sync ends it on the
            // PC too (it used to switch focus straight back on).
            if (prefs.focusId > 0L) prefs.stoppedFocusId = prefs.focusId
            prefs.clearFocus()
            "Ended on the phone (PC not reachable)."
        }
    }

    // ------------------------------------------------------------ phone-only sessions

    private fun localId(prefs: Prefs) = "local-" + (prefs.focusStartSec * 1000).toLong()

    private fun localSessions(prefs: Prefs) =
        runCatching { JSONArray(prefs.pendingLocalFocus) }.getOrDefault(JSONArray())

    /** The running phone-only session ends now (stopped early, or the PC's took over). */
    @Synchronized
    private fun endLocal(prefs: Prefs) {
        val arr = localSessions(prefs)
        val id = localId(prefs)
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            if (o.optString("id") == id && !o.has("end")) o.put("end", System.currentTimeMillis())
        }
        prefs.pendingLocalFocus = arr.toString()
    }

    /** Phone-only sessions that are over, ready to send to the PC. */
    fun finishedLocal(prefs: Prefs, nowMs: Long): JSONArray {
        val out = JSONArray()
        val arr = localSessions(prefs)
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            if (o.optLong("end", o.optLong("planned_end")) <= nowMs) out.put(o)
        }
        return out
    }

    /** Forget sessions / blocks the PC has confirmed receiving (by id). */
    @Synchronized
    fun sent(prefs: Prefs, sessionIds: Set<String>, blockIds: Set<String>) {
        fun drop(json: String, ids: Set<String>): String {
            val arr = runCatching { JSONArray(json) }.getOrDefault(JSONArray())
            val keep = JSONArray()
            for (i in 0 until arr.length()) arr.optJSONObject(i)?.let { if (it.optString("id") !in ids) keep.put(it) }
            return keep.toString()
        }
        prefs.pendingLocalFocus = drop(prefs.pendingLocalFocus, sessionIds)
        prefs.pendingBlocks = drop(prefs.pendingBlocks, blockIds)
    }

    @Synchronized
    fun recordBlock(context: Context, pkg: String) {
        val prefs = Prefs(context)
        val arr = runCatching { JSONArray(prefs.pendingBlocks) }.getOrDefault(JSONArray())
        if (arr.length() >= 200) return
        val now = System.currentTimeMillis()
        arr.put(JSONObject().put("id", "$pkg@$now").put("ts", now).put("app", Usage.label(context, pkg)))
        prefs.pendingBlocks = arr.toString()
    }

    // ------------------------------------------------------------ notifications

    private fun channel(context: Context) {
        if (Build.VERSION.SDK_INT >= 26) {
            val nm = context.getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Distraction alerts", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "\"25 min on Instagram\" nudges with a Start focus button"
            })
        }
    }

    fun canNotify(context: Context) = Build.VERSION.SDK_INT < 33 ||
        ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    private fun showNudge(context: Context, n: JSONObject) {
        val prefs = Prefs(context)
        val id = n.optLong("id")
        if (id != 0L && id == prefs.lastNudgeId) return
        prefs.lastNudgeId = id
        if (!canNotify(context)) return
        channel(context)
        fun action(a: String, code: Int) = PendingIntent.getBroadcast(
            context, code, Intent(context, NudgeReceiver::class.java).setAction(a),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val open = PendingIntent.getActivity(context, 3, Intent(context, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val mins = n.optInt("focus_minutes", 25)
        val note = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle(n.optString("title", "Time for a break?"))
            .setContentText(n.optString("message"))
            .setStyle(NotificationCompat.BigTextStyle().bigText(n.optString("message")))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(open)
            .addAction(0, "Start $mins-min focus", action(ACTION_START, 1))
            .addAction(0, "Snooze ${n.optInt("snooze_minutes", 30)} min", action(ACTION_SNOOZE, 2))
            .build()
        try { NotificationManagerCompat.from(context).notify(NUDGE_ID, note) } catch (e: SecurityException) { }
    }

    const val NUDGE_ID = 42

    fun blockingEnabled(context: Context): Boolean {
        val enabled = Settings.Secure.getString(context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
        return enabled.split(':').any { it.startsWith(context.packageName + "/") }
    }
}

/** The alert's buttons. */
class NudgeReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        NotificationManagerCompat.from(context).cancel(Focus.NUDGE_ID)
        val pending = goAsync()
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val prefs = Prefs(context)
                when (intent.action) {
                    Focus.ACTION_START -> Focus.start(context, 25, "Back on track")
                    Focus.ACTION_SNOOZE -> runCatching { Net.focusAction(context, prefs, JSONObject().put("action", "snooze")) }
                }
            } finally {
                pending.finish()
            }
        }
    }
}
