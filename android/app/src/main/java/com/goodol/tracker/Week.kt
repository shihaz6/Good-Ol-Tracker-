package com.goodol.tracker

import android.text.format.DateUtils
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.TextStyle
import java.util.Locale

private fun JSONArray.objects() = (0 until length()).map { getJSONObject(it) }
private fun sec(s: Long) = Guard.fmt(s * 1000)
private fun dayName(iso: String) = runCatching {
    LocalDate.parse(iso).dayOfWeek.getDisplayName(TextStyle.SHORT, Locale.getDefault())
}.getOrDefault(iso)

@Composable
fun WeekScreen(paired: Boolean) {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    var data by remember { mutableStateOf(runCatching { JSONObject(prefs.week) }.getOrNull()) }
    var loading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(paired) {
        if (!paired) return@LaunchedEffect
        loading = true
        withContext(Dispatchers.IO) {
            try { data = Net.week(ctx, prefs); error = null } catch (e: Exception) { error = e.message }
        }
        loading = false
    }

    Column(verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(horizontal = 4.dp)) {
            if (loading) { CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 2.dp); Spacer(Modifier.width(8.dp)) }
            Muted(when {
                !paired -> "Connect to your PC to see your week."
                loading && data == null -> "Getting your week from the PC…"
                data != null -> {
                    val at = (data!!.optDouble("generated") * 1000).toLong()
                    "From your PC · updated " + (if (System.currentTimeMillis() - at < 60_000) "just now"
                        else DateUtils.getRelativeTimeSpanString(at).toString()) + (error?.let { " · PC not reachable now" } ?: "")
                }
                else -> error ?: ""
            })
        }
        val d = data ?: return@Column
        Overview(d)
        d.optJSONArray("subjects")?.objects()?.takeIf { it.isNotEmpty() }?.let { Subjects(it) }
        d.optJSONArray("goals")?.objects()?.takeIf { it.isNotEmpty() }?.let { Goals(it, d.optJSONArray("days")?.objects().orEmpty()) }
        d.optJSONObject("sleep")?.let { Sleep(it) }
        FocusAndPhone(d)
    }
}

