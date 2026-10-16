package org.scanlame.app

import android.os.SystemClock
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.util.Locale

/** Journal du téléphone : mêmes lignes que la page web (temps en secondes depuis le lancement), joint à la prise si demandé. */
object Journal {
    private val t0 = SystemClock.elapsedRealtime()
    private val lines = ArrayList<String>()
    private val _flow = MutableStateFlow<List<String>>(emptyList())
    val flow: StateFlow<List<String>> get() = _flow
    private const val MAX = 6000

    @Synchronized
    fun log(msg: String) {
        val t = (SystemClock.elapsedRealtime() - t0) / 1000.0
        val line = String.format(Locale.ROOT, "%10.3f  %s", t, msg)
        lines.add(line)
        if (lines.size > MAX) lines.subList(0, lines.size - MAX).clear()
        _flow.value = ArrayList(lines)
        android.util.Log.i("scanlame", msg)
    }

    @Synchronized
    fun text(): String = lines.joinToString("\n")

    /** Version pour le serveur : tête et queue si le journal est long (même règle que la page). */
    @Synchronized
    fun forServer(): String =
        if (lines.size <= 1600) lines.joinToString("\n")
        else (lines.take(150) + listOf("[... ${lines.size - 1550} lignes omises ...]") + lines.takeLast(1400)).joinToString("\n")

    @Synchronized
    fun clear() { lines.clear(); _flow.value = emptyList() }
}
