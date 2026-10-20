package org.scanlame.app

import android.graphics.BitmapFactory
import java.io.File
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * Calibration µm/px à partir des hématies d'un frottis sanguin (diamètre moyen 7,5 µm chez l'adulte).
 * Sur une photo pleine résolution : objets plus sombres que le fond, ronds, de taille homogène ; le diamètre
 * équivalent médian des objets retenus donne l'échelle. Aucune dépendance : composantes connexes maison sur
 * une image réduite.
 */
object Calibrate {
    const val RBC_UM = 7.5
    class Result(val n: Int, val diamPx: Double, val umPerPx: Double, val spread: Double, val note: String)

    fun measureRbc(file: File): Result? {
        val opts = BitmapFactory.Options().apply { inSampleSize = 4 }     // ~1000 x 760 : une hématie au x100 fait ~25 px, au x40 ~10 px
        val bm = BitmapFactory.decodeFile(file.path, opts) ?: return null
        val w = bm.width; val h = bm.height; val n = w * h
        val px = IntArray(n); bm.getPixels(px, 0, w, 0, 0, w, h); bm.recycle()
        // luminance + "colore" (ecart entre canaux) : une hematie est a la fois un peu plus sombre et plus saturee que le fond
        val lum = FloatArray(n); val sat = FloatArray(n)
        for (i in 0 until n) {
            val c = px[i]; val r = (c shr 16) and 255; val g = (c shr 8) and 255; val b = c and 255
            lum[i] = (0.299f * r + 0.587f * g + 0.114f * b); sat[i] = (maxOf(r, g, b) - minOf(r, g, b)).toFloat()
        }
        val sortedL = lum.copyOf(); sortedL.sort(); val bg = sortedL[(n * 0.9).toInt()]
        val sortedS = sat.copyOf(); sortedS.sort(); val satBg = sortedS[(n * 0.5).toInt()]
        if (bg < 60) return Result(0, 0.0, 0.0, 0.0, "image trop sombre")
        val mask = BooleanArray(n) { lum[it] < 0.88f * bg || sat[it] > satBg + 18f }
        // composantes connexes (4-voisinage), pile explicite
        val label = IntArray(n); var cur = 0
        val stack = IntArray(n)
        data class Comp(var area: Int, var minX: Int, var maxX: Int, var minY: Int, var maxY: Int, var edge: Int)
        val comps = ArrayList<Comp>()
        for (s in 0 until n) {
            if (!mask[s] || label[s] != 0) continue
            cur++; var sp = 0; stack[sp++] = s; label[s] = cur
            val c = Comp(0, w, 0, h, 0, 0)
            while (sp > 0) {
                val i = stack[--sp]; val x = i % w; val y = i / w
                c.area++; if (x < c.minX) c.minX = x; if (x > c.maxX) c.maxX = x; if (y < c.minY) c.minY = y; if (y > c.maxY) c.maxY = y
                var border = false
                for (d in intArrayOf(-1, 1, -w, w)) {
                    val j = i + d
                    if (j < 0 || j >= n || (d == -1 && x == 0) || (d == 1 && x == w - 1)) { border = true; continue }
                    if (!mask[j]) { border = true; continue }
                    if (label[j] == 0) { label[j] = cur; stack[sp++] = j }
                }
                if (border) c.edge++
            }
            comps.add(c)
        }
        // objets ronds, pleins, ni minuscules ni geants, loin des bords
        val cands = comps.filter { c ->
            val bw = c.maxX - c.minX + 1; val bh = c.maxY - c.minY + 1
            val fill = c.area / (PI / 4 * bw * bh)
            val circ = 4 * PI * c.area / (c.edge * c.edge).toDouble().coerceAtLeast(1.0)   // 1 = cercle (perimetre approche par les pixels de bord)
            c.area in 30..40000 && bw in 6..300 && bh in 6..300 && abs(bw - bh) <= 0.25 * maxOf(bw, bh) && fill in 0.65..1.05 && circ > 0.45 &&
                c.minX > 2 && c.minY > 2 && c.maxX < w - 3 && c.maxY < h - 3
        }
        if (cands.size < 8) return Result(cands.size, 0.0, 0.0, 0.0, "trop peu d'objets ronds (${cands.size}) : champ trop dense, trop vide ou pas une frottis sanguin")
        // diametre equivalent, puis on garde le mode (les hematies sont la population la plus nombreuse et homogene)
        val d = cands.map { 2 * sqrt(it.area / PI) }.sorted()
        var best = 0 to 0.0
        for (i in d.indices) {
            val lo = d[i]; var j = i
            while (j < d.size && d[j] <= lo * 1.3) j++
            if (j - i > best.first) best = (j - i) to lo
        }
        val cluster = d.filter { it >= best.second && it <= best.second * 1.3 }
        val med = cluster[cluster.size / 2]
        val spread = (cluster.last() - cluster.first()) / med
        val diamFull = med * 4   // inSampleSize = 4
        val um = RBC_UM / diamFull
        return Result(cluster.size, diamFull, um, spread, if (cluster.size < 15) "peu d'hématies isolées, mesure approximative" else "")
    }

    fun fmt(v: Double) = String.format(java.util.Locale.ROOT, "%.4f", v)
    fun fmt1(v: Double) = v.roundToInt().toString()
}