@Composable
private fun Overview(d: JSONObject) {
    val days = d.optJSONArray("days")?.objects().orEmpty()
    val scheme = MaterialTheme.colorScheme
    val sig = LocalSignal.current
    // Averages only over days each device was actually tracked.
    val pcDays = days.filter { it.optBoolean("pc_tracked") }
    val phoneDays = days.filter { it.optLong("phone") > 0 }
    val prodAvg = if (pcDays.isEmpty()) null else pcDays.sumOf { it.optLong("productive") } / pcDays.size
    val phoneAvg = if (phoneDays.isEmpty()) null else phoneDays.sumOf { it.optLong("phone") } / phoneDays.size
    Panel {
        Kicker("Last 7 days")
        Spacer(Modifier.height(8.dp))
        Row(Modifier.fillMaxWidth()) {
            Column(Modifier.weight(1f)) {
                Text(prodAvg?.let { sec(it) } ?: "–", fontSize = 30.sp, fontWeight = FontWeight.SemiBold, color = scheme.primary)
                Muted("productive a day")
                if (pcDays.size in 1..6) Muted("over ${pcDays.size} PC day${if (pcDays.size == 1) "" else "s"}")
            }
            Column(Modifier.weight(1f)) {
                Text(phoneAvg?.let { sec(it) } ?: "–", fontSize = 30.sp, fontWeight = FontWeight.SemiBold, color = sig.phone)
                Muted("on the phone a day")
            }
        }
        Spacer(Modifier.height(16.dp))
        // Grouped bars per day: productive (blue) next to phone (lavender). One shared scale.
        val max = days.maxOfOrNull { maxOf(it.optLong("productive"), it.optLong("phone")) }?.coerceAtLeast(1) ?: 1
        val track = sig.track
        Canvas(Modifier.fillMaxWidth().height(120.dp)) {
            val slot = size.width / days.size.coerceAtLeast(1)
            val bar = (slot * 0.28f).coerceAtMost(16.dp.toPx())
            val gap = 2.dp.toPx()
            drawLine(track, Offset(0f, size.height), Offset(size.width, size.height), 1.dp.toPx())
            days.forEachIndexed { i, day ->
                val cx = slot * i + slot / 2
                listOf(day.optLong("productive") to scheme.primary, day.optLong("phone") to sig.phone)
                    .forEachIndexed { j, (v, c) ->
                        val h = (v.toFloat() / max) * (size.height - 4.dp.toPx())
                        if (h > 0f) {
                            val x = if (j == 0) cx - bar - gap / 2 else cx + gap / 2
                            drawRoundRect(c, Offset(x, size.height - h), Size(bar, h), CornerRadius(4.dp.toPx(), 4.dp.toPx()))
                        }
                    }
            }
        }
        Row(Modifier.fillMaxWidth().padding(top = 6.dp)) {
            days.forEach { Text(dayName(it.optString("date")).take(3), Modifier.weight(1f), textAlign = TextAlign.Center,
                style = MaterialTheme.typography.labelSmall, color = scheme.onSurfaceVariant) }
        }
        Spacer(Modifier.height(10.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Dot(scheme.primary); Spacer(Modifier.width(6.dp)); Muted("Productive"); Spacer(Modifier.width(16.dp))
            Dot(sig.phone); Spacer(Modifier.width(6.dp)); Muted("Phone")
        }
    }
}

@Composable
private fun Subjects(list: List<JSONObject>) {
    val scheme = MaterialTheme.colorScheme
    val sig = LocalSignal.current
    Panel {
        Kicker("Subjects this week")
        list.sortedWith(compareBy<JSONObject>({ if (it.isNull("days_left") || it.optInt("days_left") < 0) 1 else 0 },
            { it.optInt("days_left", 9999) }, { -it.optLong("week_seconds") })).forEach { s ->
            val wk = s.optLong("week_seconds")
            val target = s.optLong("weekly_minutes") * 60
            Spacer(Modifier.height(12.dp))
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text(s.optString("name"), Modifier.weight(1f), fontWeight = FontWeight.Medium)
                if (!s.isNull("days_left") && s.optInt("days_left") >= 0) {
                    val dl = s.optInt("days_left")
                    val (bg, fg) = when {
                        dl <= 2 -> scheme.errorContainer to scheme.error
                        dl <= 7 -> sig.warningContainer to sig.onWarningContainer
                        else -> scheme.surfaceContainerHigh to scheme.onSurfaceVariant
                    }
                    Text(if (dl == 0) "Exam today" else "Exam in $dl day${if (dl == 1) "" else "s"}",
                        style = MaterialTheme.typography.bodySmall, fontWeight = FontWeight.SemiBold, color = fg,
                        modifier = Modifier.clip(CircleShape).background(bg).padding(horizontal = 9.dp, vertical = 2.dp))
                }
            }
            Spacer(Modifier.height(4.dp))
            Muted(if (target > 0) "${sec(wk)} of ${sec(target)} this week" else "${sec(wk)} this week")
            if (target > 0) {
                Spacer(Modifier.height(6.dp))
                Bar(wk.toFloat() / target, if (wk >= target) sig.success else scheme.primary)
            }
        }
    }
}

@Composable
private fun Goals(goals: List<JSONObject>, days: List<JSONObject>) {
    val scheme = MaterialTheme.colorScheme
    val sig = LocalSignal.current
    Panel {
        Kicker("Goals")
        goals.forEach { g ->
            Spacer(Modifier.height(12.dp))
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text(g.optString("label"), Modifier.weight(1f), fontWeight = FontWeight.Medium)
                if (g.optInt("streak") > 0) Text("🔥 ${g.optInt("streak")}", style = MaterialTheme.typography.bodySmall,
                    color = sig.onWarningContainer,
                    modifier = Modifier.clip(CircleShape).background(sig.warningContainer).padding(horizontal = 8.dp, vertical = 2.dp))
            }
            Spacer(Modifier.height(8.dp))
            val hist = g.optJSONArray("history")
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                for (i in 0 until (hist?.length() ?: 0)) {
                    val met = if (hist!!.isNull(i)) null else hist.optBoolean(i)
                    val last = i == hist.length() - 1
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Box(Modifier.size(22.dp).clip(RoundedCornerShape(7.dp)).background(when (met) {
                            true -> sig.success
                            false -> if (last) sig.track else scheme.error.copy(alpha = 0.75f)
                            null -> sig.track.copy(alpha = 0.5f)
                        }), contentAlignment = Alignment.Center) {
                            if (met == true) Text("✓", fontSize = 12.sp, color = scheme.surface, fontWeight = FontWeight.Bold)
                        }
                        Text(days.getOrNull(i)?.optString("date")?.let { dayName(it).take(1) } ?: "",
                            style = MaterialTheme.typography.labelSmall, color = scheme.onSurfaceVariant)
                    }
                }
            }
        }
    }
}

