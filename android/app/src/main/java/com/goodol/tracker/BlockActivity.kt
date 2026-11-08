package com.goodol.tracker

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** Shown instead of a blocked app: during focus, at bedtime, or when its daily limit is used up. */
class BlockActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val pkg = intent.getStringExtra("pkg") ?: ""
        val reason = intent.getStringExtra("reason") ?: "focus"
        val title = intent.getStringExtra("title") ?: "${Usage.label(this, pkg)} is blocked"
        val detail = intent.getStringExtra("detail") ?: ""
        val until = intent.getLongExtra("until", 0L)
        val grace = intent.getStringExtra("grace")
        setContent {
            AppTheme {
                BlockScreen(pkg, reason, title, detail, until, grace,
                    onDone = { goHome() },
                    onGrace = {
                        // Reopen the app it blocked, now that it's allowed for 5 minutes.
                        packageManager.getLaunchIntentForPackage(pkg)?.let { startActivity(it) }
                        finish()
                    })
            }
        }
    }

    private fun goHome() {
        startActivity(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        finish()
    }
}

private fun clock(sec: Long): String {
    val s = sec.coerceAtLeast(0)
    val h = s / 3600; val m = (s % 3600) / 60; val r = s % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, r) else "%02d:%02d".format(m, r)
}

@Composable
fun BlockScreen(pkg: String, reason: String, title: String, detail: String, untilMs: Long, graceKey: String?,
                onDone: () -> Unit, onGrace: () -> Unit) {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    val scope = rememberCoroutineScope()
    val endMs = { if (reason == "focus") (prefs.focusEndSec * 1000).toLong() else untilMs }
    var left by remember { mutableLongStateOf((endMs() - System.currentTimeMillis()) / 1000) }
    var armed by remember { mutableStateOf(false) }
    var msg by remember { mutableStateOf<String?>(null) }
    val canGrace = remember { reason != "focus" && Guard.canGrace(prefs, graceKey) }

    LaunchedEffect(Unit) {
        while (true) {
            left = (endMs() - System.currentTimeMillis()) / 1000
            if (left <= 0) break
            delay(1000)
        }
    }
    LaunchedEffect(armed) { if (armed) { delay(4000); armed = false } }

    val kicker = when (reason) {
        "bedtime" -> "BEDTIME"
        "limit" -> "DAILY LIMIT"
        else -> if (left > 0) "FOCUS MODE" else "FOCUS SESSION OVER"
    }
    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(
            Modifier.safeDrawingPadding().padding(28.dp).fillMaxSize(),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(kicker, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary, letterSpacing = 2.sp)
            Spacer(Modifier.height(12.dp))
            Text(title, fontSize = 26.sp, fontWeight = FontWeight.SemiBold, textAlign = TextAlign.Center)
            if (detail.isNotBlank()) {
                Spacer(Modifier.height(10.dp))
                Text(detail, textAlign = TextAlign.Center, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(24.dp))
            if (left > 0) {
                Text(clock(left), fontSize = 52.sp, fontWeight = FontWeight.SemiBold)
                Text(if (reason == "focus") "left" else "until it opens", color = MaterialTheme.colorScheme.onSurfaceVariant)
            } else {
                Text("You can open it now.", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(36.dp))
            Button(onClick = onDone, modifier = Modifier.fillMaxWidth()) {
                Text(if (reason == "bedtime") "Put the phone down" else "Back to work")
            }
            if (canGrace && left > 0 && graceKey != null) {
                Spacer(Modifier.height(8.dp))
                OutlinedButton(onClick = { Guard.grace(prefs, graceKey); onGrace() }, modifier = Modifier.fillMaxWidth()) {
                    Text("5 more minutes (once ${if (reason == "bedtime") "tonight" else "today"})")
                }
            }
            if (reason == "focus" && left > 0) {
                Spacer(Modifier.height(8.dp))
                TextButton(onClick = {
                    if (!armed) { armed = true; return@TextButton }
                    scope.launch {
                        msg = withContext(Dispatchers.IO) { Focus.stop(ctx) }
                        left = 0
                    }
                }) { Text(if (armed) "Sure? Tap again to end it" else "End focus early") }
            }
            msg?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
        }
    }
}
