package com.macrosony.fieldprobe

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioRouting
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import android.view.KeyEvent
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executor
import kotlin.math.abs
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.sqrt

/**
 * Foreground microphone service: records raw PCM to rolling WAV segments, logs every
 * routing / audio-mode change, and accepts timestamped markers (UI, notification,
 * headset media buttons). Nothing leaves the phone; finished sessions are copied to
 * Download/FieldProbe.
 */
class RecorderService : Service() {

    companion object {
        const val ACTION_START = "start"
        const val ACTION_STOP = "stop"
        const val ACTION_MARK = "mark"
        const val EXTRA_INPUT_ID = "input_id"
        const val EXTRA_OUTPUT_ID = "output_id"
        const val EXTRA_SOURCE = "source"
        const val EXTRA_CHANNELS = "channels"
        const val EXTRA_SEGMENT_MIN = "segment_min"
        private const val CHANNEL_ID = "recording"
        private const val NOTIF_ID = 7
        private const val SAMPLE_RATE = 48_000
    }

    private lateinit var am: AudioManager
    private lateinit var cbThread: HandlerThread
    private lateinit var cbHandler: Handler
    private lateinit var cbExecutor: Executor

    @Volatile private var running = false
    private var worker: Thread? = null
    private var rec: AudioRecord? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var session: MediaSession? = null

    private lateinit var dir: File
    private lateinit var name: String
    private val events = JSONArray()
    private val meta = JSONObject()
    private val segments = JSONArray()
    @Volatile private var totalFrames = 0L
    private var startElapsed = 0L
    private var channels = 2
    private var outputId = -1

