package org.scanlame.app

import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * Suivi de position du champ (portage du worker de la page web).
 * Chaque image est ramenée à un carré gris 256 x 256 (le rapport d'aspect est restitué en convertissant
 * les déplacements en pixels natifs). Pyramide : L0 64 px, L1 128 px, L2 256 px.
 * Convention : v = argmax NCC tel que B(p + v) ~= A(p). La scène a bougé de +v de A vers B,
 * donc l'image B se place en posA - v sur la carte.
 */
class Tracker {
    companion object {
        const val S2 = 256; const val S1 = 128; const val S0 = 64

        fun downsample(src: FloatArray, s: Int): FloatArray {
            val d = s shr 1; val out = FloatArray(d * d)
            for (y in 0 until d) for (x in 0 until d) {
                val i = 2 * y * s + 2 * x
                out[y * d + x] = (src[i] + src[i + 1] + src[i + s] + src[i + s + 1]) * 0.25f
            }
            return out
        }
        private fun downsampleMask(src: ByteArray, s: Int): ByteArray {
            val d = s shr 1; val out = ByteArray(d * d)
            for (y in 0 until d) for (x in 0 until d) {
                val i = 2 * y * s + 2 * x
                out[y * d + x] = if (src[i].toInt() != 0 && src[i + 1].toInt() != 0 && src[i + s].toInt() != 0 && src[i + s + 1].toInt() != 0) 1 else 0
            }
            return out
        }
        fun pyramid(g256: FloatArray): Array<FloatArray> {
            val l1 = downsample(g256, S2); val l0 = downsample(l1, S1)
            return arrayOf(l0, l1, g256)
        }
        fun maskPyramid(bright256: ByteArray): Array<ByteArray> {
            val er = ByteArray(S2 * S2)
            for (y in 0 until S2) for (x in 0 until S2) {
                var ok = true
                var dy = -6
                while (dy <= 6 && ok) {
                    var dx = -6
                    while (dx <= 6) {
                        val yy = y + dy; val xx = x + dx
                        if (yy < 0 || yy >= S2 || xx < 0 || xx >= S2 || bright256[yy * S2 + xx].toInt() == 0) { ok = false; break }
                        dx += 3
                    }
                    dy += 3
                }
                er[y * S2 + x] = if (ok) 1 else 0
            }
            val m1 = downsampleMask(er, S2); val m0 = downsampleMask(m1, S1)
            return arrayOf(m0, m1, er)
        }

        /** Corrélation croisée normalisée (moyenne nulle) sur le recouvrement masqué pour un décalage v. */
        fun nccAt(a: FloatArray, b: FloatArray, m: ByteArray, s: Int, vx: Int, vy: Int): Float {
            val x0 = maxOf(0, -vx); val x1 = minOf(s, s - vx); val y0 = maxOf(0, -vy); val y1 = minOf(s, s - vy)
            var n = 0; var sa = 0.0; var sb = 0.0; var saa = 0.0; var sbb = 0.0; var sab = 0.0
            for (y in y0 until y1) {
                var ia = y * s + x0; var ib = (y + vy) * s + x0 + vx
                for (x in x0 until x1) {
                    if (m[ia].toInt() != 0 && m[ib].toInt() != 0) {
                        val va = a[ia].toDouble(); val vb = b[ib].toDouble()
                        n++; sa += va; sb += vb; saa += va * va; sbb += vb * vb; sab += va * vb
                    }
                    ia++; ib++
                }
            }
            if (n < s * s * 0.2) return -1f
            val cov = sab - sa * sb / n; val va = saa - sa * sa / n; val vb = sbb - sb * sb / n
            if (va <= 1e-6 || vb <= 1e-6) return -1f
            return (cov / sqrt(va * vb)).toFloat()
        }

        class Match(var vx: Int, var vy: Int, var score: Float)

        fun search(a: FloatArray, b: FloatArray, m: ByteArray, s: Int, cx: Int, cy: Int, r: Int, step: Int = 1): Match {
            val best = Match(cx, cy, -2f)
            val lim = (s * 0.8).toInt()
            var vy = cy - r
            while (vy <= cy + r) {
                var vx = cx - r
                while (vx <= cx + r) {
                    if (abs(vx) <= lim && abs(vy) <= lim) {
                        val sc = nccAt(a, b, m, s, vx, vy)
                        if (sc > best.score) { best.vx = vx; best.vy = vy; best.score = sc }
                    }
                    vx += step
                }
                vy += step
            }
            return best
        }

        /** Du grossier au fin. guess en unités L2 (256 px). Retourne le décalage en unités L2 + score. */
        fun match(pyrA: Array<FloatArray>, pyrB: Array<FloatArray>, masks: Array<ByteArray>, gvx: Int = 0, gvy: Int = 0, r0: Int = 32): Match {
            var b = search(pyrA[0], pyrB[0], masks[0], S0, (gvx / 4.0).roundToInt(), (gvy / 4.0).roundToInt(), r0, 2)
            if (b.score < 0) return Match(0, 0, -1f)
            b = search(pyrA[0], pyrB[0], masks[0], S0, b.vx, b.vy, 2)
            val gx = (gvx / 4.0).roundToInt(); val gy = (gvy / 4.0).roundToInt()
            val onEdge = abs(b.vx - gx) >= r0 || abs(b.vy - gy) >= r0
            b = search(pyrA[1], pyrB[1], masks[1], S1, b.vx * 2, b.vy * 2, 3)
            b = search(pyrA[2], pyrB[2], masks[2], S2, b.vx * 2, b.vy * 2, 2)
            if (onEdge && b.score < 0.7f) b.score = minOf(b.score, 0.3f)
            return b
        }
    }

