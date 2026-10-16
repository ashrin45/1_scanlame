package org.scanlame.app

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Rect
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.CaptureResult
import android.hardware.camera2.TotalCaptureResult
import android.os.Build
import android.util.Size
import androidx.camera.camera2.interop.Camera2CameraControl
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.camera2.interop.CaptureRequestOptions
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.AspectRatio
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.UseCase
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.video.FallbackStrategy
import androidx.camera.video.FileOutputOptions
import androidx.camera.video.Quality
import androidx.camera.video.QualitySelector
import androidx.camera.video.Recorder
import androidx.camera.video.Recording
import androidx.camera.video.VideoCapture
import androidx.camera.video.VideoRecordEvent
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import kotlinx.coroutines.flow.MutableStateFlow
import java.io.File
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.math.roundToInt

/** Description d'une caméra (Camera2) pour l'écran de diagnostic et les réglages. */
data class CamDesc(
    val id: String, val facing: String, val level: String, val physicalIds: List<String>,
    val focalMm: List<Float>, val apertures: List<Float>, val sensorMm: String, val pixels: String,
    val minFocusDiopters: Float?, val focusCalibration: String, val exposureNs: Pair<Long, Long>?, val iso: Pair<Int, Int>?,
    val zoomRange: Pair<Float, Float>?, val maxDigitalZoom: Float?, val afModes: List<String>, val aeModes: List<String>, val awbModes: List<String>,
    val caps: List<String>, val jpegSizes: List<Size>, val yuvSizes: List<Size>, val privateSizes: List<Size>, val hwLevelInt: Int
) {
    val label: String get() {
        val f = focalMm.firstOrNull()?.let { String.format(java.util.Locale.ROOT, "%.1f mm", it) } ?: "?"
        return "$id · $facing · $f${if (physicalIds.isNotEmpty()) " · logique(${physicalIds.joinToString(",")})" else ""}"
    }
}

/**
 * Pilotage de la caméra par CameraX, avec Camera2 pour tout ce que CameraX ne règle pas :
 * caméra physique, mise au point manuelle, exposition et ISO fixes, balance des blancs par gains,
 * recadrage capteur (zoom sans bascule d'objectif), lecture de l'objectif réellement actif.
 */
@OptIn(ExperimentalCamera2Interop::class)
class CameraEngine(private val ctx: Context) {
    data class Live(val activePhysical: String? = null, val exposureNs: Long? = null, val iso: Int? = null, val focusDiopters: Float? = null,
                    val awb: Int? = null, val af: Int? = null, val ae: Int? = null, val lensState: Int? = null, val zoomRatio: Float? = null, val crop: Rect? = null)
    val live = MutableStateFlow(Live())
    val error = MutableStateFlow<String?>(null)
    val bound = MutableStateFlow<String?>(null)     // description de ce qui est lié (résolutions)

    private val manager = ctx.getSystemService(Context.CAMERA_SERVICE) as CameraManager
    private var provider: ProcessCameraProvider? = null
    private var camera: Camera? = null
    private var imageCapture: ImageCapture? = null
    private var videoCapture: VideoCapture<Recorder>? = null
    private var recording: Recording? = null
    val analysisExecutor: ExecutorService = Executors.newSingleThreadExecutor()
    val ioExecutor: ExecutorService = Executors.newSingleThreadExecutor()
    var photoSize: Size? = null; private set
    var videoSize: Size? = null; private set
    var analysisSize: Size? = null; private set
    private var chars: CameraCharacteristics? = null

    // ---------------------------------------------------------------- énumération
    fun cameraIds(): List<String> {
        val ids = LinkedHashSet<String>()
        try { ids.addAll(manager.cameraIdList) } catch (e: Exception) {}
        // identifiants cachés fréquents (Samsung expose parfois des caméras hors de la liste)
        for (i in 0..9) ids.add(i.toString())
        for (i in 20..29) ids.add(i.toString())
        for (i in 50..59) ids.add(i.toString())
        return ids.filter { id -> try { manager.getCameraCharacteristics(id); true } catch (e: Exception) { false } }
    }

