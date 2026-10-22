package org.scanlame.app

import android.content.Context
import android.content.SharedPreferences

/** Réglages persistants (équivalent du localStorage de la page). */
class Settings(ctx: Context) {
    private val p: SharedPreferences = ctx.getSharedPreferences("scanlame", Context.MODE_PRIVATE)

    companion object {
        val OBJECTIVES = listOf("x10", "x40", "x50", "x100")   // les objectifs du microscope du laboratoire
        val STAINS = listOf("gram" to "Gram", "mgg" to "MGG", "giemsa" to "Giemsa", "ziehl" to "Ziehl-Neelsen", "auramine" to "Auramine", "bleu" to "Bleu de méthylène", "frais" to "État frais", "encre" to "Encre de Chine", "lactophenol" to "Lactophénol", "he" to "HE", "autre" to "Autre")
        const val MODE_PHOTOS = "photos"
        const val MODE_VIDEO = "video"
    }

    private fun str(key: String, def: String) = p.getString(key, def) ?: def
    private fun set(key: String, v: String) = p.edit().putString(key, v).apply()

    // URL et jeton : une seule ligne, sans espace (un collage malheureux du journal dans le champ cassait l'en-tête HTTP)
    var serverUrl: String get() = str("url", ""); set(v) = set("url", v.lineSequence().firstOrNull()?.trim()?.trimEnd('/') ?: "")
    var token: String get() = str("token", ""); set(v) = set("token", v.lineSequence().firstOrNull()?.trim()?.filter { it.code in 33..126 } ?: "")
    var stain: String get() = str("stain", "gram"); set(v) = set("stain", v)
    var objective: String get() = str("objective", "x100"); set(v) = set("objective", v)
    var mode: String get() = str("mode", MODE_PHOTOS); set(v) = set("mode", v)
    var sendLog: Boolean get() = p.getBoolean("sendlog", true); set(v) = p.edit().putBoolean("sendlog", v).apply()
    var autoUpload: Boolean get() = p.getBoolean("autoupload", true); set(v) = p.edit().putBoolean("autoupload", v).apply()   // faux = garder sur le téléphone jusqu'à « Envoyer maintenant »
    var depth: String get() = str("depth", "off"); set(v) = set("depth", v)

    // caméra (mêmes choix que la feuille de réglages de la page)
    var cameraId: String get() = str("camera", "5"); set(v) = set("camera", v)            // 5 = capteur principal du S24 Ultra ; vide = arrière par défaut
    var physicalId: String get() = str("physical", ""); set(v) = set("physical", v)
    var zoom: Float get() = p.getFloat("zoom", 1f); set(v) = p.edit().putFloat("zoom", v).apply()
    var zoomByCrop: Boolean get() = p.getBoolean("zoomcrop", true); set(v) = p.edit().putBoolean("zoomcrop", v).apply()
    var zoomMax: Float get() = p.getFloat("zoommax", 4f); set(v) = p.edit().putFloat("zoommax", v).apply()
    var focusMode: String get() = str("focus", "inf"); set(v) = set("focus", v)         // inf | keep (au verrouillage)
    var shutterCapMs: Float get() = p.getFloat("shuttercap", 0f); set(v) = p.edit().putFloat("shuttercap", v).apply()   // 0 = garder la pose courante
    var shutter: String get() = str("shutter", "anti"); set(v) = set("shutter", v)   // anti = 10 ms (un cycle du secteur) | court = 1/250 s au plus | tel = valeur du téléphone
    var ctemp: Int get() = p.getInt("ctemp", 0); set(v) = p.edit().putInt("ctemp", v).apply()       // 0 = figer la balance courante
    var evComp: Int get() = p.getInt("ev", 0); set(v) = p.edit().putInt("ev", v).apply()
    var advanced: Boolean get() = p.getBoolean("advanced", false); set(v) = p.edit().putBoolean("advanced", v).apply()
    var photoQuality: Int get() = p.getInt("quality", 92); set(v) = p.edit().putInt("quality", v).apply()
    var photoMaxSide: Int get() = p.getInt("photoside", 0); set(v) = p.edit().putInt("photoside", v).apply()   // 0 = pleine
    var videoQuality: String get() = str("vquality", "UHD"); set(v) = set("vquality", v)
    var videoMbps: Int get() = p.getInt("vmbps", 30); set(v) = p.edit().putInt("vmbps", v).apply()
    var stillMs: Int get() = p.getInt("stillms", 350); set(v) = p.edit().putInt("stillms", v).apply()
    var triggerDistance: Boolean get() = p.getBoolean("trigdist", true); set(v) = p.edit().putBoolean("trigdist", v).apply()   // photo aussi tous les N % de champ, platine en mouvement lent
    var stepFrac: Float get() = p.getFloat("stepfrac", 0.5f); set(v) = p.edit().putFloat("stepfrac", v).apply()
    var stillPx: Float get() = p.getFloat("stillpx", 1.5f); set(v) = p.edit().putFloat("stillpx", v).apply()
    var analysisSide: Int get() = p.getInt("aside", 640); set(v) = p.edit().putInt("aside", v).apply()
    var galleryUrl: String get() = str("gallery", "https://ashrin45.github.io/1_scanlame/"); set(v) = set("gallery", v.trim().trimEnd('/') + "/")

