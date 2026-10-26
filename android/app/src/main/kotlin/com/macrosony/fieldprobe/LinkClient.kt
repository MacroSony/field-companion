package com.macrosony.fieldprobe

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/**
 * Native "mic" client for the field-companion backend (/ws?role=mic, transport=websocket).
 * Uplink: PCM16LE mono 48 kHz. Downlink: PCM16LE mono 24 kHz between audio_start/audio_end.
 * Reports playback state like web/app.js: {type:'playback', active, response_id, drained}.
 */
class LinkClient(
    private val url: String,
    private val player: StreamPlayer,
    private val onEvent: (type: String, detail: JSONObject) -> Unit,
) {
    companion object {
        const val CLIENT_BUILD = "android-probe-" + BuildConfigLite.VERSION
        private const val MAX_QUEUED_BYTES = 192_000L
    }

    private val http = OkHttpClient.Builder()
        .pingInterval(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()
    private val sched = Executors.newSingleThreadScheduledExecutor { Thread(it, "link-sched") }
    @Volatile private var ws: WebSocket? = null
    @Volatile private var open = false
    @Volatile private var closed = false
    private var reconnectDelayMs = 1000L
    private var reportTask: ScheduledFuture<*>? = null

    // playback state (touched on the scheduler thread + OkHttp reader thread)
    @Volatile private var playbackId: Any? = null
    @Volatile private var playbackComplete = true
    private var lastReport = ""

    @Volatile var droppedFrames = 0L; private set
    @Volatile var sentFrames = 0L; private set

    val isOpen: Boolean get() = open

    fun connect() {
        if (closed) return
        val req = Request.Builder().url(url).build()
        ws = http.newWebSocket(req, Listener())
        if (reportTask == null) {
            reportTask = sched.scheduleWithFixedDelay({ reportPlayback(false) }, 20, 20, TimeUnit.MILLISECONDS)
        }
    }

    fun close() {
        closed = true
        reportTask?.cancel(false)
        ws?.close(1000, "client stop")
        sched.shutdown()
        http.dispatcher.executorService.shutdown()
    }

    /** Send one block of mono 48 kHz PCM16; drops it if the socket is backed up. */
    fun sendPcm(mono: ShortArray, count: Int) {
        val s = ws ?: return
        if (!open) return
        if (s.queueSize() > MAX_QUEUED_BYTES) { droppedFrames += count; return }
        val bb = ByteBuffer.allocate(count * 2).order(ByteOrder.LITTLE_ENDIAN)
        for (i in 0 until count) bb.putShort(mono[i])
        if (s.send(bb.array().toByteString())) sentFrames += count
    }

    fun sendJson(o: JSONObject): Boolean = open && (ws?.send(o.toString()) ?: false)

    /** Push-to-talk / interrupt, same as the web "Tap" button: flush locally, then tell the server. */
    fun tap() {
        localFlush()
        sendJson(JSONObject().put("type", "tap"))
    }

    private fun localFlush() {
        val had = player.isPlaying() || !playbackComplete
        player.flush()
        playbackComplete = true
        if (had) reportPlayback(true, activeOverride = false)
        playbackId = null
        lastReport = ""
    }

    @Synchronized
    private fun reportPlayback(force: Boolean, activeOverride: Boolean? = null) {
        if (!open) return
        val active = activeOverride ?: player.isPlaying()
        val drained = playbackComplete && !player.isPlaying()
        val o = JSONObject()
            .put("type", "playback")
            .put("active", active)
            .put("response_id", playbackId ?: JSONObject.NULL)
            .put("drained", drained)
        val enc = o.toString()
        if (!force && enc == lastReport) return
        lastReport = enc
        ws?.send(enc)
    }

    private fun scheduleReconnect() {
        if (closed) return
        val delay = reconnectDelayMs
        reconnectDelayMs = (reconnectDelayMs * 2).coerceAtMost(15_000)
        onEvent("link_reconnect_scheduled", JSONObject().put("delay_ms", delay))
        try { sched.schedule({ connect() }, delay, TimeUnit.MILLISECONDS) } catch (_: Exception) {}
    }

    private inner class Listener : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            open = true
            reconnectDelayMs = 1000
            webSocket.send(JSONObject().apply {
                put("type", "hello")
                put("client_build", CLIENT_BUILD)
                put("lang", "en")
                put("playback_protocol", 1)
                put("earphones", true)
            }.toString())
            webSocket.send(JSONObject().put("type", "audio_mode").put("earphones", true).toString())
            webSocket.send(JSONObject().put("type", "mic").put("on", true).toString())
            onEvent("link_open", JSONObject().put("url", url))
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            val raw = bytes.toByteArray()
            val n = raw.size / 2
            if (n == 0) return
            val pcm = ShortArray(n)
            ByteBuffer.wrap(raw).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().get(pcm)
            player.enqueue(pcm)
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val m = try { JSONObject(text) } catch (_: Exception) { return }
            when (m.optString("type")) {
                "audio_start" -> { playbackId = m.opt("id"); playbackComplete = false; reportPlayback(false) }
                "audio_end" -> if (m.opt("id") == playbackId) { playbackComplete = true; reportPlayback(false) }
                "flush" -> localFlush()
                "earcon" -> earcon(m.optString("kind"))
            }
            onEvent("server_" + m.optString("type", "unknown"), m)
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(code, reason)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            open = false
            onEvent("link_closed", JSONObject().put("code", code).put("reason", reason))
            scheduleReconnect()
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            open = false
            onEvent("link_failure", JSONObject().put("error", t.toString()))
            scheduleReconnect()
        }
    }

    private fun earcon(kind: String) {
        val pcm = when (kind) {
            "logged" -> TonePlayer.notes(listOf(660.0 to 0.12, 990.0 to 0.11))
            "heard" -> TonePlayer.notes(listOf(1400.0 to 0.11))
            "error" -> TonePlayer.notes(listOf(130.0 to 0.23))
            else -> return
        }
        TonePlayer.play(pcm, null)
    }
}
