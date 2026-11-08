package io.github.erikiss.steno

import android.content.Context
import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Small in-app log (also sent to Logcat) so problems on the tablet can be read and copied inside the app. */
object AppLog {
    private const val TAG = "StenoLive"
    private const val MAX_LINES = 300
    private val lines = ArrayDeque<String>()
    private var file: File? = null
    private val time = SimpleDateFormat("HH:mm:ss", Locale.GERMANY)

    @Synchronized
    fun init(context: Context) {
        if (file != null) return
        file = File(context.filesDir, "steno.log").also { f ->
            if (f.length() > 512_000) f.delete()
            if (f.exists()) f.readLines().takeLast(MAX_LINES).forEach { lines.addLast(it) }
        }
    }

    fun i(msg: String) = add('I', msg, null)
    fun w(msg: String) = add('W', msg, null)
    fun e(msg: String, t: Throwable? = null) = add('E', msg, t)

    @Synchronized
    private fun add(level: Char, msg: String, t: Throwable?) {
        val text = if (t == null) msg else "$msg – ${t::class.simpleName}: ${t.message}"
        when (level) {
            'E' -> Log.e(TAG, text, t)
            'W' -> Log.w(TAG, text)
            else -> Log.i(TAG, text)
        }
        val line = "${time.format(Date())} $level $text"
        lines.addLast(line)
        while (lines.size > MAX_LINES) lines.removeFirst()
        runCatching { file?.appendText(line + "\n") }
    }

    @Synchronized
    fun text(): String = lines.joinToString("\n")
}
