package org.scanlame.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.ForegroundInfo
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.TimeUnit

/**
 * Envoi vers la fonction Supabase « scanlame » (même protocole que la page) :
 *   POST /sign {series, name, kind} + X-Token  -> {url}, puis PUT du fichier sur cette URL
 *   POST /finish {meta}                        -> serie.json côté serveur, réveil de GitHub
 * Tout passe par une file sur disque (filesDir/queue/<série>/...) vidée par un Worker en arrière-plan :
 * si le réseau manque, les fichiers attendent et partent au retour du réseau, même application fermée.
 */
object Uploader {
    data class Status(val pendingFiles: Int = 0, val pendingBytes: Long = 0, val sentFiles: Int = 0, val sentBytes: Long = 0, val lastError: String? = null, val busy: Boolean = false)
    val status = MutableStateFlow(Status())

    fun queueDir(ctx: Context) = File(ctx.filesDir, "queue").apply { mkdirs() }
    fun seriesDir(ctx: Context, series: String) = File(queueDir(ctx), series).apply { mkdirs() }

    /** Le fichier est déjà écrit dans seriesDir/<kind>s/<name> par l'appelant ; on lance le Worker. */
    fun kick(ctx: Context, force: Boolean = false) {
        if (!force && !Settings(ctx).autoUpload) { refresh(ctx); return }    // envoi bloqué : la file attend « Envoyer maintenant »
        val req = OneTimeWorkRequestBuilder<UploadWorker>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(ctx).enqueueUniqueWork("scanlame-upload", ExistingWorkPolicy.KEEP, req)
        refresh(ctx)
    }

    fun refresh(ctx: Context) {
        var n = 0; var b = 0L
        queueDir(ctx).walkTopDown().filter { it.isFile }.forEach { n++; b += it.length() }
        status.value = status.value.copy(pendingFiles = n, pendingBytes = b)
    }

    fun pendingSeries(ctx: Context): List<String> = queueDir(ctx).listFiles()?.filter { it.isDirectory }?.map { it.name }?.sorted() ?: emptyList()

    private fun conn(url: String, method: String, token: String?): HttpURLConnection {
        val c = URL(url).openConnection() as HttpURLConnection
        c.requestMethod = method; c.connectTimeout = 20000; c.readTimeout = 120000
        if (token != null) c.setRequestProperty("X-Token", token)
        return c
    }

    private fun readBody(c: HttpURLConnection): String =
        try { (if (c.responseCode in 200..299) c.inputStream else c.errorStream)?.bufferedReader()?.readText() ?: "" } catch (e: Exception) { "" }

    /** Signature puis PUT direct au stockage. Lève une exception si l'envoi échoue. */
    fun sendFile(base: String, token: String, series: String, kind: String, name: String, file: File) {
        val c = conn("$base/sign", "POST", token)
        c.doOutput = true; c.setRequestProperty("Content-Type", "application/json")
        c.outputStream.use { it.write(JSONObject().put("series", series).put("name", name).put("kind", kind).toString().toByteArray()) }
        val code = c.responseCode; val body = readBody(c); c.disconnect()
        if (code != 200) throw RuntimeException("signature HTTP $code ${body.take(120)}")
        val url = JSONObject(body).getString("url")
        val p = conn(url, "PUT", null)
        p.doOutput = true; p.setFixedLengthStreamingMode(file.length())
        p.setRequestProperty("Content-Type", if (name.endsWith(".jpg")) "image/jpeg" else "application/octet-stream")
        p.setRequestProperty("x-upsert", "true")
        file.inputStream().use { inp -> p.outputStream.use { out -> inp.copyTo(out, 256 * 1024) } }
        val code2 = p.responseCode; val body2 = readBody(p); p.disconnect()
        if (code2 !in 200..299) throw RuntimeException("stockage HTTP $code2 ${body2.take(120)}")
    }

    /** Clôture : renvoie le code HTTP et le corps. */
    fun finish(base: String, token: String, meta: String): Pair<Int, String> {
        val c = conn("$base/finish", "POST", token)
        c.doOutput = true; c.setRequestProperty("Content-Type", "application/json")
        c.outputStream.use { it.write(meta.toByteArray()) }
        val code = c.responseCode; val body = readBody(c); c.disconnect()
        return code to body
    }

