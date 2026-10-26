package com.macrosony.fieldprobe

import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.Handler
import android.os.Looper
import kotlin.math.PI
import kotlin.math.min
import kotlin.math.sin

/**
 * Plays short cues as *media* (USAGE_MEDIA) so a Bluetooth headset should stay on A2DP.
 * Reports which output device the track was actually routed to.
 */
object TonePlayer {
    private const val RATE = 48_000
    private val main = Handler(Looper.getMainLooper())

    /** Two-note chime, ~0.35 s. */
    fun chime(): ShortArray = concat(tone(880.0, 0.15), silence(0.04), tone(1320.0, 0.16))

    /** Sequential notes (frequency Hz to seconds), used for server earcons. */
    fun notes(seq: List<Pair<Double, Double>>): ShortArray = concat(*seq.map { tone(it.first, it.second) }.toTypedArray())

    /** Longer sweep for checking routing/latency by ear (~3 s). */
    fun sweep(): ShortArray {
        val n = (RATE * 3.0).toInt()
        val out = ShortArray(n)
        var phase = 0.0
        for (i in 0 until n) {
            val t = i.toDouble() / n
            val f = 500.0 * Math.pow(16.0, t) // 500 Hz -> 8 kHz
            phase += 2 * PI * f / RATE
            out[i] = (sin(phase) * 0.25 * envelope(i, n) * Short.MAX_VALUE).toInt().toShort()
        }
        return out
    }

    fun play(pcm: ShortArray, preferred: AudioDeviceInfo?, onRouted: (AudioDeviceInfo?) -> Unit = {}) {
        val attrs = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_MEDIA)
            .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
            .build()
        val fmt = AudioFormat.Builder()
            .setSampleRate(RATE)
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
            .build()
        val track = AudioTrack.Builder()
            .setAudioAttributes(attrs)
            .setAudioFormat(fmt)
            .setTransferMode(AudioTrack.MODE_STATIC)
            .setBufferSizeInBytes(pcm.size * 2)
            .build()
        track.write(pcm, 0, pcm.size)
        if (preferred != null) track.setPreferredDevice(preferred)
        track.play()
        val durMs = pcm.size * 1000L / RATE
        main.postDelayed({ onRouted(track.routedDevice) }, min(150L, durMs / 2))
        main.postDelayed({
            try { track.stop() } catch (_: IllegalStateException) {}
            track.release()
        }, durMs + 400)
    }

    private fun tone(freq: Double, seconds: Double): ShortArray {
        val n = (RATE * seconds).toInt()
        return ShortArray(n) { i ->
            (sin(2 * PI * freq * i / RATE) * 0.3 * envelope(i, n) * Short.MAX_VALUE).toInt().toShort()
        }
    }

    private fun silence(seconds: Double) = ShortArray((RATE * seconds).toInt())

    private fun envelope(i: Int, n: Int): Double {
        val ramp = RATE / 200 // 5 ms
        return when {
            i < ramp -> i.toDouble() / ramp
            i > n - ramp -> (n - i).toDouble() / ramp
            else -> 1.0
        }
    }

    private fun concat(vararg parts: ShortArray): ShortArray {
        val out = ShortArray(parts.sumOf { it.size })
        var o = 0
        for (p in parts) { p.copyInto(out, o); o += p.size }
        return out
    }
}
