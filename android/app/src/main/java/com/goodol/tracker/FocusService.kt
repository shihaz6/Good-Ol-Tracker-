package com.goodol.tracker

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.os.PowerManager
import android.view.accessibility.AccessibilityEvent
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Two jobs, both only while you've switched it on in Settings > Accessibility:
 *  1. During a focus session, an app on the block list that comes to the front is sent back
 *     to the home screen and the "blocked" screen is shown. It only looks at WHICH app is in
 *     front (the window's package name), never at what's on screen.
 *  2. While the screen is on, sync with the PC every minute (instead of every 15 min), so
 *     focus starts quickly and distraction alerts can reach the phone in time.
 */
class FocusService : AccessibilityService() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var lastBlockPkg = ""
    private var lastBlockAt = 0L

    override fun onServiceConnected() {
        scope.launch {
            var wait = SYNC_EVERY_MS
            while (isActive) {
                delay(wait)
                val prefs = Prefs(this@FocusService)
                val screenOn = (getSystemService(POWER_SERVICE) as PowerManager).isInteractive
                if (!prefs.paired || !screenOn) { wait = SYNC_EVERY_MS; continue }
                wait = try {
                    Net.sync(this@FocusService, prefs); SYNC_EVERY_MS
                } catch (e: Exception) {
                    prefs.lastError = e.message
                    BACKOFF_MS  // PC not around: try again less often
                }
            }
        }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event?.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) return
        val pkg = event.packageName?.toString() ?: return
        if (pkg == packageName) return
        val prefs = Prefs(this)
        if (!prefs.focusActive || pkg !in prefs.blocked) return
        val now = System.currentTimeMillis()
        if (pkg == lastBlockPkg && now - lastBlockAt < 1500) return  // the same opening, reported twice
        lastBlockPkg = pkg; lastBlockAt = now
        Focus.recordBlock(this, pkg)
        performGlobalAction(GLOBAL_ACTION_HOME)
        startActivity(Intent(this, BlockActivity::class.java)
            .putExtra("pkg", pkg)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP))
    }

    override fun onInterrupt() {}

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        const val SYNC_EVERY_MS = 60_000L
        const val BACKOFF_MS = 5 * 60_000L
    }
}