    fun describe(id: String): CamDesc? {
        val c = try { manager.getCameraCharacteristics(id) } catch (e: Exception) { return null }
        fun <T> g(k: CameraCharacteristics.Key<T>): T? = try { c.get(k) } catch (e: Exception) { null }
        val facing = when (g(CameraCharacteristics.LENS_FACING)) { CameraMetadata.LENS_FACING_BACK -> "arrière"; CameraMetadata.LENS_FACING_FRONT -> "avant"; CameraMetadata.LENS_FACING_EXTERNAL -> "externe"; else -> "?" }
        val lvl = g(CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL) ?: -1
        val level = when (lvl) { 0 -> "LIMITED"; 1 -> "FULL"; 2 -> "LEGACY"; 3 -> "LEVEL_3"; 4 -> "EXTERNAL"; else -> "?" }
        val phys = if (Build.VERSION.SDK_INT >= 28) (try { c.physicalCameraIds.toList() } catch (e: Exception) { emptyList() }) else emptyList()
        val sensor = g(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE)
        val px = g(CameraCharacteristics.SENSOR_INFO_PIXEL_ARRAY_SIZE)
        val map = g(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        fun sizes(fmt: Int) = try { map?.getOutputSizes(fmt)?.sortedByDescending { it.width.toLong() * it.height }?.take(6) ?: emptyList() } catch (e: Exception) { emptyList() }
        val caps = (g(CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES) ?: intArrayOf()).map {
            when (it) { 0 -> "backward"; 1 -> "manual_sensor"; 2 -> "manual_post"; 3 -> "raw"; 4 -> "private_reproc"; 5 -> "read_sensor_settings"; 6 -> "burst"; 7 -> "yuv_reproc"; 8 -> "depth"; 9 -> "high_speed"; 10 -> "motion_tracking"; 11 -> "logical_multi"; 12 -> "monochrome"; 13 -> "secure"; 14 -> "sysc"; 15 -> "offline"; 16 -> "ultra_high_res"; 17 -> "remosaic"; 18 -> "dynamic_range_10"; 19 -> "stream_use_case"; 20 -> "color_space"; else -> it.toString() }
        }
        val zr = if (Build.VERSION.SDK_INT >= 30) g(CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE) else null
        val exp = g(CameraCharacteristics.SENSOR_INFO_EXPOSURE_TIME_RANGE)
        val iso = g(CameraCharacteristics.SENSOR_INFO_SENSITIVITY_RANGE)
        val afm = (g(CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES) ?: intArrayOf()).map { when (it) { 0 -> "off"; 1 -> "auto"; 2 -> "macro"; 3 -> "cont_video"; 4 -> "cont_picture"; 5 -> "edof"; else -> it.toString() } }
        val aem = (g(CameraCharacteristics.CONTROL_AE_AVAILABLE_MODES) ?: intArrayOf()).map { when (it) { 0 -> "off"; 1 -> "on"; 2 -> "flash_auto"; 3 -> "flash_on"; 4 -> "redeye"; 5 -> "ext_flash"; else -> it.toString() } }
        val awbm = (g(CameraCharacteristics.CONTROL_AWB_AVAILABLE_MODES) ?: intArrayOf()).map { when (it) { 0 -> "off"; 1 -> "auto"; 2 -> "incandescent"; 3 -> "fluorescent"; 4 -> "warm_fluo"; 5 -> "daylight"; 6 -> "cloudy"; 7 -> "twilight"; 8 -> "shade"; else -> it.toString() } }
        val fc = when (g(CameraCharacteristics.LENS_INFO_FOCUS_DISTANCE_CALIBRATION)) { 0 -> "non étalonnée"; 1 -> "approximative"; 2 -> "étalonnée"; else -> "?" }
        return CamDesc(
            id, facing, level, phys,
            (g(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS) ?: floatArrayOf()).toList(),
            (g(CameraCharacteristics.LENS_INFO_AVAILABLE_APERTURES) ?: floatArrayOf()).toList(),
            sensor?.let { String.format(java.util.Locale.ROOT, "%.2f x %.2f mm", it.width, it.height) } ?: "?",
            px?.let { "${it.width} x ${it.height}" } ?: "?",
            g(CameraCharacteristics.LENS_INFO_MINIMUM_FOCUS_DISTANCE), fc,
            exp?.let { it.lower to it.upper }, iso?.let { it.lower to it.upper },
            zr?.let { it.lower to it.upper }, g(CameraCharacteristics.SCALER_AVAILABLE_MAX_DIGITAL_ZOOM),
            afm, aem, awbm, caps, sizes(android.graphics.ImageFormat.JPEG), sizes(android.graphics.ImageFormat.YUV_420_888), sizes(android.graphics.ImageFormat.PRIVATE), lvl
        )
    }

    fun diagnosticText(): String {
        val sb = StringBuilder()
        sb.append("Appareil : ${Build.MANUFACTURER} ${Build.MODEL}, Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT}), ScanLame ${BuildConfig.VERSION_NAME}\n")
        sb.append("Caméras listées par le système : ${try { manager.cameraIdList.joinToString(", ") } catch (e: Exception) { "?" }}\n\n")
        for (id in cameraIds()) {
            val d = describe(id) ?: continue
            sb.append("== caméra $id (${d.facing}, ${d.level})\n")
            sb.append("  focales ${d.focalMm} mm, ouvertures ${d.apertures}, capteur ${d.sensorMm}, ${d.pixels} px\n")
            if (d.physicalIds.isNotEmpty()) sb.append("  caméra logique, physiques : ${d.physicalIds}\n")
            sb.append("  capacités : ${d.caps}\n")
            sb.append("  mise au point : modes ${d.afModes}, distance min ${d.minFocusDiopters} dioptries (${d.focusCalibration})\n")
            sb.append("  exposition : modes ${d.aeModes}, temps ${d.exposureNs?.let { "${it.first / 1000} µs à ${it.second / 1_000_000} ms" } ?: "?"}, ISO ${d.iso?.let { "${it.first} à ${it.second}" } ?: "?"}\n")
            sb.append("  balance des blancs : ${d.awbModes}\n")
            sb.append("  zoom : ratio ${d.zoomRange?.let { "${it.first} à ${it.second}" } ?: "?"}, numérique max ${d.maxDigitalZoom}\n")
            sb.append("  JPEG : ${d.jpegSizes.joinToString { "${it.width}x${it.height}" }}\n")
            sb.append("  YUV : ${d.yuvSizes.joinToString { "${it.width}x${it.height}" }}\n")
            sb.append("  PRIVATE (aperçu/vidéo) : ${d.privateSizes.joinToString { "${it.width}x${it.height}" }}\n\n")
        }
        return sb.toString()
    }

    // ---------------------------------------------------------------- liaison
    /**
     * Choix de la caméra. Les modules physiques (ultra grand-angle, principal, télés) ne sont souvent pas
     * ouvrables seuls : CameraX ne connaît que les caméras « logiques ». On ouvre alors la caméra logique
     * qui contient ce module et on lui demande ce module physique précis (setPhysicalCameraId).
     * Retourne (sélecteur, id physique demandé ou null, description).
     */
    private fun selector(st: Settings, provider: ProcessCameraProvider): Triple<CameraSelector, String?, String> {
        val avail = provider.availableCameraInfos.map { Camera2CameraInfo.from(it).cameraId }
        fun byId(id: String) = CameraSelector.Builder().addCameraFilter { infos -> infos.filter { Camera2CameraInfo.from(it).cameraId == id } }.build()
        val want = st.cameraId
        if (want.isEmpty()) return Triple(CameraSelector.DEFAULT_BACK_CAMERA, st.physicalId.ifEmpty { null }, "caméra arrière par défaut")
        if (want in avail) return Triple(byId(want), st.physicalId.ifEmpty { null }, "caméra $want")
        // module physique : on cherche la caméra logique qui le contient
        if (Build.VERSION.SDK_INT >= 28) for (lid in avail) {
            val phys = try { manager.getCameraCharacteristics(lid).physicalCameraIds } catch (e: Exception) { emptySet() }
            if (want in phys) return Triple(byId(lid), want, "caméra logique $lid, module physique $want")
        }
        Journal.log("caméra $want inaccessible (CameraX connaît : $avail) : caméra arrière par défaut")
        return Triple(CameraSelector.DEFAULT_BACK_CAMERA, null, "caméra arrière par défaut ($want inaccessible)")
    }

    private var lastBindKey = ""; private var lastBindT = 0L

    @SuppressLint("RestrictedApi")
    fun bind(owner: LifecycleOwner, previewView: PreviewView, st: Settings, mode: String, onFrame: (ImageProxy) -> Unit, onReady: () -> Unit) {
        val fut = ProcessCameraProvider.getInstance(ctx)
        fut.addListener({
            try {
                val p = fut.get(); provider = p
                // deux liaisons identiques coup sur coup (recomposition) : la seconde est ignorée
                val key = "${System.identityHashCode(previewView)}|${st.cameraId}|${st.physicalId}|$mode|${st.photoMaxSide}|${st.analysisSide}|${st.videoQuality}"
                val nowT = android.os.SystemClock.elapsedRealtime()
                if (key == lastBindKey && nowT - lastBindT < 1500 && camera != null) return@addListener
                lastBindKey = key; lastBindT = nowT
                p.unbindAll()
                val ratio = if (mode == Settings.MODE_VIDEO) AspectRatio.RATIO_16_9 else AspectRatio.RATIO_4_3
                val ratioStrategy = AspectRatioStrategy(ratio, AspectRatioStrategy.FALLBACK_RULE_AUTO)
                val (sel, physical, selDesc) = selector(st, p)

                val previewB = Preview.Builder().setResolutionSelector(ResolutionSelector.Builder().setAspectRatioStrategy(ratioStrategy).build())
                Camera2Interop.Extender(previewB).setSessionCaptureCallback(object : CameraCaptureSession.CaptureCallback() {
                    override fun onCaptureCompleted(session: CameraCaptureSession, request: CaptureRequest, result: TotalCaptureResult) {
                        try {
                            live.value = Live(
                                activePhysical = if (Build.VERSION.SDK_INT >= 29) result.get(CaptureResult.LOGICAL_MULTI_CAMERA_ACTIVE_PHYSICAL_ID) else null,
                                exposureNs = result.get(CaptureResult.SENSOR_EXPOSURE_TIME), iso = result.get(CaptureResult.SENSOR_SENSITIVITY),
                                focusDiopters = result.get(CaptureResult.LENS_FOCUS_DISTANCE), awb = result.get(CaptureResult.CONTROL_AWB_MODE),
                                af = result.get(CaptureResult.CONTROL_AF_MODE), ae = result.get(CaptureResult.CONTROL_AE_MODE), lensState = result.get(CaptureResult.LENS_STATE),
                                zoomRatio = if (Build.VERSION.SDK_INT >= 30) result.get(CaptureResult.CONTROL_ZOOM_RATIO) else null, crop = result.get(CaptureResult.SCALER_CROP_REGION))
                        } catch (e: Exception) {}
                    }
                })
                if (physical != null && Build.VERSION.SDK_INT >= 28) Camera2Interop.Extender(previewB).setPhysicalCameraId(physical)
                val preview = previewB.build()
                preview.surfaceProvider = previewView.surfaceProvider

                val aside = st.analysisSide
                val asize = if (ratio == AspectRatio.RATIO_16_9) Size(aside, aside * 9 / 16) else Size(aside, aside * 3 / 4)
                val analysisB = ImageAnalysis.Builder()
                    .setResolutionSelector(ResolutionSelector.Builder().setAspectRatioStrategy(ratioStrategy)
                        .setResolutionStrategy(ResolutionStrategy(asize, ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER)).build())
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
                if (physical != null && Build.VERSION.SDK_INT >= 28) Camera2Interop.Extender(analysisB).setPhysicalCameraId(physical)
                val analysis = analysisB.build()
                analysis.setAnalyzer(analysisExecutor) { img -> try { onFrame(img) } finally { img.close() } }

                val uses = ArrayList<UseCase>(listOf(preview, analysis))
                imageCapture = null; videoCapture = null
                if (mode == Settings.MODE_VIDEO) {
                    val q = when (st.videoQuality) { "FHD" -> Quality.FHD; "HD" -> Quality.HD; else -> Quality.UHD }
                    val rec = Recorder.Builder().setQualitySelector(QualitySelector.from(q, FallbackStrategy.lowerQualityOrHigherThan(Quality.FHD)))
                        .setTargetVideoEncodingBitRate(st.videoMbps * 1_000_000).setExecutor(ioExecutor).build()
                    val vcB = VideoCapture.Builder(rec)
                    if (physical != null && Build.VERSION.SDK_INT >= 28) Camera2Interop.Extender(vcB).setPhysicalCameraId(physical)
                    val vc = vcB.build()
                    videoCapture = vc; uses.add(vc)
                } else {
                    val icB = ImageCapture.Builder().setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY).setJpegQuality(st.photoQuality)
                    val rs = ResolutionSelector.Builder().setAspectRatioStrategy(ratioStrategy)
                    if (st.photoMaxSide > 0) rs.setResolutionStrategy(ResolutionStrategy(Size(st.photoMaxSide, st.photoMaxSide * 3 / 4), ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER))
                    else rs.setResolutionStrategy(ResolutionStrategy.HIGHEST_AVAILABLE_STRATEGY)
                    icB.setResolutionSelector(rs.build())
                    if (physical != null && Build.VERSION.SDK_INT >= 28) Camera2Interop.Extender(icB).setPhysicalCameraId(physical)
                    val ic = icB.build(); imageCapture = ic; uses.add(ic)
                }
                val cam = p.bindToLifecycle(owner, sel, *uses.toTypedArray())
                camera = cam
                chars = try { manager.getCameraCharacteristics(Camera2CameraInfo.from(cam.cameraInfo).cameraId) } catch (e: Exception) { null }
                analysisSize = analysis.resolutionInfo?.resolution
                photoSize = imageCapture?.resolutionInfo?.resolution
                videoSize = videoCapture?.attachedSurfaceResolution
                bound.value = "$selDesc, analyse ${analysisSize?.let { "${it.width}x${it.height}" }}, " +
                    (photoSize?.let { "photo ${it.width}x${it.height}" } ?: videoSize?.let { "vidéo ${it.width}x${it.height}" } ?: "")
                Journal.log("liaison : ${bound.value}")
                error.value = null
                applyControls(st)
                onReady()
            } catch (e: Exception) {
                error.value = "caméra : ${e.message}"; Journal.log("ERREUR liaison caméra : $e")
            }
        }, ContextCompat.getMainExecutor(ctx))
    }

