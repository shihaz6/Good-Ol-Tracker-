package com.goodol.tracker

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import org.json.JSONObject

/** Home-screen widget: phone time + unlocks today, PC productive time, goals, focus. */
class TodayWidget : AppWidgetProvider() {
    override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) {
        val pending = goAsync()
        CoroutineScope(Dispatchers.IO).launch {
            try { runCatching { render(context, manager, ids) } } finally { pending.finish() }  // an error here used to crash the app
        }
    }

    companion object {
        /** Re-draw all widgets (called after syncs and from the 30 s tick). Cheap if there are none. */
        fun refresh(context: Context) {
            val manager = AppWidgetManager.getInstance(context)
            val ids = manager.getAppWidgetIds(ComponentName(context, TodayWidget::class.java))
            if (ids.isEmpty()) return
            CoroutineScope(Dispatchers.IO).launch { runCatching { render(context, manager, ids) } }
        }

        private fun render(context: Context, manager: AppWidgetManager, ids: IntArray) {
            val prefs = Prefs(context)
            val views = RemoteViews(context.packageName, R.layout.widget_today)
            val (phoneMs, unlocks, _) = if (Usage.hasAccess(context)) Usage.today(context) else Triple(0L, 0, emptyList())
            views.setTextViewText(R.id.w_phone, Guard.fmt(phoneMs))
            views.setTextViewText(R.id.w_unlocks, "$unlocks unlocks")

            val today = runCatching { JSONObject(prefs.today) }.getOrDefault(JSONObject())
            val fresh = prefs.todayFresh  // not yesterday's numbers after midnight
            val prod = today.optLong("productive", -1)
            views.setTextViewText(R.id.w_pc, if (prod >= 0 && fresh) Guard.fmt(prod * 1000) else "–")

            val goals = today.optJSONArray("goals")
            val goalText = if (goals != null && goals.length() > 0 && fresh) {
                val met = (0 until goals.length()).count { goals.getJSONObject(it).optBoolean("met_today") }
                val best = (0 until goals.length()).maxOf { goals.getJSONObject(it).optInt("streak") }
                "Goals $met/${goals.length()}" + if (best > 0) " · 🔥 $best" else ""
            } else "Goals: sync with the PC"
            views.setTextViewText(R.id.w_goals, goalText)

            val focusLeft = ((prefs.focusEndSec - System.currentTimeMillis() / 1000.0) / 60).toInt()
            views.setTextViewText(R.id.w_focus, if (focusLeft > 0) "Focus · ${focusLeft + 1} min left" else "Start 25-min focus")

            val open = PendingIntent.getActivity(context, 10, Intent(context, MainActivity::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
            views.setOnClickPendingIntent(R.id.w_root, open)
            val start = PendingIntent.getBroadcast(context, 11,
                Intent(context, NudgeReceiver::class.java).setAction(Focus.ACTION_START),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
            views.setOnClickPendingIntent(R.id.w_focus, if (focusLeft > 0) open else start)
            manager.updateAppWidget(ids, views)
        }
    }
}
