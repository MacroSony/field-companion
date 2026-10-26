package com.macrosony.fieldprobe

import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Streaming 16-bit PCM WAV writer. The header is patched periodically so a
 * partially written file (crash, battery death) is still readable.
 */
class WavWriter(val file: File, private val sampleRate: Int, private val channels: Int) {
    private val raf = RandomAccessFile(file, "rw")
    private var dataBytes = 0L
    private var buf = ByteBuffer.allocate(0).order(ByteOrder.LITTLE_ENDIAN)

    init {
        raf.setLength(0)
        raf.write(header(0))
    }

    val framesWritten: Long get() = dataBytes / (2L * channels)

    fun write(samples: ShortArray, count: Int) {
        val bytes = count * 2
        if (buf.capacity() < bytes) buf = ByteBuffer.allocate(bytes).order(ByteOrder.LITTLE_ENDIAN)
        buf.clear()
        for (i in 0 until count) buf.putShort(samples[i])
        raf.write(buf.array(), 0, bytes)
        dataBytes += bytes
    }

    fun patchHeader() {
        val pos = raf.filePointer
        raf.seek(0)
        raf.write(header(dataBytes))
        raf.seek(pos)
    }

    fun close() {
        patchHeader()
        raf.fd.sync()
        raf.close()
    }

    private fun header(dataLen: Long): ByteArray {
        val clamped = dataLen.coerceAtMost(0xFFFFFFFFL - 36)
        val byteRate = sampleRate * channels * 2
        return ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN).apply {
            put("RIFF".toByteArray())
            putInt((36 + clamped).toInt())
            put("WAVE".toByteArray())
            put("fmt ".toByteArray())
            putInt(16)
            putShort(1) // PCM
            putShort(channels.toShort())
            putInt(sampleRate)
            putInt(byteRate)
            putShort((channels * 2).toShort())
            putShort(16)
            put("data".toByteArray())
            putInt(clamped.toInt())
        }.array()
    }
}