    class Frame(val i: Int, var x: Double, var y: Double, var score: Float, var status: String, var pyr: Array<FloatArray>?)

    val frames = ArrayList<Frame>()
    private val keys = ArrayList<Frame>()
    private var masks: Array<ByteArray>? = null
    private var last: Frame? = null
    var relocations = 0; private set          // nombre de relocalisations acceptees (journal, bandeau)
    private var lostFrames = 0                // images consecutives perdues
    private var scanCursor = 0                // balayage progressif des cles pendant une perte longue
    private var pendingReloc: Triple<Double, Double, Float>? = null   // relocalisation candidate a confirmer par l'image suivante
    private var forceKey = false
    /** La prochaine image devient une image cle (appele a chaque photo : nettes, a l'arret, bien reparties). */
    fun markKey() { forceKey = true }

    fun setMask(bright256: ByteArray) { masks = maskPyramid(bright256) }
    fun hasMask() = masks != null

    /** Ajoute une image (gris 256 x 256) et renvoie sa position en unités L2. */
    fun add(g256: FloatArray): Frame {
        val pyr = pyramid(g256)
        val f = Frame(frames.size, 0.0, 0.0, 1f, "ok", pyr)
        val mk = masks ?: maskPyramid(ByteArray(S2 * S2) { 1 }).also { masks = it }
        val l = last
        if (l == null) { frames.add(f); keys.add(f); last = f; return f }
        val m = match(l.pyr!!, pyr, mk)
        var x = l.x - m.vx; var y = l.y - m.vy; var score = m.score; var status = "ok"
        if (m.score < 0.35f) {
            status = "lost"; lostFrames++
            // 1) cles proches de la derniere position connue (perte courte) : meilleure et 2e meilleure, il faut une marge nette
            val cands = keys.filter { it !== l }.sortedBy { hypot(it.x - l.x, it.y - l.y) }.take(8)
            var bestK: Frame? = null; var bestM: Match? = null; var second = -2f
            for (k in cands) {
                val mk2 = match(k.pyr!!, pyr, mk, (k.x - l.x).roundToInt(), (k.y - l.y).roundToInt(), 28)
                if (bestM == null || mk2.score > bestM.score) { second = bestM?.score ?: -2f; bestK = k; bestM = mk2 } else if (mk2.score > second) second = mk2.score
            }
            // 2) perte longue : balayage progressif de toutes les cles (12 par image), recherche large au niveau grossier
            if ((bestM == null || bestM.score < 0.5f) && lostFrames > 6 && keys.size > 8) {
                repeat(minOf(12, keys.size)) {
                    val k = keys[scanCursor % keys.size]; scanCursor++
                    if (k === l || k in cands) return@repeat
                    val mk2 = match(k.pyr!!, pyr, mk, 0, 0, 30)
                    if (bestM == null || mk2.score > bestM!!.score) { second = bestM?.score ?: -2f; bestK = k; bestM = mk2 } else if (mk2.score > second) second = mk2.score
                }
            }
            val bm = bestM
            if (bestK != null && bm != null && bm.score > 0.5f && bm.score - second > 0.08f) {
                val rx = bestK.x - bm.vx; val ry = bestK.y - bm.vy
                // confirmation : deux images consecutives doivent retomber au meme endroit (a 12 unites pres) avant d'accepter
                val pr = pendingReloc
                if (pr != null && hypot(pr.first - rx, pr.second - ry) < 12.0) {
                    x = rx; y = ry; score = bm.score; status = "relocated"; relocations++; pendingReloc = null; lostFrames = 0
                } else { pendingReloc = Triple(rx, ry, bm.score); x = l.x; y = l.y }
            } else { pendingReloc = null; x = l.x; y = l.y }
        } else {
            lostFrames = 0; pendingReloc = null
            // fermeture de boucle : une cle deja vue a moins d'un demi-champ ramene la position dessus (corrige la derive)
            val k = keys.filter { it.i < f.i - 4 && hypot(it.x - x, it.y - y) < S2 * 0.55 }.minByOrNull { hypot(it.x - x, it.y - y) }
            if (k != null) {
                val mk2 = match(k.pyr!!, pyr, mk, (k.x - x).roundToInt(), (k.y - y).roundToInt(), 8)
                if (mk2.score > 0.45f) { x = 0.3 * x + 0.7 * (k.x - mk2.vx); y = 0.3 * y + 0.7 * (k.y - mk2.vy) }
            }
        }
        f.x = x; f.y = y; f.score = score; f.status = status
        frames.add(f)
        if (status != "lost") {
            val prev = last
            last = f
            // cles reparties dans l'espace (une cle au plus tous les 0,3 champ) plus toutes les photos ; 800 au plus
            val near = keys.any { hypot(it.x - x, it.y - y) < S2 * 0.3 }
            if ((forceKey || !near) && keys.size < 800) { keys.add(f); forceKey = false }
            // mémoire : seules les images clés et la dernière gardent leur pyramide
            if (prev != null && prev !in keys) prev.pyr = null
        } else f.pyr = null
        return f
    }
}
