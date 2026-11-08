package com.goodol.tracker

import android.content.Context
import org.json.JSONObject
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.LocalTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** Why an app is blocked right now (null = it isn't). */
data class Verdict(
    val reason: String,          // "focus" | "bedtime" | "limit"
    val title: String,
    val detail: String,
    val untilMs: Long,           // when it opens again (for the countdown)
    val graceKey: String?,       // "5 more minutes" allowed once per key
)

/**
 * The phone's rules: focus sessions (from the PC or started here), bedtime and daily app
 * limits (settings from the PC dashboard, cached so they work offline), scheduled focus.
 */
object Guard {
    const val GRACE_MS = 5 * 60_000L
    private val hhmm = DateTimeFormatter.ofPattern("HH:mm")

    private var usage: Map<String, Long> = emptyMap()
    private var usageAt = 0L

    fun settings(prefs: Prefs): JSONObject =
        runCatching { JSONObject(prefs.wellbeing) }.getOrElse { JSONObject(Prefs.DEFAULT_WELLBEING) }

    /** Today's time per app (ms), refreshed at most every 20 s. */
    @Synchronized
    fun usageToday(context: Context, fresh: Boolean = false): Map<String, Long> {
        val now = System.currentTimeMillis()
        if (fresh || now - usageAt > 20_000) {
            val midnight = LocalDate.now().atStartOfDay(ZoneId.systemDefault()).toInstant().toEpochMilli()
            usage = runCatching {
                Usage.collect(context, midnight, now).sessions.groupBy { it.pkg }.mapValues { (_, v) -> v.sumOf { it.end - it.start } }
            }.getOrDefault(usage)
            usageAt = now
        }
        return usage
    }

    /** Time on this app's website on the PC today (ms), from the last sync. Shared daily budget. */
    fun pcUsed(prefs: Prefs, pkg: String): Long {
        val j = runCatching { JSONObject(prefs.pcUsed) }.getOrNull() ?: return 0L
        if (j.optString("date") != LocalDate.now().toString()) return 0L
        return (j.optJSONObject("used")?.optLong(pkg, 0L) ?: 0L) * 1000
    }

    /** Phone + PC use of a limited app today (ms). */
    fun used(context: Context, prefs: Prefs, pkg: String): Long = (usageToday(context)[pkg] ?: 0L) + pcUsed(prefs, pkg)

    /** (start, end) of the bedtime / schedule window containing `now`, or null. */
    fun window(start: String, end: String, now: LocalDateTime = LocalDateTime.now()): Pair<LocalDateTime, LocalDateTime>? {
        val s = runCatching { LocalTime.parse(start) }.getOrNull() ?: return null
        val e = runCatching { LocalTime.parse(end) }.getOrNull() ?: return null
        val t = now.toLocalTime()
        val today = now.toLocalDate()
        return if (s < e) {
            if (!t.isBefore(s) && t.isBefore(e)) today.atTime(s) to today.atTime(e) else null
        } else when {
            !t.isBefore(s) -> today.atTime(s) to today.plusDays(1).atTime(e)
            t.isBefore(e) -> today.minusDays(1).atTime(s) to today.atTime(e)
            else -> null
        }
    }

    private fun LocalDateTime.ms() = atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()

    private fun graced(prefs: Prefs, key: String) = prefs.graceUntil(key) > System.currentTimeMillis()

    fun check(context: Context, pkg: String): Verdict? {
        if (pkg == context.packageName) return null
        val prefs = Prefs(context)
        val label = Usage.label(context, pkg)

        if (prefs.focusActive && pkg in prefs.blocked) {
            return Verdict("focus", "$label is blocked", if (prefs.focusLabel.isNotBlank()) "Working on: ${prefs.focusLabel}" else "Focus session",
                (prefs.focusEndSec * 1000).toLong(), null)
        }
        val s = settings(prefs)
        s.optJSONObject("bedtime")?.let { b ->
            if (b.optBoolean("enabled") && pkg in prefs.blocked) {
                window(b.optString("start"), b.optString("end"))?.let { (ws, we) ->
                    val key = "bed|${ws.toLocalDate()}"
                    if (!graced(prefs, key)) {
                        return Verdict("bedtime", "Bedtime", "$label is locked until ${we.format(hhmm)}. Time to wind down.", we.ms(), key)
                    }
                }
            }
        }
        val limitMin = s.optJSONObject("limits")?.optInt(pkg, 0) ?: 0
        if (limitMin > 0) {
            val used = used(context, prefs, pkg)
            val key = "limit:$pkg|${LocalDate.now()}"
            if (used >= limitMin * 60_000L && !graced(prefs, key)) {
                val midnight = LocalDate.now().plusDays(1).atStartOfDay().ms()
                val pc = pcUsed(prefs, pkg)
                return Verdict("limit", "Daily limit reached",
                    "$label: ${fmt(used)} used of your ${fmt(limitMin * 60_000L)} a day" +
                        (if (pc > 60_000) " (${fmt(pc)} of it on the PC)" else "") + ". It opens again at midnight.", midnight, key)
            }
        }
        return null
    }

    /** "5 more minutes": once per bedtime night / app per day. */
    fun canGrace(prefs: Prefs, key: String?) = key != null && prefs.graceUntil(key) == 0L

    fun grace(prefs: Prefs, key: String) = prefs.setGrace(key, System.currentTimeMillis() + GRACE_MS)

    /** Warnings and scheduled things, run every 30 s while the screen is on. */
    fun tick(context: Context, currentPkg: String?) {
        val prefs = Prefs(context)
        val s = settings(prefs)
        val now = LocalDateTime.now()
        // App limit warning at 80%.
        if (currentPkg != null) {
            val limitMin = s.optJSONObject("limits")?.optInt(currentPkg, 0) ?: 0
            val used = used(context, prefs, currentPkg)
            if (limitMin > 0 && used >= limitMin * 60_000L * 0.8 && used < limitMin * 60_000L &&
                prefs.once("warn:$currentPkg|${LocalDate.now()}")) {
                Focus.notify(context, 50, "${Usage.label(context, currentPkg)}: ${fmt(used)} of ${fmt(limitMin * 60_000L)}",
                    "About ${fmt(limitMin * 60_000L - used)} left today before it's blocked until midnight.")
            }
        }
        // Wind-down reminder before bedtime.
        s.optJSONObject("bedtime")?.let { b ->
            val wind = b.optInt("wind_down", 30)
            if (b.optBoolean("enabled") && wind > 0) {
                val start = runCatching { LocalTime.parse(b.optString("start")) }.getOrNull()
                if (start != null) {
                    // The next time bedtime begins (today or tomorrow).
                    val todayAt = now.toLocalDate().atTime(start)
                    val startAt = if (now.isBefore(todayAt)) todayAt else todayAt.plusDays(1)
                    if (!now.isBefore(startAt.minusMinutes(wind.toLong())) && prefs.once("wind|${startAt.toLocalDate()}")) {
                        Focus.notify(context, 51, "Bedtime in ${java.time.Duration.between(now, startAt).toMinutes() + 1} min",
                            "Social and video apps lock at ${start.format(hhmm)}. Good time to wrap up.")
                    }
                }
            }
        }
        // Scheduled focus (the PC starts it too; whoever is first wins, ending early sticks).
        val schedules = s.optJSONArray("schedules")
        if (schedules != null && !prefs.focusActive) {
            for (i in 0 until schedules.length()) {
                val sc = schedules.getJSONObject(i)
                val (ws, we) = window(sc.optString("start"), sc.optString("end"), now) ?: continue
                val days = sc.optJSONArray("days") ?: continue
                val dow = ws.dayOfWeek.value - 1  // Mon = 0, like the PC
                if ((0 until days.length()).none { days.optInt(it) == dow }) continue
                if (!prefs.once("sched:$i:${ws.toLocalTime()}|${ws.toLocalDate()}")) continue
                val mins = java.time.Duration.between(now, we).toMinutes().toInt().coerceIn(5, 240)
                Focus.start(context, mins, sc.optString("label").ifBlank { "Scheduled focus" })
            }
        }
        // Adult filter (Private DNS) switched off? Say so once.
        val dns = Dns.filterHost(context)
        if (prefs.dnsWasOn && dns == null && prefs.once("dnsoff|${LocalDate.now()}")) {
            Focus.notify(context, 52, "Adult content filter is off", "Private DNS was turned off. Open Good ol Tracker to turn it back on.")
        }
        prefs.dnsWasOn = dns != null
    }

    fun fmt(ms: Long): String {
        val m = ms / 60_000
        return if (m >= 60) "${m / 60}h ${m % 60}m".replace(" 0m", "") else "${m}m"
    }
}
