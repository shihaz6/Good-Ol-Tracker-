package com.goodol.tracker

import android.Manifest
import android.annotation.SuppressLint
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.format.DateUtils
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

class MainActivity : ComponentActivity() {
    private val resumed = mutableIntStateOf(0)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        if (Prefs(this).paired) SyncWorker.schedule(this)
        val startTab = if (intent?.getStringExtra("tab") == "week") 1 else 0
        setContent { AppTheme { Screen(resumed.intValue, startTab) } }
    }

    override fun onResume() {
        super.onResume()
        resumed.intValue++  // re-check permissions after coming back from Settings
    }
}

private fun isXiaomi() = Build.MANUFACTURER.lowercase().let { it.contains("xiaomi") || it.contains("redmi") || it.contains("poco") }

private fun ignoringBattery(ctx: Context): Boolean =
    (ctx.getSystemService(Context.POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(ctx.packageName)

// ------------------------------------------------------------------ building blocks

internal val CardShape = RoundedCornerShape(20.dp)

@Composable
internal fun Panel(
    modifier: Modifier = Modifier,
    container: Color = MaterialTheme.colorScheme.surfaceContainer,
    border: Color? = MaterialTheme.colorScheme.outlineVariant,
    content: @Composable () -> Unit,
) {
    Card(
        modifier.fillMaxWidth(), shape = CardShape,
        colors = CardDefaults.cardColors(containerColor = container),
        border = border?.let { BorderStroke(1.dp, it) },
    ) { Column(Modifier.padding(18.dp)) { content() } }
}

@Composable
internal fun Kicker(text: String, color: Color = MaterialTheme.colorScheme.onSurfaceVariant, modifier: Modifier = Modifier) =
    Text(text.uppercase(), style = MaterialTheme.typography.labelSmall, color = color, modifier = modifier)

@Composable
internal fun Muted(text: String, modifier: Modifier = Modifier) =
    Text(text, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = modifier)

@Composable
internal fun Bar(fraction: Float, color: Color, modifier: Modifier = Modifier) {
    Box(modifier.fillMaxWidth().height(6.dp).clip(CircleShape).background(LocalSignal.current.track)) {
        Box(Modifier.fillMaxWidth(fraction.coerceIn(0f, 1f)).height(6.dp).clip(CircleShape).background(color))
    }
}

@Composable
internal fun Dot(color: Color, size: Int = 8) = Box(Modifier.size(size.dp).clip(CircleShape).background(color))

// ------------------------------------------------------------------ the screen

@Composable
fun Screen(resumeTick: Int, startTab: Int = 0) {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    var access by remember { mutableStateOf(Usage.hasAccess(ctx)) }
    var paired by remember { mutableStateOf(prefs.paired) }
    var today by remember { mutableStateOf<Triple<Long, Int, List<Pair<String, Long>>>?>(null) }
    var tick by remember { mutableIntStateOf(0) }
    var tab by remember { mutableIntStateOf(startTab) }

    LaunchedEffect(resumeTick, tick) {
        access = Usage.hasAccess(ctx)
        paired = prefs.paired
        if (access) today = withContext(Dispatchers.IO) { runCatching { Usage.today(ctx) }.getOrNull() }
    }

    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(
            Modifier.safeDrawingPadding().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Header(paired, prefs.lastSyncMs, prefs.lastError)
            Tabs(tab) { tab = it }
            if (tab == 1) { WeekScreen(paired); return@Column }
            today?.let { TodayCard(it, prefs) }
            FocusCard(tick) { tick++ }
            RulesCard(tick)
            Kicker("Setup", modifier = Modifier.padding(top = 6.dp, start = 4.dp))
            SetupSection(access, paired, resumeTick, onChange = { tick++ }, onPaired = { paired = true; tick++ })
            Muted("Sends which apps you use and when to your PC's tracker. Never what's inside them.",
                Modifier.padding(horizontal = 4.dp, vertical = 8.dp))
        }
    }
}

/** Today | Week, as a pill-shaped segmented switch. */
@Composable
private fun Tabs(selected: Int, onSelect: (Int) -> Unit) {
    val scheme = MaterialTheme.colorScheme
    Row(Modifier.fillMaxWidth().clip(CircleShape).background(scheme.surfaceContainerHigh).padding(4.dp)) {
        listOf("Today", "Week").forEachIndexed { i, label ->
            val on = i == selected
            Box(Modifier.weight(1f).clip(CircleShape).background(if (on) scheme.primary else Color.Transparent)
                .clickable { onSelect(i) }.padding(vertical = 9.dp), contentAlignment = Alignment.Center) {
                Text(label, fontWeight = if (on) FontWeight.SemiBold else FontWeight.Normal,
                    color = if (on) scheme.onPrimary else scheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
private fun Header(paired: Boolean, lastSync: Long, lastError: String?) {
    val sig = LocalSignal.current
    Row(Modifier.fillMaxWidth().padding(top = 4.dp), verticalAlignment = Alignment.CenterVertically) {
        Image(painterResource(R.drawable.ic_launcher), contentDescription = null, modifier = Modifier.size(40.dp).clip(RoundedCornerShape(12.dp)))
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text("Good ol Tracker", style = MaterialTheme.typography.headlineSmall)
            val fresh = paired && lastSync > 0 && System.currentTimeMillis() - lastSync < 20 * 60_000
            Row(verticalAlignment = Alignment.CenterVertically) {
                Dot(when { !paired -> MaterialTheme.colorScheme.outline; fresh -> sig.success; else -> sig.warning }, 7)
                Spacer(Modifier.width(6.dp))
                Muted(when {
                    !paired -> "Not connected to your PC"
                    lastSync == 0L -> "Connected · waiting for first sync"
                    fresh && System.currentTimeMillis() - lastSync < 60_000 -> "Synced just now"
                    fresh -> "Synced " + DateUtils.getRelativeTimeSpanString(lastSync)
                    else -> "PC not reachable · last sync " + DateUtils.getRelativeTimeSpanString(lastSync)
                })
            }
        }
    }
}

@Composable
private fun TodayCard(data: Triple<Long, Int, List<Pair<String, Long>>>, prefs: Prefs) {
    val (total, unlocks, top) = data
    val scheme = MaterialTheme.colorScheme
    val pc = runCatching { JSONObject(prefs.today) }.getOrDefault(JSONObject())
    val fresh = prefs.todayFresh  // not yesterday's numbers after midnight
    val productive = pc.optLong("productive", -1).takeIf { it >= 0 && fresh }
    val exam = pc.optJSONObject("next_exam")?.takeIf { fresh }
    Panel {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Kicker("Today", modifier = Modifier.weight(1f))
            exam?.let { e ->
                val dl = e.optInt("days_left")
                val sig = LocalSignal.current
                val (bg, fg) = when {
                    dl <= 2 -> scheme.errorContainer to scheme.error
                    dl <= 7 -> sig.warningContainer to sig.onWarningContainer
                    else -> scheme.surfaceContainerHigh to scheme.onSurfaceVariant
                }
                Text("${e.optString("name")} exam " + if (dl == 0) "today" else "in $dl day${if (dl == 1) "" else "s"}",
                    style = MaterialTheme.typography.bodySmall, fontWeight = FontWeight.SemiBold, color = fg,
                    modifier = Modifier.clip(CircleShape).background(bg).padding(horizontal = 10.dp, vertical = 3.dp))
            }
        }
        Spacer(Modifier.height(8.dp))
        Row(Modifier.fillMaxWidth()) {
            Column(Modifier.weight(1f)) {
                Text(Guard.fmt(total), fontSize = 34.sp, fontWeight = FontWeight.SemiBold, lineHeight = 36.sp)
                Muted("on the phone")
                Muted("$unlocks unlocks")
            }
            Column(Modifier.weight(1f)) {
                Text(productive?.let { Guard.fmt(it * 1000) } ?: "–", fontSize = 34.sp, fontWeight = FontWeight.SemiBold,
                    lineHeight = 36.sp, color = scheme.primary)
                Muted("productive")
                Muted("PC + phone")
            }
        }
        if (top.isNotEmpty()) {
            Spacer(Modifier.height(14.dp))
            val max = top.maxOf { it.second }.coerceAtLeast(1)
            top.forEach { (name, ms) ->
                Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text(name, Modifier.width(110.dp), maxLines = 1, overflow = TextOverflow.Ellipsis,
                        style = MaterialTheme.typography.bodyMedium)
                    Bar(ms.toFloat() / max, scheme.primary.copy(alpha = 0.55f), Modifier.weight(1f))
                    Text(Guard.fmt(ms), Modifier.width(58.dp), textAlign = TextAlign.End,
                        style = MaterialTheme.typography.bodySmall, color = scheme.onSurfaceVariant)
                }
            }
        }
    }
}

// ------------------------------------------------------------------ focus

@Composable
fun FocusCard(tick: Int, onChange: () -> Unit) {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    val scope = rememberCoroutineScope()
    val scheme = MaterialTheme.colorScheme
    var busy by remember { mutableStateOf(false) }
    var msg by remember { mutableStateOf<String?>(null) }
    var left by remember { mutableLongStateOf(0L) }
    var armed by remember { mutableStateOf(false) }

    LaunchedEffect(tick) {
        while (true) {
            left = (prefs.focusEndSec - System.currentTimeMillis() / 1000.0).toLong()
            if (left <= 0) break
            delay(1000)
        }
    }
    LaunchedEffect(armed) { if (armed) { delay(4000); armed = false } }

    val active = left > 0
    Panel(container = if (active) scheme.primaryContainer else scheme.surfaceContainer,
        border = if (active) null else scheme.outlineVariant) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (active) { Dot(scheme.primary); Spacer(Modifier.width(8.dp)) }
            Kicker(if (active) "Focusing" else "Focus mode", if (active) scheme.primary else scheme.onSurfaceVariant)
        }
        if (active) {
            val total = (prefs.focusEndSec - prefs.focusStartSec).coerceAtLeast(1.0)
            val h = left / 3600; val m = (left % 3600) / 60; val s = left % 60
            Text(if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%02d:%02d".format(m, s),
                fontSize = 48.sp, fontWeight = FontWeight.SemiBold, color = scheme.onPrimaryContainer)
            Bar((1 - left / total).toFloat(), scheme.primary)
            Spacer(Modifier.height(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(buildString {
                    append(if (prefs.focusLabel.isNotBlank()) prefs.focusLabel else "Distracting apps are blocked")
                    if (prefs.focusLocal) append(" · phone only")
                }, Modifier.weight(1f), style = MaterialTheme.typography.bodySmall, color = scheme.onPrimaryContainer)
                TextButton(enabled = !busy, onClick = {
                    if (!armed) { armed = true; return@TextButton }
                    busy = true
                    scope.launch {
                        msg = withContext(Dispatchers.IO) { Focus.stop(ctx) }
                        busy = false; armed = false; onChange()
                    }
                }, colors = ButtonDefaults.textButtonColors(contentColor = if (armed) scheme.error else scheme.onPrimaryContainer)) {
                    Text(if (armed) "Tap again to end" else "End early")
                }
            }
        } else {
            Spacer(Modifier.height(4.dp))
            Muted("Blocks Facebook, Instagram, YouTube and other distracting apps here, and distracting sites on the PC.")
            Spacer(Modifier.height(12.dp))
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf(25, 50, 90).forEachIndexed { i, mins ->
                    val onClick = {
                        busy = true
                        scope.launch {
                            msg = withContext(Dispatchers.IO) { Focus.start(ctx, mins) }
                            busy = false; onChange()
                        }
                        Unit
                    }
                    val pad = PaddingValues(horizontal = 4.dp, vertical = 12.dp)
                    if (i == 0) Button(onClick, Modifier.weight(1f), enabled = !busy, contentPadding = pad, shape = RoundedCornerShape(14.dp)) {
                        Text("$mins min", maxLines = 1, fontWeight = FontWeight.SemiBold)
                    } else OutlinedButton(onClick, Modifier.weight(1f), enabled = !busy, contentPadding = pad, shape = RoundedCornerShape(14.dp)) {
                        Text("$mins min", maxLines = 1)
                    }
                }
            }
        }
        msg?.let { Spacer(Modifier.height(6.dp)); Muted(it) }
    }
}

// ------------------------------------------------------------------ goals, limits, bedtime

@Composable
fun RulesCard(tick: Int) {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    val scheme = MaterialTheme.colorScheme
    val sig = LocalSignal.current
    var usage by remember { mutableStateOf<Map<String, Long>>(emptyMap()) }
    LaunchedEffect(tick) { usage = withContext(Dispatchers.IO) { runCatching { Guard.usageToday(ctx, fresh = true) }.getOrDefault(emptyMap()) } }
    val s = Guard.settings(prefs)
    val today = runCatching { JSONObject(prefs.today) }.getOrDefault(JSONObject())

    Panel {
        Kicker("Goals & limits")
        today.optJSONArray("goals")?.takeIf { prefs.todayIsToday }?.let { goals ->
            for (i in 0 until goals.length()) {
                val g = goals.getJSONObject(i)
                val target = g.optLong("target").coerceAtLeast(1)
                val v = g.optLong("today")
                val over = g.optBoolean("over"); val met = g.optBoolean("met_today")
                val atLeast = g.optString("kind") == "productive_min"
                val color = when { over -> scheme.error; met && atLeast -> sig.success; atLeast -> scheme.primary; else -> sig.success }
                Spacer(Modifier.height(12.dp))
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text(g.optString("label"), Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Medium)
                    if (g.optInt("streak") > 0) {
                        Text("🔥 ${g.optInt("streak")}", style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.clip(CircleShape).background(sig.warningContainer).padding(horizontal = 8.dp, vertical = 2.dp),
                            color = sig.onWarningContainer)
                        Spacer(Modifier.width(8.dp))
                    }
                    Text(when {
                        over -> "${Guard.fmt((v - target) * 1000)} over"
                        met && atLeast -> "Done ✓"
                        atLeast -> "${Guard.fmt((target - v) * 1000)} to go"
                        else -> "${Guard.fmt((target - v) * 1000)} left"
                    }, style = MaterialTheme.typography.bodySmall, color = if (over) scheme.error else scheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(6.dp))
                Bar(v.toFloat() / target, color)
            }
        } ?: Muted("Goals show up after the next sync with your PC.", Modifier.padding(top = 8.dp))

        s.optJSONObject("limits")?.let { lim ->
            val keys = lim.keys().asSequence().toList()
            if (keys.isNotEmpty()) {
                Spacer(Modifier.height(16.dp))
                Kicker("Daily app limits")
                keys.forEach { pkg ->
                    val limMs = lim.optInt(pkg) * 60_000L
                    val used = (usage[pkg] ?: 0L) + Guard.pcUsed(prefs, pkg)  // phone + its website on the PC
                    val frac = used.toFloat() / limMs
                    val color = when { frac >= 1f -> scheme.error; frac >= 0.8f -> sig.warning; else -> sig.success }
                    Spacer(Modifier.height(10.dp))
                    Row(Modifier.fillMaxWidth()) {
                        Text(Usage.label(ctx, pkg), Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium)
                        Text(if (frac >= 1f) "Blocked until midnight" else "${Guard.fmt(used)} of ${Guard.fmt(limMs)}",
                            style = MaterialTheme.typography.bodySmall, color = if (frac >= 1f) scheme.error else scheme.onSurfaceVariant)
                    }
                    Spacer(Modifier.height(6.dp))
                    Bar(frac, color)
                }
            }
        }
        s.optJSONObject("bedtime")?.let { b ->
            Spacer(Modifier.height(16.dp))
            Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(scheme.surfaceContainerHigh).padding(12.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Text("🌙", fontSize = 18.sp)
                Spacer(Modifier.width(10.dp))
                Column(Modifier.weight(1f)) {
                    Text(if (b.optBoolean("enabled")) "Bedtime ${b.optString("start")} – ${b.optString("end")}" else "Bedtime is off",
                        style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Medium)
                    Muted(if (b.optBoolean("enabled")) "Social and video apps lock overnight" else "Turn it on from the PC dashboard")
                }
            }
        }
        Spacer(Modifier.height(10.dp))
        Muted("Change goals, limits and bedtime on the PC dashboard.")
    }
}

// ------------------------------------------------------------------ setup

/** Done = one compact line with a mint check. Not done = an amber-edged card with the steps. */
@Composable
fun SetupItem(title: String, done: Boolean, summary: String, content: @Composable () -> Unit) {
    val sig = LocalSignal.current
    val scheme = MaterialTheme.colorScheme
    if (done) {
        Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(scheme.surfaceContainerLow)
            .padding(horizontal = 14.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(22.dp).clip(CircleShape).background(sig.successContainer), contentAlignment = Alignment.Center) {
                Text("✓", color = sig.success, fontSize = 13.sp, fontWeight = FontWeight.Bold)
            }
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(title, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Medium)
                Muted(summary)
            }
        }
    } else {
        Panel(container = scheme.surfaceContainer, border = sig.warning.copy(alpha = 0.55f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(22.dp).clip(CircleShape).background(sig.warningContainer), contentAlignment = Alignment.Center) {
                    Text("!", color = sig.warning, fontSize = 13.sp, fontWeight = FontWeight.Bold)
                }
                Spacer(Modifier.width(12.dp))
                Text(title, style = MaterialTheme.typography.titleMedium)
            }
            Spacer(Modifier.height(10.dp))
            content()
        }
    }
}

