package com.goodol.tracker

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import org.json.JSONArray
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.SocketTimeoutException
import java.net.URL
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/** knowsUs = false: it's the PC we paired with (same PC id) but it no longer has our token. */
data class PcAddress(val host: String, val port: Int, val name: String, val network: Network?,
                     val knowsUs: Boolean = true)

class NetError(message: String) : Exception(message)

/**
 * Finding the PC and talking to it. Works when the PC is on this phone's hotspot (the
 * phone is the router) and on a shared Wi-Fi. Only local addresses are ever contacted.
 */
object Net {
    private const val DISCOVERY_PORT = 47324
    private const val LISTEN_MS = 1500

    private fun wifiNetworks(context: Context): List<Network> {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        return cm.allNetworks.filter {
            cm.getNetworkCapabilities(it)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
        }
    }

    /** Wi-Fi, hotspot, USB/Bluetooth tethering. Never mobile data (rmnet/ccmni use 10.x too). */
    private fun localInterface(name: String): Boolean {
        val n = name.lowercase()
        return listOf("wlan", "ap", "swlan", "softap", "wifi", "eth", "rndis", "usb", "bt-pan", "ncm")
            .any { n.startsWith(it) }
    }

    /** The Wi-Fi Network that owns this local address (null for the hotspot side, which
     *  Android doesn't expose as a Network; plain routing reaches it fine). */
    private fun networkFor(context: Context, local: InetAddress): Network? {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        return wifiNetworks(context).firstOrNull { n ->
            cm.getLinkProperties(n)?.linkAddresses?.any { it.address == local } == true
        }
    }