@Composable
private fun Sleep(s: JSONObject) {
    val scheme = MaterialTheme.colorScheme
    val sig = LocalSignal.current
    val nights = s.optJSONArray("nights")?.objects().orEmpty()
    if (nights.none { !it.isNull("seconds") }) return
    Panel {
        Kicker("Sleep (estimated)")
        Spacer(Modifier.height(6.dp))
        if (!s.isNull("avg_seconds")) {
            Text(sec(s.optLong("avg_seconds")), fontSize = 26.sp, fontWeight = FontWeight.SemiBold)
            Muted("a night · usually ${s.optString("avg_bedtime")} to ${s.optString("avg_wake")}")
        }
        Spacer(Modifier.height(10.dp))
        // Each night on an 20:00 → 14:00 axis.
        val axis0 = 20.0; val span = 18.0
        fun pos(ts: Double): Float {
            val t = Instant.ofEpochMilli((ts * 1000).toLong()).atZone(ZoneId.systemDefault()).toLocalTime()
            val h = ((t.hour + t.minute / 60.0 - axis0) + 24) % 24
            return (h / span).toFloat().coerceIn(0f, 1f)
        }
        nights.reversed().forEach { n ->
            Row(Modifier.fillMaxWidth().padding(vertical = 3.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(dayName(n.optString("date")), Modifier.width(40.dp), style = MaterialTheme.typography.bodySmall,
                    color = scheme.onSurfaceVariant)
                BoxWithConstraints(Modifier.weight(1f).height(14.dp).clip(RoundedCornerShape(5.dp)).background(sig.track)) {
                    if (!n.isNull("seconds")) {
                        val l = pos(n.optDouble("bedtime")); val r = pos(n.optDouble("wake"))
                        Box(Modifier.padding(start = maxWidth * l).width(maxWidth * (r - l).coerceAtLeast(0.01f)).height(14.dp)
                            .clip(RoundedCornerShape(5.dp))
                            .background(if (n.optLong("seconds") >= 7 * 3600) sig.phone else scheme.error.copy(alpha = 0.8f)))
                    }
                }
                Text(if (n.isNull("seconds")) "–" else sec(n.optLong("seconds")), Modifier.width(56.dp),
                    textAlign = TextAlign.End, style = MaterialTheme.typography.bodySmall)
            }
        }
        Row(Modifier.fillMaxWidth().padding(start = 40.dp, end = 56.dp, top = 2.dp), horizontalArrangement = Arrangement.SpaceBetween) {
            listOf("20:00", "02:00", "08:00", "14:00").forEach { Text(it, style = MaterialTheme.typography.labelSmall, color = scheme.onSurfaceVariant) }
        }
        Spacer(Modifier.height(8.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Dot(sig.phone); Spacer(Modifier.width(6.dp)); Muted("7h or more"); Spacer(Modifier.width(14.dp))
            Dot(scheme.error.copy(alpha = 0.8f)); Spacer(Modifier.width(6.dp)); Muted("under 7h")
        }
    }
}

@Composable
private fun FocusAndPhone(d: JSONObject) {
    val f = d.optJSONObject("focus") ?: JSONObject()
    val p = d.optJSONObject("pickups") ?: JSONObject()
    val apps = d.optJSONArray("top_apps")?.objects().orEmpty()
    val sig = LocalSignal.current
    Panel {
        Kicker("Focus & phone")
        Spacer(Modifier.height(10.dp))
        Row(Modifier.fillMaxWidth()) {
            Stat(Modifier.weight(1f), f.optInt("sessions").toString(), "focus sessions",
                "${f.optInt("completed")} finished · ${sec(f.optLong("seconds"))}")
            Stat(Modifier.weight(1f), p.optInt("count").toString(), "phone pickups while studying",
                if (p.isNull("per_study_hour")) "" else "${p.optDouble("per_study_hour")} per study hour")
        }
        if (apps.isNotEmpty()) {
            Spacer(Modifier.height(14.dp))
            Kicker("Top phone apps")
            val max = apps.maxOf { it.optLong("seconds") }.coerceAtLeast(1)
            apps.forEach { a ->
                Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text(a.optString("app"), Modifier.width(110.dp), maxLines = 1, overflow = TextOverflow.Ellipsis,
                        style = MaterialTheme.typography.bodyMedium)
                    Bar(a.optLong("seconds").toFloat() / max, sig.phone.copy(alpha = 0.7f), Modifier.weight(1f))
                    Text(sec(a.optLong("seconds")), Modifier.width(62.dp), textAlign = TextAlign.End,
                        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }
}

@Composable
private fun Stat(modifier: Modifier, value: String, label: String, note: String) {
    Column(modifier) {
        Text(value, fontSize = 26.sp, fontWeight = FontWeight.SemiBold)
        Text(label, style = MaterialTheme.typography.bodySmall)
        if (note.isNotBlank()) Muted(note)
    }
}