    private val modeListener = AudioManager.OnModeChangedListener { mode ->
        event("audio_mode", JSONObject().put("mode", AudioInfo.modeName(mode)))
        ProbeState.log("audio mode -> ${AudioInfo.modeName(mode)}")
    }
    private val commListener = AudioManager.OnCommunicationDeviceChangedListener { d ->
        event("communication_device", AudioInfo.describe(d))
        ProbeState.log("communication device -> ${AudioInfo.shortLabel(d)}")
    }
    private val routeListener = AudioRouting.OnRoutingChangedListener { router ->
        val d = router.routedDevice
        ProbeState.routedInput = AudioInfo.shortLabel(d)
        event("input_route", AudioInfo.describe(d))
        ProbeState.log("input routed -> ${AudioInfo.shortLabel(d)}")
        ProbeState.stateChanged()
    }
    private val devicesAtStart = mutableSetOf<Int>()
    private val deviceCallback = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(added: Array<out AudioDeviceInfo>) {
            // The first callback replays every existing device; only log genuinely new ones.
            for (d in added) if (devicesAtStart.add(d.id)) {
                event("device_added", AudioInfo.describe(d))
                ProbeState.log("device added: ${AudioInfo.shortLabel(d)}")
            }
        }
        override fun onAudioDevicesRemoved(removed: Array<out AudioDeviceInfo>) {
            for (d in removed) {
                devicesAtStart.remove(d.id)
                event("device_removed", AudioInfo.describe(d))
                ProbeState.log("device removed: ${AudioInfo.shortLabel(d)}")
            }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        am = getSystemService(AudioManager::class.java)
        cbThread = HandlerThread("probe-callbacks").also { it.start() }
        cbHandler = Handler(cbThread.looper)
        cbExecutor = Executor { cbHandler.post(it) }
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Recording", NotificationManager.IMPORTANCE_LOW)
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> if (!running) start(intent)
            ACTION_STOP -> stopRecording("stop_requested")
            ACTION_MARK -> mark("notification")
        }
        return START_NOT_STICKY
    }

    @SuppressLint("MissingPermission")
    private fun start(intent: Intent) {
        val inputId = intent.getIntExtra(EXTRA_INPUT_ID, -1)
        outputId = intent.getIntExtra(EXTRA_OUTPUT_ID, -1)
        val source = intent.getIntExtra(EXTRA_SOURCE, android.media.MediaRecorder.AudioSource.UNPROCESSED)
        channels = intent.getIntExtra(EXTRA_CHANNELS, 2)
        val segmentMin = intent.getIntExtra(EXTRA_SEGMENT_MIN, 30)

        name = "probe-" + Times.stampNow()
        dir = File(getExternalFilesDir("sessions"), name).also { it.mkdirs() }
        startForeground(NOTIF_ID, notification("Starting…"), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)

        val preferred = am.getDevices(AudioManager.GET_DEVICES_INPUTS).firstOrNull { it.id == inputId }
        val mask = if (channels == 2) AudioFormat.CHANNEL_IN_STEREO else AudioFormat.CHANNEL_IN_MONO
        val fmt = AudioFormat.Builder()
            .setSampleRate(SAMPLE_RATE)
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setChannelMask(mask)
            .build()
        val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, mask, AudioFormat.ENCODING_PCM_16BIT)
        val r = try {
            AudioRecord.Builder()
                .setAudioSource(source)
                .setAudioFormat(fmt)
                .setBufferSizeInBytes(max(minBuf * 4, SAMPLE_RATE * channels * 2 / 2))
                .build()
        } catch (e: Exception) {
            ProbeState.log("AudioRecord build failed: $e")
            stopSelf()
            return
        }
        if (r.state != AudioRecord.STATE_INITIALIZED) {
            ProbeState.log("AudioRecord not initialized (source=${AudioInfo.sourceName(source)})")
            r.release()
            stopSelf()
            return
        }
        if (preferred != null) r.setPreferredDevice(preferred)
        r.addOnRoutingChangedListener(routeListener, cbHandler)
        rec = r

        meta.put("kind", "recording_session")
        meta.put("app_version", BuildConfigLite.VERSION)
        meta.put("session", name)
        meta.put("started_at", Times.isoNow())
        meta.put("build", AudioInfo.buildInfo())
        meta.put("properties", AudioInfo.properties(am))
        meta.put("requested", JSONObject().apply {
            put("source", AudioInfo.sourceName(source))
            put("sample_rate", SAMPLE_RATE)
            put("channels", channels)
            put("preferred_input", AudioInfo.describe(preferred))
            put("cue_output_id", outputId)
            put("segment_minutes", segmentMin)
        })
        meta.put("system_state_start", AudioInfo.systemState(am))
        val allDevices = am.getDevices(AudioManager.GET_DEVICES_ALL)
        devicesAtStart.clear()
        allDevices.forEach { devicesAtStart.add(it.id) }
        meta.put("devices_at_start", JSONArray(allDevices.map { AudioInfo.describe(it) }))

        am.addOnModeChangedListener(cbExecutor, modeListener)
        am.addOnCommunicationDeviceChangedListener(cbExecutor, commListener)
        am.registerAudioDeviceCallback(deviceCallback, cbHandler)

        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "FieldProbe:recording")
            .also { it.acquire(12 * 60 * 60 * 1000L) }

        startElapsed = SystemClock.elapsedRealtime()
        totalFrames = 0
        try {
            r.startRecording()
        } catch (e: Exception) {
            ProbeState.log("startRecording failed: $e")
            stopRecording("start_failed")
            return
        }
        running = true
        ProbeState.recording = true
        ProbeState.sessionName = name
        ProbeState.marker = { label -> mark(label) }
        event("recording_started", JSONObject().put("record_state", r.recordingState))
        ProbeState.log("recording $name  source=${AudioInfo.sourceName(source)} ch=$channels")
        ProbeState.stateChanged()
        setupMediaSession()
        cue()

        val segmentFrames = segmentMin.toLong() * 60 * SAMPLE_RATE
        worker = Thread({ loop(r, segmentFrames) }, "probe-recorder").also { it.start() }
        updateNotification()
    }

    private fun loop(r: AudioRecord, segmentFrames: Long) {
        val block = SAMPLE_RATE / 10 * channels // 100 ms
        val buf = ShortArray(block)
        var seg = 0
        var writer = openSegment(seg)
        var segStart = 0L
        var lastSlow = SystemClock.elapsedRealtime()
        var lastSilenced: Boolean? = null
        var actualLogged = false
        var clipped = 0L
        try {
            while (running) {
                val n = r.read(buf, 0, buf.size, AudioRecord.READ_BLOCKING)
                if (n < 0) {
                    event("read_error", JSONObject().put("code", n))
                    ProbeState.log("read error $n")
                    Thread.sleep(50)
                    continue
                }
                if (n == 0) continue
                writer.write(buf, n)
                val frames = n / channels
                totalFrames += frames

                val sumSq = DoubleArray(channels)
                val peak = IntArray(channels)
                for (i in 0 until frames * channels) {
                    val c = i % channels
                    val v = buf[i].toInt()
                    sumSq[c] += v.toDouble() * v
                    val a = abs(v)
                    if (a > peak[c]) peak[c] = a
                    if (a >= 32767) clipped++
                }
                val rms = DoubleArray(channels) { db(sqrt(sumSq[it] / frames)) }
                val pk = DoubleArray(channels) { db(peak[it].toDouble()) }

                val now = SystemClock.elapsedRealtime()
                if (now - lastSlow >= 1000) {
                    lastSlow = now
                    if (!actualLogged) {
                        actualLogged = true
                        meta.put("actual", JSONObject().apply {
                            put("sample_rate", r.sampleRate)
                            put("channel_count", r.channelCount)
                            put("encoding", AudioInfo.encodingName(r.audioFormat))
                            put("audio_session_id", r.audioSessionId)
                            put("routed_input", AudioInfo.describe(r.routedDevice))
                        })
                        ProbeState.routedInput = AudioInfo.shortLabel(r.routedDevice)
                        ProbeState.stateChanged()
                    }
                    val silenced = am.activeRecordingConfigurations
                        .firstOrNull { it.clientAudioSessionId == r.audioSessionId }?.isClientSilenced
                    if (silenced != lastSilenced) {
                        lastSilenced = silenced
                        event("client_silenced", JSONObject().put("silenced", silenced ?: JSONObject.NULL))
                        ProbeState.log("client silenced = $silenced")
                    }
                    if ((now - startElapsed) % 10_000 < 1000) {
                        writer.patchHeader()
                        saveMeta(final = false)
                    }
                }
                ProbeState.postLevels(ProbeState.Levels(rms, pk, totalFrames, lastSilenced, clipped))

                if (totalFrames - segStart >= segmentFrames) {
                    closeSegment(writer, segStart)
                    seg += 1
                    segStart = totalFrames
                    writer = openSegment(seg)
                    ProbeState.log("new segment ${writer.file.name}")
                }
            }
        } catch (e: Exception) {
            event("recorder_exception", JSONObject().put("error", e.toString()))
            ProbeState.log("recorder exception: $e")
        } finally {
            closeSegment(writer, segStart)
        }
    }

    private fun openSegment(index: Int): WavWriter =
        WavWriter(File(dir, "%s_%02d.wav".format(name, index)), SAMPLE_RATE, channels)

    private fun closeSegment(w: WavWriter, startFrame: Long) {
        try { w.close() } catch (_: Exception) {}
        synchronized(segments) {
            segments.put(JSONObject().apply {
                put("file", w.file.name)
                put("start_frame", startFrame)
                put("frames", w.framesWritten)
            })
        }
    }

    private fun db(v: Double): Double = if (v <= 0.0) -120.0 else max(-120.0, 20 * log10(v / 32768.0))

    private fun event(type: String, detail: JSONObject) {
        val e = JSONObject().apply {
            put("type", type)
            put("t_ms", if (startElapsed == 0L) 0 else SystemClock.elapsedRealtime() - startElapsed)
            put("frame", totalFrames)
            put("wall", Times.isoNow())
            put("detail", detail)
        }
        synchronized(events) { events.put(e) }
    }

    private fun mark(source: String) {
        if (!running) return
        event("mark", JSONObject().put("source", source))
        ProbeState.log("MARK ($source) at ${"%.1f".format(totalFrames / SAMPLE_RATE.toDouble())} s")
        cue()
    }

    private fun cue() {
        val out = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).firstOrNull { it.id == outputId }
        TonePlayer.play(TonePlayer.chime(), out) { routed ->
            event("cue_output_route", AudioInfo.describe(routed))
        }
    }

    private fun setupMediaSession() {
        session = MediaSession(this, "FieldProbe").apply {
            setCallback(object : MediaSession.Callback() {
                override fun onMediaButtonEvent(mediaButtonIntent: Intent): Boolean {
                    val ke = mediaButtonIntent.getParcelableExtra(Intent.EXTRA_KEY_EVENT, KeyEvent::class.java)
                    if (ke != null && ke.action == KeyEvent.ACTION_DOWN && ke.repeatCount == 0) {
                        mark("media_button:" + KeyEvent.keyCodeToString(ke.keyCode))
                    }
                    return true
                }
            }, cbHandler)
            setPlaybackState(
                PlaybackState.Builder()
                    .setActions(
                        PlaybackState.ACTION_PLAY or PlaybackState.ACTION_PAUSE or
                            PlaybackState.ACTION_PLAY_PAUSE or PlaybackState.ACTION_SKIP_TO_NEXT or
                            PlaybackState.ACTION_SKIP_TO_PREVIOUS
                    )
                    .setState(PlaybackState.STATE_PLAYING, 0, 1f)
                    .build()
            )
            isActive = true
        }
    }

    private fun stopRecording(reason: String) {
        val wasRunning = running
        running = false
        worker?.join(3000)
        worker = null
        rec?.let {
            try { it.stop() } catch (_: Exception) {}
            it.removeOnRoutingChangedListener(routeListener)
            it.release()
        }
        rec = null
        session?.release()
        session = null
        try {
            am.removeOnModeChangedListener(modeListener)
            am.removeOnCommunicationDeviceChangedListener(commListener)
            am.unregisterAudioDeviceCallback(deviceCallback)
        } catch (_: Exception) {}
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        ProbeState.marker = null
        ProbeState.recording = false

        if (wasRunning) {
            event("recording_stopped", JSONObject().put("reason", reason))
            meta.put("system_state_end", AudioInfo.systemState(am))
            saveMeta(final = true)
            exportSession()
        }
        ProbeState.stateChanged()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun saveMeta(final: Boolean) {
        meta.put("ended_at", if (final) Times.isoNow() else JSONObject.NULL)
        meta.put("total_frames", totalFrames)
        meta.put("duration_s", totalFrames / SAMPLE_RATE.toDouble())
        synchronized(segments) { meta.put("segments", JSONArray(segments.toString())) }
        synchronized(events) { meta.put("events", JSONArray(events.toString())) }
        File(dir, "$name.json").writeText(meta.toString(2))
    }

    private fun exportSession() {
        val ctx = applicationContext
        Thread {
            try {
                val files = dir.listFiles()?.sortedBy { it.name } ?: emptyList()
                for (f in files) {
                    val mime = if (f.name.endsWith(".wav")) "audio/wav" else "application/json"
                    val dest = Exporter.export(ctx, f, mime)
                    ProbeState.log("saved $dest")
                }
            } catch (e: Exception) {
                ProbeState.log("export failed: $e (files remain in ${dir.absolutePath})")
            }
        }.start()
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, RecorderService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE
        )
        val markPi = PendingIntent.getService(
            this, 2, Intent(this, RecorderService::class.java).setAction(ACTION_MARK), PendingIntent.FLAG_IMMUTABLE
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("Field Probe recording")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "Mark", markPi).build())
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
    }

    private fun updateNotification() {
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, notification(name))
    }

    override fun onDestroy() {
        if (running) stopRecording("service_destroyed")
        cbThread.quitSafely()
        super.onDestroy()
    }
}
