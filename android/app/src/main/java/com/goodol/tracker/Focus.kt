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
            val res = Net.focusAction(context, prefs, JSONObject().put("action", "start").put("minutes", minutes).put("label", label))
            applyReply(context, res)
            "Focus started for $minutes min."
        } catch (e: Exception) {
            val now = System.currentTimeMillis() / 1000.0
            prefs.setFocus(-1L, now, now + minutes * 60, label, true)
            "PC not reachable: blocking apps on the phone only for $minutes min."
        }
    }

    fun stop(context: Context): String {
        val prefs = Prefs(context)
        if (prefs.focusLocal) { prefs.clearFocus(); return "Focus ended." }
        return try {
            applyReply(context, Net.focusAction(context, prefs, JSONObject().put("action", "stop")))
            prefs.clearFocus()
            "Focus ended."
        } catch (e: Exception) {
            prefs.clearFocus()  // never lock you out: end it here; the PC's session runs out on its own
            "Ended on the phone (PC not reachable)."
        }
    }

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
