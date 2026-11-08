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
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** Shown instead of a blocked app during a focus session. */
class BlockActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val pkg = intent.getStringExtra("pkg") ?: ""
        val name = if (pkg.isNotEmpty()) Usage.label(this, pkg) else "This app"
        setContent { AppTheme { BlockScreen(name, onDone = { goHome() }) } }
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

@androidx.compose.runtime.Composable
fun BlockScreen(appName: String, onDone: () -> Unit) {
    val ctx = androidx.compose.ui.platform.LocalContext.current
    val prefs = remember { Prefs(ctx) }
    val scope = rememberCoroutineScope()
    var left by remember { mutableLongStateOf(((prefs.focusEndSec - System.currentTimeMillis() / 1000.0)).toLong()) }
    var armed by remember { mutableStateOf(false) }
    var msg by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        while (true) {
            left = (prefs.focusEndSec - System.currentTimeMillis() / 1000.0).toLong()
            if (left <= 0) break
            delay(1000)
        }
    }
    LaunchedEffect(armed) { if (armed) { delay(4000); armed = false } }

    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(
            Modifier.safeDrawingPadding().padding(28.dp).fillMaxSize(),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(if (left > 0) "FOCUS MODE" else "FOCUS SESSION OVER", style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.primary, letterSpacing = 2.sp)
            Spacer(Modifier.height(12.dp))
            Text("$appName is blocked", fontSize = 26.sp, fontWeight = FontWeight.SemiBold, textAlign = TextAlign.Center)
            Spacer(Modifier.height(24.dp))
            if (left > 0) {
                Text(clock(left), fontSize = 56.sp, fontWeight = FontWeight.SemiBold)
                Text("left", color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (prefs.focusLabel.isNotBlank()) {
                    Spacer(Modifier.height(12.dp))
                    Text("Working on: ${prefs.focusLabel}", textAlign = TextAlign.Center)
                }
            } else {
                Text("You can open it now.", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(36.dp))
            Button(onClick = onDone, modifier = Modifier.fillMaxWidth()) { Text("Back to work") }
            if (left > 0) {
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
