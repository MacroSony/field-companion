package com.macrosony.fieldprobe

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView

class MainActivity : Activity() {

    private lateinit var am: AudioManager
    private val ui = Handler(Looper.getMainLooper())

    private lateinit var status: TextView
    private lateinit var levels: TextView
    private lateinit var logView: TextView
    private lateinit var inSpinner: Spinner
    private lateinit var srcSpinner: Spinner
    private lateinit var chSpinner: Spinner
    private lateinit var outSpinner: Spinner
    private lateinit var startBtn: Button
    private lateinit var stopBtn: Button
    private lateinit var markBtn: Button

    private var inputs: List<AudioDeviceInfo?> = listOf(null)
    private var outputs: List<AudioDeviceInfo?> = listOf(null)

    private val deviceCallback = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(added: Array<out AudioDeviceInfo>) {
            added.forEach { ProbeState.log("+ ${AudioInfo.shortLabel(it)} (${if (it.isSource) "in" else "out"})") }
            refreshDevices()
        }
        override fun onAudioDevicesRemoved(removed: Array<out AudioDeviceInfo>) {
            removed.forEach { ProbeState.log("- ${AudioInfo.shortLabel(it)} (${if (it.isSource) "in" else "out"})") }
            refreshDevices()
        }
    }

    private val ticker = object : Runnable {
        override fun run() {
            renderStatus()
            ui.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        am = getSystemService(AudioManager::class.java)
        setContentView(buildLayout())
        requestNeededPermissions()
        refreshDevices()
        logView.text = ProbeState.logText()
    }

    override fun onStart() {
        super.onStart()
        ProbeState.levelListener = { renderLevels(it) }
        ProbeState.logListener = { line ->
            logView.append(if (logView.text.isEmpty()) line else "\n$line")
        }
        ProbeState.stateListener = { renderStatus() }
        am.registerAudioDeviceCallback(deviceCallback, ui)
        ui.post(ticker)
    }

    override fun onStop() {
        ProbeState.levelListener = null
        ProbeState.logListener = null
        ProbeState.stateListener = null
        am.unregisterAudioDeviceCallback(deviceCallback)
        ui.removeCallbacks(ticker)
        super.onStop()
    }

    // ---------------------------------------------------------------- actions

    private fun startRecording() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestNeededPermissions()
            ProbeState.log("microphone permission missing")
            return
        }
        val input = inputs.getOrNull(inSpinner.selectedItemPosition)
        val output = outputs.getOrNull(outSpinner.selectedItemPosition)
        val source = AudioInfo.SOURCES[srcSpinner.selectedItemPosition].second
        val channels = if (chSpinner.selectedItemPosition == 0) 2 else 1
        val intent = Intent(this, RecorderService::class.java)
            .setAction(RecorderService.ACTION_START)
            .putExtra(RecorderService.EXTRA_INPUT_ID, input?.id ?: -1)
            .putExtra(RecorderService.EXTRA_OUTPUT_ID, output?.id ?: -1)
            .putExtra(RecorderService.EXTRA_SOURCE, source)
            .putExtra(RecorderService.EXTRA_CHANNELS, channels)
        startForegroundService(intent)
    }

    private fun stopRecording() {
        startService(Intent(this, RecorderService::class.java).setAction(RecorderService.ACTION_STOP))
    }

    private fun playCue(sweep: Boolean) {
        val output = outputs.getOrNull(outSpinner.selectedItemPosition)
        val before = AudioInfo.systemStateLine(am)
        TonePlayer.play(if (sweep) TonePlayer.sweep() else TonePlayer.chime(), output) { routed ->
            ProbeState.log("cue routed -> ${AudioInfo.shortLabel(routed)}  [$before]")
        }
    }

    private fun saveReport() {
        Thread {
            try {
                val text = AudioInfo.fullReport(am).toString(2)
                val dest = Exporter.exportText(applicationContext, "report-${Times.stampNow()}.json", text)
                ProbeState.log("saved $dest")
            } catch (e: Exception) {
                ProbeState.log("report failed: $e")
            }
        }.start()
    }

    // ---------------------------------------------------------------- rendering

    private fun refreshDevices() {
        val prevIn = inputs.getOrNull(inSpinner.selectedItemPosition)?.id
        val prevOut = outputs.getOrNull(outSpinner.selectedItemPosition)?.id
        val ins = am.getDevices(AudioManager.GET_DEVICES_INPUTS).toList()
        val outs = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).toList()
        inputs = listOf<AudioDeviceInfo?>(null) + ins
        outputs = listOf<AudioDeviceInfo?>(null) + outs
        inSpinner.adapter = adapter(inputs.map { AudioInfo.shortLabel(it) })
        outSpinner.adapter = adapter(outputs.map { AudioInfo.shortLabel(it) })

        val usb = setOf(AudioDeviceInfo.TYPE_USB_DEVICE, AudioDeviceInfo.TYPE_USB_HEADSET, AudioDeviceInfo.TYPE_USB_ACCESSORY)
        val inIdx = inputs.indexOfFirst { it != null && it.id == prevIn }.takeIf { it >= 0 }
            ?: inputs.indexOfFirst { it != null && it.type in usb }.takeIf { it >= 0 } ?: 0
        val outIdx = outputs.indexOfFirst { it != null && it.id == prevOut }.takeIf { it >= 0 }
            ?: outputs.indexOfFirst { it != null && it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP }.takeIf { it >= 0 } ?: 0
        inSpinner.setSelection(inIdx)
        outSpinner.setSelection(outIdx)
        renderStatus()
    }

    private fun renderStatus() {
        val rec = if (ProbeState.recording) "REC ${ProbeState.sessionName}" else "idle"
        val unprocessed = am.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED)
        status.text = buildString {
            append(rec).append('\n')
            append(AudioInfo.systemStateLine(am)).append('\n')
            append("unprocessed_supported=").append(unprocessed).append('\n')
            if (ProbeState.recording) append("routed input: ").append(ProbeState.routedInput)
        }
        startBtn.isEnabled = !ProbeState.recording
        stopBtn.isEnabled = ProbeState.recording
        markBtn.isEnabled = ProbeState.recording
    }

    private fun renderLevels(l: ProbeState.Levels) {
        val sb = StringBuilder()
        val names = if (l.rmsDb.size == 2) listOf("L", "R") else listOf("M")
        for (i in l.rmsDb.indices) {
            sb.append("%s %6.1f/%6.1f %s\n".format(names[i], l.rmsDb[i], l.peakDb[i], bar(l.rmsDb[i])))
        }
        if (l.rmsDb.size == 2) sb.append("L-R  %+6.1f dB\n".format(l.rmsDb[0] - l.rmsDb[1]))
        sb.insert(0, "  rms/peak dBFS\n")
        sb.append("t %.1f s  silenced=%s  clip=%d".format(l.frames / 48_000.0, l.silenced, l.clipped))
        levels.text = sb.toString()
    }

    private fun bar(db: Double): String {
        val n = (((db + 80.0) / 80.0) * 14).toInt().coerceIn(0, 14)
        return "#".repeat(n) + ".".repeat(14 - n)
    }

    private fun adapter(items: List<String>) =
        ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, items)

    private fun requestNeededPermissions() {
        val wanted = arrayOf(
            Manifest.permission.RECORD_AUDIO,
            Manifest.permission.POST_NOTIFICATIONS,
            Manifest.permission.BLUETOOTH_CONNECT,
        ).filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (wanted.isNotEmpty()) requestPermissions(wanted.toTypedArray(), 1)
    }

    // ---------------------------------------------------------------- layout

    private fun buildLayout(): View {
        val pad = (12 * resources.displayMetrics.density).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        fun label(text: String) = TextView(this).apply {
            this.text = text
            setPadding(0, pad, 0, pad / 4)
            setTypeface(typeface, Typeface.BOLD)
        }
        fun mono() = TextView(this).apply {
            typeface = Typeface.MONOSPACE
            textSize = 12f
            setTextIsSelectable(true)
        }
        fun row(vararg buttons: Button) = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            buttons.forEach { addView(it, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)) }
        }
        fun button(text: String, onClick: () -> Unit) = Button(this).apply {
            this.text = text
            isAllCaps = false
            setOnClickListener { onClick() }
        }

        root.addView(TextView(this).apply {
            text = "Field Probe ${BuildConfigLite.VERSION}"
            textSize = 20f
        })
        status = mono()
        root.addView(status)

        root.addView(label("Input"))
        inSpinner = Spinner(this); root.addView(inSpinner)
        root.addView(label("Source / channels"))
        srcSpinner = Spinner(this).apply { adapter = adapter(AudioInfo.SOURCES.map { it.first }) }
        root.addView(srcSpinner)
        chSpinner = Spinner(this).apply { adapter = adapter(listOf("Stereo (2 ch)", "Mono (1 ch)")) }
        root.addView(chSpinner)

        startBtn = button("● Record") { startRecording() }
        stopBtn = button("■ Stop") { stopRecording() }
        markBtn = button("Mark") { if (!ProbeState.mark("button")) ProbeState.log("not recording") }
        root.addView(row(startBtn, stopBtn, markBtn))

        levels = mono().apply { textSize = 13f; text = "levels appear while recording" }
        root.addView(levels)

        root.addView(label("Cue output"))
        outSpinner = Spinner(this); root.addView(outSpinner)
        root.addView(row(
            button("Chime") { playCue(false) },
            button("Sweep 3 s") { playCue(true) },
        ))
        root.addView(row(
            button("Refresh devices") { refreshDevices() },
            button("Save device report") { saveReport() },
        ))

        root.addView(label("Log"))
        logView = mono()
        root.addView(logView)

        return ScrollView(this).apply {
            addView(root)
            // targetSdk 36 is always edge-to-edge: keep content clear of the status/navigation bars.
            setOnApplyWindowInsetsListener { v, insets ->
                val bars = insets.getInsets(android.view.WindowInsets.Type.systemBars())
                v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
                insets
            }
        }
    }
}
