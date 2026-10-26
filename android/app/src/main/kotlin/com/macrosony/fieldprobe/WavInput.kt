package com.macrosony.fieldprobe

import android.os.SystemClock
import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * "Fake microphone" for repeatable emulator tests: streams a PCM16 WAV file in real time
 * (blocking like AudioRecord.read), optionally looping. Only 16-bit PCM is supported.
 */
class WavInput(file: File, private val loop: Boolean = true) {
    private val raf = RandomAccessFile(file, "r")
    val sampleRate: Int
    val channels: Int
    private val dataStart: Long
    private val dataEnd: Long
    private var framesDelivered = 0L
    private val t0 = SystemClock.elapsedRealtimeNanos()
    private var bytes = ByteArray(0)

    init {
        val head = ByteArray(12)
        raf.readFully(head)
        require(String(head, 0, 4) == "RIFF" && String(head, 8, 4) == "WAVE") { "not a WAV file" }
        var rate = 0
        var ch = 0
        var bits = 0
        var start = -1L
        var len = 0L
        while (raf.filePointer < raf.length()) {
            val hdr = ByteArray(8)
            raf.readFully(hdr)
            val id = String(hdr, 0, 4)
            val size = ByteBuffer.wrap(hdr, 4, 4).order(ByteOrder.LITTLE_ENDIAN).int.toLong() and 0xFFFFFFFFL
            if (id == "fmt ") {
                val fmt = ByteArray(size.toInt())
                raf.readFully(fmt)
                val b = ByteBuffer.wrap(fmt).order(ByteOrder.LITTLE_ENDIAN)
                val format = b.short.toInt()
                ch = b.short.toInt()
                rate = b.int
                b.int; b.short
                bits = b.short.toInt()
                require(format == 1 || format == -2) { "only PCM WAV supported" }
            } else if (id == "data") {
                start = raf.filePointer
                len = minOf(size, raf.length() - start)
                break
            } else {
                raf.seek(raf.filePointer + size + (size and 1))
            }
        }
        require(start >= 0 && bits == 16 && ch in 1..2) { "need 16-bit mono/stereo PCM (bits=$bits ch=$ch)" }
        sampleRate = rate
        channels = ch
        dataStart = start
        dataEnd = start + len
        raf.seek(dataStart)
    }

    /** Fills [buf] with up to [count] interleaved samples, pacing to real time. Returns samples read, or -1 at EOF. */
    fun read(buf: ShortArray, count: Int): Int {
        val wantFrames = count / channels
        // pace: don't deliver ahead of wall clock
        val dueNs = t0 + (framesDelivered + wantFrames) * 1_000_000_000L / sampleRate
        val waitNs = dueNs - SystemClock.elapsedRealtimeNanos()
        if (waitNs > 0) Thread.sleep(waitNs / 1_000_000, (waitNs % 1_000_000).toInt())

        val needBytes = wantFrames * channels * 2
        if (bytes.size < needBytes) bytes = ByteArray(needBytes)
        var got = 0
        while (got < needBytes) {
            if (raf.filePointer >= dataEnd) {
                if (!loop) break
                raf.seek(dataStart)
            }
            val n = raf.read(bytes, got, minOf(needBytes - got, (dataEnd - raf.filePointer).toInt()))
            if (n <= 0) break
            got += n
        }
        if (got == 0) return -1
        val samples = got / 2
        ByteBuffer.wrap(bytes, 0, samples * 2).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().get(buf, 0, samples)
        framesDelivered += samples / channels
        return samples
    }

    fun release() = raf.close()
}