    private fun hmacHex(key: String, msg: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key.toByteArray(), "HmacSHA256"))
        return mac.doFinal(msg.toByteArray()).joinToString("") { "%02x".format(it) }
    }

    /**
     * Broadcast "where is the tracker?" on every local network (hotspot + Wi-Fi) and wait for
     * answers. When paired, only an answer that proves it knows our token is accepted.
     */
    fun discover(context: Context, prefs: Prefs): PcAddress? {
        val nonce = ByteArray(16).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }
        val msg = JSONObject().put("t", "got-discover").put("nonce", nonce).put("device", prefs.deviceId)
            .toString().toByteArray()
        val token = prefs.token

        // (socket, network it's bound to (null = plain routing via that interface's address))
        val sockets = ArrayList<Pair<DatagramSocket, Network?>>()
        val targets = ArrayList<InetAddress>()
        try {
            for (nif in NetworkInterface.getNetworkInterfaces()?.toList().orEmpty()) {
                if (!nif.isUp || nif.isLoopback || !localInterface(nif.name)) continue
                for (ia in nif.interfaceAddresses) {
                    val addr = ia.address as? Inet4Address ?: continue
                    if (!addr.isSiteLocalAddress) continue  // never mobile-data addresses
                    val s = DatagramSocket(InetSocketAddress(addr, 0)).apply { broadcast = true; soTimeout = 200 }
                    // Remember the Wi-Fi Network too: on a Wi-Fi without internet Android sends
                    // unbound traffic over mobile data, so HTTP to the PC must be bound to it.
                    sockets.add(s to runCatching { networkFor(context, addr) }.getOrNull())
                    ia.broadcast?.let { targets.add(it) }
                }
            }
            for (net in wifiNetworks(context)) {
                val s = DatagramSocket().apply { broadcast = true; soTimeout = 200 }
                try { net.bindSocket(s); sockets.add(s to net) } catch (e: Exception) { s.close() }
            }
            targets.add(InetAddress.getByName("255.255.255.255"))
            prefs.lastHost?.let { h -> runCatching { InetAddress.getByName(h) }.getOrNull()?.let { targets.add(it) } }

            for ((s, _) in sockets) for (t in targets.distinct()) {
                runCatching { s.send(DatagramPacket(msg, msg.size, t, DISCOVERY_PORT)) }
            }
            val deadline = System.currentTimeMillis() + LISTEN_MS
            val buf = ByteArray(2048)
            var forgotUs: PcAddress? = null  // our PC answered, but without proof: it was unpaired
            while (System.currentTimeMillis() < deadline) {
                for ((s, net) in sockets) {
                    val pkt = DatagramPacket(buf, buf.size)
                    try { s.receive(pkt) } catch (e: SocketTimeoutException) { continue } catch (e: Exception) { continue }
                    val reply = runCatching { JSONObject(String(pkt.data, 0, pkt.length)) }.getOrNull() ?: continue
                    if (reply.optString("t") != "got-here") continue
                    val host = pkt.address.hostAddress ?: continue
                    if (!pkt.address.isSiteLocalAddress) continue
                    val pc = PcAddress(host, reply.optInt("port", 47323), reply.optString("name", "PC"), net)
                    if (token != null && reply.optString("proof") != hmacHex(token, nonce)) {
                        val ourPc = prefs.pcId
                        if (!ourPc.isNullOrEmpty() && reply.optString("pc") == ourPc) forgotUs = pc.copy(knowsUs = false)
                        continue  // not our PC (or our PC, unpaired): keep listening for a real answer
                    }
                    return pc
                }
            }
            return forgotUs
        } finally {
            sockets.forEach { it.first.close() }
        }
    }

    fun request(pc: PcAddress, path: String, body: JSONObject? = null, token: String? = null,
                timeoutMs: Int = 8000): Pair<Int, JSONObject> {
        val url = URL("http://${pc.host}:${pc.port}$path")
        val conn = (pc.network?.openConnection(url) ?: url.openConnection()) as HttpURLConnection
        try {
            conn.connectTimeout = timeoutMs
            conn.readTimeout = timeoutMs
            conn.setRequestProperty("Content-Type", "application/json")
            token?.let { conn.setRequestProperty("X-Phone-Token", it) }
            if (body != null) {
                conn.requestMethod = "POST"
                conn.doOutput = true
                conn.outputStream.use { it.write(body.toString().toByteArray()) }
            }
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
            return code to (runCatching { JSONObject(text) }.getOrNull() ?: JSONObject())
        } finally {
            conn.disconnect()
        }
    }

    /** The PC we're paired with: last known address if it still answers, else discovery. */
    fun findPc(context: Context, prefs: Prefs): PcAddress? {
        val token = prefs.token ?: return null
        prefs.lastHost?.let { host ->
            for (net in listOf<Network?>(null) + wifiNetworks(context)) {
                val pc = PcAddress(host, prefs.lastPort, prefs.pcName ?: "PC", net)
                val ok = runCatching { request(pc, "/phone/hello", token = token, timeoutMs = 1500).first == 200 }
                    .getOrDefault(false)
                if (ok) return pc
            }
        }
        val pc = discover(context, prefs) ?: return null
        if (!pc.knowsUs) {
            // Our PC is here but doesn't know this phone any more (unpaired on the dashboard).
            // Used to look like "PC not found" forever. Confirm with the PC, then ask to reconnect.
            val unpaired = runCatching { request(pc, "/phone/hello", token = token, timeoutMs = 5000).first == 401 }
                .getOrDefault(false)
            if (!unpaired) return null
            prefs.forget()
            throw NetError("Your PC no longer knows this phone (it was disconnected on the dashboard). Connect again.")
        }
        prefs.lastHost = pc.host
        prefs.lastPort = pc.port
        return pc
    }

    // ---------------------------------------------------------------- sync

    private const val BATCH = 1000
    private const val BACKFILL_MS = 7L * 24 * 3600 * 1000  // Android keeps roughly a week

    /** Start/stop focus or snooze alerts on the PC. Returns its reply (focus state etc). */
    fun focusAction(context: Context, prefs: Prefs, body: JSONObject): JSONObject {
        val pc = findPc(context, prefs) ?: throw NetError("PC not found on this network")
        val (code, res) = request(pc, "/phone/focus", body, prefs.token, timeoutMs = 5000)
        if (code != 200) throw NetError(res.optString("error", "PC answered $code"))
        return res
    }

    /** The Week tab's data (last 7 days, goals, sleep, subjects...). Cached in prefs. */
    fun week(context: Context, prefs: Prefs): JSONObject {
        val pc = findPc(context, prefs) ?: throw NetError("PC not found on this network")
        val (code, res) = request(pc, "/phone/week", token = prefs.token, timeoutMs = 15000)
        if (code != 200) throw NetError(res.optString("error", "PC answered $code"))
        prefs.week = res.toString()
        return res
    }

    /** If you ended a focus session on the phone while the PC was away and the PC still has
     *  it running, end it on the PC now rather than letting this reply switch it back on. */
    private fun endFocusStoppedHere(pc: PcAddress, prefs: Prefs, res: JSONObject): JSONObject {
        val stopped = prefs.stoppedFocusId
        if (stopped == 0L) return res
        val f = res.optJSONObject("focus")
        if (f == null || f.optLong("id") != stopped) {
            prefs.stoppedFocusId = 0L  // it's already over on the PC
            return res
        }
        val r = runCatching {
            request(pc, "/phone/focus", JSONObject().put("action", "stop"), prefs.token, timeoutMs = 5000)
        }.getOrNull()
        if (r != null && r.first == 200) {
            prefs.stoppedFocusId = 0L
            return r.second
        }
        return JSONObject(res.toString()).put("focus", JSONObject.NULL)  // try again next sync
    }

    /** Sends everything since the cursor. Returns how many sessions went over. */
    @Synchronized
    fun sync(context: Context, prefs: Prefs): Int {
        if (!prefs.paired) throw NetError("Not connected to a PC yet")
        if (!Usage.hasAccess(context)) throw NetError("Usage access is off")
        val pc = findPc(context, prefs) ?: throw NetError("PC not found on this network")
        val now = System.currentTimeMillis()
        val from = prefs.cursorMs.takeIf { it > 0 } ?: (now - BACKFILL_MS)
        val data = Usage.collect(context, from, now)
        var sent = 0
        val chunks = data.sessions.chunked(BATCH).ifEmpty { listOf(emptyList()) }
        for ((i, chunk) in chunks.withIndex()) {
            val body = JSONObject()
                .put("sessions", JSONArray().apply {
                    chunk.forEach { s ->
                        put(JSONObject().put("pkg", s.pkg).put("label", s.label).put("start", s.start).put("end", s.end))
                    }
                })
                .put("unlocks", JSONArray().apply { if (i == 0) data.unlocks.forEach { put(it) } })
                .put("focus_blocks", if (i == 0) JSONArray(prefs.pendingBlocks) else JSONArray())
                .put("client", JSONObject().put("version", BuildConfig.VERSION_NAME)
                    .put("behind", (now - from) / 1000)
                    .put("blocking", Focus.blockingEnabled(context))
                    .put("private_dns", Dns.filterHost(context) ?: JSONObject.NULL))
            val (code, res) = request(pc, "/phone/sync", body, prefs.token)
            if (code == 401) {
                // Only forget the pairing if the PC confirms it: a one-off 401 used to wipe it.
                val confirmed = runCatching { request(pc, "/phone/hello", token = prefs.token, timeoutMs = 5000).first == 401 }
                    .getOrDefault(false)
                if (confirmed) { prefs.forget(); throw NetError("The PC no longer knows this phone. Connect again.") }
                throw NetError("The PC didn't recognise this phone just now; will retry")
            }
            if (code != 200) throw NetError(res.optString("error", "PC answered $code"))
            if (i == 0) prefs.pendingBlocks = "[]"
            Focus.applyReply(context, endFocusStoppedHere(pc, prefs, res))  // focus on/off, apps to block, alert to show
            sent += chunk.size
            // Next time, start at the last session sent: if it was still running, it's re-sent
            // with the same start (the PC just extends it). Nothing before it is read again.
            prefs.cursorMs = chunk.lastOrNull()?.start ?: (now - 10_000)
        }
        prefs.lastSyncMs = now
        prefs.lastError = null
        return sent
    }
}
