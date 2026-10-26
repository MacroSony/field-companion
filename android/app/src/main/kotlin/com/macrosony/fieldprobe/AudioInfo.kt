package com.macrosony.fieldprobe

import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.MediaRecorder
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject

/** Human-readable helpers for Android audio routing state. */
object AudioInfo {

    fun typeName(type: Int): String = when (type) {
        AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "builtin_earpiece"
        AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "builtin_speaker"
        AudioDeviceInfo.TYPE_BUILTIN_SPEAKER_SAFE -> "builtin_speaker_safe"
        AudioDeviceInfo.TYPE_BUILTIN_MIC -> "builtin_mic"
        AudioDeviceInfo.TYPE_WIRED_HEADSET -> "wired_headset"
        AudioDeviceInfo.TYPE_WIRED_HEADPHONES -> "wired_headphones"
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "bluetooth_sco"
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "bluetooth_a2dp"
        AudioDeviceInfo.TYPE_BLE_HEADSET -> "ble_headset"
        AudioDeviceInfo.TYPE_BLE_SPEAKER -> "ble_speaker"
        AudioDeviceInfo.TYPE_BLE_BROADCAST -> "ble_broadcast"
        AudioDeviceInfo.TYPE_HEARING_AID -> "hearing_aid"
        AudioDeviceInfo.TYPE_USB_DEVICE -> "usb_device"
        AudioDeviceInfo.TYPE_USB_HEADSET -> "usb_headset"
        AudioDeviceInfo.TYPE_USB_ACCESSORY -> "usb_accessory"
        AudioDeviceInfo.TYPE_HDMI -> "hdmi"
        AudioDeviceInfo.TYPE_TELEPHONY -> "telephony"
        AudioDeviceInfo.TYPE_REMOTE_SUBMIX -> "remote_submix"
        AudioDeviceInfo.TYPE_FM_TUNER -> "fm_tuner"
        28 -> "echo_reference" // TYPE_ECHO_REFERENCE is @SystemApi
        AudioDeviceInfo.TYPE_DOCK -> "dock"
        AudioDeviceInfo.TYPE_IP -> "ip"
        AudioDeviceInfo.TYPE_BUS -> "bus"
        else -> "type$type"
    }

    fun modeName(mode: Int): String = when (mode) {
        AudioManager.MODE_NORMAL -> "NORMAL"
        AudioManager.MODE_RINGTONE -> "RINGTONE"
        AudioManager.MODE_IN_CALL -> "IN_CALL"
        AudioManager.MODE_IN_COMMUNICATION -> "IN_COMMUNICATION"
        AudioManager.MODE_CALL_SCREENING -> "CALL_SCREENING"
        else -> "mode$mode"
    }

    val SOURCES: List<Pair<String, Int>> = listOf(
        "UNPROCESSED" to MediaRecorder.AudioSource.UNPROCESSED,
        "VOICE_RECOGNITION" to MediaRecorder.AudioSource.VOICE_RECOGNITION,
        "MIC" to MediaRecorder.AudioSource.MIC,
        "CAMCORDER" to MediaRecorder.AudioSource.CAMCORDER,
        "VOICE_PERFORMANCE" to MediaRecorder.AudioSource.VOICE_PERFORMANCE,
        "VOICE_COMMUNICATION (call mode test)" to MediaRecorder.AudioSource.VOICE_COMMUNICATION,
    )

    fun sourceName(source: Int): String =
        SOURCES.firstOrNull { it.second == source }?.first?.substringBefore(" ") ?: "source$source"

    fun encodingName(enc: Int): String = when (enc) {
        AudioFormat.ENCODING_PCM_16BIT -> "pcm16"
        AudioFormat.ENCODING_PCM_8BIT -> "pcm8"
        AudioFormat.ENCODING_PCM_FLOAT -> "pcm_float"
        AudioFormat.ENCODING_PCM_24BIT_PACKED -> "pcm24_packed"
        AudioFormat.ENCODING_PCM_32BIT -> "pcm32"
        else -> "enc$enc"
    }

    fun shortLabel(d: AudioDeviceInfo?): String {
        if (d == null) return "(system default)"
        val name = d.productName?.toString().orEmpty()
        return "${typeName(d.type)} · $name · id=${d.id}"
    }

    fun describe(d: AudioDeviceInfo?): JSONObject {
        val o = JSONObject()
        if (d == null) {
            o.put("id", JSONObject.NULL)
            return o
        }
        o.put("id", d.id)
        o.put("type", typeName(d.type))
        o.put("product_name", d.productName?.toString() ?: "")
        o.put("address", d.address ?: "")
        o.put("is_source", d.isSource)
        o.put("is_sink", d.isSink)
        o.put("sample_rates", JSONArray(d.sampleRates.toList()))
        o.put("channel_counts", JSONArray(d.channelCounts.toList()))
        o.put("encodings", JSONArray(d.encodings.map { encodingName(it) }))
        return o
    }

    /** Snapshot of the global audio routing state that tells us whether we fell into call mode. */
    fun systemState(am: AudioManager): JSONObject {
        val o = JSONObject()
        o.put("mode", modeName(am.mode))
        o.put("communication_device", describe(am.communicationDevice))
        @Suppress("DEPRECATION")
        o.put("bluetooth_sco_on", am.isBluetoothScoOn)
        o.put("speakerphone_on", am.isSpeakerphoneOn)
        o.put("mic_muted", am.isMicrophoneMute)
        o.put("music_active", am.isMusicActive)
        return o
    }

    fun systemStateLine(am: AudioManager): String {
        val comm = am.communicationDevice
        @Suppress("DEPRECATION")
        val sco = am.isBluetoothScoOn
        return "mode=${modeName(am.mode)}  comm=${comm?.let { typeName(it.type) } ?: "none"}  sco=$sco"
    }

    fun buildInfo(): JSONObject = JSONObject().apply {
        put("manufacturer", Build.MANUFACTURER)
        put("model", Build.MODEL)
        put("device", Build.DEVICE)
        put("sdk_int", Build.VERSION.SDK_INT)
        put("release", Build.VERSION.RELEASE)
        put("display", Build.DISPLAY)
    }

    fun properties(am: AudioManager): JSONObject = JSONObject().apply {
        put("unprocessed_supported", am.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED) ?: "null")
        put("output_sample_rate", am.getProperty(AudioManager.PROPERTY_OUTPUT_SAMPLE_RATE) ?: "null")
        put("output_frames_per_buffer", am.getProperty(AudioManager.PROPERTY_OUTPUT_FRAMES_PER_BUFFER) ?: "null")
        put("mic_near_ultrasound", am.getProperty(AudioManager.PROPERTY_SUPPORT_MIC_NEAR_ULTRASOUND) ?: "null")
    }

    fun fullReport(am: AudioManager): JSONObject = JSONObject().apply {
        put("kind", "device_report")
        put("app_version", BuildConfigLite.VERSION)
        put("created_at", Times.isoNow())
        put("build", buildInfo())
        put("properties", properties(am))
        put("system_state", systemState(am))
        put("inputs", JSONArray(am.getDevices(AudioManager.GET_DEVICES_INPUTS).map { describe(it) }))
        put("outputs", JSONArray(am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).map { describe(it) }))
    }
}

object BuildConfigLite {
    const val VERSION = "0.2.0"
}

object Times {
    private val iso = java.time.format.DateTimeFormatter.ISO_OFFSET_DATE_TIME
    private val stamp = java.time.format.DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss")
    fun isoNow(): String = java.time.OffsetDateTime.now().format(iso)
    fun stampNow(): String = java.time.LocalDateTime.now().format(stamp)
}
