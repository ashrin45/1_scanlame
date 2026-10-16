package org.scanlame.app

import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/** Mise à jour depuis la release « android » du dépôt : version.json publié à côté de l'APK par GitHub Actions. */
object Updater {
    data class Info(val version: String, val code: Int, val url: String, val date: String)
    const val VERSION_URL = "https://github.com/ashrin45/1_scanlame/releases/download/android/version.json"

    suspend fun check(): Info? = withContext(Dispatchers.IO) {
        try {
            val c = URL(VERSION_URL).openConnection() as HttpURLConnection
            c.connectTimeout = 8000; c.readTimeout = 8000; c.instanceFollowRedirects = true
            val j = JSONObject(c.inputStream.bufferedReader().readText()); c.disconnect()
            Info(j.getString("version"), j.getInt("code"), j.getString("url"), j.optString("date"))
        } catch (e: Exception) { Journal.log("mise à jour : vérification impossible (${e.message})"); null }
    }

    /** Télécharge l'APK dans le cache puis ouvre l'installateur Android. */
    suspend fun download(ctx: Context, info: Info, onProgress: (Int) -> Unit): File? = withContext(Dispatchers.IO) {
        try {
            val f = File(ctx.cacheDir, "scanlame-${info.version}.apk")
            val c = URL(info.url).openConnection() as HttpURLConnection
            c.connectTimeout = 15000; c.readTimeout = 60000; c.instanceFollowRedirects = true
            val total = c.contentLengthLong
            c.inputStream.use { inp -> f.outputStream().use { out ->
                val buf = ByteArray(1 shl 16); var done = 0L; var n: Int
                while (inp.read(buf).also { n = it } > 0) { out.write(buf, 0, n); done += n; if (total > 0) onProgress((100 * done / total).toInt()) }
            } }
            c.disconnect()
            Journal.log("mise à jour ${info.version} téléchargée (${f.length() shr 20} Mo)")
            f
        } catch (e: Exception) { Journal.log("mise à jour : téléchargement échoué (${e.message})"); null }
    }

    fun install(ctx: Context, apk: File) {
        val uri = FileProvider.getUriForFile(ctx, ctx.packageName + ".files", apk)
        val i = Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        ctx.startActivity(i)
    }
}
