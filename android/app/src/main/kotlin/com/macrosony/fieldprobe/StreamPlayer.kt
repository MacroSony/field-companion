package com.macrosony.fieldprobe

import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioTrack
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Streams assistant speech (PCM16 mono 24 kHz) as USAGE_MEDIA so Bluetooth stays on A2DP.
 * Mirrors the web client's playback semantics: `active` is true only while frames are
 * actually being played out (playback head behind the write position), and `drained`
 * becomes true once the server sent audio_end and every written frame has been played.
 */
class StreamPlayer(preferred: AudioDeviceInfo?) {
    companion object { const val RATE = 24_000 }

    private val queue = LinkedBlockingQueue<ShortArray>()
    private val track: AudioTrack
    @Volatile private var running = true
    @Volatile private var written = 0L
    @Volatile private var generation = 0
    private val lock = Object()
    private val writer: Thread

    init {
        val minBuf = AudioTrack.getMinBufferSize(RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        track = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setSampleRate(RATE)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build()
            )
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setBufferSizeInBytes(maxOf(minBuf * 2, RATE / 5 * 2)) // >= 200 ms
            .build()
        if (preferred != null) track.setPreferredDevice(preferred)
        track.play()
        writer = Thread({ writeLoop() }, "stream-player").also { it.start() }
    }

    val routedDevice: AudioDeviceInfo? get() = track.routedDevice

    fun enqueue(pcm: ShortArray) {
        if (pcm.isNotEmpty()) queue.put(pcm)
    }

    /** Frames handed to the track but not yet played, plus anything still queued. */
    fun pendingFrames(): Long {
        synchronized(lock) {
            val head = track.playbackHeadPosition.toLong() and 0xFFFFFFFFL
            return (written - head).coerceAtLeast(0) + queue.sumOf { it.size.toLong() }
        }
    }

    fun isPlaying(): Boolean = pendingFrames() > 0

    /** Drop everything immediately (interruption / server flush). */
    fun flush() {
        synchronized(lock) {
            queue.clear()
            try {
                track.pause()
                track.flush()
                track.stop() // resets playbackHeadPosition to 0
                track.play()
            } catch (_: IllegalStateException) {}
            written = 0
            generation += 1
        }
    }

    fun release() {
        running = false
        writer.interrupt()
        try { writer.join(500) } catch (_: InterruptedException) {}
        try { track.stop() } catch (_: IllegalStateException) {}
        track.release()
    }

    private fun writeLoop() {
        while (running) {
            val chunk = try { queue.poll(100, TimeUnit.MILLISECONDS) } catch (_: InterruptedException) { null } ?: continue
            val gen = generation
            var off = 0
            while (off < chunk.size && running && gen == generation) {
                val n = track.write(chunk, off, minOf(chunk.size - off, RATE / 50), AudioTrack.WRITE_BLOCKING)
                if (n <= 0) break
                synchronized(lock) { if (gen == generation) written += n }
                off += n
            }
        }
    }
}
