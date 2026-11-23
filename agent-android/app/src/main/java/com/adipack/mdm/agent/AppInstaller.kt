package com.adipack.mdm.agent

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import java.io.File
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

data class Result(val success: Boolean, val output: String, val after: (() -> Unit)? = null)

/**
 * Instalación silenciosa con PackageInstaller (Device Owner no necesita confirmación del usuario).
 * Nota Xiaomi/HyperOS: si la ROM exige confirmación, se abre el diálogo del sistema y se informa al servidor.
 */
object AppInstaller {
    private const val ACTION = "com.adipack.mdm.agent.INSTALL_RESULT"
    private val latches = ConcurrentHashMap<Int, CountDownLatch>()
    private val results = ConcurrentHashMap<Int, Result>()

    fun installFromServer(ctx: Context, api: Api, url: String, sha256: String, expectedPkg: String): Result {
        val file = File(ctx.cacheDir, "apk/$expectedPkg.apk")
        return try {
            api.download(url, file, sha256)
            val r = installFile(ctx, file, expectedPkg)
            if (r.success) PolicyApplier.applyKioskIfTarget(ctx, expectedPkg)
            r
        } catch (e: Exception) {
            Result(false, "Descarga/instalación falló: ${e.message}")
        } finally {
            file.delete()
        }
    }

    private fun installFile(ctx: Context, apk: File, pkg: String): Result {
        val installer = ctx.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(pkg)
            setSize(apk.length())
            if (Build.VERSION.SDK_INT >= 31) setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
        }
        val sessionId = installer.createSession(params)
        val latch = CountDownLatch(1)
        latches[sessionId] = latch
        try {
            installer.openSession(sessionId).use { session ->
                session.openWrite("base.apk", 0, apk.length()).use { out ->
                    apk.inputStream().use { it.copyTo(out, 64 * 1024) }
                    session.fsync(out)
                }
                val intent = Intent(ctx, InstallResultReceiver::class.java).setAction(ACTION).setPackage(ctx.packageName)
                val flags = PendingIntent.FLAG_UPDATE_CURRENT or
                    (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
                val pi = PendingIntent.getBroadcast(ctx, sessionId, intent, flags)
                session.commit(pi.intentSender)
            }
            if (!latch.await(5, TimeUnit.MINUTES))
                return results.remove(sessionId) ?: Result(false, "Tiempo agotado esperando la instalación")
            return results.remove(sessionId) ?: Result(false, "Sin resultado")
        } catch (e: Exception) {
            try { installer.abandonSession(sessionId) } catch (_: Exception) {}
            return Result(false, "Error de instalación: ${e.message}")
        } finally {
            latches.remove(sessionId)
        }
    }

    fun uninstall(ctx: Context, pkg: String): Result {
        val sessionKey = pkg.hashCode()
        val latch = CountDownLatch(1)
        latches[sessionKey] = latch
        val intent = Intent(ctx, InstallResultReceiver::class.java).setAction(ACTION).setPackage(ctx.packageName)
            .putExtra("key", sessionKey)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
        return try {
            ctx.packageManager.packageInstaller.uninstall(pkg, PendingIntent.getBroadcast(ctx, sessionKey, intent, flags).intentSender)
            if (!latch.await(2, TimeUnit.MINUTES)) Result(false, "Tiempo agotado") else results.remove(sessionKey) ?: Result(false, "Sin resultado")
        } catch (e: Exception) {
            Result(false, "Error al desinstalar: ${e.message}")
        } finally { latches.remove(sessionKey) }
    }

    internal fun onResult(ctx: Context, intent: Intent) {
        val key = if (intent.hasExtra("key")) intent.getIntExtra("key", -1)
                  else intent.getIntExtra(PackageInstaller.EXTRA_SESSION_ID, -1)
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        val msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE).orEmpty()
        if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            // La ROM pide confirmación (caso HyperOS): mostramos el diálogo y seguimos esperando.
            @Suppress("DEPRECATION")
            val confirm = if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                          else intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
            confirm?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)?.let {
                try { ctx.startActivity(it) } catch (e: Exception) { logE("No se pudo abrir confirmación", e) }
            }
            results[key] = Result(false, "La ROM exigió confirmación manual en pantalla (Xiaomi). Revisa la tablet.")
            return  // no liberamos el latch: llegará otro callback con el resultado final
        }
        results[key] = if (status == PackageInstaller.STATUS_SUCCESS) Result(true, "OK")
                       else Result(false, "Estado $status: $msg")
        latches[key]?.countDown()
    }
}

class InstallResultReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) = AppInstaller.onResult(context, intent)
}