    /** Séries envoyées par ce téléphone (slug prévu côté serveur, titre, objectif), les plus récentes d'abord ; 20 au plus. */
    fun recentSeries(): List<Triple<String, String, String>> = str("recent", "").split('\n').filter { it.contains('\t') }.map { it.split('\t') }.filter { it.size >= 3 }.map { Triple(it[0], it[1], it[2]) }
    fun addRecentSeries(slug: String, title: String, objective: String) {
        val l = listOf(listOf(slug, title.replace('\t', ' ').replace('\n', ' '), objective).joinToString("\t")) + str("recent", "").split('\n').filter { it.contains('\t') && !it.startsWith("$slug\t") }
        set("recent", l.take(20).joinToString("\n"))
    }
    var patron: String get() = str("patron", "libre"); set(v) = set("patron", v)       // (ancien reglage global, conserve comme valeur par defaut)
    fun patronFor(obj: String): String = str("patron.$obj", patron)                     // patron par objectif : CxR ou libre
    fun setPatronFor(obj: String, v: String) = set("patron.$obj", v)
    /** Profondeur (preparations epaisses) : plusieurs plans de mise au point par champ, fusionnes par le pipeline. Seulement au x40 et au-dela. */
    fun depthAllowed(obj: String) = (Regex("\\d+").find(obj)?.value?.toIntOrNull() ?: 0) >= 40
    fun depthFor(obj: String): Boolean = depthAllowed(obj) && p.getBoolean("depth.$obj", false)
    fun setDepthFor(obj: String, v: Boolean) = p.edit().putBoolean("depth.$obj", v).apply()
    /** Rafale (avec la profondeur) : une photo toutes les 300 ms tant que la vis tourne, toutes les couches gardees pour la molette du site. */
    fun burstFor(obj: String): Boolean = depthFor(obj) && p.getBoolean("burst.$obj", false)
    fun setBurstFor(obj: String, v: Boolean) = p.edit().putBoolean("burst.$obj", v).apply()
    var autoCrop: Boolean get() = p.getBoolean("autocrop", true); set(v) = p.edit().putBoolean("autocrop", v).apply()   // rognage au plus grand rectangle couvert a la publication
    var hint: String get() = str("hint", ""); set(v) = set("hint", v)           // "fx,fy" : repère touché sur la lame d'ensemble (fraction de l'image), vidé au départ de la série
    var stageX: String get() = str("stagex", ""); set(v) = set("stagex", v)     // coordonnées de la platine (vernier, mm) à la première photo
    var stageY: String get() = str("stagey", ""); set(v) = set("stagey", v)
    fun umPerPx(obj: String): Float = p.getFloat("umpx.$obj", 0f)
    fun setUmPerPx(obj: String, v: Float) = p.edit().putFloat("umpx.$obj", v).apply()
    fun calibrations(): Map<String, Float> = OBJECTIVES.mapNotNull { o -> umPerPx(o).takeIf { it > 0 }?.let { o to it } }.toMap()
}