@Composable
private fun SetupSection(access: Boolean, paired: Boolean, resumeTick: Int, onChange: () -> Unit, onPaired: () -> Unit) {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    val scope = rememberCoroutineScope()
    var pair by remember { mutableStateOf<PairState>(PairState.Idle) }
    var syncing by remember { mutableStateOf(false) }
    var syncMsg by remember { mutableStateOf<String?>(null) }
    val notifLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { onChange() }
    var dnsHost by remember { mutableStateOf<String?>(null) }
    var dnsOther by remember { mutableStateOf<String?>(null) }
    var copied by remember { mutableStateOf(false) }
    LaunchedEffect(resumeTick) { dnsHost = Dns.filterHost(ctx); dnsOther = Dns.activeHost(ctx) }
    val blocking = Focus.blockingEnabled(ctx)
    val notify = Focus.canNotify(ctx)
    val battery = ignoringBattery(ctx)

    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        SetupItem("Usage access", access, "Allowed") {
            Muted("Android needs your OK before an app can read app usage. Find Good ol Tracker in the list and switch it on.")
            Spacer(Modifier.height(10.dp))
            Button(onClick = { ctx.startActivity(Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS)) }) { Text("Allow usage access") }
        }

        if (paired) {
            Panel(container = MaterialTheme.colorScheme.surfaceContainerLow, border = null) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("Connected to ${prefs.pcName ?: "your PC"}", style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Medium)
                        Muted(syncMsg ?: prefs.lastError?.let { "Last try: $it" } ?: "Syncs every minute while you use the phone")
                    }
                    OutlinedButton(enabled = !syncing && access, onClick = {
                        syncing = true; syncMsg = null
                        scope.launch {
                            syncMsg = withContext(Dispatchers.IO) {
                                try { "Sent ${Net.sync(ctx, prefs)} app sessions" }
                                catch (e: Exception) { prefs.lastError = e.message; "Couldn't sync: ${e.message}" }
                            }
                            syncing = false; onChange()
                        }
                    }) { Text(if (syncing) "Syncing…" else "Sync now") }
                }
                TextButton(onClick = { prefs.forget(); pair = PairState.Idle; onChange() }, contentPadding = PaddingValues(0.dp)) {
                    Text("Disconnect", color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall)
                }
            }
        } else SetupItem("Connect to your PC", false, "") {
            when (val s = pair) {
                PairState.Idle -> {
                    Muted("Connect the laptop to this phone's hotspot (or the same Wi-Fi) with the tracker running, then:")
                    Spacer(Modifier.height(10.dp))
                    Button(enabled = access, onClick = {
                        scope.launch { Pairing.run(ctx) { st -> scope.launch { pair = st; if (st is PairState.Done) onPaired() } } }
                    }) { Text("Connect to PC") }
                }
                PairState.Searching -> Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                    Spacer(Modifier.width(10.dp)); Text("Looking for your PC…")
                }
                is PairState.Waiting -> Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.fillMaxWidth()) {
                    Muted("On the PC dashboard (Phone card), allow the request with this code:")
                    Text(s.code, fontSize = 44.sp, fontWeight = FontWeight.Bold, letterSpacing = 8.sp,
                        color = MaterialTheme.colorScheme.primary, modifier = Modifier.padding(vertical = 8.dp))
                    Muted("Found ${s.pcName}")
                    Spacer(Modifier.height(8.dp))
                    LinearProgressIndicator(Modifier.fillMaxWidth())
                }
                is PairState.Failed -> {
                    Text(s.message, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
                    Spacer(Modifier.height(10.dp))
                    OutlinedButton(onClick = { pair = PairState.Idle }) { Text("Try again") }
                }
                PairState.Done -> Text("Connected.")
            }
        }

        SetupItem("App blocking", blocking, "On · it only sees which app is open") {
            Muted("Switch on \"Good ol Tracker focus mode\" in Accessibility to block apps during focus, at bedtime and over limits. " +
                "It only sees which app is open, never what's on the screen.")
            Spacer(Modifier.height(4.dp))
            Muted("If Android says it's restricted: App info → ⋮ → Allow restricted settings, then try again.")
            Spacer(Modifier.height(10.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { ctx.startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }) { Text("Open Accessibility") }
                OutlinedButton(onClick = {
                    ctx.startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${ctx.packageName}")))
                }) { Text("App info") }
            }
        }

        SetupItem("Alerts", notify, "Distraction alerts and limit warnings can show") {
            Muted("For \"25 min on Instagram\" nudges, limit warnings and the bedtime reminder.")
            Spacer(Modifier.height(10.dp))
            Button(onClick = { if (Build.VERSION.SDK_INT >= 33) notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS) }) {
                Text("Allow notifications")
            }
        }

        SetupItem("Adult content filter", dnsHost != null, "On · via ${dnsHost ?: ""}") {
            Muted("Blocks adult sites in every browser and app, using Android's Private DNS with a family filter. Nothing on your screen is read.")
            dnsOther?.let {
                Spacer(Modifier.height(4.dp))
                Text("Private DNS is set to $it right now, which doesn't filter adult content.",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
            }
            Spacer(Modifier.height(10.dp))
            Text("1. Copy the address  2. Settings → Connection & sharing → Private DNS → Private DNS provider hostname  3. Paste and Save",
                style = MaterialTheme.typography.bodySmall)
            Text(Dns.FAMILY_HOST, fontWeight = FontWeight.SemiBold, color = MaterialTheme.colorScheme.primary,
                modifier = Modifier.padding(vertical = 8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { Dns.copyHost(ctx); copied = true }) { Text(if (copied) "Copied ✓" else "Copy address") }
                OutlinedButton(onClick = { Dns.openSettings(ctx) }) { Text("Open settings") }
            }
        }

        if (isXiaomi() || !battery) {
            SetupItem("Keep it running", battery, "Battery: no restrictions") {
                Muted("So syncs and blocking keep working in the background. Missed syncs catch up when you open the app.")
                Spacer(Modifier.height(10.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    if (!battery) Button(onClick = { requestBattery(ctx) }) { Text("No restrictions") }
                    if (isXiaomi()) OutlinedButton(onClick = { openXiaomiAutostart(ctx) }) { Text("Autostart") }
                }
                if (isXiaomi()) Muted("Also: in Recents, long-press Good ol Tracker and tap the lock.", Modifier.padding(top = 8.dp))
            }
        }
    }
}

@SuppressLint("BatteryLife")
private fun requestBattery(ctx: Context) {
    runCatching {
        ctx.startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${ctx.packageName}")))
    }.onFailure { ctx.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) }
}

private fun openXiaomiAutostart(ctx: Context) {
    val tries = listOf(
        Intent().setComponent(ComponentName("com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity")),
        Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${ctx.packageName}")),
    )
    for (i in tries) if (runCatching { ctx.startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }.isSuccess) return
}
