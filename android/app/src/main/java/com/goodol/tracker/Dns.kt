package com.goodol.tracker

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.os.Build
import android.provider.Settings

/**
 * Adult-content filter for the phone = Android's own Private DNS pointed at a family DNS
 * service. It covers every browser and app (no screen reading), and survives this app
 * being closed. The app can't switch it on by itself (Android doesn't allow that), so it
 * checks whether it's on and walks you through it.
 */
object Dns {
    const val FAMILY_HOST = "family.cloudflare-dns.com"   // Cloudflare for Families: blocks adult + malware
    private val FILTER_WORDS = listOf("family", "adult", "cleanbrowsing", "safe", "kids", "nextdns")

    /** The Private DNS host in use, if it's a filtering one; null otherwise. */
    fun filterHost(context: Context): String? {
        val host = activeHost(context) ?: return null
        return host.takeIf { h -> FILTER_WORDS.any { h.contains(it, ignoreCase = true) } }
    }

    /** Whatever Private DNS host is active (filtering or not). */
    fun activeHost(context: Context): String? {
        if (Build.VERSION.SDK_INT < 28) return null
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val lp = cm.activeNetwork?.let { cm.getLinkProperties(it) } ?: return null
        return if (lp.isPrivateDnsActive) lp.privateDnsServerName else null
    }

    fun copyHost(context: Context) {
        val cb = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cb.setPrimaryClip(ClipData.newPlainText("Private DNS", FAMILY_HOST))
    }

    /** Private DNS lives in different places per brand; try the likely screens in order. */
    fun openSettings(context: Context) {
        val tries = listOf(
            Intent("android.settings.PRIVATE_DNS_SETTINGS"),
            Intent().setClassName("com.android.settings", "com.android.settings.Settings\$NetworkDashboardActivity"),
            Intent(Settings.ACTION_WIRELESS_SETTINGS),
            Intent(Settings.ACTION_SETTINGS),
        )
        for (i in tries) {
            if (runCatching { context.startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }.isSuccess) return
        }
    }
}