    fun unbind() { try { provider?.unbindAll() } catch (e: Exception) {}; camera = null }

    // ---------------------------------------------------------------- réglages manuels
    val locked = MutableStateFlow(false)

    /** Zoom et traitements (toujours appliqués), puis AF/AE/AWB selon l'état du verrou. */
    fun applyControls(st: Settings) {
        val cam = camera ?: return
        val c = chars
        val b = CaptureRequestOptions.Builder()
        val z = st.zoom.coerceAtLeast(1f)
        if (st.zoomByCrop && c != null) {
            val arr = c.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)
            if (arr != null) {
                val w = (arr.width() / z).roundToInt(); val h = (arr.height() / z).roundToInt()
                val x0 = arr.left + (arr.width() - w) / 2; val y0 = arr.top + (arr.height() - h) / 2
                b.setCaptureRequestOption(CaptureRequest.SCALER_CROP_REGION, Rect(x0, y0, x0 + w, y0 + h))
                if (Build.VERSION.SDK_INT >= 30) b.setCaptureRequestOption(CaptureRequest.CONTROL_ZOOM_RATIO, 1f)
            }
        } else {
            try { cam.cameraControl.setZoomRatio(z) } catch (e: Exception) { Journal.log("zoom refusé : ${e.message}") }
        }
        // pas de traitement qui lisse les détails
        b.setCaptureRequestOption(CaptureRequest.NOISE_REDUCTION_MODE, CameraMetadata.NOISE_REDUCTION_MODE_OFF)
        b.setCaptureRequestOption(CaptureRequest.EDGE_MODE, CameraMetadata.EDGE_MODE_OFF)
        b.setCaptureRequestOption(CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE, CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_OFF)
        b.setCaptureRequestOption(CaptureRequest.LENS_OPTICAL_STABILIZATION_MODE, CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_OFF)
        b.setCaptureRequestOption(CaptureRequest.CONTROL_SCENE_MODE, CameraMetadata.CONTROL_SCENE_MODE_DISABLED)
        lockOptions(st, locked.value, b)
        Camera2CameraControl.from(cam.cameraControl).setCaptureRequestOptions(b.build()).addListener({
            Journal.log("réglages appliqués : zoom $z (${if (st.zoomByCrop) "recadrage" else "ratio"}), AF/AE ${if (locked.value) "verrouillés" else "auto"}, EV ${st.evComp}")
        }, ContextCompat.getMainExecutor(ctx))
    }

    /** Même logique que le bouton « Verrouiller AF/AE » de la page : fige les valeurs courantes, avec plafond de pose et température optionnels. */
    private fun lockOptions(st: Settings, on: Boolean, b: CaptureRequestOptions.Builder) {
        val cam = camera ?: return
        val lv = live.value
        if (!on) {
            b.setCaptureRequestOption(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
            b.setCaptureRequestOption(CaptureRequest.CONTROL_AE_MODE, CameraMetadata.CONTROL_AE_MODE_ON)
            b.setCaptureRequestOption(CaptureRequest.CONTROL_AE_LOCK, false)
            b.setCaptureRequestOption(CaptureRequest.CONTROL_AWB_MODE, CameraMetadata.CONTROL_AWB_MODE_AUTO)
            b.setCaptureRequestOption(CaptureRequest.CONTROL_AWB_LOCK, false)
            try { cam.cameraControl.setExposureCompensationIndex(st.evComp) } catch (e: Exception) {}
            return
        }
        // valeurs figees au moment du verrouillage (reutilisees telles quelles si on change le zoom ensuite)
        val lk = lockVals ?: run {
            var expNs = lv.exposureNs ?: 10_000_000L; var iso = lv.iso ?: 200
            val cap = (st.shutterCapMs * 1_000_000).toLong()
            if (cap > 0 && expNs > cap) { iso = (iso.toDouble() * expNs / cap).roundToInt(); expNs = cap }
            val isoRange = chars?.get(CameraCharacteristics.SENSOR_INFO_SENSITIVITY_RANGE)
            if (isoRange != null) iso = iso.coerceIn(isoRange.lower, isoRange.upper)
            Triple(expNs, iso, if (st.focusMode == "keep") (lv.focusDiopters ?: 0f) else 0f).also { lockVals = it }
        }
        val expNs = lk.first; val iso = lk.second
        // mise au point
        b.setCaptureRequestOption(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_OFF)
        b.setCaptureRequestOption(CaptureRequest.LENS_FOCUS_DISTANCE, lk.third)
        b.setCaptureRequestOption(CaptureRequest.CONTROL_AE_MODE, CameraMetadata.CONTROL_AE_MODE_OFF)
        b.setCaptureRequestOption(CaptureRequest.SENSOR_EXPOSURE_TIME, expNs)
        b.setCaptureRequestOption(CaptureRequest.SENSOR_SENSITIVITY, iso)
        // balance des blancs : figée telle quelle, ou température demandée
        if (st.ctemp > 0) {
            b.setCaptureRequestOption(CaptureRequest.CONTROL_AWB_MODE, CameraMetadata.CONTROL_AWB_MODE_OFF)
            b.setCaptureRequestOption(CaptureRequest.COLOR_CORRECTION_MODE, CameraMetadata.COLOR_CORRECTION_MODE_TRANSFORM_MATRIX)
            val g = kelvinGains(st.ctemp)
            b.setCaptureRequestOption(CaptureRequest.COLOR_CORRECTION_GAINS, android.hardware.camera2.params.RggbChannelVector(g[0], g[1], g[1], g[2]))
            b.setCaptureRequestOption(CaptureRequest.COLOR_CORRECTION_TRANSFORM, android.hardware.camera2.params.ColorSpaceTransform(intArrayOf(1, 1, 0, 1, 0, 1, 0, 1, 1, 1, 0, 1, 0, 1, 0, 1, 1, 1)))
        } else {
            b.setCaptureRequestOption(CaptureRequest.CONTROL_AWB_LOCK, true)
        }
        if (lockVals === lk && !lockLogged) { lockLogged = true; Journal.log("verrouillage : focus ${if (st.focusMode == "keep") "distance courante ${lk.third}" else "infini"}, pose ${expNs / 1e6} ms, ISO $iso, WB ${if (st.ctemp > 0) "${st.ctemp} K" else "figée"}") }
    }
    private var lockVals: Triple<Long, Int, Float>? = null
    private var lockLogged = false

    fun setLocked(st: Settings, on: Boolean) { locked.value = on; lockVals = null; lockLogged = false; applyControls(st) }

    /** Gains R, G, B approximatifs pour une température de couleur (normalisés, vert = 1). */
    private fun kelvinGains(k: Int): FloatArray {
        val t = k / 100.0
        val r = if (t <= 66) 255.0 else 329.698727446 * Math.pow(t - 60, -0.1332047592)
        val g = if (t <= 66) 99.4708025861 * Math.log(t) - 161.1195681661 else 288.1221695283 * Math.pow(t - 60, -0.0755148492)
        val bl = if (t >= 66) 255.0 else if (t <= 19) 0.0 else 138.5177312231 * Math.log(t - 10) - 305.0447927307
        val rr = r.coerceIn(1.0, 255.0); val gg = g.coerceIn(1.0, 255.0); val bb = bl.coerceIn(1.0, 255.0)
        // un éclairage de couleur (r,g,b) se corrige par des gains inverses
        return floatArrayOf((gg / rr).toFloat().coerceIn(0.5f, 4f), 1f, (gg / bb).toFloat().coerceIn(0.5f, 4f))
    }

    // ---------------------------------------------------------------- captures
    fun takePhoto(file: File, onDone: (Boolean, String?) -> Unit) {
        val ic = imageCapture ?: return onDone(false, "pas de capture photo liée")
        val opts = ImageCapture.OutputFileOptions.Builder(file).build()
        ic.takePicture(opts, ioExecutor, object : ImageCapture.OnImageSavedCallback {
            override fun onImageSaved(r: ImageCapture.OutputFileResults) = onDone(true, null)
            override fun onError(e: ImageCaptureException) = onDone(false, e.message)
        })
    }

    fun startVideo(file: File, onEvent: (VideoRecordEvent) -> Unit): Boolean {
        val vc = videoCapture ?: return false
        recording = vc.output.prepareRecording(ctx, FileOutputOptions.Builder(file).build()).start(ioExecutor) { ev -> onEvent(ev) }
        return true
    }
    fun stopVideo() { recording?.stop(); recording = null }
    fun isRecording() = recording != null
}
