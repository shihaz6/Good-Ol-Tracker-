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

data class PcAddress(val host: String, val port: Int, val name: String, val network: Network?)

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
                    sockets.add(s to null)
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
            while (System.currentTimeMillis() < deadline) {
                for ((s, net) in sockets) {
                    val pkt = DatagramPacket(buf, buf.size)
                    try { s.receive(pkt) } catch (e: SocketTimeoutException) { continue } catch (e: Exception) { continue }
                    val reply = runCatching { JSONObject(String(pkt.data, 0, pkt.length)) }.getOrNull() ?: continue
                    if (reply.optString("t") != "got-here") continue
                    val host = pkt.address.hostAddress ?: continue
                    if (!pkt.address.isSiteLocalAddress) continue
                    if (token != null) {
                        if (reply.optString("proof") != hmacHex(token, nonce)) continue  // not our PC
                    }
                    return PcAddress(host, reply.optInt("port", 47323), reply.optString("name", "PC"), net)
                }
            }
            return null
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
        prefs.lastHost = pc.host
        prefs.lastPort = pc.port
        return pc
    }

    // ---------------------------------------------------------------- sync

    private const val BATCH = 1000
    private const val BACKFILL_MS = 7L * 24 * 3600 * 1000  // Android keeps roughly a week

    /** Sends everything since the cursor. Returns how many sessions went over. */
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
                .put("client", JSONObject().put("version", BuildConfig.VERSION_NAME)
                    .put("behind", (now - from) / 1000))
            val (code, res) = request(pc, "/phone/sync", body, prefs.token)
            if (code == 401) { prefs.forget(); throw NetError("The PC no longer knows this phone. Connect again.") }
            if (code != 200) throw NetError(res.optString("error", "PC answered $code"))
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
