package com.goodol.tracker

import android.Manifest
import android.annotation.SuppressLint
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import kotlinx.coroutines.delay
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
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
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
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : ComponentActivity() {
    private val resumed = mutableIntStateOf(0)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        if (Prefs(this).paired) SyncWorker.schedule(this)
        setContent { AppTheme { Screen(resumed.intValue) } }
    }

    override fun onResume() {
        super.onResume()
        resumed.intValue++  // re-check permissions after coming back from Settings
    }
}

@Composable
fun AppTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val ctx = LocalContext.current
    val scheme = when {
        Build.VERSION.SDK_INT >= 31 -> if (dark) dynamicDarkColorScheme(ctx) else dynamicLightColorScheme(ctx)
        dark -> darkColorScheme(primary = Color(0xFF5B9BEA))
        else -> lightColorScheme(primary = Color(0xFF2A78D6))
    }
    MaterialTheme(colorScheme = scheme, content = content)
}

private fun fmtDur(ms: Long): String {
    val m = ms / 60_000
    return if (m >= 60) "${m / 60}h ${m % 60}m" else "${m}m"
}

private fun isXiaomi() = Build.MANUFACTURER.lowercase().let { it.contains("xiaomi") || it.contains("redmi") || it.contains("poco") }

private fun ignoringBattery(ctx: Context): Boolean =
    (ctx.getSystemService(Context.POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(ctx.packageName)

@Composable
fun Screen(resumeTick: Int) {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    val scope = rememberCoroutineScope()
    var access by remember { mutableStateOf(Usage.hasAccess(ctx)) }
    var paired by remember { mutableStateOf(prefs.paired) }
    var pair by remember { mutableStateOf<PairState>(PairState.Idle) }
    var syncing by remember { mutableStateOf(false) }
    var syncMsg by remember { mutableStateOf<String?>(null) }
    var today by remember { mutableStateOf<Triple<Long, Int, List<Pair<String, Long>>>?>(null) }
    var battery by remember { mutableStateOf(ignoringBattery(ctx)) }
    var tick by remember { mutableIntStateOf(0) }

    LaunchedEffect(resumeTick, tick) {
        access = Usage.hasAccess(ctx)
        paired = prefs.paired
        battery = ignoringBattery(ctx)
        if (access) today = withContext(Dispatchers.IO) { runCatching { Usage.today(ctx) }.getOrNull() }
    }

    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(
            Modifier.safeDrawingPadding().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("Good ol Tracker", fontSize = 24.sp, fontWeight = FontWeight.SemiBold)
            Text("Sends which apps you use and when to your PC's tracker. Never what's inside them.",
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)

            // 1. Usage access
            StepCard("1", "Usage access", done = access) {
                if (access) Text("Allowed. The app can read which apps you used.")
                else {
                    Text("Android needs your OK before an app can read app usage. Find Good ol Tracker in the list and switch it on.")
                    Spacer(Modifier.height(8.dp))
                    Button(onClick = { ctx.startActivity(Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS)) }) { Text("Allow usage access") }
                }
            }

            // 2. Connect to the PC
            StepCard("2", "Your PC", done = paired) {
                if (paired) {
                    Text("Connected to ${prefs.pcName ?: "your PC"}")
                    val last = prefs.lastSyncMs
                    Text(
                        if (last > 0) "Last sync " + DateUtils.getRelativeTimeSpanString(last) else "Waiting for the first sync…",
                        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    prefs.lastError?.let {
                        Text("Last try: $it", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
                    }
                    syncMsg?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                        Button(enabled = !syncing && access, onClick = {
                            syncing = true; syncMsg = null
                            scope.launch {
                                syncMsg = withContext(Dispatchers.IO) {
                                    try { "Sent ${Net.sync(ctx, prefs)} app sessions." }
                                    catch (e: Exception) { prefs.lastError = e.message; "Couldn't sync: ${e.message}" }
                                }
                                syncing = false; tick++
                            }
                        }) { Text(if (syncing) "Syncing…" else "Sync now") }
                        TextButton(onClick = { prefs.forget(); paired = false; pair = PairState.Idle }) { Text("Disconnect") }
                    }
                } else {
                    when (val s = pair) {
                        PairState.Idle -> {
                            Text("Connect the laptop to this phone's hotspot (or the same Wi-Fi) with the tracker running, then:")
                            Spacer(Modifier.height(8.dp))
                            Button(enabled = access, onClick = {
                                scope.launch { Pairing.run(ctx) { st -> scope.launch { pair = st; if (st is PairState.Done) { paired = true; tick++ } } } }
                            }) { Text("Connect to PC") }
                        }
                        PairState.Searching -> Row(verticalAlignment = Alignment.CenterVertically) {
                            CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                            Spacer(Modifier.width(10.dp)); Text("Looking for your PC…")
                        }
                        is PairState.Waiting -> Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.fillMaxWidth()) {
                            Text("On the PC dashboard (Phone card), allow the request showing this code:", textAlign = TextAlign.Center)
                            Text(s.code, fontSize = 44.sp, fontWeight = FontWeight.Bold, letterSpacing = 8.sp,
                                modifier = Modifier.padding(vertical = 8.dp))
                            Text("Found ${s.pcName}", style = MaterialTheme.typography.bodySmall)
                            Spacer(Modifier.height(8.dp))
                            LinearProgressIndicator(Modifier.fillMaxWidth())
                        }
                        is PairState.Failed -> {
                            Text(s.message, color = MaterialTheme.colorScheme.error)
                            Spacer(Modifier.height(8.dp))
                            OutlinedButton(onClick = { pair = PairState.Idle }) { Text("Try again") }
                        }
                        PairState.Done -> Text("Connected.")
                    }
                }
            }

            // Focus mode
            FocusCard(tick) { tick++ }

            // Blocking + alerts setup
            val notifLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { tick++ }
            val blocking = Focus.blockingEnabled(ctx)
            val notify = Focus.canNotify(ctx)
            StepCard("★", "Blocking and alerts", done = blocking && notify) {
                if (blocking) Text("App blocking is on. Distracting apps close during focus sessions.")
                else {
                    Text("To block apps during focus, switch on \"Good ol Tracker focus mode\" in Accessibility. " +
                        "It only sees which app is open, never what's on the screen.")
                    Spacer(Modifier.height(4.dp))
                    Text("If Android says it's restricted: App info → ⋮ (top right) → Allow restricted settings, then try again.",
                        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(onClick = { ctx.startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }) { Text("Open Accessibility") }
                        OutlinedButton(onClick = {
                            ctx.startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${ctx.packageName}")))
                        }) { Text("App info") }
                    }
                }
                Spacer(Modifier.height(8.dp))
                if (notify) Text("Distraction alerts can show here (\"25 min on Instagram\").")
                else OutlinedButton(onClick = {
                    if (Build.VERSION.SDK_INT >= 33) notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                }) { Text("Allow alert notifications") }
            }

            // 3. Keep it running (Xiaomi kills background apps)
            if (isXiaomi() || !battery) {
                StepCard("3", "Keep it running", done = battery) {
                    Text("So syncs happen in the background. Missed ones aren't lost: it catches up when you open the app.")
                    Spacer(Modifier.height(8.dp))
                    if (!battery) OutlinedButton(onClick = { requestBattery(ctx) }) { Text("Battery: no restrictions") }
                    if (isXiaomi()) {
                        OutlinedButton(onClick = { openXiaomiAutostart(ctx) }) { Text("Allow autostart") }
                        Text("Also: in Recents, long-press Good ol Tracker and tap the lock.",
                            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }

            // Today on this phone
            today?.let { (total, unlocks, top) ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp)) {
                        Text("Today on this phone", fontWeight = FontWeight.SemiBold)
                        Row(verticalAlignment = Alignment.Bottom) {
                            Text(fmtDur(total), fontSize = 32.sp, fontWeight = FontWeight.SemiBold)
                            Spacer(Modifier.width(8.dp))
                            Text("· $unlocks unlocks", color = MaterialTheme.colorScheme.onSurfaceVariant,
                                modifier = Modifier.padding(bottom = 6.dp))
                        }
                        val max = top.maxOfOrNull { it.second }?.coerceAtLeast(1) ?: 1
                        top.forEach { (name, ms) ->
                            Spacer(Modifier.height(6.dp))
                            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                                Text(name, maxLines = 1); Text(fmtDur(ms), color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                            LinearProgressIndicator(progress = { ms.toFloat() / max }, modifier = Modifier.fillMaxWidth().height(4.dp))
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun FocusCard(tick: Int, onChange: () -> Unit) {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    val scope = rememberCoroutineScope()
    var busy by remember { mutableStateOf(false) }
    var msg by remember { mutableStateOf<String?>(null) }
    var left by remember { mutableStateOf(0L) }
    var armed by remember { mutableStateOf(false) }

    LaunchedEffect(tick) {
        while (true) {
            left = (prefs.focusEndSec - System.currentTimeMillis() / 1000.0).toLong()
            if (left <= 0) break
            delay(1000)
        }
    }
    LaunchedEffect(armed) { if (armed) { delay(4000); armed = false } }

    Card(Modifier.fillMaxWidth(), colors = CardDefaults.cardColors(
        containerColor = if (left > 0) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surfaceVariant)) {
        Column(Modifier.padding(16.dp)) {
            Text("Focus mode", fontWeight = FontWeight.SemiBold)
            if (left > 0) {
                val h = left / 3600; val m = (left % 3600) / 60; val s = left % 60
                Text(if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%02d:%02d".format(m, s),
                    fontSize = 40.sp, fontWeight = FontWeight.SemiBold)
                Text(buildString {
                    append("left")
                    if (prefs.focusLabel.isNotBlank()) append(" · ${prefs.focusLabel}")
                    if (prefs.focusLocal) append(" · phone only")
                }, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(8.dp))
                TextButton(enabled = !busy, onClick = {
                    if (!armed) { armed = true; return@TextButton }
                    busy = true
                    scope.launch {
                        msg = withContext(Dispatchers.IO) { Focus.stop(ctx) }
                        busy = false; armed = false; onChange()
                    }
                }) { Text(if (armed) "Sure? Tap again to end it" else "End early") }
            } else {
                Text("Blocks Facebook, Instagram, YouTube and your other distracting apps here, and distracting sites on the PC.",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(8.dp))
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    listOf(25, 50, 90).forEach { mins ->
                        OutlinedButton(enabled = !busy, modifier = Modifier.weight(1f),
                            contentPadding = PaddingValues(horizontal = 4.dp, vertical = 8.dp), onClick = {
                            busy = true
                            scope.launch {
                                msg = withContext(Dispatchers.IO) { Focus.start(ctx, mins) }
                                busy = false; onChange()
                            }
                        }) { Text("$mins min", maxLines = 1) }
                    }
                }
            }
            msg?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
        }
    }
}

@Composable
fun StepCard(n: String, title: String, done: Boolean, content: @Composable () -> Unit) {
    Card(
        Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = if (done) MaterialTheme.colorScheme.surfaceVariant else MaterialTheme.colorScheme.secondaryContainer),
    ) {
        Column(Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(if (done) "✓" else n, fontWeight = FontWeight.Bold,
                    color = if (done) Color(0xFF1BAF7A) else MaterialTheme.colorScheme.primary)
                Spacer(Modifier.width(10.dp))
                Text(title, fontWeight = FontWeight.SemiBold)
            }
            Spacer(Modifier.height(6.dp))
            content()
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
