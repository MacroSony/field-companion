package com.macrosony.fieldprobe

import android.os.Handler
import android.os.Looper

/** In-process bridge between RecorderService (audio thread) and MainActivity (UI). */
object ProbeState {
    data class Levels(
        val rmsDb: DoubleArray,
        val peakDb: DoubleArray,
        val frames: Long,
        val silenced: Boolean?,
        val clipped: Long,
    )

    private val main = Handler(Looper.getMainLooper())
    private val lines = ArrayDeque<String>()

    @Volatile var recording = false
    @Volatile var sessionName: String? = null
    @Volatile var routedInput: String = ""

    /** Called on the main thread. */
    var levelListener: ((Levels) -> Unit)? = null
    var logListener: ((String) -> Unit)? = null
    var stateListener: (() -> Unit)? = null

    /** Set by the service while recording; invoked from any thread. */
    @Volatile var marker: ((String) -> Unit)? = null

    fun postLevels(l: Levels) = main.post { levelListener?.invoke(l) }

    fun stateChanged() = main.post { stateListener?.invoke() }

    fun log(msg: String) {
        val line = "${java.time.LocalTime.now().withNano(0)}  $msg"
        main.post {
            lines.addLast(line)
            while (lines.size > 300) lines.removeFirst()
            logListener?.invoke(line)
        }
    }

    fun logText(): String = lines.joinToString("\n")

    fun mark(label: String): Boolean {
        val m = marker ?: return false
        m(label)
        return true
    }
}
