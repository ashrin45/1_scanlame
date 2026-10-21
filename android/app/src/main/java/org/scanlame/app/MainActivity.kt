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
import androidx.compose.foundation.layout.imePadding
import androidx.compose.material3.CheckboxDefaults
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.requiredSize
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
import androidx.compose.material3.TextButton
import androidx.compose.material3.AlertDialog
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
import androidx.compose.foundation.layout.heightIn
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.foundation.gestures.transformable
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.Image
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
import kotlinx.coroutines.flow.MutableStateFlow
import android.content.Intent
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
// palette Paillasse, theme « champ polarise » (sombre) : polar = interactif, biref = ce qui s'illumine (patron, reperes), ok / alert = juste et faux
val BG = Color(0xFF0D1017); val PANEL = Color(0xFF161B26); val FG = Color(0xFFE9EDF4); val MUTED = Color(0xFF9AA4B5); val LINE = Color(0xFF2A3140)
val ACCENT = Color(0xFFA99AF0); val ACCENT_TINT = Color(0xFF262242); val BIREF = Color(0xFFE3B95C); val BIREF_TINT = Color(0xFF3A2F17)
val OK = Color(0xFF7FD6A4); val WARN = Color(0xFFE3B95C); val REC = Color(0xFFF08AA3); val REC_TINT = Color(0xFF3B1D26)
val CAPSULE = Color(0xD10D1017)   // capsules posees sur le flux camera
val Sans = androidx.compose.ui.text.font.FontFamily(androidx.compose.ui.text.font.Font(R.font.figtree, FontWeight.Normal), androidx.compose.ui.text.font.Font(R.font.figtree, FontWeight.Medium), androidx.compose.ui.text.font.Font(R.font.figtree, FontWeight.SemiBold), androidx.compose.ui.text.font.Font(R.font.figtree, FontWeight.Bold))
val Mono = androidx.compose.ui.text.font.FontFamily(androidx.compose.ui.text.font.Font(R.font.plex_mono_regular, FontWeight.Normal), androidx.compose.ui.text.font.Font(R.font.plex_mono_semibold, FontWeight.SemiBold))
val Display = androidx.compose.ui.text.font.FontFamily(androidx.compose.ui.text.font.Font(R.font.young_serif, FontWeight.Normal))
val AppTypography = androidx.compose.material3.Typography().let { t ->
    fun androidx.compose.ui.text.TextStyle.sans() = copy(fontFamily = Sans)
    t.copy(displayLarge = t.displayLarge.copy(fontFamily = Display), displayMedium = t.displayMedium.copy(fontFamily = Display), displaySmall = t.displaySmall.copy(fontFamily = Display),
        headlineLarge = t.headlineLarge.copy(fontFamily = Display), headlineMedium = t.headlineMedium.copy(fontFamily = Display), headlineSmall = t.headlineSmall.copy(fontFamily = Display),
        titleLarge = t.titleLarge.copy(fontFamily = Display), titleMedium = t.titleMedium.sans(), titleSmall = t.titleSmall.sans(),
        bodyLarge = t.bodyLarge.sans(), bodyMedium = t.bodyMedium.sans(), bodySmall = t.bodySmall.sans(), labelLarge = t.labelLarge.sans(), labelMedium = t.labelMedium.sans(), labelSmall = t.labelSmall.sans())
}

class MainActivity : ComponentActivity() {
    private lateinit var engine: CameraEngine
    private lateinit var settings: Settings
    private lateinit var session: Session

    override fun onNewIntent(intent: Intent) { super.onNewIntent(intent); handleLink(intent) }

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
        handleLink(intent)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(primary = ACCENT, onPrimary = BG, primaryContainer = ACCENT_TINT, background = BG, surface = PANEL, surfaceContainerHigh = PANEL, onSurface = FG, onSurfaceVariant = MUTED, onBackground = FG, outline = LINE, outlineVariant = LINE, error = REC),
                typography = AppTypography) {
                Surface(Modifier.fillMaxSize(), color = BG) {
                    androidx.compose.runtime.CompositionLocalProvider(androidx.compose.material3.LocalTextStyle provides androidx.compose.ui.text.TextStyle(fontFamily = Sans, color = FG)) { App(engine, settings, session) }
                }
            }
        }
    }
}

/** Lien scanlame://reprendre/<slug> (QR code de la page de la lame) ou scanlame://ensemble/<slug> : la lame est preselectionnee. */
val pendingLink = MutableStateFlow<Pair<String, String>?>(null)
fun handleLink(intent: Intent?) {
    val u = intent?.data ?: return
    if (u.scheme != "scanlame") return
    val slug = u.pathSegments?.firstOrNull()?.lowercase()?.filter { it.isLetterOrDigit() || it == '-' } ?: return
    val kind = u.host ?: return
    Journal.log("lien reçu : $kind $slug")
    pendingLink.value = kind to slug
}

/** Entrée de la galerie (lames.json) pour les listes « lame d'ensemble » et « compléter ». */
data class LameRef(val slug: String, val title: String, val objective: String?, val width: Int = 0, val height: Int = 0, val tile: Int = 0, val maxlvl: Int = 0, val thumb: String = "")

