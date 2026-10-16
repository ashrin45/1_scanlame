package org.scanlame.app

import android.content.Context
import android.content.SharedPreferences

/** Réglages persistants (équivalent du localStorage de la page). */
class Settings(ctx: Context) {
    private val p: SharedPreferences = ctx.getSharedPreferences("scanlame", Context.MODE_PRIVATE)

    companion object {
        val OBJECTIVES = listOf("x4", "x10", "x20", "x40", "x50", "x60", "x63", "x100")
        val STAINS = listOf("gram" to "Gram", "mgg" to "MGG", "giemsa" to "Giemsa", "ziehl" to "Ziehl-Neelsen", "auramine" to "Auramine", "bleu" to "Bleu de méthylène", "frais" to "État frais", "encre" to "Encre de Chine", "lactophenol" to "Lactophénol", "he" to "HE", "autre" to "Autre")
        const val MODE_PHOTOS = "photos"
        const val MODE_VIDEO = "video"
    }

    private fun str(key: String, def: String) = p.getString(key, def) ?: def
    private fun set(key: String, v: String) = p.edit().putString(key, v).apply()

    var serverUrl: String get() = str("url", ""); set(v) = set("url", v.trim().trimEnd('/'))
    var token: String get() = str("token", ""); set(v) = set("token", v.trim())
    var stain: String get() = str("stain", "gram"); set(v) = set("stain", v)
    var objective: String get() = str("objective", "x100"); set(v) = set("objective", v)
    var mode: String get() = str("mode", MODE_PHOTOS); set(v) = set("mode", v)
    var sendLog: Boolean get() = p.getBoolean("sendlog", true); set(v) = p.edit().putBoolean("sendlog", v).apply()
    var depth: String get() = str("depth", "off"); set(v) = set("depth", v)

    // caméra
    var cameraId: String get() = str("camera", ""); set(v) = set("camera", v)            // vide = caméra arrière par défaut (CameraX)
    var physicalId: String get() = str("physical", ""); set(v) = set("physical", v)      // vide = laisser la caméra logique choisir
    var zoom: Float get() = p.getFloat("zoom", 1f); set(v) = p.edit().putFloat("zoom", v).apply()
    var zoomByCrop: Boolean get() = p.getBoolean("zoomcrop", true); set(v) = p.edit().putBoolean("zoomcrop", v).apply()   // recadrage capteur plutôt que ratio (évite la bascule d'objectif)
    var focusMode: String get() = str("focus", "infinity"); set(v) = set("focus", v)     // auto | infinity | manual
    var focusDiopters: Float get() = p.getFloat("focusd", 0f); set(v) = p.edit().putFloat("focusd", v).apply()
    var exposureMode: String get() = str("expo", "auto"); set(v) = set("expo", v)       // auto | manual
    var shutterMs: Float get() = p.getFloat("shutter", 4f); set(v) = p.edit().putFloat("shutter", v).apply()
    var iso: Int get() = p.getInt("iso", 100); set(v) = p.edit().putInt("iso", v).apply()
    var evComp: Int get() = p.getInt("ev", 0); set(v) = p.edit().putInt("ev", v).apply()
    var wbMode: String get() = str("wb", "auto"); set(v) = set("wb", v)                 // auto | daylight | manual
    var wbKelvin: Int get() = p.getInt("kelvin", 5000); set(v) = p.edit().putInt("kelvin", v).apply()
    var photoQuality: Int get() = p.getInt("quality", 95); set(v) = p.edit().putInt("quality", v).apply()
    var photoMaxSide: Int get() = p.getInt("photoside", 4096); set(v) = p.edit().putInt("photoside", v).apply()   // 0 = maximum du capteur
    var videoQuality: String get() = str("vquality", "UHD"); set(v) = set("vquality", v)
    var videoMbps: Int get() = p.getInt("vmbps", 40); set(v) = p.edit().putInt("vmbps", v).apply()
    var stillMs: Int get() = p.getInt("stillms", 350); set(v) = p.edit().putInt("stillms", v).apply()      // durée d'immobilité avant photo
    var stillPx: Float get() = p.getFloat("stillpx", 1.5f); set(v) = p.edit().putFloat("stillpx", v).apply()   // mouvement toléré (unités 256) pendant l'immobilité
    var analysisSide: Int get() = p.getInt("aside", 640); set(v) = p.edit().putInt("aside", v).apply()

    fun umPerPx(obj: String): Float = p.getFloat("umpx.$obj", 0f)
    fun setUmPerPx(obj: String, v: Float) = p.edit().putFloat("umpx.$obj", v).apply()
    fun calibrations(): Map<String, Float> = OBJECTIVES.mapNotNull { o -> umPerPx(o).takeIf { it > 0 }?.let { o to it } }.toMap()
}