    /** État du serveur (espace utilisé). */
    suspend fun serverStatus(base: String): JSONObject? = withContext(Dispatchers.IO) {
        try { val c = conn(base, "GET", null); val b = readBody(c); c.disconnect(); JSONObject(b) } catch (e: Exception) { null }
    }

    /**
     * Vide la file : pour chaque série, d'abord les fichiers (photos et morceaux, par ordre de nom), puis finish.json.
     * Retourne true si tout est parti.
     */
    fun drain(ctx: Context, onProgress: (String) -> Unit = {}): Boolean {
        val st = Settings(ctx)
        val base = st.serverUrl; val token = st.token
        if (base.isEmpty()) { status.value = status.value.copy(lastError = "serveur non réglé"); return false }
        var allOk = true
        for (series in pendingSeries(ctx)) {
            val dir = File(queueDir(ctx), series)
            val files = dir.walkTopDown().filter { it.isFile && it.name != "finish.json" }.sortedBy { it.path }.toList()
            for (f in files) {
                val kind = if (f.parentFile?.name == "photos") "photo" else "chunk"
                try {
                    onProgress("${series.takeLast(6)} : ${f.name}")
                    sendFile(base, token, series, kind, f.name, f)
                    val len = f.length(); f.delete()
                    status.value = status.value.copy(sentFiles = status.value.sentFiles + 1, sentBytes = status.value.sentBytes + len, lastError = null)
                    Journal.log("envoyé ${f.name} (${len shr 10} ko)")
                    refresh(ctx)
                } catch (e: Exception) {
                    allOk = false
                    status.value = status.value.copy(lastError = e.message)
                    Journal.log("ERREUR envoi ${f.name} : ${e.message}")
                    break
                }
            }
            if (!allOk) break
            val fin = File(dir, "finish.json")
            if (fin.exists()) {
                try {
                    val (code, body) = finish(base, token, fin.readText())
                    if (code == 200) {
                        val j = try { JSONObject(body) } catch (e: Exception) { JSONObject() }
                        Journal.log("série $series clôturée côté serveur : HTTP 200, réveil GitHub ${if (j.optBoolean("dispatched")) "oui" else "non"}")
                        fin.delete(); dir.deleteRecursively()
                        status.value = status.value.copy(lastError = null)
                    } else {
                        allOk = false; status.value = status.value.copy(lastError = "clôture HTTP $code")
                        Journal.log("clôture $series refusée : HTTP $code ${body.take(100)}")
                        if (code == 401) break
                    }
                } catch (e: Exception) {
                    allOk = false; status.value = status.value.copy(lastError = e.message); Journal.log("ERREUR clôture $series : ${e.message}")
                }
            } else if (files.isEmpty()) dir.deleteRecursively()   // dossier vide (série ouverte sans fin) : nettoyé
            if (!allOk) break
        }
        refresh(ctx)
        return allOk
    }
}

class UploadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        try { setForeground(info("Envoi des prises...")) } catch (e: Exception) { /* notification refusée : on continue en arrière-plan simple */ }
        Uploader.status.value = Uploader.status.value.copy(busy = true)
        val ok = try { Uploader.drain(applicationContext) { msg -> try { setForegroundAsync(info(msg)) } catch (e: Exception) {} } } finally {
            Uploader.status.value = Uploader.status.value.copy(busy = false)
        }
        if (ok) Result.success() else if (runAttemptCount < 30) Result.retry() else Result.failure()
    }

    private fun info(text: String): ForegroundInfo {
        val nm = applicationContext.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26) nm.createNotificationChannel(NotificationChannel("upload", "Envoi des prises", NotificationManager.IMPORTANCE_LOW))
        val n = NotificationCompat.Builder(applicationContext, "upload")
            .setSmallIcon(android.R.drawable.stat_sys_upload).setContentTitle("ScanLame").setContentText(text).setOngoing(true).build()
        return if (Build.VERSION.SDK_INT >= 29) ForegroundInfo(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC) else ForegroundInfo(1, n)
    }
}
