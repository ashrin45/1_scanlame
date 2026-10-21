package org.scanlame.app

import android.content.Context
import android.os.Build
import android.os.SystemClock
import androidx.camera.core.ImageProxy
import androidx.camera.video.VideoRecordEvent
import kotlinx.coroutines.flow.MutableStateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.Normalizer
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.hypot
import kotlin.math.abs
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * Une prise (série) : suivi de position sur le flux d'analyse, détection des arrêts de platine,
 * photo à chaque arrêt (mode photos) ou enregistrement vidéo continu (mode vidéo), phases (fond blanc,
 * changement d'objectif), métadonnées serie.json identiques à celles de la page web, puis file d'envoi.
 */
class Session(private val ctx: Context, private val engine: CameraEngine, private val st: Settings) {
    class Placed(val x: Double, val y: Double, val status: String, val photo: Boolean, val thumb: android.graphics.Bitmap?, val grade: Int = 0, val bonus: Boolean = false)   // grade : 0 net, 1 doux, 2 flou ou vide
    data class State(
        val running: Boolean = false, val mode: String = Settings.MODE_PHOTOS, val seriesId: String = "", val elapsedMs: Long = 0,
        val track: String = "", val score: Float = 0f, val posX: Double = 0.0, val posY: Double = 0.0, val sharp: Float = 0f,
        val still: Boolean = false, val photos: Int = 0, val samples: Int = 0, val bytes: Long = 0, val shooting: Boolean = false,
        val phase: String = "", val objective: String = "x100", val placed: List<Placed> = emptyList(), val fieldW: Double = 1.0, val fieldH: Double = 1.0,
        val message: String = "", val trackMs: Int = 0, val fps: Float = 0f,
        val warn: String = "", val sharpRef: Float = 0f, val blurred: Int = 0, val bigSteps: Int = 0, val exposure: String = "",
        val lastGrade: Int = -1, val lastShotAt: Long = 0L,
        val detailAt: Pair<Double, Double>? = null,
        val patron: DoubleArray? = null, val patronFill: Float = 0f,
        val relocations: Int = 0, val reinforce: Boolean = false,
        val discFill: Float = 0f, val discOff: Float = 1f, val sharpPeak: Float = 0f   // pre-controle : disque de l'oculaire (fraction du cadre, ecart du centre), nettete de pointe sur 5 s   // relocalisations du suivi ; mode renforcer (photos bonus)   // rectangle cible [x0,y0,w,h] dans le repere des positions, et fraction remplie   // position (repere courant) du dernier champ pris au plus fort grossissement, apres passage a un objectif plus faible   // derniere photo : -1 aucune, 0 bonne, 1 moyenne, 2 ratee (floue, suivi perdu ou pas trop grand) ; horodatage pour le clignotement du cadre
    )
    val state = MutableStateFlow(State())

    private var tracker = Tracker()
    private var t0 = 0L
    private val frames = JSONArray()
    private val phases = JSONArray()
    private val segments = ArrayList<JSONObject>()
    private var curBlank: JSONObject? = null
    private var switching: JSONObject? = null
    private var seq = 0
    private var sampleSeq = 0
    private var meta = JSONObject()
    private var seriesDir: File? = null
    private var videoFile: File? = null
    private val placed = ArrayList<Placed>()        // positions recentes (fenetre glissante)
    private val placedPhotos = ArrayList<Placed>()  // toutes les photos de la serie : la couverture ne s'efface jamais
    private var W = 1.0; private var H = 1.0           // taille du média capturé (portrait), unité des positions
    private var lastPos: Pair<Double, Double>? = null
    private var stillSince = 0L
    private var lastShot: Pair<Double, Double>? = null
    @Volatile private var shooting = false
    private var lastFrameT = 0L
    private var fpsAcc = 0f
    private val gray = FloatArray(Tracker.S2 * Tracker.S2)
    private var frameCount = 0
    private var videoDone: ((Boolean) -> Unit)? = null
    @Volatile private var lastRot = 90

    private fun now() = SystemClock.elapsedRealtime()
    private fun tnow() = now() - t0
    private fun inPause() = curBlank != null || switching != null

    // ---------------------------------------------------------------- image d'analyse -> gris 256 x 256 redressé
    private fun toGray(img: ImageProxy): FloatArray {
        val plane = img.planes[0]; val buf = plane.buffer; val rs = plane.rowStride; val ps = plane.pixelStride
        val w = img.width; val h = img.height; val rot = img.imageInfo.rotationDegrees; lastRot = rot
        val S = Tracker.S2
        for (v in 0 until S) for (u in 0 until S) {
            val fu = (u + 0.5f) / S; val fv = (v + 0.5f) / S
            val sx: Float; val sy: Float
            when (rot) {
                90 -> { sx = fv; sy = 1f - fu }
                180 -> { sx = 1f - fu; sy = 1f - fv }
                270 -> { sx = 1f - fv; sy = fu }
                else -> { sx = fu; sy = fv }
            }
            val x = (sx * w).toInt().coerceIn(0, w - 2); val y = (sy * h).toInt().coerceIn(0, h - 2)
            val i = y * rs + x * ps
            val a = buf.get(i).toInt() and 255; val b = buf.get(i + ps).toInt() and 255
            val c = buf.get(i + rs).toInt() and 255; val d = buf.get(i + rs + ps).toInt() and 255
            gray[v * S + u] = (a + b + c + d) * 0.25f
        }
        return gray
    }

    /** Nettete normalisee par le contenu : energie du laplacien / energie du gradient (x100). Un champ vide n'est plus note flou
     *  (la variance brute du laplacien chute avec le contenu, pas seulement avec le flou) ; un champ plein et defocalise reste bas. */
    private fun sharpness(g: FloatArray): Float {
        val S = Tracker.S2; var l2 = 0.0; var g2 = 0.0; var n = 0
        var y = 1
        while (y < S - 1) {
            var x = 1
            while (x < S - 1) {
                val c = g[y * S + x]
                val l = 4 * c - g[y * S + x - 1] - g[y * S + x + 1] - g[(y - 1) * S + x] - g[(y + 1) * S + x]
                val gx = g[y * S + x + 1] - g[y * S + x - 1]; val gy = g[(y + 1) * S + x] - g[(y - 1) * S + x]
                l2 += l * l; g2 += gx * gx + gy * gy; n++
                x += 2
            }
            y += 2
        }
        lastContent = sqrt(g2 / n).toFloat()
        return (100.0 * l2 / (g2 + 1e-3)).toFloat()
    }
    @Volatile var lastContent = 0f   // energie du gradient de la derniere image analysee (richesse du champ)

    private fun makeMask(g: FloatArray): ByteArray {
        val sorted = g.copyOf(); sorted.sort()
        val p95 = sorted[(sorted.size * 0.95).toInt()]
        val m = ByteArray(g.size); var n = 0
        for (i in g.indices) if (g[i] > 0.3f * p95) { m[i] = 1; n++ }
        Journal.log("masque oculaire : ${100 * n / g.size} % du cadre utile (p95 ${p95.roundToInt()})")
        return m
    }

    /** Appelé sur le thread d'analyse pour chaque image du flux. */
    fun onFrame(img: ImageProxy) {
        val tFrame = now()
        val g = toGray(img)
        val sh = sharpness(g)
        val dtf = if (lastFrameT > 0) (tFrame - lastFrameT) else 0; lastFrameT = tFrame
        if (dtf > 0) fpsAcc = 0.9f * fpsAcc + 0.1f * (1000f / dtf)
        val s = state.value
        if (!s.running || inPause()) {
            // pre-controle avant la serie : centrage et taille du disque de l'oculaire, nettete de pointe recente
            if (!s.running) {
                previewSharp.addLast(tFrame to sh); while (previewSharp.isNotEmpty() && tFrame - previewSharp.first().first > 5000) previewSharp.removeFirst()
                if (frameCount++ % 8 == 0) {
                    val m = makeMask(g); val S = Tracker.S2; var n = 0; var sx = 0.0; var sy = 0.0
                    for (y in 0 until S) for (x in 0 until S) if (m[y * S + x].toInt() != 0) { n++; sx += x; sy += y }
                    discFill = n.toFloat() / (S * S); discOff = if (n > 0) (hypot(sx / n - S / 2.0, sy / n - S / 2.0) / S).toFloat() else 1f
                }
            }
            val peak = previewSharp.maxOfOrNull { it.second } ?: sh
            state.value = s.copy(sharp = sh, fps = fpsAcc, discFill = discFill, discOff = discOff, sharpPeak = peak); return
        }
        if (!tracker.hasMask()) { val m = makeMask(g); mask256 = m; tracker.setMask(m) }
        val tt = now()
        val f = tracker.add(g.copyOf())
        val ms = (now() - tt).toInt()
        val t = tnow()
        val px = f.x * W / Tracker.S2; val py = f.y * H / Tracker.S2
        sampleSeq++
        if (s.mode == Settings.MODE_VIDEO || frameCount % 2 == 0) {
            val name = "${s.seriesId}_t${String.format(Locale.ROOT, "%07d", t)}"
            synchronized(frames) { frames.put(JSONObject().put("name", name).put("kind", "sample").put("t_ms", t).put("x", if (f.status == "lost") JSONObject.NULL else px.roundToInt()).put("y", if (f.status == "lost") JSONObject.NULL else py.roundToInt()).put("score", f.score.toDouble()).put("track", f.status)) }
        }
        frameCount++
        if (f.status != "lost" && frameCount % 3 == 0) synchronized(placed) { placed.add(Placed(px, py, f.status, false, thumbColor(img))); if (placed.size > 300) placed.removeAt(0) }
        // ---- qualité en direct : exposition (toutes les 5 images) et netteté par rapport aux meilleures photos de la série
        if (frameCount % 5 == 0) {
            val sorted = g.copyOf(); sorted.sort()
            val p99 = sorted[(sorted.size * 0.99).toInt()]; val p50 = sorted[sorted.size / 2]
            exposureNote = when { p99 >= 254f && p50 > 240f -> "trop clair : baisse la lampe"; p50 < 70f -> "trop sombre : monte la lampe"; else -> "" }
        }

        if (f.status == "relocated") Journal.log("suivi relocalisé (n° ${tracker.relocations}) à ${px.roundToInt()},${py.roundToInt()} score ${String.format(Locale.ROOT, "%.2f", f.score)}")
        if (frameCount % 10 == 0) Journal.log("t${String.format(Locale.ROOT, "%07d", t)} suivi ${f.status} score ${String.format(Locale.ROOT, "%.2f", f.score)} pos ${px.roundToInt()},${py.roundToInt()} net ${sh.roundToInt()} ($ms ms) carte ${synchronized(placed) { placedPhotos.size }} photos + ${synchronized(placed) { placed.size }} positions")
        // ---- détection d'arrêt
        var still = false
        val lp = lastPos
        if (f.status != "lost") {
            val moved = lp?.let { hypot(f.x - it.first, f.y - it.second) } ?: 99.0
            if (moved <= st.stillPx) { if (stillSince == 0L) stillSince = now(); still = now() - stillSince >= st.stillMs } else stillSince = 0L
            lastPos = f.x to f.y
        } else { stillSince = 0L }
        if (markDetail && f.status != "lost") { markDetail = false; state.value = state.value.copy(detailAt = px to py) }
        liveWarn = when {
            exposureNote.isNotEmpty() -> exposureNote
            sharpRef > 0 && still && sh < 0.5f * sharpRef -> "flou : retouche la vis micrométrique"
            else -> ""
        }
        // declenchement : a l'arret (nouvel endroit), ou, platine en mouvement lent, des que la distance a la derniere photo atteint stepFrac du champ
        val lsd = lastShot
        val gapNow = if (lsd == null) 0.0 else hypot(f.x - lsd.first, f.y - lsd.second)
        val speed = if (lp != null && f.status != "lost") hypot(f.x - lp.first, f.y - lp.second) * fpsAcc else 999.0   // unites (sur 256) par seconde
        val byDistance = st.triggerDistance && f.status != "lost" && lsd != null && gapNow >= st.stepFrac * Tracker.S2 && speed < 40.0 && now() - lastShotT > 400
        if ((still || byDistance) && s.mode == Settings.MODE_PHOTOS && !shooting) {
            val ls = lastShot
            val gap = if (ls == null) 0.0 else hypot(f.x - ls.first, f.y - ls.second)
            val far = ls == null || gap >= 10.0   // ~4 % du champ : nouvel arrêt
            if (far) {
                if (byDistance && !still) Journal.log("photo en mouvement lent (${(100 * gap / Tracker.S2).roundToInt()} % du champ, ${speed.roundToInt()} u/s)")
                // pas trop grand entre deux arrêts : au-dela de 70 % du champ, les photos ne se recouvrent plus assez pour l'assemblage
                bigStepPending = gap > 0.7 * Tracker.S2
                if (gap > 0.7 * Tracker.S2) { nBigSteps++; Journal.log("ATTENTION pas trop grand (${(100 * gap / Tracker.S2).roundToInt()} % du champ) : recouvrement insuffisant"); state.value = state.value.copy(message = "Pas trop grand (${(100 * gap / Tracker.S2).roundToInt()} % du champ) : recule d'un demi-champ, les photos doivent se recouvrir d'au moins 30 %.") }
                else if (state.value.message.startsWith("Pas trop grand")) state.value = state.value.copy(message = "Série en cours : déplace la platine d'un demi-champ, arrête-toi, une photo part à chaque arrêt.")
                shoot(f, px, py, t, sh, img)
            } else if (still && lastShotSharp > 0 && sh > 1.3f * lastShotSharp && retakes < 3 && now() - lastShotT > 900 && abs(sh - prevSharp) < 0.08f * sh) {
                // meme endroit, nettete nettement meilleure et stabilisee : la vis micrometrique a ete retouchee -> nouvelle photo (le pipeline garde la plus nette)
                retakes++
                Journal.log("reprise de mise au point au même endroit : netteté ${sh.roundToInt()} contre ${lastShotSharp.roundToInt()} (reprise $retakes)")
                shoot(f, px, py, t, sh, img, retake = true)
            }
        }
        prevSharp = sh
        state.value = state.value.copy(track = f.status, score = f.score, posX = px, posY = py, sharp = sh, still = still, samples = sampleSeq,
            elapsedMs = t, placed = synchronized(placed) { ArrayList<Placed>(placedPhotos.size + placed.size).apply { addAll(placedPhotos); addAll(placed) } }, trackMs = ms, fps = fpsAcc, shooting = shooting,
            warn = liveWarn, sharpRef = sharpRef, blurred = nBlurred, bigSteps = nBigSteps, exposure = exposureNote, relocations = tracker.relocations)
    }

    /** Vignette couleur 48 x 48 tirée des plans YUV de l'image d'analyse, redressée comme le gris (carte de couverture). */
    @Volatile private var mask256: ByteArray? = null   // disque oculaire (1 = utile) : les vignettes sont transparentes en dehors
    private fun thumbColor(img: ImageProxy): android.graphics.Bitmap? = try { thumbColorRaw(img) } catch (e: Exception) { Journal.log("vignette : ${e.message}"); null }
    private fun thumbColorRaw(img: ImageProxy): android.graphics.Bitmap {
        val n = 48; val out = IntArray(n * n); val mk = mask256; val S = Tracker.S2
        val yP = img.planes[0]; val uP = img.planes[1]; val vP = img.planes[2]
        val yb = yP.buffer; val ub = uP.buffer; val vb = vP.buffer
        val w = img.width; val h = img.height; val rot = img.imageInfo.rotationDegrees
        for (v in 0 until n) for (u in 0 until n) {
            val fu = (u + 0.5f) / n; val fv = (v + 0.5f) / n
            val sx: Float; val sy: Float
            when (rot) { 90 -> { sx = fv; sy = 1f - fu }; 180 -> { sx = 1f - fu; sy = 1f - fv }; 270 -> { sx = 1f - fv; sy = fu }; else -> { sx = fu; sy = fv } }
            val x = (sx * w).toInt().coerceIn(0, w - 1); val y = (sy * h).toInt().coerceIn(0, h - 1)
            val yy = yb.get(y * yP.rowStride + x * yP.pixelStride).toInt() and 255
            val ci = (y / 2) * uP.rowStride + (x / 2) * uP.pixelStride
            val cu = (if (ci < ub.limit()) ub.get(ci).toInt() and 255 else 128) - 128
            val cv = (if (ci < vb.limit()) vb.get(ci).toInt() and 255 else 128) - 128
            val r = (yy + 1.402f * cv).toInt().coerceIn(0, 255); val gg = (yy - 0.344f * cu - 0.714f * cv).toInt().coerceIn(0, 255); val b = (yy + 1.772f * cu).toInt().coerceIn(0, 255)
            val inside = mk == null || mk[(v * S / n) * S + u * S / n].toInt() != 0
            out[v * n + u] = if (inside) (0xFF shl 24) or (r shl 16) or (gg shl 8) or b else 0
        }
        return android.graphics.Bitmap.createBitmap(out, n, n, android.graphics.Bitmap.Config.ARGB_8888)
    }

    /** Vignette 48 x 48 en gris à partir du carré 256 (secours). */
    private fun thumbOf(g: FloatArray): android.graphics.Bitmap {
        val n = 48; val px = IntArray(n * n); val S = Tracker.S2
        for (y in 0 until n) for (x in 0 until n) {
            val v = g[(y * S / n) * S + x * S / n].toInt().coerceIn(0, 255)
            px[y * n + x] = (0xFF shl 24) or (v shl 16) or (v shl 8) or v
        }
        return android.graphics.Bitmap.createBitmap(px, n, n, android.graphics.Bitmap.Config.ARGB_8888)
    }

    private var sharpRef = 0f; private var nBlurred = 0; private var nBigSteps = 0
    private var lastShotT = 0L
    private var lastShotSharp = 0f; private var prevSharp = 0f; private var retakes = 0   // reprise de mise au point au meme endroit
    private var bigStepPending = false
    private val previewSharp = ArrayDeque<Pair<Long, Float>>(); @Volatile private var discFill = 0f; @Volatile private var discOff = 1f
    private var markDetail = false
    @Volatile private var exposureNote = ""; @Volatile private var liveWarn = ""
    private val photoSharps = ArrayList<Float>()

    /** Fraction du rectangle cible couverte par les photos (grille de 48 x 48 cases). */
    private fun patronFill(pt: DoubleArray): Float {
        val G = 48; val hit = BooleanArray(G * G)
        val photos = synchronized(placed) { placedPhotos.filter { it.photo } }
        for (p in photos) {
            val x0 = ((p.x - pt[0]) / pt[2] * G).toInt().coerceIn(0, G); val x1 = ((p.x + W - pt[0]) / pt[2] * G).toInt().coerceIn(0, G)
            val y0 = ((p.y - pt[1]) / pt[3] * G).toInt().coerceIn(0, G); val y1 = ((p.y + H - pt[1]) / pt[3] * G).toInt().coerceIn(0, G)
            for (y in y0 until y1) for (x in x0 until x1) hit[y * G + x] = true
        }
        return hit.count { it }.toFloat() / (G * G)
    }

    fun setReinforce(on: Boolean) { state.value = state.value.copy(reinforce = on); Journal.log(if (on) "mode renforcer : photos bonus (le pipeline garde la plus nette dans les recouvrements)" else "mode renforcer désactivé") }

    private fun shoot(f: Tracker.Frame, px: Double, py: Double, t: Long, sh: Float, img: ImageProxy, retake: Boolean = false) {
        val dir = seriesDir ?: return
        val th = thumbColor(img)   // avant la photo : le tampon d'analyse est réutilisé par l'image suivante
        // reference de nettete : mediane des 5 meilleures photos de la serie ; une photo sous 50 % est notee floue, sous 70 % douce
        photoSharps.add(sh); val best = photoSharps.sortedDescending().take(5); sharpRef = best[best.size / 2]
        val grade = if (photoSharps.size >= 3 && sh < 0.5f * sharpRef) 2 else if (photoSharps.size >= 3 && sh < 0.7f * sharpRef) 1 else 0
        if (grade == 2) { nBlurred++; Journal.log("photo floue (netteté ${sh.roundToInt()} contre ${sharpRef.roundToInt()} de référence)") }
        // note affichee par le cadre : rouge aussi si le suivi etait perdu ou si le pas depuis la photo precedente est trop grand
        val frameGrade = if (bigStepPending || f.status == "lost") 2 else grade
        bigStepPending = false
        state.value = state.value.copy(lastGrade = frameGrade, lastShotAt = now())
        shooting = true
        tracker.markKey()
        lastShot = f.x to f.y; lastShotT = now(); lastShotSharp = sh; if (!retake) retakes = 0
        seq++
        val name = "${state.value.seriesId}_${String.format(Locale.ROOT, "%04d", seq)}.jpg"
        val file = File(File(dir, "photos").apply { mkdirs() }, name)
        val tt = now()
        engine.takePhoto(file) { ok, err ->
            shooting = false
            if (ok) {
                val len = file.length()
                synchronized(frames) { frames.put(JSONObject().put("name", name).put("kind", "photo").put("t_ms", t).put("x", px.roundToInt()).put("y", py.roundToInt()).put("score", f.score.toDouble()).put("track", f.status).put("retake", retake).put("bonus", state.value.reinforce).put("enc_ms", now() - tt).put("sharp", sh.toDouble())) }
                synchronized(placed) { placedPhotos.add(Placed(px, py, f.status, true, th, grade, state.value.reinforce)) }
                val patronSpec = st.patronFor(state.value.objective)
                if (state.value.patron == null && patronSpec != "libre") {
                    val (cols, rows) = patronSpec.split('x').map { it.toInt() }
                    // apres un passage a un objectif plus faible, le detail (= cette premiere photo) doit etre au centre du patron ; sinon coin haut-gauche
                    val centered = state.value.detailAt != null
                    val x0 = if (centered) px + W / 2 - cols * W / 2 else px; val y0 = if (centered) py + H / 2 - rows * H / 2 else py
                    state.value = state.value.copy(patron = doubleArrayOf(x0, y0, cols * W, rows * H))
                    Journal.log("patron $patronSpec champs posé ${if (centered) "autour du détail" else "en haut à gauche"} (${x0.roundToInt()},${y0.roundToInt()})")
                }
                state.value.patron?.let { pt -> state.value = state.value.copy(patronFill = patronFill(pt)) }
                state.value = state.value.copy(photos = seq, bytes = state.value.bytes + len)
                Journal.log("photo ${name.takeLast(8, 4)} à ${px.roundToInt()},${py.roundToInt()} (${len shr 10} ko, ${now() - tt} ms)")
                Uploader.kick(ctx)
            } else {
                seq--
                Journal.log("ERREUR photo : $err")
            }
        }
    }
    private fun String.takeLast(n: Int, dropEnd: Int) = this.dropLast(dropEnd).takeLast(n)

    // ---------------------------------------------------------------- phases
    fun setBlank(on: Boolean) {
        if (!state.value.running || switching != null) return
        if (on && curBlank == null) {
            curBlank = JSONObject().put("kind", "blank").put("t0", tnow())
            Journal.log("fond blanc : début à ${tnow()} ms")
            state.value = state.value.copy(phase = "blank", message = "Fond blanc : place une zone vide de la lame et laisse 2 à 3 s, puis retouche le bouton.")
        } else if (!on && curBlank != null) {
            val b = curBlank!!; b.put("t1", tnow()); phases.put(b); curBlank = null
            Journal.log("fond blanc : fin à ${tnow()} ms")
            state.value = state.value.copy(phase = "", message = "Fond blanc enregistré. Reprends le balayage.")
        }
    }

    fun startSwitch(obj: String) {
        if (!state.value.running || curBlank != null) return
        segments.last().put("t1", tnow())
        // position de la platine au moment du clic (px du flux, repere du segment en cours) : le premier champ du nouvel objectif en est proche
        val sv = state.value
        switching = JSONObject().put("objective", obj).put("t0", tnow()).put("from_x", sv.posX.roundToInt()).put("from_y", sv.posY.roundToInt()).put("from_track", sv.track)
        Journal.log("changement d'objectif vers $obj : début à ${tnow()} ms, position ${sv.posX.roundToInt()},${sv.posY.roundToInt()} (${sv.track})")
        state.value = state.value.copy(phase = "switching", message = "Change l'objectif ($obj), refais la mise au point, puis touche Reprendre.")
    }

    fun resumeSwitch() {
        val sw = switching ?: return
        val obj = sw.getString("objective"); val t = tnow()
        val cal = st.umPerPx(obj)
        segments.add(JSONObject().put("objective", obj).put("t0", t).put("t1", JSONObject.NULL).put("um_per_px", if (cal > 0) cal.toDouble() else JSONObject.NULL)
            .put("from_x", sw.opt("from_x")).put("from_y", sw.opt("from_y")).put("from_track", sw.opt("from_track")).put("patron", st.patronFor(obj).takeIf { it != "libre" } ?: JSONObject.NULL))
        // si on passe a un grossissement plus faible, le point de depart du nouveau repere est l'emplacement du detail : repere sur la carte
        val prevObj = Regex("\\d+").find(segments.getOrNull(segments.size - 2)?.optString("objective") ?: "")?.value?.toIntOrNull() ?: 0
        val newObj = Regex("\\d+").find(obj)?.value?.toIntOrNull() ?: 0
        markDetail = prevObj > newObj
        state.value = state.value.copy(detailAt = null, patron = null, patronFill = 0f)   // nouveau repere : le patron se repose a la premiere photo du nouvel objectif
        // nouvel objectif = nouvelle reference de nettete (celle du x10 n'a rien a voir avec celle du x100) ; le cadre repart neutre
        photoSharps.clear(); sharpRef = 0f; bigStepPending = false; lastShot = null
        state.value = state.value.copy(lastGrade = -1, sharpRef = 0f)
        st.objective = obj
        tracker = Tracker(); synchronized(placed) { placed.clear(); placedPhotos.clear() }; lastPos = null; lastShot = null; stillSince = 0L
        Journal.log("objectif $obj : reprise à $t ms (calibration ${if (cal > 0) cal else "absente"})")
        switching = null
        state.value = state.value.copy(phase = "", objective = obj, message = "Objectif $obj : balayage des zones de détail.")
    }

    // ---------------------------------------------------------------- début et fin
    private fun stamp() = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.ROOT).format(Date())
    /** Même règle que process_inbox.py : titre + MMJJ + HHMM si titre, sinon l'identifiant avec des tirets. */
    private fun predictedSlug(seriesId: String, title: String): String {
        val parts = seriesId.split('_')
        if (title.isNotBlank() && parts.size >= 3) return Normalizer.normalize(title, Normalizer.Form.NFKD).replace(Regex("[^\\x00-\\x7F]"), "").lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-').take(40) + "-" + parts[1].drop(4) + parts[2].take(4)
        return seriesId.lowercase().replace('_', '-')
    }
    private fun slug(s: String) = Normalizer.normalize(s, Normalizer.Form.NFD).replace(Regex("[\\u0300-\\u036f]"), "").lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-').take(30)

    fun start(title: String, sample: String, comment: String, parent: String?, complete: String?) {
        if (state.value.running) return
        val mode = st.mode
        val id = "lame_${stamp()}${slug(title).takeIf { it.isNotEmpty() }?.let { "_$it" } ?: ""}"
        t0 = now(); seq = 0; sampleSeq = 0; frameCount = 0
        tracker = Tracker(); synchronized(placed) { placed.clear(); placedPhotos.clear() }; lastPos = null; lastShot = null; stillSince = 0L; shooting = false
        sharpRef = 0f; nBlurred = 0; nBigSteps = 0; photoSharps.clear(); exposureNote = ""; liveWarn = ""; mask256 = null
        while (frames.length() > 0) frames.remove(0)
        while (phases.length() > 0) phases.remove(0)
        segments.clear(); curBlank = null; switching = null
        val cal = st.umPerPx(st.objective)
        segments.add(JSONObject().put("objective", st.objective).put("t0", 0).put("t1", JSONObject.NULL).put("um_per_px", if (cal > 0) cal.toDouble() else JSONObject.NULL))
        // taille du média (portrait) = unité des positions
        val sz = if (mode == Settings.MODE_VIDEO) engine.videoSize else engine.photoSize
        val rot = lastRot   // rotation appliquée par ffmpeg (métadonnée mp4) et par EXIF (photos) : positions dans l'image redressée
        if (sz != null) { W = (if (rot % 180 != 0) sz.height else sz.width).toDouble(); H = (if (rot % 180 != 0) sz.width else sz.height).toDouble() } else { W = 3000.0; H = 4000.0 }
        val dir = Uploader.seriesDir(ctx, id); seriesDir = dir
        st.hint = ""
        meta = JSONObject()
            .put("series", id).put("title", title.ifBlank { null }).put("um_per_px", if (cal > 0) cal.toDouble() else JSONObject.NULL)
            .put("stain", st.stain).put("objective", st.objective).put("sample", sample.ifBlank { null }).put("comment", comment.ifBlank { null })
            .put("parent", parent ?: JSONObject.NULL).put("complete", complete ?: JSONObject.NULL)
            .put("hint", st.hint.split(',').mapNotNull { it.toDoubleOrNull() }.takeIf { parent != null && it.size == 2 }?.let { JSONObject().put("fx", it[0]).put("fy", it[1]) } ?: JSONObject.NULL)
            .put("stage", st.stageX.replace(',', '.').toDoubleOrNull()?.let { x -> st.stageY.replace(',', '.').toDoubleOrNull()?.let { y -> JSONObject().put("x", x).put("y", y) } } ?: JSONObject.NULL)
            .put("captured", SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", Locale.ROOT).format(Date()))
            .put("calibrations", JSONObject(st.calibrations().mapValues { it.value.toDouble() }))
            .put("patron", st.patronFor(st.objective).takeIf { it != "libre" } ?: JSONObject.NULL).put("auto_crop", st.autoCrop)
            .put("depth", st.depth).put("mode", mode).put("sample_ms", 100).put("position_unit", "px of the captured image (portrait)")
            .put("width", W.roundToInt()).put("height", H.roundToInt()).put("zoom", st.zoom.toDouble())
            .put("app", "scanlame-android ${BuildConfig.VERSION_NAME}").put("device", "${Build.MANUFACTURER} ${Build.MODEL} Android ${Build.VERSION.RELEASE}")
            .put("camera", JSONObject().put("id", st.cameraId.ifEmpty { "auto" }).put("physical", st.physicalId.ifEmpty { null }).put("locked", engine.locked.value).put("focus", st.focusMode)
                .put("shutter_cap_ms", st.shutterCapMs.toDouble()).put("ctemp", st.ctemp).put("ev", st.evComp).put("exposure_ns", engine.live.value.exposureNs ?: JSONObject.NULL).put("iso", engine.live.value.iso ?: JSONObject.NULL).put("analysis", engine.analysisSize?.toString())
                .put("photo", engine.photoSize?.toString()).put("video", engine.videoSize?.toString()).put("video_mbps", st.videoMbps).put("quality", st.photoQuality))
        Journal.log("=== série $id ($mode) « ${title.ifBlank { "sans nom" }} », ${st.stain}, objectif ${st.objective} : ${engine.bound.value}")
        state.value = State(running = true, mode = mode, seriesId = id, objective = st.objective, fieldW = W, fieldH = H,
            message = if (mode == Settings.MODE_VIDEO) "Enregistrement en cours. Platine lente, en serpentin, arrêt net à chaque champ." else "Série en cours : déplace la platine d'un demi-champ, arrête-toi, une photo part à chaque arrêt.")
        if (mode == Settings.MODE_VIDEO) {
            val vf = File(dir, "video.mp4"); videoFile = vf
            val ok = engine.startVideo(vf) { ev ->
                when (ev) {
                    is VideoRecordEvent.Start -> Journal.log("vidéo : enregistrement démarré")
                    is VideoRecordEvent.Status -> state.value = state.value.copy(bytes = ev.recordingStats.numBytesRecorded)
                    is VideoRecordEvent.Finalize -> {
                        Journal.log("vidéo : fin (${ev.recordingStats.numBytesRecorded shr 20} Mo, erreur ${ev.error})")
                        videoDone?.invoke(!ev.hasError() || ev.error == VideoRecordEvent.Finalize.ERROR_FILE_SIZE_LIMIT_REACHED || ev.recordingStats.numBytesRecorded > 0)
                    }
                }
            }
            if (!ok) { Journal.log("ERREUR : impossible de démarrer la vidéo"); state.value = state.value.copy(running = false, message = "Vidéo impossible, voir le journal.") }
        }
    }

    /** Abandon de la série en cours : rien n'est envoyé, les photos déjà en file pour cette série sont supprimées du téléphone. */
    fun discard() {
        val s = state.value
        if (!s.running) return
        curBlank = null; switching = null
        if (s.mode == Settings.MODE_VIDEO && engine.isRecording()) { videoDone = { seriesDir?.deleteRecursively() }; engine.stopVideo() }
        val n = seq
        seriesDir?.let { d ->
            d.deleteRecursively()
            // des photos sont peut-etre deja parties : une cloture « abandonnee » demande au serveur de les supprimer (sinon il assemblerait un orphelin)
            if (st.serverUrl.isNotEmpty()) { d.mkdirs(); File(d, "finish.json").writeText(JSONObject().put("series", s.seriesId).put("discarded", true).toString()); Uploader.kick(ctx) }
        }
        Uploader.refresh(ctx)
        Journal.log("série ${s.seriesId} abandonnée ($n photo(s) supprimée(s) du téléphone ; les photos déjà envoyées seront supprimées du serveur)")
        state.value = state.value.copy(running = false, still = false, warn = "", message = "Série abandonnée : $n photo(s) supprimée(s), rien n'a été envoyé." + (if (s.photos > 0 && st.autoUpload) " Les photos déjà parties restent sur le serveur mais ne seront pas assemblées (pas de clôture)." else ""))
    }

    /** Arrêt de la série : en vidéo, attend la finalisation puis découpe en morceaux ; écrit finish.json et lance l'envoi. */
    fun stop(onDone: () -> Unit) {
        val s = state.value
        if (!s.running) return
        if (curBlank != null) setBlank(false)
        segments.last().put("t1", tnow())
        state.value = s.copy(running = false, message = "Clôture en cours...")
        val finish = {
            val dir = seriesDir!!
            val m = JSONObject(meta.toString())
            m.put("phases", phases).put("segments", JSONArray(segments)).put("count", seq).put("duration_ms", tnow())
            m.put("frames", synchronized(frames) { JSONArray(frames.toString()) })
            if (st.sendLog) m.put("journal", Journal.forServer())
            if (s.mode == Settings.MODE_VIDEO) {
                val vf = videoFile
                var n = 0
                if (vf != null && vf.exists()) {
                    n = splitChunks(vf, File(dir, "chunks").apply { mkdirs() })
                    vf.delete()
                }
                m.put("video_mime", "video/mp4").put("chunks", n).put("failed", JSONArray())
                Journal.log("vidéo ${s.seriesId} : $n morceau(x) en file d'envoi, $sampleSeq échantillons de position")
            } else {
                Journal.log("série ${s.seriesId} : $seq photos, $sampleSeq échantillons de position")
            }
            File(dir, "finish.json").writeText(m.toString())
            // slug que le serveur donnera a cette lame (meme regle que process_inbox) : pour la proposer comme lame d'ensemble sans attendre
            st.addRecentSeries(predictedSlug(s.seriesId, meta.optString("title", "")), meta.optString("title", "").ifBlank { s.seriesId }, st.objective)
            Uploader.kick(ctx)
            val bilan = if (s.mode == Settings.MODE_PHOTOS) " Bilan : $seq photos, ${nBlurred} floue(s), ${nBigSteps} pas trop grand(s)." else ""
            state.value = state.value.copy(message = "Prise ${s.seriesId.takeLast(6)} en file d'envoi.$bilan L'assemblage se fait côté serveur, la lame apparaîtra dans la galerie.", warn = "")
            onDone()
        }
        if (s.mode == Settings.MODE_VIDEO && engine.isRecording()) {
            videoDone = { finish() }
            engine.stopVideo()
        } else finish()
    }

    /** Découpe le fichier en morceaux de 45 Mo (limite de taille du bucket), nommés 00000.bin, 00001.bin... */
    private fun splitChunks(src: File, dir: File): Int {
        val part = 45L * 1024 * 1024
        var n = 0
        src.inputStream().use { inp ->
            val buf = ByteArray(1 shl 20)
            var done = false
            while (!done) {
                val out = File(dir, String.format(Locale.ROOT, "%05d.bin", n))
                var written = 0L
                out.outputStream().use { o ->
                    while (written < part) {
                        val r = inp.read(buf, 0, minOf(buf.size.toLong(), part - written).toInt())
                        if (r < 0) { done = true; break }
                        o.write(buf, 0, r); written += r
                    }
                }
                if (written == 0L && n > 0) { out.delete(); break }
                n++
            }
        }
        return n
    }
}
