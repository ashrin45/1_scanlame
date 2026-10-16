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
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * Une prise (série) : suivi de position sur le flux d'analyse, détection des arrêts de platine,
 * photo à chaque arrêt (mode photos) ou enregistrement vidéo continu (mode vidéo), phases (fond blanc,
 * changement d'objectif), métadonnées serie.json identiques à celles de la page web, puis file d'envoi.
 */
class Session(private val ctx: Context, private val engine: CameraEngine, private val st: Settings) {
    class Placed(val x: Double, val y: Double, val status: String, val photo: Boolean, val thumb: android.graphics.Bitmap?)
    data class State(
        val running: Boolean = false, val mode: String = Settings.MODE_PHOTOS, val seriesId: String = "", val elapsedMs: Long = 0,
        val track: String = "", val score: Float = 0f, val posX: Double = 0.0, val posY: Double = 0.0, val sharp: Float = 0f,
        val still: Boolean = false, val photos: Int = 0, val samples: Int = 0, val bytes: Long = 0, val shooting: Boolean = false,
        val phase: String = "", val objective: String = "x100", val placed: List<Placed> = emptyList(), val fieldW: Double = 1.0, val fieldH: Double = 1.0,
        val message: String = "", val trackMs: Int = 0, val fps: Float = 0f
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
    private val placed = ArrayList<Placed>()
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

    private fun sharpness(g: FloatArray): Float {
        val S = Tracker.S2; var sum = 0.0; var sum2 = 0.0; var n = 0
        var y = 1
        while (y < S - 1) {
            var x = 1
            while (x < S - 1) {
                val l = 4 * g[y * S + x] - g[y * S + x - 1] - g[y * S + x + 1] - g[(y - 1) * S + x] - g[(y + 1) * S + x]
                sum += l; sum2 += l * l; n++
                x += 2
            }
            y += 2
        }
        val m = sum / n
        return sqrt(sum2 / n - m * m).toFloat()
    }

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
        if (!s.running || inPause()) { state.value = s.copy(sharp = sh, fps = fpsAcc); return }
        if (!tracker.hasMask()) tracker.setMask(makeMask(g))
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
        if (f.status != "lost" && frameCount % 3 == 0) synchronized(placed) { placed.add(Placed(px, py, f.status, false, thumbOf(g))); if (placed.size > 1500) placed.removeAt(0) }
        if (frameCount % 10 == 0) Journal.log("t${String.format(Locale.ROOT, "%07d", t)} suivi ${f.status} score ${String.format(Locale.ROOT, "%.2f", f.score)} pos ${px.roundToInt()},${py.roundToInt()} net ${sh.roundToInt()} ($ms ms)")
        // ---- détection d'arrêt
        var still = false
        val lp = lastPos
        if (f.status != "lost") {
            val moved = lp?.let { hypot(f.x - it.first, f.y - it.second) } ?: 99.0
            if (moved <= st.stillPx) { if (stillSince == 0L) stillSince = now(); still = now() - stillSince >= st.stillMs } else stillSince = 0L
            lastPos = f.x to f.y
        } else { stillSince = 0L }
        if (still && s.mode == Settings.MODE_PHOTOS && !shooting) {
            val ls = lastShot
            val gap = if (ls == null) 0.0 else hypot(f.x - ls.first, f.y - ls.second)
            val far = ls == null || gap >= 10.0   // ~4 % du champ : nouvel arrêt
            if (far) {
                // pas trop grand entre deux arrêts : au-dela de 70 % du champ, les photos ne se recouvrent plus assez pour l'assemblage
                if (gap > 0.7 * Tracker.S2) { Journal.log("ATTENTION pas trop grand (${(100 * gap / Tracker.S2).roundToInt()} % du champ) : recouvrement insuffisant"); state.value = state.value.copy(message = "Pas trop grand (${(100 * gap / Tracker.S2).roundToInt()} % du champ) : recule d'un demi-champ, les photos doivent se recouvrir d'au moins 30 %.") }
                else if (state.value.message.startsWith("Pas trop grand")) state.value = state.value.copy(message = "Série en cours : déplace la platine d'un demi-champ, arrête-toi, une photo part à chaque arrêt.")
                shoot(f, px, py, t, sh, g)
            }
        }
        state.value = state.value.copy(track = f.status, score = f.score, posX = px, posY = py, sharp = sh, still = still, samples = sampleSeq,
            elapsedMs = t, placed = synchronized(placed) { ArrayList(placed) }, trackMs = ms, fps = fpsAcc, shooting = shooting)
    }

    /** Vignette 48 x 48 en gris à partir du carré 256 (pour la carte de couverture). */
    private fun thumbOf(g: FloatArray): android.graphics.Bitmap {
        val n = 48; val px = IntArray(n * n); val S = Tracker.S2
        for (y in 0 until n) for (x in 0 until n) {
            val v = g[(y * S / n) * S + x * S / n].toInt().coerceIn(0, 255)
            px[y * n + x] = (0xFF shl 24) or (v shl 16) or (v shl 8) or v
        }
        return android.graphics.Bitmap.createBitmap(px, n, n, android.graphics.Bitmap.Config.ARGB_8888)
    }

    private fun shoot(f: Tracker.Frame, px: Double, py: Double, t: Long, sh: Float, g: FloatArray) {
        val dir = seriesDir ?: return
        val th = thumbOf(g)   // avant la photo : le tampon gris est réutilisé par l'image suivante
        shooting = true
        lastShot = f.x to f.y
        seq++
        val name = "${state.value.seriesId}_${String.format(Locale.ROOT, "%04d", seq)}.jpg"
        val file = File(File(dir, "photos").apply { mkdirs() }, name)
        val tt = now()
        engine.takePhoto(file) { ok, err ->
            shooting = false
            if (ok) {
                val len = file.length()
                synchronized(frames) { frames.put(JSONObject().put("name", name).put("kind", "photo").put("t_ms", t).put("x", px.roundToInt()).put("y", py.roundToInt()).put("score", f.score.toDouble()).put("track", f.status).put("enc_ms", now() - tt).put("sharp", sh.toDouble())) }
                synchronized(placed) { placed.add(Placed(px, py, f.status, true, th)) }
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
        switching = JSONObject().put("objective", obj).put("t0", tnow())
        Journal.log("changement d'objectif vers $obj : début à ${tnow()} ms")
        state.value = state.value.copy(phase = "switching", message = "Change l'objectif ($obj), refais la mise au point, puis touche Reprendre.")
    }

    fun resumeSwitch() {
        val sw = switching ?: return
        val obj = sw.getString("objective"); val t = tnow()
        val cal = st.umPerPx(obj)
        segments.add(JSONObject().put("objective", obj).put("t0", t).put("t1", JSONObject.NULL).put("um_per_px", if (cal > 0) cal.toDouble() else JSONObject.NULL))
        st.objective = obj
        tracker = Tracker(); synchronized(placed) { placed.clear() }; lastPos = null; lastShot = null; stillSince = 0L
        Journal.log("objectif $obj : reprise à $t ms (calibration ${if (cal > 0) cal else "absente"})")
        switching = null
        state.value = state.value.copy(phase = "", objective = obj, message = "Objectif $obj : balayage des zones de détail.")
    }

    // ---------------------------------------------------------------- début et fin
    private fun stamp() = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.ROOT).format(Date())
    private fun slug(s: String) = Normalizer.normalize(s, Normalizer.Form.NFD).replace(Regex("[\\u0300-\\u036f]"), "").lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-').take(30)

    fun start(title: String, sample: String, comment: String, parent: String?, complete: String?) {
        if (state.value.running) return
        val mode = st.mode
        val id = "lame_${stamp()}${slug(title).takeIf { it.isNotEmpty() }?.let { "_$it" } ?: ""}"
        t0 = now(); seq = 0; sampleSeq = 0; frameCount = 0
        tracker = Tracker(); synchronized(placed) { placed.clear() }; lastPos = null; lastShot = null; stillSince = 0L; shooting = false
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
        meta = JSONObject()
            .put("series", id).put("title", title.ifBlank { null }).put("um_per_px", if (cal > 0) cal.toDouble() else JSONObject.NULL)
            .put("stain", st.stain).put("objective", st.objective).put("sample", sample.ifBlank { null }).put("comment", comment.ifBlank { null })
            .put("parent", parent ?: JSONObject.NULL).put("complete", complete ?: JSONObject.NULL)
            .put("captured", SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", Locale.ROOT).format(Date()))
            .put("calibrations", JSONObject(st.calibrations().mapValues { it.value.toDouble() }))
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
            Uploader.kick(ctx)
            state.value = state.value.copy(message = "Prise ${s.seriesId.takeLast(6)} en file d'envoi. L'assemblage se fait côté serveur, la lame apparaîtra dans la galerie.")
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
