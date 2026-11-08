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
 * Runs while you've switched it on in Settings > Accessibility. It only looks at WHICH app is
 * in front (the window's package name), never at what's on screen.
 *  - Blocks apps during focus, at bedtime and once their daily limit is used up (Guard).
 *  - Every 30 s while the screen is on: re-checks the app in front (a limit can run out, or
 *    bedtime start, while you're in it), limit warnings, wind-down, scheduled focus, widget.
 *  - Every minute while the screen is on: syncs with the PC.
 */
class FocusService : AccessibilityService() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    @Volatile private var currentPkg: String? = null
    private var lastBlockPkg = ""
    private var lastBlockAt = 0L

    override fun onServiceConnected() {
        scope.launch {
            var tick = 0
            var nextSync = 0L
            while (isActive) {
                delay(TICK_MS)
                val screenOn = (getSystemService(POWER_SERVICE) as PowerManager).isInteractive
                if (!screenOn) continue
                tick++
                runCatching {
                    Guard.usageToday(this@FocusService, fresh = true)
                    currentPkg?.let { pkg -> Guard.check(this@FocusService, pkg)?.let { block(pkg, it) } }
                    Guard.tick(this@FocusService, currentPkg)
                    if (tick % 2 == 0) TodayWidget.refresh(this@FocusService)
                }
                val prefs = Prefs(this@FocusService)
                val now = System.currentTimeMillis()
                if (prefs.paired && now >= nextSync) {
                    nextSync = try {
                        Net.sync(this@FocusService, prefs); now + SYNC_EVERY_MS - 1000
                    } catch (e: Exception) {
                        prefs.lastError = e.message
                        now + BACKOFF_MS  // PC not around: try again less often
                    }
                }
            }
        }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event?.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) return
        val pkg = event.packageName?.toString() ?: return
        if (pkg == packageName || pkg == "com.android.systemui") return
        currentPkg = pkg
        scope.launch { Guard.check(this@FocusService, pkg)?.let { block(pkg, it) } }
    }

    private fun block(pkg: String, v: Verdict) {
        val now = System.currentTimeMillis()
        if (pkg == lastBlockPkg && now - lastBlockAt < 1500) return  // the same opening, reported twice
        lastBlockPkg = pkg; lastBlockAt = now
        if (v.reason == "focus") Focus.recordBlock(this, pkg)
        currentPkg = null
        performGlobalAction(GLOBAL_ACTION_HOME)
        startActivity(Intent(this, BlockActivity::class.java)
            .putExtra("pkg", pkg).putExtra("reason", v.reason).putExtra("title", v.title)
            .putExtra("detail", v.detail).putExtra("until", v.untilMs).putExtra("grace", v.graceKey)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP))
    }

    override fun onInterrupt() {}

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        const val TICK_MS = 30_000L
        const val SYNC_EVERY_MS = 60_000L
        const val BACKOFF_MS = 5 * 60_000L
    }
}
