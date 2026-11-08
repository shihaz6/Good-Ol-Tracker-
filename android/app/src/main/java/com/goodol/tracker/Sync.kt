package com.goodol.tracker

import android.content.Context
import android.os.Build
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.URLEncoder
import java.security.SecureRandom
import java.util.concurrent.TimeUnit

/** Background sync every 15 minutes (Android's minimum). Missed runs just catch up later. */
class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val prefs = Prefs(applicationContext)
        if (!prefs.paired) return@withContext Result.success()
        try {
            Net.sync(applicationContext, prefs)
        } catch (e: Exception) {
            prefs.lastError = e.message ?: e.javaClass.simpleName
        }
        Result.success()  // never "retry": the next periodic run picks everything up anyway
    }

    companion object {
        fun schedule(context: Context) {
            val req = PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES).build()
            WorkManager.getInstance(context)
                .enqueueUniquePeriodicWork("sync", ExistingPeriodicWorkPolicy.KEEP, req)
        }

        fun now(context: Context) {
            WorkManager.getInstance(context)
                .enqueueUniqueWork("sync-now", ExistingWorkPolicy.REPLACE, OneTimeWorkRequestBuilder<SyncWorker>().build())
        }
    }
}

sealed class PairState {
    data object Idle : PairState()
    data object Searching : PairState()
    data class Waiting(val code: String, val pcName: String) : PairState()
    data class Failed(val message: String) : PairState()
    data object Done : PairState()
}

object Pairing {
    /** Find the PC, ask to pair with a 4-digit code, wait until it's allowed on the dashboard. */
    suspend fun run(context: Context, onState: (PairState) -> Unit) = withContext(Dispatchers.IO) {
        val prefs = Prefs(context)
        prefs.forget()
        onState(PairState.Searching)
        val pc = Net.discover(context, prefs)
        if (pc == null) {
            onState(PairState.Failed(
                "Couldn't find the PC. Is the laptop on this phone's hotspot (or the same Wi-Fi), " +
                    "the tracker running, and phone_firewall.ps1 done on the PC?"))
            return@withContext
        }
        val code = "%04d".format(SecureRandom().nextInt(10000))
        val name = "${Build.MANUFACTURER.replaceFirstChar { it.uppercase() }} ${Build.MODEL}".trim()
        val (status, res) = try {
            Net.request(pc, "/phone/pair", JSONObject().put("device", prefs.deviceId).put("name", name).put("code", code))
        } catch (e: Exception) {
            onState(PairState.Failed("Found the PC but couldn't connect: ${e.message}")); return@withContext
        }
        if (status != 200) { onState(PairState.Failed(res.optString("error", "PC said $status"))); return@withContext }
        val requestId = res.getString("request")
        onState(PairState.Waiting(code, pc.name))

        val q = "/phone/pair/status?request=${URLEncoder.encode(requestId, "UTF-8")}&device=${URLEncoder.encode(prefs.deviceId, "UTF-8")}"
        val deadline = System.currentTimeMillis() + 5 * 60_000
        while (System.currentTimeMillis() < deadline) {
            delay(2000)
            val st = runCatching { Net.request(pc, q).second }.getOrNull() ?: continue
            when (st.optString("status")) {
                "allowed" -> {
                    prefs.token = st.getString("token")
                    prefs.pcId = st.optString("pc_id")
                    prefs.pcName = pc.name
                    prefs.lastHost = pc.host
                    prefs.lastPort = pc.port
                    SyncWorker.schedule(context)
                    SyncWorker.now(context)
                    onState(PairState.Done)
                    return@withContext
                }
                "denied" -> { onState(PairState.Failed("Denied on the PC.")); return@withContext }
                "expired" -> { onState(PairState.Failed("The request expired. Try again.")); return@withContext }
            }
        }
        onState(PairState.Failed("Nobody allowed it on the PC within 5 minutes. Try again."))
    }
}
