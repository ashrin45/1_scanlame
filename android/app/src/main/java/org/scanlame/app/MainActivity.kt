package org.scanlame.app

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.view.PreviewView
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import kotlinx.coroutines.delay
import java.util.Locale

class MainActivity : ComponentActivity() {
    private lateinit var engine: CameraEngine
    private lateinit var settings: Settings
    private lateinit var session: Session

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        settings = Settings(this)
        engine = CameraEngine(this)
        session = Session(this, engine, settings)
        Journal.log("ScanLame ${BuildConfig.VERSION_NAME} démarré sur ${Build.MANUFACTURER} ${Build.MODEL} (Android ${Build.VERSION.RELEASE})")
        Uploader.refresh(this)
        if (Uploader.pendingSeries(this).isNotEmpty()) Uploader.kick(this)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(primary = Color(0xFFC58AD6), background = Color(0xFF17131B), surface = Color(0xFF211C26))) {
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) { App(engine, settings, session) }
            }
        }
    }
}

@Composable
fun App(engine: CameraEngine, st: Settings, session: Session) {
    val ctx = LocalContext.current
    var tab by remember { mutableIntStateOf(0) }
    var granted by remember { mutableStateOf(ContextCompat.checkSelfPermission(ctx, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { r -> granted = r[Manifest.permission.CAMERA] == true }
    LaunchedEffect(Unit) {
        if (!granted) {
            val perms = arrayListOf(Manifest.permission.CAMERA)
            if (Build.VERSION.SDK_INT >= 33) perms.add(Manifest.permission.POST_NOTIFICATIONS)
            launcher.launch(perms.toTypedArray())
        }
    }
    val sess by session.state.collectAsState()
    Scaffold(bottomBar = {
        if (!sess.running) TabRow(selectedTabIndex = tab) {
            listOf("Capture", "Réglages", "Diagnostic", "Journal").forEachIndexed { i, t -> Tab(selected = tab == i, onClick = { tab = i }, text = { Text(t, fontSize = 13.sp) }) }
        }
    }) { pad ->
        Box(Modifier.padding(pad).fillMaxSize()) {
            if (!granted) Text("Autorisation caméra nécessaire.", Modifier.padding(24.dp))
            else when (tab) {
                0 -> CaptureScreen(engine, st, session)
                1 -> SettingsScreen(engine, st) { tab = 0 }
                2 -> DiagnosticScreen(engine)
                else -> JournalScreen()
            }
        }
    }
}

// ---------------------------------------------------------------- capture
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CaptureScreen(engine: CameraEngine, st: Settings, session: Session) {
    val ctx = LocalContext.current
    val owner = LocalLifecycleOwner.current
    val sess by session.state.collectAsState()
    val live by engine.live.collectAsState()
    val bound by engine.bound.collectAsState()
    val err by engine.error.collectAsState()
    val up by Uploader.status.collectAsState()
    var title by remember { mutableStateOf("") }
    var sample by remember { mutableStateOf("") }
    var objective by remember { mutableStateOf(st.objective) }
    var stain by remember { mutableStateOf(st.stain) }
    var showObjPick by remember { mutableStateOf(false) }
    var preview by remember { mutableStateOf<PreviewView?>(null) }
    var bindKey by remember { mutableIntStateOf(0) }

    LaunchedEffect(preview, bindKey) {
        val pv = preview ?: return@LaunchedEffect
        engine.bind(owner, pv, st, st.mode, { img -> session.onFrame(img) }) {}
    }
    LaunchedEffect(sess.objective) { objective = sess.objective }

    Column(Modifier.fillMaxSize()) {
        Box(Modifier.fillMaxWidth().aspectRatio(if (st.mode == Settings.MODE_VIDEO) 9f / 16f else 3f / 4f).background(Color.Black)) {
            AndroidView(factory = { c -> PreviewView(c).apply { implementationMode = PreviewView.ImplementationMode.COMPATIBLE; scaleType = PreviewView.ScaleType.FIT_CENTER; preview = this } }, modifier = Modifier.fillMaxSize())
            // bandeau d'état
            Column(Modifier.align(Alignment.TopStart).padding(6.dp).background(Color(0x99000000), RoundedCornerShape(6.dp)).padding(6.dp)) {
                val expo = live.exposureNs?.let { String.format(Locale.ROOT, "%.1f ms", it / 1e6) } ?: "?"
                Text("${if (st.mode == Settings.MODE_VIDEO) "vidéo" else "photos"} · ${live.activePhysical?.let { "cam $it" } ?: ""} · $expo ISO ${live.iso ?: "?"} · AF ${afName(live.af)} ${live.focusDiopters?.let { String.format(Locale.ROOT, "%.2f", it) } ?: ""} · WB ${awbName(live.awb)}", color = Color.White, fontSize = 11.sp, maxLines = 2)
                Text("netteté ${sess.sharp.toInt()} · ${String.format(Locale.ROOT, "%.0f", sess.fps)} img/s" + if (sess.running) " · suivi ${sess.track} ${String.format(Locale.ROOT, "%.2f", sess.score)} (${sess.trackMs} ms)${if (sess.still) " · ARRÊT" else ""}" else "", color = if (sess.track == "lost") Color(0xFFEB5757) else if (sess.still) Color(0xFF6FCF97) else Color.White, fontSize = 11.sp)
                if (sess.running) Text("${sess.elapsedMs / 1000} s · ${if (sess.mode == Settings.MODE_VIDEO) "${sess.bytes shr 20} Mo" else "${sess.photos} photo(s), ${sess.bytes shr 20} Mo"} · ${sess.samples} positions" + (if (sess.shooting) " · déclenchement" else ""), color = Color.White, fontSize = 11.sp)
            }
            // carte de couverture
            CoverageMap(sess, Modifier.align(Alignment.TopEnd).padding(6.dp).size(110.dp, 140.dp))
            if (sess.still && sess.running) Box(Modifier.align(Alignment.BottomCenter).padding(8.dp).background(Color(0xCC1B9466), RoundedCornerShape(6.dp)).padding(6.dp, 3.dp)) { Text("platine immobile", color = Color.White, fontSize = 12.sp) }
        }
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(10.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            err?.let { Text(it, color = Color(0xFFEB5757), fontSize = 13.sp) }
            Text(sess.message.ifEmpty { bound ?: "caméra..." }, fontSize = 13.sp, color = Color(0xFFCFC6D6))
            if (!sess.running) {
                OutlinedTextField(title, { title = it }, label = { Text("Nom de la lame") }, singleLine = true, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(sample, { sample = it }, label = { Text("Prélèvement (optionnel)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    Column(Modifier.weight(1f)) {
                        Text("Coloration", fontSize = 12.sp, color = Color(0xFFA79CB1))
                        FlowChips(Settings.STAINS.map { it.first to it.second }, stain) { stain = it; st.stain = it }
                    }
                }
                Text("Objectif", fontSize = 12.sp, color = Color(0xFFA79CB1))
                FlowChips(Settings.OBJECTIVES.map { it to it }, objective) { objective = it; st.objective = it }
                val cal = st.umPerPx(objective)
                Text(if (cal > 0) "calibration $objective : $cal µm/px" else "calibration $objective : absente (réglages)", fontSize = 12.sp, color = Color(0xFFA79CB1))
                Button(onClick = { session.start(title, sample, "", null, null) }, modifier = Modifier.fillMaxWidth(), enabled = err == null && bound != null) { Text(if (st.mode == Settings.MODE_VIDEO) "Lancer l'enregistrement" else "Lancer la série") }
            } else {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    if (sess.phase != "switching") {
                        FilterChip(selected = sess.phase == "blank", onClick = { session.setBlank(sess.phase != "blank") }, label = { Text("Fond blanc") })
                        OutlinedButton(onClick = { showObjPick = !showObjPick }, enabled = sess.phase != "blank") { Text("Changer d'objectif") }
                    } else Button(onClick = { session.resumeSwitch() }) { Text("Reprendre (${sess.objective})") }
                }
                if (showObjPick && sess.phase == "") FlowChips(Settings.OBJECTIVES.filter { it != sess.objective }.map { it to it }, "") { showObjPick = false; session.startSwitch(it) }
                Button(onClick = { session.stop {} }, colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFC0392B)), modifier = Modifier.fillMaxWidth()) { Text("Arrêter et envoyer") }
            }
            Text("Envoi : ${up.pendingFiles} fichier(s) en attente (${up.pendingBytes shr 20} Mo), ${up.sentFiles} envoyé(s) (${up.sentBytes shr 20} Mo)${up.lastError?.let { " · $it" } ?: ""}${if (up.busy) " · en cours" else ""}", fontSize = 12.sp, color = if (up.lastError != null) Color(0xFFE2B25A) else Color(0xFFA79CB1))
            if (!sess.running && up.pendingFiles > 0 && !up.busy) OutlinedButton(onClick = { Uploader.kick(ctx) }) { Text("Relancer l'envoi") }
            if (!sess.running) OutlinedButton(onClick = { bindKey++ }) { Text("Reconnecter la caméra") }
        }
    }
    // rafraîchissement du temps écoulé
    LaunchedEffect(sess.running) { while (sess.running) { delay(1000); Uploader.refresh(ctx) } }
}

fun afName(v: Int?) = when (v) { 0 -> "off"; 1 -> "auto"; 2 -> "macro"; 3 -> "contV"; 4 -> "contP"; null -> "?"; else -> v.toString() }
fun awbName(v: Int?) = when (v) { 0 -> "off"; 1 -> "auto"; 5 -> "jour"; null -> "?"; else -> v.toString() }

@OptIn(ExperimentalMaterial3Api::class, androidx.compose.foundation.layout.ExperimentalLayoutApi::class)
@Composable
fun FlowChips(items: List<Pair<String, String>>, selected: String, onPick: (String) -> Unit) {
    androidx.compose.foundation.layout.FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        for ((v, label) in items) FilterChip(selected = v == selected, onClick = { onPick(v) }, label = { Text(label, fontSize = 12.sp) })
    }
}

@Composable
fun CoverageMap(s: Session.State, modifier: Modifier) {
    Canvas(modifier.background(Color(0xAA000000), RoundedCornerShape(6.dp))) {
        val pl = s.placed
        if (pl.isEmpty()) return@Canvas
        val W = s.fieldW.toFloat(); val H = s.fieldH.toFloat()
        var x0 = Float.MAX_VALUE; var y0 = Float.MAX_VALUE; var x1 = -Float.MAX_VALUE; var y1 = -Float.MAX_VALUE
        for (p in pl) { x0 = minOf(x0, p.x.toFloat()); y0 = minOf(y0, p.y.toFloat()); x1 = maxOf(x1, p.x.toFloat() + W); y1 = maxOf(y1, p.y.toFloat() + H) }
        val pad = 6f
        val sc = minOf((size.width - 2 * pad) / (x1 - x0), (size.height - 2 * pad) / (y1 - y0))
        val ox = pad + ((size.width - 2 * pad) - (x1 - x0) * sc) / 2; val oy = pad + ((size.height - 2 * pad) - (y1 - y0) * sc) / 2
        for (p in pl) drawRect(if (p.photo) Color(0x66C58AD6) else Color(0x22FFFFFF), androidx.compose.ui.geometry.Offset(ox + (p.x.toFloat() - x0) * sc, oy + (p.y.toFloat() - y0) * sc), androidx.compose.ui.geometry.Size(W * sc, H * sc))
        val cur = pl.last()
        drawRect(if (s.track == "lost") Color(0xFFEB5757) else if (s.track == "relocated") Color(0xFFF2C94C) else Color(0xFF6FCF97), androidx.compose.ui.geometry.Offset(ox + (cur.x.toFloat() - x0) * sc, oy + (cur.y.toFloat() - y0) * sc), androidx.compose.ui.geometry.Size(W * sc, H * sc), style = androidx.compose.ui.graphics.drawscope.Stroke(2f))
    }
}

// ---------------------------------------------------------------- réglages
@Composable
fun SettingsScreen(engine: CameraEngine, st: Settings, onApplied: () -> Unit) {
    val ctx = LocalContext.current
    var url by remember { mutableStateOf(st.serverUrl) }
    var token by remember { mutableStateOf(st.token) }
    var mode by remember { mutableStateOf(st.mode) }
    var cameraId by remember { mutableStateOf(st.cameraId) }
    var physical by remember { mutableStateOf(st.physicalId) }
    var zoom by remember { mutableStateOf(st.zoom) }
    var zoomCrop by remember { mutableStateOf(st.zoomByCrop) }
    var focus by remember { mutableStateOf(st.focusMode) }
    var diopters by remember { mutableStateOf(st.focusDiopters) }
    var expo by remember { mutableStateOf(st.exposureMode) }
    var shutter by remember { mutableStateOf(st.shutterMs) }
    var iso by remember { mutableStateOf(st.iso.toFloat()) }
    var ev by remember { mutableStateOf(st.evComp.toFloat()) }
    var wb by remember { mutableStateOf(st.wbMode) }
    var kelvin by remember { mutableStateOf(st.wbKelvin.toFloat()) }
    var side by remember { mutableStateOf(st.photoMaxSide.toString()) }
    var quality by remember { mutableStateOf(st.photoQuality.toFloat()) }
    var vq by remember { mutableStateOf(st.videoQuality) }
    var vmbps by remember { mutableStateOf(st.videoMbps.toFloat()) }
    var stillMs by remember { mutableStateOf(st.stillMs.toFloat()) }
    var stillPx by remember { mutableStateOf(st.stillPx) }
    var aside by remember { mutableStateOf(st.analysisSide.toString()) }
    var sendLog by remember { mutableStateOf(st.sendLog) }
    var depth by remember { mutableStateOf(st.depth) }
    var umpx by remember { mutableStateOf(st.umPerPx(st.objective).takeIf { it > 0 }?.toString() ?: "") }
    var storage by remember { mutableStateOf("") }
    val cams = remember { engine.cameraIds().mapNotNull { engine.describe(it) } }
    val selCam = cams.firstOrNull { it.id == cameraId } ?: cams.firstOrNull { it.facing == "arrière" }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Serveur", fontSize = 16.sp)
        OutlinedTextField(url, { url = it }, label = { Text("URL de la fonction Supabase (.../functions/v1/scanlame)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(token, { token = it }, label = { Text("Jeton (X-Token)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Row(verticalAlignment = Alignment.CenterVertically) { Checkbox(sendLog, { sendLog = it }); Text("Joindre le journal du téléphone à la prise", fontSize = 13.sp) }
        if (storage.isNotEmpty()) Text(storage, fontSize = 12.sp, color = Color(0xFFA79CB1))
        LaunchedEffect(url) { if (url.trimEnd('/').endsWith("scanlame")) { val j = Uploader.serverStatus(url.trimEnd('/')); storage = j?.let { "stockage : ${it.optLong("used_bytes") shr 30} Go utilisés sur ${it.optLong("limit_bytes") shr 30} Go, ${it.optInt("series")} prise(s) brutes" } ?: "serveur injoignable" } }

        Text("Mode de capture", fontSize = 16.sp)
        FlowChips(listOf(Settings.MODE_PHOTOS to "photos à chaque arrêt", Settings.MODE_VIDEO to "vidéo continue"), mode) { mode = it }
        FlowChips(listOf("off" to "profondeur : image la plus nette", "on" to "profondeur : fusion (essai)"), depth) { depth = it }

        Text("Caméra", fontSize = 16.sp)
        FlowChips(cams.map { it.id to it.label }, cameraId.ifEmpty { selCam?.id ?: "" }) { cameraId = it; physical = "" }
        if (selCam != null && selCam.physicalIds.isNotEmpty()) {
            Text("Objectif physique (évite la bascule automatique)", fontSize = 12.sp, color = Color(0xFFA79CB1))
            FlowChips(listOf("" to "auto (logique)") + selCam.physicalIds.map { id -> id to (engine.describe(id)?.let { "$id · ${it.focalMm.firstOrNull()?.let { f -> String.format(Locale.ROOT, "%.1f mm", f) } ?: ""}" } ?: id) }, physical) { physical = it }
        }
        Text("Zoom ${String.format(Locale.ROOT, "%.1f", zoom)}x", fontSize = 13.sp)
        Slider(zoom, { zoom = it }, valueRange = 1f..4f, steps = 29)
        Row(verticalAlignment = Alignment.CenterVertically) { Switch(zoomCrop, { zoomCrop = it }); Text("zoom par recadrage capteur (pas de changement d'objectif)", fontSize = 13.sp, modifier = Modifier.padding(start = 8.dp)) }

        Text("Mise au point", fontSize = 16.sp)
        FlowChips(listOf("infinity" to "infini (fixe)", "manual" to "manuelle", "auto" to "autofocus"), focus) { focus = it }
        if (focus == "manual") { Text("distance ${String.format(Locale.ROOT, "%.2f", diopters)} dioptries (0 = infini${selCam?.minFocusDiopters?.let { ", max $it" } ?: ""})", fontSize = 13.sp); Slider(diopters, { diopters = it }, valueRange = 0f..(selCam?.minFocusDiopters ?: 10f).coerceAtLeast(0.5f)) }

        Text("Exposition", fontSize = 16.sp)
        FlowChips(listOf("auto" to "automatique", "manual" to "manuelle"), expo) { expo = it }
        if (expo == "manual") {
            Text("temps de pose ${String.format(Locale.ROOT, "%.1f", shutter)} ms", fontSize = 13.sp); Slider(shutter, { shutter = it }, valueRange = 0.2f..33f)
            Text("ISO ${iso.toInt()}", fontSize = 13.sp); Slider(iso, { iso = it }, valueRange = (selCam?.iso?.first ?: 50).toFloat()..(selCam?.iso?.second ?: 3200).toFloat().coerceAtMost(3200f))
        } else { Text("correction ${ev.toInt()} EV", fontSize = 13.sp); Slider(ev, { ev = it }, valueRange = -4f..4f, steps = 7) }

        Text("Balance des blancs", fontSize = 16.sp)
        FlowChips(listOf("auto" to "automatique", "daylight" to "lumière du jour (fixe)", "manual" to "température"), wb) { wb = it }
        if (wb == "manual") { Text("${kelvin.toInt()} K", fontSize = 13.sp); Slider(kelvin, { kelvin = it }, valueRange = 2500f..8000f) }

        Text("Photos", fontSize = 16.sp)
        OutlinedTextField(side, { side = it.filter { c -> c.isDigit() } }, label = { Text("grand côté max en px (0 = maximum du capteur)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Text("qualité JPEG ${quality.toInt()}", fontSize = 13.sp); Slider(quality, { quality = it }, valueRange = 70f..100f)
        Text("immobilité avant photo ${stillMs.toInt()} ms, tolérance ${String.format(Locale.ROOT, "%.1f", stillPx)}/256", fontSize = 13.sp)
        Slider(stillMs, { stillMs = it }, valueRange = 100f..1500f); Slider(stillPx, { stillPx = it }, valueRange = 0.5f..5f)

        Text("Vidéo", fontSize = 16.sp)
        FlowChips(listOf("UHD" to "4K", "FHD" to "1080p", "HD" to "720p"), vq) { vq = it }
        Text("débit ${vmbps.toInt()} Mbit/s", fontSize = 13.sp); Slider(vmbps, { vmbps = it }, valueRange = 8f..100f)

        Text("Suivi", fontSize = 16.sp)
        OutlinedTextField(aside, { aside = it.filter { c -> c.isDigit() } }, label = { Text("largeur du flux d'analyse (px)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(umpx, { umpx = it }, label = { Text("calibration ${st.objective} (µm par pixel de photo)") }, singleLine = true, modifier = Modifier.fillMaxWidth())

        Button(onClick = {
            st.serverUrl = url; st.token = token; st.mode = mode; st.cameraId = cameraId; st.physicalId = physical; st.zoom = zoom; st.zoomByCrop = zoomCrop
            st.focusMode = focus; st.focusDiopters = diopters; st.exposureMode = expo; st.shutterMs = shutter; st.iso = iso.toInt(); st.evComp = ev.toInt()
            st.wbMode = wb; st.wbKelvin = kelvin.toInt(); st.photoMaxSide = side.toIntOrNull() ?: 0; st.photoQuality = quality.toInt(); st.videoQuality = vq; st.videoMbps = vmbps.toInt()
            st.stillMs = stillMs.toInt(); st.stillPx = stillPx; st.analysisSide = (aside.toIntOrNull() ?: 640).coerceIn(256, 1920); st.sendLog = sendLog; st.depth = depth
            umpx.replace(',', '.').toFloatOrNull()?.let { st.setUmPerPx(st.objective, it) }
            Journal.log("réglages enregistrés")
            onApplied()
        }, modifier = Modifier.fillMaxWidth()) { Text("Enregistrer et appliquer") }
        Spacer(Modifier.height(24.dp))
    }
}

// ---------------------------------------------------------------- diagnostic
@Composable
fun DiagnosticScreen(engine: CameraEngine) {
    val ctx = LocalContext.current
    val live by engine.live.collectAsState()
    val txt = remember { engine.diagnosticText() }
    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Text("En direct : objectif actif ${live.activePhysical ?: "?"}, pose ${live.exposureNs?.let { String.format(Locale.ROOT, "%.2f ms", it / 1e6) } ?: "?"}, ISO ${live.iso ?: "?"}, focus ${live.focusDiopters ?: "?"} dpt, zoom ${live.zoomRatio ?: "?"}, recadrage ${live.crop ?: "?"}", fontSize = 12.sp)
        Row(Modifier.padding(vertical = 6.dp)) { OutlinedButton(onClick = { copy(ctx, txt + "\n\n" + Journal.text()) }) { Text("Copier le diagnostic et le journal") } }
        Text(txt, fontSize = 11.sp, fontFamily = FontFamily.Monospace, modifier = Modifier.verticalScroll(rememberScrollState()))
    }
}

@Composable
fun JournalScreen() {
    val ctx = LocalContext.current
    val lines by Journal.flow.collectAsState()
    Column(Modifier.fillMaxSize().padding(8.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) { OutlinedButton(onClick = { copy(ctx, Journal.text()) }) { Text("Copier") }; OutlinedButton(onClick = { Journal.clear() }) { Text("Effacer") } }
        LazyColumn(Modifier.fillMaxSize()) { items(lines.takeLast(600)) { Text(it, fontSize = 10.sp, fontFamily = FontFamily.Monospace, maxLines = 2, overflow = TextOverflow.Ellipsis) } }
    }
}

fun copy(ctx: Context, text: String) {
    val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    cm.setPrimaryClip(ClipData.newPlainText("scanlame", text))
}
