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
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.view.PreviewView
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MenuAnchorType
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.withContext
import org.json.JSONArray
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale
import kotlin.math.roundToInt

// Couleurs de la page web
val BG = Color(0xFF000000); val PANEL = Color(0xFF17151C); val FG = Color(0xFFF0ECF4); val MUTED = Color(0xFFA79CB1); val LINE = Color(0xFF2D2936)
val ACCENT = Color(0xFFC58AD6); val OK = Color(0xFF6FCF97); val WARN = Color(0xFFF2C94C); val REC = Color(0xFFEB5757)

class MainActivity : ComponentActivity() {
    private lateinit var engine: CameraEngine
    private lateinit var settings: Settings
    private lateinit var session: Session

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        settings = Settings(this)
        engine = CameraEngine(this)
        session = Session(this, engine, settings)
        Journal.log("ScanLame ${BuildConfig.VERSION_NAME} démarré sur ${Build.MANUFACTURER} ${Build.MODEL} (Android ${Build.VERSION.RELEASE})")
        Uploader.refresh(this)
        if (Uploader.pendingSeries(this).isNotEmpty()) Uploader.kick(this)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(primary = ACCENT, background = BG, surface = PANEL, onSurface = FG, onBackground = FG, outline = LINE)) {
                Surface(Modifier.fillMaxSize(), color = BG) { App(engine, settings, session) }
            }
        }
    }
}

/** Entrée de la galerie (lames.json) pour les listes « lame d'ensemble » et « compléter ». */
data class LameRef(val slug: String, val title: String, val objective: String?)