@OptIn(ExperimentalLayoutApi::class, ExperimentalMaterial3Api::class)
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
    var startDlg by remember { mutableStateOf(false) }
    var msg by remember { mutableStateOf("Touche le bouton violet pour démarrer la caméra.") }
    var msgErr by remember { mutableStateOf(false) }
    var showObjPick by remember { mutableStateOf(false) }
    var switchDlg by remember { mutableStateOf<String?>(null) }
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
    // lien QR « Reprendre » / « Ajouter un détail » : lame preselectionnee, titre affiche
    val link by pendingLink.collectAsState()
    LaunchedEffect(link) {
        val l = link ?: return@LaunchedEffect
        val ll = withContext(Dispatchers.IO) { loadLames(st.galleryUrl) }; if (ll.isNotEmpty()) lames = ll
        val ref = ll.firstOrNull { it.slug == l.second }
        val name = ref?.title ?: l.second
        if (l.first == "reprendre") { complete = l.second; parent = ""; title = ref?.title ?: title; say("Reprise de « $name » : filme seulement les zones manquantes (même objectif), puis clôture.", false) }
        else if (l.first == "ensemble") { parent = l.second; complete = ""; say("Détail de « $name » : lance la série au fort grossissement, la lame d'ensemble est déjà choisie.", false) }
        pendingLink.value = null
    }
    // galerie relue à chaque ouverture des réglages (une lame publiée pendant que l'appli tourne doit apparaître)
    LaunchedEffect(sheet) { val l = withContext(Dispatchers.IO) { loadLames(st.galleryUrl) }; if (l.isNotEmpty()) lames = l }
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
        if (sess.running) {
            // cadre d'enregistrement : couleur de la derniere photo (vert bonne, orange moyenne, rouge ratee ou pas trop grand), clignote a chaque prise
            var flash by remember { mutableStateOf(false) }
            LaunchedEffect(sess.lastShotAt) { if (sess.lastShotAt > 0) { repeat(2) { flash = true; delay(140); flash = false; delay(90) } } }
            val col = when (sess.lastGrade) { 0 -> OK; 1 -> WARN; 2 -> REC; else -> Color(0x59E9EDF4) }
            Box(Modifier.fillMaxSize().border(if (flash) 14.dp else 5.dp, col))
        }

        Column(Modifier.fillMaxSize().statusBarsPadding()) {
            // état (pilule centrée)
            Box(Modifier.fillMaxWidth().padding(top = 12.dp), contentAlignment = Alignment.Center) {
                val (txt, bg, fg) = when {
                    sess.phase == "blank" -> Triple("FOND BLANC", Color(0xFFF5F0E6), Color(0xFF1A0F1F))
                    sess.phase == "switching" -> Triple("CHANGEMENT D'OBJECTIF", ACCENT_TINT, ACCENT)
                    sess.running -> Triple("● " + fmtTime(sess.elapsedMs), REC, Color.White)
                    up.busy -> Triple("ENVOI ${up.pendingFiles} fichier(s)", BIREF_TINT, BIREF)
                    cameraOn && bound != null -> Triple("Prêt, caméra active", CAPSULE, FG)
                    else -> Triple("Caméra arrêtée", CAPSULE, MUTED)
                }
                Text(txt, Modifier.background(bg, RoundedCornerShape(999.dp)).padding(16.dp, 8.dp), color = fg, fontSize = 15.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
            }
            if (sess.running && sess.warn.isNotEmpty()) Box(Modifier.fillMaxWidth().padding(top = 6.dp), contentAlignment = Alignment.Center) {
                Text(sess.warn.uppercase(), Modifier.background(REC_TINT, RoundedCornerShape(999.dp)).border(1.dp, REC, RoundedCornerShape(999.dp)).padding(14.dp, 6.dp), color = REC, fontSize = 13.sp, fontWeight = FontWeight.Bold)
            }
            Row(Modifier.fillMaxWidth().padding(10.dp, 6.dp), horizontalArrangement = Arrangement.SpaceBetween) {
                // HUD gauche : prend ce qui reste ; la colonne de droite (suivi + carte) garde sa taille
                Column(Modifier.weight(1f).padding(end = 6.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                    val expo = live.exposureNs?.let { String.format(Locale.ROOT, "%.1f ms", it / 1e6) } ?: "?"
                    Hud(if (cameraOn && bound != null) "${engine.photoSize?.let { "${it.height}x${it.width}" } ?: engine.videoSize?.let { "${it.height}x${it.width}" } ?: ""} · cam ${live.activePhysical ?: "?"}" else "caméra arrêtée")
                    Hud(if (sess.mode == Settings.MODE_VIDEO && sess.running) "vidéo ${sess.bytes shr 20} Mo" else "${sess.photos} photo${if (sess.photos > 1) "s" else ""}${if (sess.blurred > 0) " · ${sess.blurred} floue(s)" else ""}${if (sess.patron != null) " · patron ${(sess.patronFill * 100).roundToInt()} %" else ""}${if (sess.relocations > 0) " · ${sess.relocations} reloc." else ""}${if (sess.bigSteps > 0) " · ${sess.bigSteps} pas trop grand(s)" else ""}", if (sess.blurred > 0) WARN else FG)
                    if (sess.running && sess.depth) Hud("${if (st.burstFor(sess.objective)) "rafale" else "profondeur"} · champ en cours : ${sess.stackPlanes} plan${if (sess.stackPlanes > 1) "s" else ""}", ACCENT)
                    Hud("$expo · ISO ${live.iso ?: "?"} · netteté ${sess.sharp.toInt()}")
                    Hud(if (locked) "AF/AE verrouillés" else "AF/AE auto", if (locked) ACCENT else FG)
                    if (up.pendingFiles > 0) Hud("${if (st.autoUpload) "" else "envoi bloqué · "}${up.pendingFiles} en file (${up.pendingBytes shr 20} Mo)${up.lastError?.let { " · $it" } ?: ""}", Color(0xFF1A0F1F), WARN)
                }
                // suivi + carte
                Column(horizontalAlignment = Alignment.End, verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Row(Modifier.background(CAPSULE, RoundedCornerShape(999.dp)).border(1.dp, LINE, RoundedCornerShape(999.dp)).padding(10.dp, 4.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Box(Modifier.size(9.dp).background(when (sess.track) { "ok" -> OK; "lost" -> REC; "relocated" -> WARN; else -> MUTED }, CircleShape))
                        Text(when { !sess.running -> "suivi inactif"; sess.track == "ok" -> "suivi ok (${String.format(Locale.ROOT, "%.2f", sess.score)})"; sess.track == "lost" -> "suivi perdu : reviens sur une zone déjà vue"; sess.track == "relocated" -> "recalé sur la carte"; else -> "suivi en cours" }, color = FG, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.widthIn(max = 220.dp))
                    }
                    CoverageMap(sess, Modifier.requiredSize(if (mapBig) 340.dp else 150.dp).clickable { mapBig = !mapBig })
                }
            }
            // phases (fonctions en validation)
            if (sess.running && st.advanced) Column(Modifier.padding(10.dp, 4.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (sess.phase != "switching") {
                    WebButton(if (sess.phase == "blank") "Fond blanc ●" else "Fond blanc", pressed = sess.phase == "blank", small = true) { session.setBlank(sess.phase != "blank") }
                    WebButton("Changer d'objectif", small = true, enabled = sess.phase != "blank") { showObjPick = !showObjPick }
                    WebButton(if (sess.reinforce) "Renforcer : actif" else "Renforcer", small = true, pressed = sess.reinforce) { session.setReinforce(!sess.reinforce) }
                    if (showObjPick) FlowRow(Modifier.background(PANEL, RoundedCornerShape(12.dp)).border(1.dp, LINE, RoundedCornerShape(12.dp)).padding(8.dp), verticalArrangement = Arrangement.spacedBy(6.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        for (o in Settings.OBJECTIVES.filter { it != sess.objective }) WebButton(o, small = true) { showObjPick = false; switchDlg = o }
                    }
                } else WebButton("Reprendre", small = true) { session.resumeSwitch() }
            }
            Spacer(Modifier.weight(1f))
            // message, zoom, netteté (juste au-dessus de la barre)
            Column(Modifier.fillMaxWidth().padding(14.dp, 0.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (cameraOn && bound != null && !sess.running && update == null) PreCheck(sess, live, locked)
                if (update != null && !sess.running) Row(Modifier.fillMaxWidth().background(ACCENT, RoundedCornerShape(12.dp)).clickable { doUpdate() }.padding(12.dp, 10.dp), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                    Text("Mise à jour ${update!!.version} disponible (installée : ${BuildConfig.VERSION_NAME})", color = BG, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
                    Text(if (updProgress >= 0) "$updProgress %" else "Installer", color = BG, fontSize = 14.sp, fontWeight = FontWeight.Bold)
                }
                if (msg.isNotEmpty()) Text(msg, Modifier.fillMaxWidth().background(if (msgErr) REC_TINT else CAPSULE, RoundedCornerShape(12.dp)).padding(12.dp, 10.dp), color = FG, fontSize = 15.sp)
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("netteté", Modifier.background(CAPSULE, RoundedCornerShape(999.dp)).padding(10.dp, 4.dp).width(60.dp), color = MUTED, fontSize = 14.sp)
                    // barre de nettete : echelle = meilleure photo de la serie (sinon 40) ; verte au-dessus de 70 %, orange, rouge sous 50 %
                    val scale = if (sess.sharpRef > 0) sess.sharpRef * 1.1f else 40f
                    val frac = (sess.sharp / scale).coerceIn(0f, 1f)
                    Box(Modifier.weight(1f).height(8.dp).background(Color(0xB30D1017), RoundedCornerShape(4.dp))) { Box(Modifier.fillMaxWidth(frac).height(8.dp).background(if (sess.sharpRef <= 0 || frac >= 0.64f) OK else if (frac >= 0.45f) WARN else REC, RoundedCornerShape(4.dp))) }
                }
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("zoom ${String.format(Locale.ROOT, "%.1f", zoom)}×", Modifier.background(CAPSULE, RoundedCornerShape(999.dp)).padding(10.dp, 4.dp), color = FG, fontSize = 14.sp)
                    Slider(zoom, { zoom = (it * 10).roundToInt() / 10f }, valueRange = 1f..st.zoomMax.coerceAtLeast(1.1f), modifier = Modifier.weight(1f), enabled = cameraOn)
                }
            }
            // ---- barre du bas : verrou | déclencheur | réglages
            Row(Modifier.fillMaxWidth().background(androidx.compose.ui.graphics.Brush.verticalGradient(listOf(Color(0x000D1017), Color(0xEB0D1017)))).navigationBarsPadding().padding(16.dp, 12.dp), verticalAlignment = Alignment.CenterVertically) {
                var autoUp by remember { mutableStateOf(st.autoUpload) }
                Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    WebButton(if (locked) "AF/AE verrouillés" else "Verrouiller AF/AE", pressed = locked, enabled = cameraOn && bound != null) { engine.setLocked(st, !locked) }
                    WebButton(if (autoUp) "Envoi auto" else "Envoi bloqué", pressed = !autoUp) { autoUp = !autoUp; st.autoUpload = autoUp; if (autoUp) Uploader.kick(ctx) else Uploader.refresh(ctx); say(if (autoUp) "Envoi automatique : la file part dès que possible." else "Envoi bloqué : les prises restent sur le téléphone jusqu'à « Envoi auto » ou « Envoyer maintenant » dans les réglages.", false) }
                }
                // déclencheur : violet = démarrer la caméra, blanc = lancer la série, rouge = arrêter
                val stop = sess.running
                Box(Modifier.size(88.dp).border(4.dp, FG, CircleShape).clickable(enabled = cameraOn.not() || bound != null) {
                    when {
                        !cameraOn -> { cameraOn = true; say("Démarrage de la caméra...", false) }
                        stop -> { say("Clôture en cours...", false); session.stop { Uploader.refresh(ctx) } }
                        else -> startDlg = true   // nom de la lame et objectif demandés au départ
                    }
                }, contentAlignment = Alignment.Center) {
                    if (stop) Box(Modifier.size(38.dp).background(REC, RoundedCornerShape(8.dp)))
                    else Box(Modifier.size(66.dp).background(if (cameraOn) FG else ACCENT, CircleShape))
                }
                Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    WebButton("Réglages") { sheet = true }
                    var confirmDiscard by remember { mutableStateOf(false) }
                    LaunchedEffect(confirmDiscard) { if (confirmDiscard) { delay(4000); confirmDiscard = false } }
                    if (sess.running) WebButton(if (confirmDiscard) "Confirmer l'abandon" else "Abandonner la série", danger = true, pressed = confirmDiscard) {
                        if (confirmDiscard) { confirmDiscard = false; session.discard() } else { confirmDiscard = true; say("Abandonner la série en cours ? Appuie encore sur le bouton pour confirmer (4 s).", true) }
                    }
                    else WebButton("Éteindre la caméra", danger = true, enabled = cameraOn) { cameraOn = false; say("Caméra arrêtée.", false) }
                }
            }
        }
        if (!granted) Box(Modifier.fillMaxSize().background(Color(0xCC000000)), contentAlignment = Alignment.Center) { Text("Autorisation caméra nécessaire.", color = FG) }
    }

    switchDlg?.let { o ->
        AlertDialog(onDismissRequest = { switchDlg = null }, containerColor = PANEL, titleContentColor = FG, textContentColor = FG,
            title = { Text("Passer au $o") },
            text = { Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                PatronFields(o, st)
                if (st.depthAllowed(o)) {
                    var depth by remember(o) { mutableStateOf(st.depthFor(o)) }
                    Row(Modifier.fillMaxWidth().clickable { depth = !depth; st.setDepthFor(o, depth) }, verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(depth, { depth = it; st.setDepthFor(o, it) }, colors = CheckboxDefaults.colors(checkedColor = ACCENT, checkmarkColor = BG, uncheckedColor = MUTED))
                        Text("Profondeur au $o : plusieurs plans par champ", fontSize = 13.sp, color = FG)
                    }
                    if (depth) {
                        var burst by remember(o) { mutableStateOf(st.burstFor(o)) }
                        Row(Modifier.fillMaxWidth().padding(start = 32.dp).clickable { burst = !burst; st.setBurstFor(o, burst) }, verticalAlignment = Alignment.CenterVertically) {
                            Checkbox(burst, { burst = it; st.setBurstFor(o, it) }, colors = CheckboxDefaults.colors(checkedColor = ACCENT, checkmarkColor = BG, uncheckedColor = MUTED))
                            Text("Rafale : toutes les couches", fontSize = 13.sp, color = FG)
                        }
                    }
                }
                val lower = (Regex("\\d+").find(o)?.value?.toIntOrNull() ?: 0) < (Regex("\\d+").find(sess.objective)?.value?.toIntOrNull() ?: 0)
                Text(if (lower) "Le patron sera centré sur le détail que tu viens de prendre." else "Le patron se posera en haut à gauche, sur la première photo.", color = MUTED, fontSize = 12.sp)
            } },
            confirmButton = { TextButton(onClick = { switchDlg = null; session.startSwitch(o) }) { Text("Changer d'objectif", color = ACCENT) } },
            dismissButton = { TextButton(onClick = { switchDlg = null }) { Text("Annuler", color = MUTED) } })
    }
    if (startDlg) {
        // feuille de depart (canevas « Nouvelle lame ») : nom, objectif en quatre gros boutons, patron, rognage, prelevement
        var t by remember { mutableStateOf(title) }; var smp by remember { mutableStateOf(sample) }; var obj by remember { mutableStateOf(st.objective) }
        var autoCrop by remember { mutableStateOf(st.autoCrop) }
        val startState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
        ModalBottomSheet(onDismissRequest = { startDlg = false }, sheetState = startState, containerColor = PANEL, shape = RoundedCornerShape(topStart = 24.dp, topEnd = 24.dp),
            dragHandle = { Box(Modifier.padding(top = 12.dp).size(40.dp, 4.dp).background(LINE, RoundedCornerShape(999.dp))) }) {
            Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).imePadding().navigationBarsPadding().padding(start = 20.dp, end = 20.dp, top = 12.dp, bottom = 20.dp),
                verticalArrangement = Arrangement.spacedBy(18.dp)) {
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("Nouvelle lame", fontSize = 27.sp, lineHeight = 34.sp, fontFamily = Display, color = FG)
                    val stain = Settings.STAINS.firstOrNull { it.first == st.stain }?.second ?: st.stain
                    Text("$stain · zoom ${"%.1f".format(java.util.Locale.FRANCE, st.zoom)} · " + if (locked) "AF/AE verrouillés" else "AF/AE à verrouiller sur une zone vide", fontSize = 14.sp, color = if (locked) MUTED else BIREF)
                }
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    FieldLabel("Nom de la lame")
                    OutlinedTextField(t, { t = it }, placeholder = { Text("ex. hémoculture 1234") }, singleLine = true, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(6.dp))
                }
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    FieldLabel("Objectif")
                    // boutons plutot qu'une liste deroulante (les menus deroulants s'ouvrent mal dans une feuille)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        for (o in Settings.OBJECTIVES) GridPill(o, o == obj, Modifier.weight(1f)) { obj = o }
                    }
                    val n = Regex("\\d+").find(obj)?.value?.toIntOrNull() ?: 0
                    if (n >= 100) Text("Commence par le plus fort grossissement : au passage au x10, le détail sera au centre du patron.", fontSize = 13.sp, color = MUTED)
                }
                if (st.depthAllowed(obj)) {
                    var depth by remember(obj) { mutableStateOf(st.depthFor(obj)) }
                    Row(Modifier.fillMaxWidth().clickable { depth = !depth; st.setDepthFor(obj, depth) }, verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(depth, { depth = it; st.setDepthFor(obj, it) }, colors = CheckboxDefaults.colors(checkedColor = ACCENT, checkmarkColor = BG, uncheckedColor = MUTED))
                        Column {
                            Text("Profondeur au $obj : plusieurs plans par champ", fontSize = 15.sp, color = FG)
                            Text("Préparations épaisses : à chaque champ, tourne doucement la vis micrométrique ; une photo part quand une partie devient nette.", fontSize = 12.sp, color = MUTED)
                        }
                    }
                    if (depth) {
                        var burst by remember(obj) { mutableStateOf(st.burstFor(obj)) }
                        Row(Modifier.fillMaxWidth().padding(start = 32.dp).clickable { burst = !burst; st.setBurstFor(obj, burst) }, verticalAlignment = Alignment.CenterVertically) {
                            Checkbox(burst, { burst = it; st.setBurstFor(obj, it) }, colors = CheckboxDefaults.colors(checkedColor = ACCENT, checkmarkColor = BG, uncheckedColor = MUTED))
                            Column {
                                Text("Rafale : toutes les couches", fontSize = 15.sp, color = FG)
                                Text("Une photo toutes les 300 ms tant que la vis tourne (40 au plus par champ), pour la molette de mise au point du site.", fontSize = 12.sp, color = MUTED)
                            }
                        }
                    }
                }
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) { PatronFields(obj, st) }
                Row(Modifier.fillMaxWidth().clickable { autoCrop = !autoCrop; st.autoCrop = autoCrop }, verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(autoCrop, { autoCrop = it; st.autoCrop = it }, colors = CheckboxDefaults.colors(checkedColor = ACCENT, checkmarkColor = BG, uncheckedColor = MUTED))
                    Text("Rogner la lame au plus grand rectangle couvert", fontSize = 15.sp, color = FG)
                }
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    FieldLabel("Prélèvement (optionnel)")
                    OutlinedTextField(smp, { smp = it }, placeholder = { Text("ex. hémoculture, ECBU, LCR") }, singleLine = true, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(6.dp))
                }
                val par = lames.firstOrNull { it.slug == parent }?.title ?: st.recentSeries().firstOrNull { it.first == parent }?.second
                Row(Modifier.fillMaxWidth().background(ACCENT_TINT, RoundedCornerShape(12.dp)).padding(12.dp, 10.dp), horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text("i", Modifier.size(18.dp).border(2.dp, ACCENT, RoundedCornerShape(999.dp)), color = ACCENT, fontSize = 11.sp, fontWeight = FontWeight.Bold, textAlign = TextAlign.Center)
                    Text(if (par != null) "Détail de la lame d'ensemble « $par » (changer dans les réglages)." else "Autres options (coloration, lame d'ensemble, caméra) dans les réglages.", fontSize = 13.sp, color = FG)
                }
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("Annuler", Modifier.heightIn(min = 52.dp).border(1.dp, LINE, RoundedCornerShape(6.dp)).clickable { startDlg = false }.padding(horizontal = 18.dp, vertical = 15.dp), color = MUTED, fontSize = 15.sp, fontWeight = FontWeight.Medium)
                    Text("Lancer la série", Modifier.weight(1f).heightIn(min = 52.dp).background(ACCENT, RoundedCornerShape(6.dp)).clickable {
                        title = t; sample = smp; st.objective = obj; startDlg = false
                        if (st.serverUrl.isEmpty()) say("Serveur non réglé : la prise restera sur le téléphone (file d'attente).", true)
                        session.start(title, sample, comment, parent.ifEmpty { null }, complete.ifEmpty { null })
                    }.padding(vertical = 15.dp), color = BG, fontSize = 16.sp, fontWeight = FontWeight.SemiBold, textAlign = TextAlign.Center)
                }
            }
        }
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
fun Hud(t: String, fg: Color = FG, bg: Color = CAPSULE) {
    // capsule posee sur le flux : fond sombre translucide, filet, chiffres en mono
    Text(t, Modifier.background(bg, RoundedCornerShape(999.dp)).border(1.dp, LINE, RoundedCornerShape(999.dp)).padding(12.dp, 6.dp), color = fg, fontSize = 13.sp, fontFamily = Sans, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
}

/** Bouton au style de la page (fond sombre, bordure, violet quand enfoncé, rouge pour les actions dangereuses). */
@Composable
fun WebButton(label: String, pressed: Boolean = false, danger: Boolean = false, small: Boolean = false, enabled: Boolean = true, onClick: () -> Unit) {
    // pilule Paillasse : polar quand actif, alert pour l'action destructrice, sinon panneau sombre et filet ; cible tactile >= 44 dp
    val border = if (pressed) ACCENT else if (danger) REC_TINT else LINE
    val fg = if (pressed) ACCENT else if (danger) REC else FG
    val bg = if (pressed) ACCENT_TINT else CAPSULE
    Text(label, Modifier.widthIn(max = 180.dp).heightIn(min = if (small) 40.dp else 44.dp).background(bg, RoundedCornerShape(999.dp)).border(1.dp, border, RoundedCornerShape(999.dp))
        .clickable(enabled = enabled) { onClick() }.padding(horizontal = 14.dp, vertical = if (small) 10.dp else 12.dp),
        color = if (enabled) fg else fg.copy(alpha = 0.4f), fontSize = if (small) 13.sp else 14.sp, fontFamily = Sans, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
}

@Composable
fun CoverageMap(s: Session.State, modifier: Modifier) {
    Canvas(modifier.background(Color(0xE60D1017), RoundedCornerShape(12.dp)).border(1.dp, LINE, RoundedCornerShape(8.dp))) {
        val all = s.placed
        if (all.isEmpty()) {
            // rien encore : un repère au centre pour montrer que la carte est vivante
            drawRect(Color(0x33FFFFFF), Offset(size.width * 0.4f, size.height * 0.4f), Size(size.width * 0.2f, size.height * 0.2f), style = Stroke(1f))
            return@Canvas
        }
        val W = s.fieldW.toFloat().coerceAtLeast(1f); val H = s.fieldH.toFloat().coerceAtLeast(1f)
        var x0 = Float.MAX_VALUE; var y0 = Float.MAX_VALUE; var x1 = -Float.MAX_VALUE; var y1 = -Float.MAX_VALUE
        // etendue : les photos et les positions suivies, sauf celles prises suivi perdu (position non fiable, elles ecraseraient la carte)
        for (p in all) { if (p.status == "lost" && !p.photo) continue; x0 = minOf(x0, p.x.toFloat()); y0 = minOf(y0, p.y.toFloat()); x1 = maxOf(x1, p.x.toFloat() + W); y1 = maxOf(y1, p.y.toFloat() + H) }
        if (x0 > x1) { x0 = all.last().x.toFloat(); y0 = all.last().y.toFloat(); x1 = x0 + W; y1 = y0 + H }
        s.patron?.let { pt -> x0 = minOf(x0, pt[0].toFloat()); y0 = minOf(y0, pt[1].toFloat()); x1 = maxOf(x1, (pt[0] + pt[2]).toFloat()); y1 = maxOf(y1, (pt[1] + pt[3]).toFloat()) }
        val pad = 8f
        val sc = minOf((size.width - 2 * pad) / (x1 - x0), (size.height - 2 * pad) / (y1 - y0))
        val ox = pad + ((size.width - 2 * pad) - (x1 - x0) * sc) / 2; val oy = pad + ((size.height - 2 * pad) - (y1 - y0) * sc) / 2
        val pl = all.filter { !it.photo } + all.filter { it.photo }   // positions suivies dessous, photos de toute la serie par-dessus : une photo n'est jamais recouverte
        for (p in pl) {
            val dx = (ox + (p.x.toFloat() - x0) * sc).roundToInt(); val dy = (oy + (p.y.toFloat() - y0) * sc).roundToInt()
            val dw = (W * sc).roundToInt().coerceAtLeast(2); val dh = (H * sc).roundToInt().coerceAtLeast(2)
            val th = p.thumb
            var drawn = false
            if (th != null) try { drawImage(th.asImageBitmap(), IntOffset(0, 0), IntSize(th.width, th.height), IntOffset(dx, dy), IntSize(dw, dh), alpha = 0.95f); drawn = true } catch (e: Exception) {}
            if (!drawn) drawRect(if (p.photo) Color(0x66A99AF0) else Color(0x22E9EDF4), Offset(dx.toFloat(), dy.toFloat()), Size(dw.toFloat(), dh.toFloat()))
            if (p.photo && p.grade > 0) drawRect(if (p.grade == 2) REC else WARN, Offset(dx.toFloat(), dy.toFloat()), Size(dw.toFloat(), dh.toFloat()), style = Stroke(2f))
            if (p.photo && p.bonus) drawRect(Color(0xFF5DADE2), Offset(dx.toFloat(), dy.toFloat()), Size(dw.toFloat(), dh.toFloat()), style = Stroke(1.5f))
        }
        val cur = all.last()
        drawRect(if (s.track == "lost") REC else if (s.track == "relocated") WARN else OK, Offset(ox + (cur.x.toFloat() - x0) * sc, oy + (cur.y.toFloat() - y0) * sc), Size(W * sc, H * sc), style = Stroke(2f))
        // patron : rectangle cible et cases restantes (grille 48x48 trop fine a dessiner : on trace le contour et le taux)
        s.patron?.let { pt ->
            val x = ox + (pt[0].toFloat() - x0) * sc; val y = oy + (pt[1].toFloat() - y0) * sc
            drawRect(BIREF, Offset(x, y), Size(pt[2].toFloat() * sc, pt[3].toFloat() * sc), style = Stroke(2f))
            val cols = (pt[2] / W).roundToInt(); val rows = (pt[3] / H).roundToInt()
            for (i in 1 until cols) drawLine(Color(0x66E3B95C), Offset(x + i * W * sc, y), Offset(x + i * W * sc, y + pt[3].toFloat() * sc), 1f)
            for (j in 1 until rows) drawLine(Color(0x66E3B95C), Offset(x, y + j * H * sc), Offset(x + pt[2].toFloat() * sc, y + j * H * sc), 1f)
        }
        // emplacement du detail pris au plus fort grossissement (centre du premier champ apres le changement d'objectif) : a garder au milieu du balayage
        s.detailAt?.let { d ->
            val c = Offset(ox + (d.first.toFloat() + W / 2 - x0) * sc, oy + (d.second.toFloat() + H / 2 - y0) * sc)
            drawCircle(BIREF, 7f, c, style = Stroke(3f)); drawCircle(BIREF, 2f, c)
        }
    }
}

/** lames.json de la galerie : slug, titre, objectif (pour « lame d'ensemble » et « compléter »). */
fun loadLames(gallery: String): List<LameRef> = try {
    val c = URL(gallery + "lames/lames.json?t=" + System.currentTimeMillis()).openConnection() as HttpURLConnection
    c.connectTimeout = 8000; c.readTimeout = 8000
    val arr = JSONArray(c.inputStream.bufferedReader().readText()); c.disconnect()
    (0 until arr.length()).map { arr.getJSONObject(it) }.map { LameRef(it.getString("slug"), it.optString("title", it.getString("slug")), it.optString("objective").ifEmpty { null }, it.optInt("width"), it.optInt("height"), it.optInt("tile"), it.optInt("maxlvl"), it.optString("thumb")) }
} catch (e: Exception) { Journal.log("galerie injoignable : ${e.message}"); emptyList() }

/** Titre de section des réglages (eyebrow Paillasse : capitales espacées, filet au-dessus). */
/** Etiquette de champ (capitales espacees, gris), comme sur la page. */
@Composable
fun FieldLabel(t: String) = Text(t.uppercase(), fontSize = 12.sp, fontWeight = FontWeight.Bold, letterSpacing = 0.7.sp, color = MUTED)

/** Bouton de grille (objectifs) : coins de 6 dp, polar quand choisi. */
@Composable
fun GridPill(label: String, on: Boolean, modifier: Modifier = Modifier, onClick: () -> Unit) {
    Box(modifier.heightIn(min = 48.dp).background(if (on) ACCENT_TINT else CAPSULE, RoundedCornerShape(6.dp)).border(1.dp, if (on) ACCENT else LINE, RoundedCornerShape(6.dp)).clickable { onClick() },
        contentAlignment = Alignment.Center) {
        Text(label, color = if (on) ACCENT else FG, fontSize = 15.sp, fontWeight = if (on) FontWeight.SemiBold else FontWeight.Medium, fontFamily = Sans)
    }
}

@Composable
fun SectionTitle(t: String) {
    Column(Modifier.fillMaxWidth().padding(top = 10.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Box(Modifier.fillMaxWidth().height(1.dp).background(LINE))
        Text(t.uppercase(), fontSize = 12.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp, color = MUTED)
    }
}

/** Pré-contrôle avant la première photo : disque centré, exposition, mise au point, verrou AF/AE sur zone vide. Indicatif, ne bloque rien. */
@Composable
fun PreCheck(s: Session.State, live: CameraEngine.Live, locked: Boolean) {
    val disc = s.discFill > 0.30f && s.discOff < 0.08f
    val expoNs = live.exposureNs; val iso = live.iso
    val expo = iso != null && iso <= 400 && (expoNs == null || expoNs <= 34_000_000L)
    val focus = s.sharpPeak > 0 && s.sharp >= 0.85f * s.sharpPeak
    val items = listOf(
        Triple(disc, "Disque centré", if (s.discFill <= 0f) "en attente de l'image" else if (disc) "bord net, ${(100 * s.discFill).roundToInt()} % du cadre" else if (s.discOff >= 0.08f) "décentré : molettes X et Y de l'adaptateur" else "disque petit : molette Z (hauteur)"),
        Triple(expo, "Exposition", if (iso == null) "…" else "ISO $iso${expoNs?.let { " · 1/${(1e9 / it).roundToInt()} s" } ?: ""}${if (expo) "" else " : lampe plus forte"}"),
        Triple(focus, "Mise au point", if (focus) "au plus net des 5 dernières secondes" else "pas au plus net : tourne la vis micrométrique"),
        Triple(locked, "AF/AE verrouillés", if (locked) "réglages figés pour la série" else "à verrouiller sur une zone vide et claire"))
    val ok = items.count { it.first }
    Column(Modifier.fillMaxWidth().background(Color(0xE60D1017), RoundedCornerShape(12.dp)).border(1.dp, LINE, RoundedCornerShape(12.dp)).padding(14.dp, 12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Avant de lancer", color = FG, fontSize = 15.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
            Text("$ok sur ${items.size}", color = if (ok == items.size) OK else MUTED, fontSize = 12.sp, fontFamily = Mono)
        }
        for ((good, title, detail) in items) Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Box(Modifier.size(18.dp).background(if (good) OK else Color.Transparent, CircleShape).border(2.dp, if (good) OK else BIREF, CircleShape), contentAlignment = Alignment.Center) {
                if (good) Canvas(Modifier.size(10.dp)) { drawLine(BG, Offset(size.width * 0.1f, size.height * 0.55f), Offset(size.width * 0.4f, size.height * 0.85f), 3f); drawLine(BG, Offset(size.width * 0.4f, size.height * 0.85f), Offset(size.width * 0.95f, size.height * 0.15f), 3f) }
            }
            Column { Text(title, color = if (good) FG else BIREF, fontSize = 14.sp, fontWeight = FontWeight.Medium); Text(detail, color = MUTED, fontSize = 12.sp) }
        }
    }
}

/** Patron (colonnes x lignes de champs) propre à un objectif : champs libres, raccourcis, « libre ». Mémorisé par objectif. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun PatronFields(obj: String, st: Settings) {
    var patron by remember(obj) { mutableStateOf(st.patronFor(obj)) }
    var pc by remember(obj) { mutableStateOf(patron.substringBefore('x').takeIf { patron != "libre" } ?: "") }
    var pr by remember(obj) { mutableStateOf(patron.substringAfter('x').takeIf { patron != "libre" } ?: "") }
    fun setPatron(c: String, r: String) { pc = c; pr = r; val ci = c.toIntOrNull() ?: 0; val ri = r.toIntOrNull() ?: 0; patron = if (ci in 1..40 && ri in 1..40) "${ci}x${ri}" else "libre"; st.setPatronFor(obj, patron) }
    FieldLabel("Patron au $obj · champs à remplir")
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
        OutlinedTextField(pc, { setPatron(it.filter { ch -> ch.isDigit() }.take(2), pr) }, label = { Text("colonnes", fontSize = 11.sp) }, singleLine = true, modifier = Modifier.weight(1f), keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number))
        Text("x", color = MUTED)
        OutlinedTextField(pr, { setPatron(pc, it.filter { ch -> ch.isDigit() }.take(2)) }, label = { Text("lignes", fontSize = 11.sp) }, singleLine = true, modifier = Modifier.weight(1f), keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number))
        val nc = (pc.toIntOrNull() ?: 0) * (pr.toIntOrNull() ?: 0)
        if (nc > 0) Text("$nc champs", fontSize = 13.sp, color = MUTED)
        WebButton("libre", pressed = patron == "libre", small = true) { setPatron("", "") }
    }
    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        for (o in listOf("2x2", "3x3", "4x4", "5x5", "6x4", "8x6")) WebButton(o, pressed = o == patron, small = true) { setPatron(o.substringBefore('x'), o.substringAfter('x')) }
    }
}

/** Aperçu de la lame d'ensemble (tuiles du niveau tenant dans ~1024 px) : un toucher marque l'endroit approximatif où se trouve le détail. */
@Composable
fun ParentHint(gallery: String, ref: LameRef, st: Settings) {
    var bmp by remember(ref.slug) { mutableStateOf<android.graphics.Bitmap?>(null) }
    var failed by remember(ref.slug) { mutableStateOf(false) }
    var hint by remember(ref.slug) { mutableStateOf(st.hint.split(',').mapNotNull { it.toFloatOrNull() }.takeIf { it.size == 2 }) }
    LaunchedEffect(ref.slug) {
        bmp = withContext(Dispatchers.IO) { try { loadOverview(gallery, ref) } catch (e: Exception) { Journal.log("aperçu ${ref.slug} : ${e.message}"); null } }
        if (bmp == null) failed = true
    }
    Text("Repère : touchez l'endroit approximatif du détail sur la lame d'ensemble (facultatif, aide le recalage)", color = MUTED, fontSize = 11.sp)
    val b = bmp
    if (b != null) {
        // zoom (pincement) et deplacement pour poser le repere avec precision ; un toucher pose le repere
        var scale by remember(ref.slug) { mutableStateOf(1f) }; var offset by remember(ref.slug) { mutableStateOf(Offset.Zero) }
        val tstate = androidx.compose.foundation.gestures.rememberTransformableState { zoomChange, panChange, _ ->
            scale = (scale * zoomChange).coerceIn(1f, 8f); offset += panChange
        }
        Box(Modifier.fillMaxWidth().aspectRatio(b.width.toFloat() / b.height).clipToBounds()
            .transformable(tstate)
            .pointerInput(ref.slug) {
                detectTapGestures { o ->
                    // ecran -> image : la transformation est centree (graphicsLayer) : s = c + (p - c) * k + offset
                    val cx = size.width / 2f; val cy = size.height / 2f
                    val px = cx + (o.x - offset.x - cx) / scale; val py = cy + (o.y - offset.y - cy) / scale
                    val f = listOf((px / size.width).coerceIn(0f, 1f), (py / size.height).coerceIn(0f, 1f)); hint = f; st.hint = "${f[0]},${f[1]}"
                }
            }) {
            Box(Modifier.fillMaxSize().graphicsLayer(scaleX = scale, scaleY = scale, translationX = offset.x, translationY = offset.y)) {
                Image(b.asImageBitmap(), null, Modifier.fillMaxSize())
                hint?.let { h ->
                    Canvas(Modifier.fillMaxSize()) {
                        val c = Offset(h[0] * size.width, h[1] * size.height); val r = 16f / scale; val w = 3f / scale
                        drawCircle(Color.Red, r, c, style = Stroke(w))
                        drawLine(Color.Red, Offset(c.x - 1.8f * r, c.y), Offset(c.x + 1.8f * r, c.y), w); drawLine(Color.Red, Offset(c.x, c.y - 1.8f * r), Offset(c.x, c.y + 1.8f * r), w)
                    }
                }
            }
            if (scale > 1.01f) Text("${String.format(Locale.ROOT, "%.1f", scale)}×", Modifier.align(Alignment.TopEnd).padding(6.dp).background(Color(0x99000000), RoundedCornerShape(6.dp)).padding(6.dp, 2.dp), color = FG, fontSize = 11.sp)
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(hint?.let { "repère : ${(it[0] * 100).roundToInt()} % de la largeur, ${(it[1] * 100).roundToInt()} % de la hauteur" } ?: "aucun repère (pince pour zoomer, touche pour poser)", color = MUTED, fontSize = 11.sp, modifier = Modifier.weight(1f))
            if (scale > 1.01f) WebButton("Vue entière", small = true) { scale = 1f; offset = Offset.Zero }
        }
    } else Text(if (failed) "aperçu indisponible (galerie injoignable)" else "chargement de l'aperçu…", color = MUTED, fontSize = 11.sp)
}

/** Reconstruit la lame entière à partir des tuiles du niveau Deep Zoom où elle tient dans 1024 px (quelques tuiles). */
fun loadOverview(gallery: String, ref: LameRef): android.graphics.Bitmap? {
    fun fetch(path: String): android.graphics.Bitmap? {
        val c = URL(gallery + "lames/" + path).openConnection() as HttpURLConnection
        c.connectTimeout = 8000; c.readTimeout = 8000
        return try { if (c.responseCode == 200) android.graphics.BitmapFactory.decodeStream(c.inputStream) else null } finally { c.disconnect() }
    }
    if (ref.tile <= 0 || ref.maxlvl <= 0) return fetch(ref.thumb)
    var lvl = ref.maxlvl
    while (lvl > 0 && maxOf(ref.width, ref.height) / (1 shl (ref.maxlvl - lvl)) > 1024) lvl--
    val f = 1.0 / (1 shl (ref.maxlvl - lvl))
    val lw = Math.ceil(ref.width * f).toInt(); val lh = Math.ceil(ref.height * f).toInt()
    val out = android.graphics.Bitmap.createBitmap(lw, lh, android.graphics.Bitmap.Config.ARGB_8888)
    val cv = android.graphics.Canvas(out); cv.drawColor(0xFFEEEEEE.toInt())
    for (ty in 0 until Math.ceil(lh / ref.tile.toDouble()).toInt()) for (tx in 0 until Math.ceil(lw / ref.tile.toDouble()).toInt()) {
        val t = fetch("${ref.slug}/lame_files/$lvl/${tx}_$ty.jpg") ?: continue
        cv.drawBitmap(t, maxOf(0, tx * ref.tile - 1).toFloat(), maxOf(0, ty * ref.tile - 1).toFloat(), null)   // tuiles Deep Zoom avec 1 px de recouvrement
    }
    return out
}

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
    var stageX by remember { mutableStateOf(st.stageX) }
    var stageY by remember { mutableStateOf(st.stageY) }
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
    var trigDist by remember { mutableStateOf(st.triggerDistance) }
    var stepFrac by remember { mutableStateOf(st.stepFrac) }
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
        st.videoMbps = vmbps; st.videoQuality = vq; st.stillMs = stillMs; st.triggerDistance = trigDist; st.stepFrac = stepFrac; st.photoMaxSide = side; st.photoQuality = quality
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
                Column { Text("Réglages", fontSize = 27.sp, fontFamily = Display, color = FG); Text("ScanLame ${BuildConfig.VERSION_NAME}", fontSize = 12.sp, fontFamily = Mono, color = MUTED) }
                WebButton("Fermer", small = true) { onClose(save()) }
            }
            SectionTitle("Lame")
            OutlinedTextField(title, { title = it }, label = { Text("Nom de la lame (pour la galerie)") }, placeholder = { Text("ex. Gram, prélèvement vaginal, 14/10") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Select("Coloration", Settings.STAINS, stain, Modifier.weight(1f)) { stain = it }
                Select("Objectif", Settings.OBJECTIVES.map { it to (if (it == "x50" || it == "x100") "$it (immersion)" else it) }, objective, Modifier.weight(1f)) { objective = it; umpx = st.umPerPx(it).takeIf { v -> v > 0 }?.toString() ?: "" }
            }
            OutlinedTextField(sample, { sample = it }, label = { Text("Prélèvement") }, placeholder = { Text("ex. hémoculture, ECBU, LCR…") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(comment, { comment = it }, label = { Text("Commentaire (optionnel)") }, placeholder = { Text("ex. CGP en amas, PNN nombreux") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            Select("Lame d'ensemble (si cette prise est un détail à plus fort grossissement)", listOf("" to "Aucune : nouvelle lame") + st.recentSeries().filter { r -> lames.none { it.slug == r.first } }.map { it.first to "${it.second} (obj. ${it.third}, envoyée, en cours d'assemblage)" } + lames.map { it.slug to "${it.title}${it.objective?.let { o -> " (obj. $o)" } ?: ""}" }, parent) { parent = it; st.hint = ""; if (it.isNotEmpty()) complete = "" }
            if (parent.isNotEmpty()) lames.firstOrNull { it.slug == parent && it.width > 0 }?.let { ParentHint(st.galleryUrl, it, st) }
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                OutlinedTextField(stageX, { stageX = it; st.stageX = it }, label = { Text("Platine X (vernier, mm)", fontSize = 12.sp) }, singleLine = true, modifier = Modifier.weight(1f), keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal))
                OutlinedTextField(stageY, { stageY = it; st.stageY = it }, label = { Text("Platine Y (vernier, mm)", fontSize = 12.sp) }, singleLine = true, modifier = Modifier.weight(1f), keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal))
            }
            Text("Relevés de la platine au premier champ de la série (facultatif). Avec ceux de la lame d'ensemble, ils servent à placer le détail ; la correspondance est apprise au premier recalage réussi.", color = MUTED, fontSize = 11.sp)
            Select("Compléter une lame existante (reprise des zones manquantes, même objectif)", listOf("" to "Non : nouvelle lame") + lames.map { it.slug to "${it.title}${it.objective?.let { o -> " (obj. $o)" } ?: ""}" }, complete) { complete = it; if (it.isNotEmpty()) { parent = ""; lames.firstOrNull { l -> l.slug == it }?.objective?.let { o -> objective = o } } }
            SectionTitle("Échelle")
            OutlinedTextField(umpx, { umpx = it }, label = { Text("Échelle pour cet objectif : µm par pixel de photo (optionnel)") }, placeholder = { Text("ex. 0.085 (mesuré avec un micromètre objet, à ce zoom)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            // calibration par les hematies : une photo du champ courant (frottis sanguin, zone ou les hematies sont isolees), diametre median = 7,5 µm
            var calMsg by remember { mutableStateOf("") }; var calBusy by remember { mutableStateOf(false) }
            val calScope = androidx.compose.runtime.rememberCoroutineScope()
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                WebButton(if (calBusy) "Mesure…" else "Mesurer sur les hématies", small = true, enabled = cameraOn && !calBusy) {
                    calBusy = true; calMsg = "photo du champ…"
                    val f = File(ctx.cacheDir, "calib.jpg")
                    engine.takePhoto(f) { ok, err ->
                        if (!ok) { calBusy = false; calMsg = "photo impossible : $err"; return@takePhoto }
                        calScope.launch {
                            val r = withContext(Dispatchers.IO) { try { Calibrate.measureRbc(f) } catch (e: Exception) { null } }
                            calBusy = false
                            if (r == null || r.umPerPx <= 0) calMsg = "mesure impossible" + (r?.note?.let { " : $it" } ?: "")
                            else { umpx = Calibrate.fmt(r.umPerPx); calMsg = "${r.n} hématies, diamètre médian ${Calibrate.fmt1(r.diamPx)} px (dispersion ${(100 * r.spread).roundToInt()} %) → ${Calibrate.fmt(r.umPerPx)} µm/px" + (if (r.note.isNotEmpty()) " ; ${r.note}" else "") + ". Enregistré à la fermeture des réglages." }
                            Journal.log("calibration $objective : $calMsg")
                        }
                    }
                }
                Text(if (calMsg.isEmpty()) "Frottis sanguin, hématies isolées dans le champ, mise au point faite : hématie = 7,5 µm." else calMsg, color = MUTED, fontSize = 11.sp, modifier = Modifier.weight(1f))
            }
            SectionTitle("Prise de vue")
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
            SectionTitle("Caméra")
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
                Select("Déclenchement", listOf("stop" to "à l'arrêt de la platine seulement", "dist" to "à l'arrêt, et en mouvement lent tous les N % de champ"), if (trigDist) "dist" else "stop", Modifier.weight(1f)) { trigDist = it == "dist" }
                Select("Pas en mouvement", listOf("0.4" to "40 % du champ", "0.5" to "50 % du champ", "0.6" to "60 % du champ"), stepFrac.toString(), Modifier.weight(1f)) { stepFrac = it.toFloat() }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Select("Taille photo", listOf("0" to "pleine (12 MP)", "3072" to "3/4", "2048" to "1/2"), side.toString(), Modifier.weight(1f)) { side = it.toInt() }
                Select("Qualité JPEG", listOf("95" to "95 %", "92" to "92 %", "85" to "85 %", "75" to "75 %"), quality.toString(), Modifier.weight(1f)) { quality = it.toInt() }
            }
            SectionTitle("Envoi")
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
                Text(lines.takeLast(300).joinToString("\n"), fontSize = 11.sp, fontFamily = Mono, color = MUTED, modifier = Modifier.fillMaxWidth().background(BG, RoundedCornerShape(8.dp)).border(1.dp, LINE, RoundedCornerShape(8.dp)).padding(8.dp))
            }
            Text(if (showDiag) "▾ Diagnostic des caméras" else "▸ Diagnostic des caméras", fontSize = 12.sp, color = MUTED, modifier = Modifier.clickable { showDiag = !showDiag }.padding(vertical = 6.dp))
            if (showDiag) {
                val txt = remember { engine.diagnosticText() }
                WebButton("Copier le diagnostic", small = true) { copy(ctx, txt) }
                Text(txt, fontSize = 11.sp, fontFamily = Mono, color = MUTED, modifier = Modifier.fillMaxWidth().background(BG, RoundedCornerShape(8.dp)).border(1.dp, LINE, RoundedCornerShape(8.dp)).padding(8.dp))
            }
            Spacer(Modifier.height(16.dp))
        }
    }
}

fun copy(ctx: Context, text: String) {
    val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    cm.setPrimaryClip(ClipData.newPlainText("scanlame", text))
}