@Composable
fun App(engine: CameraEngine, st: Settings, session: Session) {
    val ctx = LocalContext.current
    val owner = LocalLifecycleOwner.current
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
    val live by engine.live.collectAsState()
    val bound by engine.bound.collectAsState()
    val camErr by engine.error.collectAsState()
    val locked by engine.locked.collectAsState()
    val up by Uploader.status.collectAsState()
    var cameraOn by remember { mutableStateOf(false) }
    var preview by remember { mutableStateOf<PreviewView?>(null) }
    var sheet by remember { mutableStateOf(false) }
    var msg by remember { mutableStateOf("Touche le bouton violet pour démarrer la caméra.") }
    var msgErr by remember { mutableStateOf(false) }
    var showObjPick by remember { mutableStateOf(false) }
    var zoom by remember { mutableStateOf(st.zoom) }
    var mapBig by remember { mutableStateOf(false) }
    var title by remember { mutableStateOf("") }
    var sample by remember { mutableStateOf("") }
    var comment by remember { mutableStateOf("") }
    var parent by remember { mutableStateOf("") }
    var complete by remember { mutableStateOf("") }
    var lames by remember { mutableStateOf<List<LameRef>>(emptyList()) }
    var bindKey by remember { mutableStateOf(0) }
    var update by remember { mutableStateOf<Updater.Info?>(null) }
    var updProgress by remember { mutableStateOf(-1) }
    val scope = androidx.compose.runtime.rememberCoroutineScope()
    val say = { t: String, e: Boolean -> msg = t; msgErr = e; if (e) Journal.log("MSG $t") }
    LaunchedEffect(Unit) { val u = Updater.check(); if (u != null && u.code > BuildConfig.VERSION_CODE) { update = u; Journal.log("mise à jour disponible : ${u.version} (installée : ${BuildConfig.VERSION_NAME})") } }
    val doUpdate: () -> Unit = {
        val u = update
        if (u != null && updProgress < 0) scope.launch {
            updProgress = 0
            val f = Updater.download(ctx, u) { updProgress = it }
            updProgress = -1
            if (f != null) Updater.install(ctx, f) else say("Téléchargement de la mise à jour impossible, voir le journal.", true)
        }
    }

    LaunchedEffect(sess.message) { if (sess.message.isNotEmpty()) say(sess.message, false) }
    LaunchedEffect(camErr) { camErr?.let { say(it, true) } }
    LaunchedEffect(Unit) { lames = withContext(Dispatchers.IO) { loadLames(st.galleryUrl) } }
    LaunchedEffect(cameraOn, preview, bindKey) {
        val pv = preview
        if (cameraOn && pv != null) engine.bind(owner, pv, st, st.mode, { img -> session.onFrame(img) }) { say("Prêt, caméra active. Pose le téléphone sur l'oculaire, règle la netteté à la vis, verrouille AF/AE puis lance la série.", false) }
        else if (!cameraOn) { engine.unbind(); engine.bound.value = null }
    }
    // zoom en direct
    LaunchedEffect(zoom) { if (cameraOn && bound != null) { delay(150); st.zoom = zoom; engine.applyControls(st) } }

    Box(Modifier.fillMaxSize().background(BG)) {
        // ---- scène : caméra plein écran
        AndroidView(factory = { c -> PreviewView(c).apply { implementationMode = PreviewView.ImplementationMode.COMPATIBLE; scaleType = PreviewView.ScaleType.FIT_CENTER; preview = this } }, modifier = Modifier.fillMaxSize())
        if (sess.running) Box(Modifier.fillMaxSize().border(4.dp, REC))

        Column(Modifier.fillMaxSize().statusBarsPadding()) {
            // état (pilule centrée)
            Box(Modifier.fillMaxWidth().padding(top = 12.dp), contentAlignment = Alignment.Center) {
                val (txt, bg, fg) = when {
                    sess.phase == "blank" -> Triple("FOND BLANC", Color(0xFFF5F0E6), Color(0xFF1A0F1F))
                    sess.phase == "switching" -> Triple("CHANGEMENT D'OBJECTIF", Color(0xFF5B2A86), Color.White)
                    sess.running -> Triple("● " + fmtTime(sess.elapsedMs), REC, Color.White)
                    up.busy -> Triple("ENVOI ${up.pendingFiles} fichier(s)", WARN, Color(0xFF1A0F1F))
                    cameraOn && bound != null -> Triple("Prêt, caméra active", Color(0xA6000000), FG)
                    else -> Triple("Caméra arrêtée", Color(0x99000000), MUTED)
                }
                Text(txt, Modifier.background(bg, RoundedCornerShape(999.dp)).padding(16.dp, 8.dp), color = fg, fontSize = 15.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
            }
            if (sess.running && sess.warn.isNotEmpty()) Box(Modifier.fillMaxWidth().padding(top = 6.dp), contentAlignment = Alignment.Center) {
                Text(sess.warn.uppercase(), Modifier.background(Color(0xE6EB5757), RoundedCornerShape(999.dp)).padding(14.dp, 6.dp), color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.Bold)
            }
            Row(Modifier.fillMaxWidth().padding(10.dp, 6.dp), horizontalArrangement = Arrangement.SpaceBetween) {
                // HUD gauche
                Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
                    val expo = live.exposureNs?.let { String.format(Locale.ROOT, "%.1f ms", it / 1e6) } ?: "?"
                    Hud(if (cameraOn && bound != null) "${engine.photoSize?.let { "${it.height}x${it.width}" } ?: engine.videoSize?.let { "${it.height}x${it.width}" } ?: ""} · cam ${live.activePhysical ?: "?"}" else "caméra arrêtée")
                    Hud(if (sess.mode == Settings.MODE_VIDEO && sess.running) "vidéo ${sess.bytes shr 20} Mo" else "${sess.photos} photo${if (sess.photos > 1) "s" else ""}${if (sess.blurred > 0) " · ${sess.blurred} floue(s)" else ""}${if (sess.bigSteps > 0) " · ${sess.bigSteps} pas trop grand(s)" else ""}", if (sess.blurred > 0) WARN else FG)
                    Hud("$expo · ISO ${live.iso ?: "?"} · netteté ${sess.sharp.toInt()}")
                    Hud(if (locked) "AF/AE verrouillés" else "AF/AE auto", if (locked) ACCENT else FG)
                    if (up.pendingFiles > 0) Hud("${if (st.autoUpload) "" else "envoi bloqué · "}${up.pendingFiles} en file (${up.pendingBytes shr 20} Mo)${up.lastError?.let { " · $it" } ?: ""}", Color(0xFF1A0F1F), WARN)
                }
                // suivi + carte
                Column(horizontalAlignment = Alignment.End, verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Row(Modifier.background(Color(0x8C000000), RoundedCornerShape(5.dp)).padding(7.dp, 3.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Box(Modifier.size(9.dp).background(when (sess.track) { "ok" -> OK; "lost" -> REC; "relocated" -> WARN; else -> MUTED }, CircleShape))
                        Text(when { !sess.running -> "suivi inactif"; sess.track == "ok" -> "suivi ok (${String.format(Locale.ROOT, "%.2f", sess.score)})"; sess.track == "lost" -> "suivi perdu : reviens sur une zone déjà vue"; sess.track == "relocated" -> "recalé sur la carte"; else -> "suivi en cours" }, color = FG, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.widthIn(max = 220.dp))
                    }
                    CoverageMap(sess, Modifier.size(if (mapBig) 340.dp else 150.dp).clickable { mapBig = !mapBig })
                }
            }
            // phases (fonctions en validation)
            if (sess.running && st.advanced) Column(Modifier.padding(10.dp, 4.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (sess.phase != "switching") {
                    WebButton(if (sess.phase == "blank") "Fond blanc ●" else "Fond blanc", pressed = sess.phase == "blank", small = true) { session.setBlank(sess.phase != "blank") }
                    WebButton("Changer d'objectif", small = true, enabled = sess.phase != "blank") { showObjPick = !showObjPick }
                    if (showObjPick) Row(Modifier.background(Color(0xF217151C), RoundedCornerShape(12.dp)).border(1.dp, LINE, RoundedCornerShape(12.dp)).padding(8.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        for (o in Settings.OBJECTIVES.filter { it != sess.objective }.take(4)) WebButton(o, small = true) { showObjPick = false; session.startSwitch(o) }
                    }
                } else WebButton("Reprendre", small = true) { session.resumeSwitch() }
            }
            Spacer(Modifier.weight(1f))
            // message, zoom, netteté (juste au-dessus de la barre)
            Column(Modifier.fillMaxWidth().padding(14.dp, 0.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (update != null && !sess.running) Row(Modifier.fillMaxWidth().background(Color(0xE6C58AD6), RoundedCornerShape(10.dp)).clickable { doUpdate() }.padding(12.dp, 10.dp), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                    Text("Mise à jour ${update!!.version} disponible (installée : ${BuildConfig.VERSION_NAME})", color = Color(0xFF1A0F1F), fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
                    Text(if (updProgress >= 0) "$updProgress %" else "Installer", color = Color(0xFF1A0F1F), fontSize = 14.sp, fontWeight = FontWeight.Bold)
                }
                if (msg.isNotEmpty()) Text(msg, Modifier.fillMaxWidth().background(if (msgErr) Color(0xD9EB5757) else Color(0xA6000000), RoundedCornerShape(10.dp)).padding(12.dp, 10.dp), color = FG, fontSize = 15.sp)
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("netteté", Modifier.background(Color(0x8C000000), RoundedCornerShape(6.dp)).padding(10.dp, 4.dp).width(60.dp), color = MUTED, fontSize = 14.sp)
                    // barre de nettete : echelle = meilleure photo de la serie (sinon 40) ; verte au-dessus de 70 %, orange, rouge sous 50 %
                    val scale = if (sess.sharpRef > 0) sess.sharpRef * 1.1f else 40f
                    val frac = (sess.sharp / scale).coerceIn(0f, 1f)
                    Box(Modifier.weight(1f).height(8.dp).background(Color(0x2EFFFFFF), RoundedCornerShape(4.dp))) { Box(Modifier.fillMaxWidth(frac).height(8.dp).background(if (sess.sharpRef <= 0 || frac >= 0.64f) OK else if (frac >= 0.45f) WARN else REC, RoundedCornerShape(4.dp))) }
                }
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("zoom ${String.format(Locale.ROOT, "%.1f", zoom)}×", Modifier.background(Color(0x8C000000), RoundedCornerShape(6.dp)).padding(10.dp, 4.dp), color = FG, fontSize = 14.sp)
                    Slider(zoom, { zoom = (it * 10).roundToInt() / 10f }, valueRange = 1f..st.zoomMax.coerceAtLeast(1.1f), modifier = Modifier.weight(1f), enabled = cameraOn)
                }
            }
            // ---- barre du bas : verrou | déclencheur | réglages
            Row(Modifier.fillMaxWidth().background(Color(0xEB0A090C)).navigationBarsPadding().padding(16.dp, 12.dp), verticalAlignment = Alignment.CenterVertically) {
                var autoUp by remember { mutableStateOf(st.autoUpload) }
                Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    WebButton(if (locked) "AF/AE verrouillés" else "Verrouiller AF/AE", pressed = locked, enabled = cameraOn && bound != null) { engine.setLocked(st, !locked) }
                    WebButton(if (autoUp) "Envoi auto" else "Envoi bloqué", pressed = !autoUp) { autoUp = !autoUp; st.autoUpload = autoUp; if (autoUp) Uploader.kick(ctx) else Uploader.refresh(ctx); say(if (autoUp) "Envoi automatique : la file part dès que possible." else "Envoi bloqué : les prises restent sur le téléphone jusqu'à « Envoi auto » ou « Envoyer maintenant » dans les réglages.", false) }
                }
                // déclencheur : violet = démarrer la caméra, blanc = lancer la série, rouge = arrêter
                val stop = sess.running
                Box(Modifier.size(88.dp).border(5.dp, if (stop) REC else Color.White, CircleShape).clickable(enabled = cameraOn.not() || bound != null) {
                    when {
                        !cameraOn -> { cameraOn = true; say("Démarrage de la caméra...", false) }
                        stop -> { say("Clôture en cours...", false); session.stop { Uploader.refresh(ctx) } }
                        else -> {
                            if (st.serverUrl.isEmpty()) say("Serveur non réglé : la prise restera sur le téléphone (file d'attente).", true)
                            session.start(title, sample, comment, parent.ifEmpty { null }, complete.ifEmpty { null })
                        }
                    }
                }, contentAlignment = Alignment.Center) {
                    if (stop) Box(Modifier.size(38.dp).background(REC, RoundedCornerShape(8.dp)))
                    else Box(Modifier.size(68.dp).background(if (cameraOn) Color.White else ACCENT, CircleShape))
                }
                Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    WebButton("Réglages") { sheet = true }
                    WebButton("Éteindre la caméra", danger = true, enabled = cameraOn && !sess.running) { cameraOn = false; say("Caméra arrêtée.", false) }
                }
            }
        }
        if (!granted) Box(Modifier.fillMaxSize().background(Color(0xCC000000)), contentAlignment = Alignment.Center) { Text("Autorisation caméra nécessaire.", color = FG) }
    }

    if (sheet) SettingsSheet(engine, st, cameraOn, lames, title, sample, comment, parent, complete,
        onFields = { t, s, c, p, co -> title = t; sample = s; comment = c; parent = p; complete = co },
        onClose = { changedCamera ->
            sheet = false
            if (cameraOn) { if (changedCamera) bindKey++ else engine.applyControls(st) }
            zoom = st.zoom
        })
    LaunchedEffect(sess.running) { while (sess.running) { delay(1000); Uploader.refresh(ctx) } }
}

fun fmtTime(ms: Long): String { val s = ms / 1000; return String.format(Locale.ROOT, "%02d:%02d", s / 60, s % 60) }

@Composable
fun Hud(t: String, fg: Color = FG, bg: Color = Color(0x8C000000)) {
    Text(t, Modifier.background(bg, RoundedCornerShape(6.dp)).padding(10.dp, 4.dp), color = fg, fontSize = 14.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
}

/** Bouton au style de la page (fond sombre, bordure, violet quand enfoncé, rouge pour les actions dangereuses). */
@Composable
fun WebButton(label: String, pressed: Boolean = false, danger: Boolean = false, small: Boolean = false, enabled: Boolean = true, onClick: () -> Unit) {
    val border = if (pressed) ACCENT else if (danger) Color(0x99EB5757) else LINE
    val fg = if (pressed) ACCENT else if (danger) Color(0xFFFF9A9A) else FG
    Text(label, Modifier.widthIn(max = 160.dp).background(if (pressed) Color(0x1FC58AD6) else Color(0xD91D1A22), RoundedCornerShape(12.dp)).border(1.dp, border, RoundedCornerShape(12.dp))
        .clickable(enabled = enabled) { onClick() }.padding(if (small) 12.dp else 10.dp, if (small) 10.dp else 12.dp).let { if (!enabled) it else it },
        color = if (enabled) fg else fg.copy(alpha = 0.4f), fontSize = 14.sp, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
}

@Composable
fun CoverageMap(s: Session.State, modifier: Modifier) {
    Canvas(modifier.background(Color(0xD917151C), RoundedCornerShape(8.dp)).border(1.dp, LINE, RoundedCornerShape(8.dp))) {
        val pl = s.placed
        if (pl.isEmpty()) return@Canvas
        val W = s.fieldW.toFloat(); val H = s.fieldH.toFloat()
        var x0 = Float.MAX_VALUE; var y0 = Float.MAX_VALUE; var x1 = -Float.MAX_VALUE; var y1 = -Float.MAX_VALUE
        for (p in pl) { x0 = minOf(x0, p.x.toFloat()); y0 = minOf(y0, p.y.toFloat()); x1 = maxOf(x1, p.x.toFloat() + W); y1 = maxOf(y1, p.y.toFloat() + H) }
        val pad = 8f
        val sc = minOf((size.width - 2 * pad) / (x1 - x0), (size.height - 2 * pad) / (y1 - y0))
        val ox = pad + ((size.width - 2 * pad) - (x1 - x0) * sc) / 2; val oy = pad + ((size.height - 2 * pad) - (y1 - y0) * sc) / 2
        for (p in pl) {
            val dx = (ox + (p.x.toFloat() - x0) * sc).roundToInt(); val dy = (oy + (p.y.toFloat() - y0) * sc).roundToInt()
            val dw = (W * sc).roundToInt().coerceAtLeast(1); val dh = (H * sc).roundToInt().coerceAtLeast(1)
            val th = p.thumb
            if (th != null) drawImage(th.asImageBitmap(), IntOffset(0, 0), IntSize(th.width, th.height), IntOffset(dx, dy), IntSize(dw, dh), alpha = 0.95f)
            else drawRect(Color(0x33FFFFFF), Offset(dx.toFloat(), dy.toFloat()), Size(dw.toFloat(), dh.toFloat()))
            if (p.photo && p.grade > 0) drawRect(if (p.grade == 2) REC else WARN, Offset(dx.toFloat(), dy.toFloat()), Size(dw.toFloat(), dh.toFloat()), style = Stroke(2f))
        }
        val cur = pl.last()
        drawRect(if (s.track == "lost") REC else if (s.track == "relocated") WARN else OK, Offset(ox + (cur.x.toFloat() - x0) * sc, oy + (cur.y.toFloat() - y0) * sc), Size(W * sc, H * sc), style = Stroke(2f))
    }
}

/** lames.json de la galerie : slug, titre, objectif (pour « lame d'ensemble » et « compléter »). */
fun loadLames(gallery: String): List<LameRef> = try {
    val c = URL(gallery + "lames/lames.json").openConnection() as HttpURLConnection
    c.connectTimeout = 8000; c.readTimeout = 8000
    val arr = JSONArray(c.inputStream.bufferedReader().readText()); c.disconnect()
    (0 until arr.length()).map { arr.getJSONObject(it) }.map { LameRef(it.getString("slug"), it.optString("title", it.getString("slug")), it.optString("objective").ifEmpty { null }) }
} catch (e: Exception) { Journal.log("galerie injoignable : ${e.message}"); emptyList() }

// ---------------------------------------------------------------- feuille de réglages (même ordre que la page)
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun Select(label: String, options: List<Pair<String, String>>, value: String, modifier: Modifier = Modifier, onChange: (String) -> Unit) {
    var open by remember { mutableStateOf(false) }
    ExposedDropdownMenuBox(expanded = open, onExpandedChange = { open = it }, modifier = modifier) {
        OutlinedTextField(value = options.firstOrNull { it.first == value }?.second ?: value, onValueChange = {}, readOnly = true, label = { Text(label, fontSize = 12.sp) },
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(open) }, modifier = Modifier.menuAnchor(MenuAnchorType.PrimaryNotEditable).fillMaxWidth(), singleLine = true)
        ExposedDropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            for ((v, l) in options) DropdownMenuItem(text = { Text(l) }, onClick = { onChange(v); open = false })
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsSheet(engine: CameraEngine, st: Settings, cameraOn: Boolean, lames: List<LameRef>, title0: String, sample0: String, comment0: String, parent0: String, complete0: String,
                  onFields: (String, String, String, String, String) -> Unit, onClose: (Boolean) -> Unit) {
    val ctx = LocalContext.current
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    var title by remember { mutableStateOf(title0) }
    var sample by remember { mutableStateOf(sample0) }
    var comment by remember { mutableStateOf(comment0) }
    var parent by remember { mutableStateOf(parent0) }
    var complete by remember { mutableStateOf(complete0) }
    var stain by remember { mutableStateOf(st.stain) }
    var objective by remember { mutableStateOf(st.objective) }
    var umpx by remember { mutableStateOf(st.umPerPx(st.objective).takeIf { it > 0 }?.toString() ?: "") }
    var mode by remember { mutableStateOf(st.mode) }
    var advanced by remember { mutableStateOf(st.advanced) }
    var focus by remember { mutableStateOf(st.focusMode) }
    var shutterCap by remember { mutableStateOf(st.shutterCapMs) }
    var ctemp by remember { mutableStateOf(st.ctemp) }
    var ev by remember { mutableStateOf(st.evComp) }
    var depth by remember { mutableStateOf(st.depth) }
    var cameraId by remember { mutableStateOf(st.cameraId) }
    var zoomMax by remember { mutableStateOf(st.zoomMax) }
    var zoomCrop by remember { mutableStateOf(st.zoomByCrop) }
    var aside by remember { mutableStateOf(st.analysisSide) }
    var vmbps by remember { mutableStateOf(st.videoMbps) }
    var vq by remember { mutableStateOf(st.videoQuality) }
    var stillMs by remember { mutableStateOf(st.stillMs) }
    var side by remember { mutableStateOf(st.photoMaxSide) }
    var quality by remember { mutableStateOf(st.photoQuality) }
    var url by remember { mutableStateOf(st.serverUrl) }
    var token by remember { mutableStateOf(st.token) }
    var sendLog by remember { mutableStateOf(st.sendLog) }
    var autoUpload by remember { mutableStateOf(st.autoUpload) }
    var gallery by remember { mutableStateOf(st.galleryUrl) }
    var storage by remember { mutableStateOf("") }
    var testMsg by remember { mutableStateOf("") }
    var showLog by remember { mutableStateOf(false) }
    var showDiag by remember { mutableStateOf(false) }
    val up by Uploader.status.collectAsState()
    val cams = remember { engine.cameraIds().mapNotNull { engine.describe(it) }.filter { it.facing == "arrière" } }
    val cam0 = remember { st.cameraId }
    fun save(): Boolean {
        val camChanged = cam0 != cameraId || st.mode != mode || st.analysisSide != aside || st.photoMaxSide != side || st.videoQuality != vq || st.videoMbps != vmbps || st.photoQuality != quality
        st.stain = stain; st.objective = objective; umpx.replace(',', '.').toFloatOrNull()?.let { st.setUmPerPx(objective, it) }
        st.mode = mode; st.advanced = advanced; st.focusMode = focus; st.shutterCapMs = shutterCap; st.ctemp = ctemp; st.evComp = ev; st.depth = depth
        st.cameraId = cameraId; st.zoomMax = zoomMax; st.zoom = st.zoom.coerceAtMost(zoomMax); st.zoomByCrop = zoomCrop; st.analysisSide = aside
        st.videoMbps = vmbps; st.videoQuality = vq; st.stillMs = stillMs; st.photoMaxSide = side; st.photoQuality = quality
        st.serverUrl = url; st.token = token; st.sendLog = sendLog; st.autoUpload = autoUpload; st.galleryUrl = gallery
        if (autoUpload && up.pendingFiles > 0) Uploader.kick(ctx)
        onFields(title, sample, comment, parent, complete)
        Journal.log("réglages enregistrés")
        return camChanged
    }
    LaunchedEffect(url) { storage = if (url.trimEnd('/').endsWith("scanlame")) (Uploader.serverStatus(url.trimEnd('/'))?.let { "stockage : ${it.optLong("used_bytes") shr 30} Go sur ${it.optLong("limit_bytes") shr 30} Go, ${it.optInt("series")} prise(s) brutes" } ?: "serveur injoignable") else "" }

    ModalBottomSheet(onDismissRequest = { onClose(save()) }, sheetState = sheetState, containerColor = PANEL, dragHandle = null) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(16.dp, 12.dp).navigationBarsPadding(), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                Text("Réglages · ScanLame ${BuildConfig.VERSION_NAME}", fontSize = 15.sp, fontWeight = FontWeight.Bold, color = FG)
                WebButton("Fermer", small = true) { onClose(save()) }
            }
            OutlinedTextField(title, { title = it }, label = { Text("Nom de la lame (pour la galerie)") }, placeholder = { Text("ex. Gram, prélèvement vaginal, 14/10") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Select("Coloration", Settings.STAINS, stain, Modifier.weight(1f)) { stain = it }
                Select("Objectif", Settings.OBJECTIVES.map { it to (if (it == "x50" || it == "x100") "$it (immersion)" else it) }, objective, Modifier.weight(1f)) { objective = it; umpx = st.umPerPx(it).takeIf { v -> v > 0 }?.toString() ?: "" }
            }
            OutlinedTextField(sample, { sample = it }, label = { Text("Prélèvement") }, placeholder = { Text("ex. hémoculture, ECBU, LCR…") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(comment, { comment = it }, label = { Text("Commentaire (optionnel)") }, placeholder = { Text("ex. CGP en amas, PNN nombreux") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            Select("Lame d'ensemble (si cette prise est un détail à plus fort grossissement)", listOf("" to "Aucune : nouvelle lame") + lames.map { it.slug to "${it.title}${it.objective?.let { o -> " (obj. $o)" } ?: ""}" }, parent) { parent = it; if (it.isNotEmpty()) complete = "" }
            Select("Compléter une lame existante (reprise des zones manquantes, même objectif)", listOf("" to "Non : nouvelle lame") + lames.map { it.slug to "${it.title}${it.objective?.let { o -> " (obj. $o)" } ?: ""}" }, complete) { complete = it; if (it.isNotEmpty()) { parent = ""; lames.firstOrNull { l -> l.slug == it }?.objective?.let { o -> objective = o } } }
            OutlinedTextField(umpx, { umpx = it }, label = { Text("Échelle pour cet objectif : µm par pixel de photo (optionnel)") }, placeholder = { Text("ex. 0.085 (mesuré avec un micromètre objet, à ce zoom)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Select("Mode", listOf(Settings.MODE_PHOTOS to "Photos à chaque arrêt de platine", Settings.MODE_VIDEO to "Vidéo (encodeur matériel)"), mode, Modifier.weight(1f)) { mode = it }
                Select("Profondeur (vis micrométrique)", listOf("off" to "désactivée : image la plus nette à chaque arrêt", "on" to "fusion + plans navigables (en essai)"), depth, Modifier.weight(1f)) { depth = it }
            }
            Row(verticalAlignment = Alignment.CenterVertically) { Checkbox(advanced, { advanced = it }); Text("Fonctions en cours de validation : boutons « Fond blanc » et « Changer d'objectif » pendant la prise", fontSize = 14.sp, color = FG) }
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Select("Mise au point au verrouillage", listOf("inf" to "infini (oculaire à 0 dioptrie, recommandé)", "keep" to "garder la distance de l'autofocus"), focus, Modifier.weight(1f)) { focus = it }
                Select("Temps de pose au verrouillage", listOf("0" to "garder la valeur courante", "8.3" to "1/120 s au plus (monter la lampe si sombre)", "4" to "1/250 s au plus", "2" to "1/500 s au plus"), if (shutterCap == 0f) "0" else shutterCap.toString().removeSuffix(".0"), Modifier.weight(1f)) { shutterCap = it.toFloat() }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Select("Température de couleur au verrouillage", listOf("0" to "garder la valeur courante", "3200" to "3200 K (halogène)", "4000" to "4000 K", "5000" to "5000 K (LED blanche)", "5600" to "5600 K (lumière du jour)"), ctemp.toString(), Modifier.weight(1f)) { ctemp = it.toInt() }
                Select("Compensation d'exposition", listOf("-3" to "-1 IL", "-2" to "-0,7 IL", "-1" to "-0,3 IL", "0" to "0", "1" to "+0,3 IL", "2" to "+0,7 IL"), ev.toString(), Modifier.weight(1f)) { ev = it.toInt() }
            }
            Column {
                Text("Photo pleine résolution (essai)", fontSize = 14.sp, color = MUTED)
                WebButton("Prendre une photo d'essai et mesurer sa taille", enabled = cameraOn && mode == Settings.MODE_PHOTOS) {
                    val f = File(ctx.cacheDir, "essai.jpg"); val t0 = System.currentTimeMillis()
                    engine.takePhoto(f) { ok, err ->
                        testMsg = if (ok) { val o = android.graphics.BitmapFactory.Options().apply { inJustDecodeBounds = true }; android.graphics.BitmapFactory.decodeFile(f.path, o)
                            "photo d'essai : ${o.outWidth} x ${o.outHeight}, ${String.format(Locale.ROOT, "%.1f", f.length() / 1048576.0)} Mo, ${System.currentTimeMillis() - t0} ms" } else "photo d'essai refusée : $err"
                        Journal.log(testMsg)
                    }
                }
                if (testMsg.isNotEmpty()) Text(testMsg, fontSize = 12.sp, color = MUTED)
            }
            Select("Caméra", cams.map { it.id to it.label }, cameraId) { cameraId = it }
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Select("Zoom maximal", listOf("2.9" to "2,9×", "4" to "4× (recadrage capteur)", "6" to "6× (recadrage capteur)"), zoomMax.toString().removeSuffix(".0"), Modifier.weight(1f)) { zoomMax = it.toFloat() }
                Select("Zoom par", listOf("crop" to "recadrage capteur (même objectif)", "ratio" to "ratio Android (peut changer d'objectif)"), if (zoomCrop) "crop" else "ratio", Modifier.weight(1f)) { zoomCrop = it == "crop" }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Select("Flux d'analyse (suivi)", listOf("480" to "réduit (480 px)", "640" to "moyen (640 px)", "960" to "grand (960 px)"), aside.toString(), Modifier.weight(1f)) { aside = it.toInt() }
                Select("Débit vidéo", listOf("15" to "15 Mbit/s", "30" to "30 Mbit/s", "60" to "60 Mbit/s", "100" to "100 Mbit/s"), vmbps.toString(), Modifier.weight(1f)) { vmbps = it.toInt() }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Select("Définition vidéo", listOf("UHD" to "4K", "FHD" to "1080p", "HD" to "720p"), vq, Modifier.weight(1f)) { vq = it }
                Select("Immobilité avant photo", listOf("200" to "0,2 s", "350" to "0,35 s", "500" to "0,5 s", "1000" to "1 s"), stillMs.toString(), Modifier.weight(1f)) { stillMs = it.toInt() }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Select("Taille photo", listOf("0" to "pleine (12 MP)", "3072" to "3/4", "2048" to "1/2"), side.toString(), Modifier.weight(1f)) { side = it.toInt() }
                Select("Qualité JPEG", listOf("95" to "95 %", "92" to "92 %", "85" to "85 %", "75" to "75 %"), quality.toString(), Modifier.weight(1f)) { quality = it.toInt() }
            }
            Text("ENVOI AU SERVEUR", fontSize = 12.sp, color = MUTED, letterSpacing = 1.sp, modifier = Modifier.padding(top = 8.dp))
            OutlinedTextField(url, { url = it }, label = { Text("URL du serveur (vide = garder sur le téléphone)") }, placeholder = { Text("https://xxxx.supabase.co/functions/v1/scanlame") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(token, { token = it }, label = { Text("Jeton d'accès") }, placeholder = { Text("le même que sur le serveur") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            Row(verticalAlignment = Alignment.CenterVertically) { Checkbox(sendLog, { sendLog = it }); Text("Joindre le journal du téléphone à la prise (mise au point de l'application)", fontSize = 14.sp, color = FG) }
            Row(verticalAlignment = Alignment.CenterVertically) { Checkbox(autoUpload, { autoUpload = it }); Text("Envoi automatique pendant la prise (décoché : tout reste sur le téléphone jusqu'à « Envoyer maintenant »)", fontSize = 14.sp, color = FG) }
            OutlinedTextField(gallery, { gallery = it }, label = { Text("Adresse de la galerie (listes des lames)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            if (storage.isNotEmpty()) Text(storage, fontSize = 12.sp, color = MUTED)
            Text("FILE D'ATTENTE (ENVOIS EN PANNE DE RÉSEAU)", fontSize = 12.sp, color = MUTED, letterSpacing = 1.sp, modifier = Modifier.padding(top = 8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
                WebButton("Envoyer maintenant", enabled = up.pendingFiles > 0) { st.serverUrl = url; st.token = token; Uploader.kick(ctx, force = true) }
                WebButton("Abandonner", danger = true, enabled = up.pendingFiles > 0) { Uploader.queueDir(ctx).deleteRecursively(); Uploader.refresh(ctx); Journal.log("file d'attente abandonnée") }
            }
            Text(if (up.pendingFiles == 0) "rien en attente" else "${up.pendingFiles} fichier(s), ${up.pendingBytes shr 20} Mo${up.lastError?.let { " · dernière erreur : $it" } ?: ""}${if (up.busy) " · envoi en cours" else ""}", fontSize = 12.sp, color = MUTED)
            Text(if (showLog) "▾ Journal" else "▸ Journal", fontSize = 12.sp, color = MUTED, modifier = Modifier.clickable { showLog = !showLog }.padding(vertical = 6.dp))
            if (showLog) {
                val lines by Journal.flow.collectAsState()
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) { WebButton("Copier le journal", small = true) { copy(ctx, Journal.text()) }; WebButton("Effacer", small = true) { Journal.clear() } }
                Text(lines.takeLast(300).joinToString("\n"), fontSize = 11.sp, fontFamily = FontFamily.Monospace, color = MUTED, modifier = Modifier.fillMaxWidth().background(BG, RoundedCornerShape(8.dp)).border(1.dp, LINE, RoundedCornerShape(8.dp)).padding(8.dp))
            }
            Text(if (showDiag) "▾ Diagnostic des caméras" else "▸ Diagnostic des caméras", fontSize = 12.sp, color = MUTED, modifier = Modifier.clickable { showDiag = !showDiag }.padding(vertical = 6.dp))
            if (showDiag) {
                val txt = remember { engine.diagnosticText() }
                WebButton("Copier le diagnostic", small = true) { copy(ctx, txt) }
                Text(txt, fontSize = 11.sp, fontFamily = FontFamily.Monospace, color = MUTED, modifier = Modifier.fillMaxWidth().background(BG, RoundedCornerShape(8.dp)).border(1.dp, LINE, RoundedCornerShape(8.dp)).padding(8.dp))
            }
            Spacer(Modifier.height(16.dp))
        }
    }
}

fun copy(ctx: Context, text: String) {
    val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    cm.setPrimaryClip(ClipData.newPlainText("scanlame", text))
}
