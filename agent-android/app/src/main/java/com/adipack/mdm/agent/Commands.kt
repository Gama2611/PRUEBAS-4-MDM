package com.adipack.mdm.agent

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ApplicationInfo
import android.net.ConnectivityManager
import android.os.BatteryManager
import android.os.Build
import android.os.Environment
import android.os.StatFs
import android.os.SystemClock
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

object CommandRunner {

    fun run(ctx: Context, c: JSONObject, api: Api, prefs: Prefs): Result {
        val params = c.optJSONObject("params") ?: JSONObject()
        val dpm = Dpm.dpm(ctx)
        val admin = Dpm.admin(ctx)
        return try {
            when (c.getString("type")) {
                "sync" -> Result(true, "Sincronizado")
                "lock" -> { dpm.lockNow(); Result(true, "Pantalla bloqueada") }
                "reboot" -> Result(true, "Reiniciando…") { dpm.reboot(admin) }
                "message" -> {
                    ctx.startActivity(Intent(ctx, MessageActivity::class.java)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                        .putExtra("text", params.optString("text")))
                    Result(true, "Mensaje mostrado")
                }
                "exit_kiosk" -> {
                    val min = params.optInt("minutes", 10).coerceIn(1, 240)
                    prefs.kioskSuspendedUntil = System.currentTimeMillis() + min * 60_000L
                    PolicyApplier.applyKiosk(ctx, prefs.policy())
                    Result(true, "Kiosco suspendido $min min")
                }
                "install_app" -> AppInstaller.installFromServer(
                    ctx, api, params.getString("url"), params.getString("sha256"), params.getString("package"))
                "uninstall_app" -> {
                    val pkg = params.getString("package")
                    if (pkg == ctx.packageName) Result(false, "No se puede desinstalar el agente") else AppInstaller.uninstall(ctx, pkg)
                }
                "clear_app_data" -> clearData(ctx, params.getString("package"))
                "wipe" -> Result(true, "Borrando equipo…") {
                    if (Build.VERSION.SDK_INT >= 34) dpm.wipeDevice(0) else @Suppress("DEPRECATION") dpm.wipeData(0)
                }
                else -> Result(false, "Comando no soportado por el agente Android: ${c.optString("type")}")
            }
        } catch (e: Exception) {
            logE("Comando ${c.optString("type")} falló", e)
            Result(false, "${e.javaClass.simpleName}: ${e.message}")
        }
    }

    private fun clearData(ctx: Context, pkg: String): Result {
        if (Build.VERSION.SDK_INT < 28) return Result(false, "Requiere Android 9+")
        if (pkg == ctx.packageName) return Result(false, "No se borra el agente")
        val latch = CountDownLatch(1)
        var ok = false
        val exec = Executors.newSingleThreadExecutor()
        Dpm.dpm(ctx).clearApplicationUserData(Dpm.admin(ctx), pkg, exec) { _, succeeded -> ok = succeeded; latch.countDown() }
        latch.await(60, TimeUnit.SECONDS)
        exec.shutdown()
        if (ok) PolicyApplier.applyKioskIfTarget(ctx, pkg)
        return Result(ok, if (ok) "Datos borrados" else "No se pudieron borrar los datos")
    }
}

object StatusCollector {
    fun collect(ctx: Context, prefs: Prefs): JSONObject {
        val s = JSONObject()
        try {
            val bat = ctx.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            if (bat != null) {
                val level = bat.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
                val scale = bat.getIntExtra(BatteryManager.EXTRA_SCALE, 100)
                if (level >= 0) s.put("battery", level * 100 / scale)
                s.put("charging", bat.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0)
                s.put("battery_temp_c", bat.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) / 10.0)
            }
        } catch (e: Exception) { logE("batería", e) }
        try {
            val cm = ctx.getSystemService(ConnectivityManager::class.java)!!
            val lp = cm.getLinkProperties(cm.activeNetwork)
            s.put("ip", lp?.linkAddresses?.firstOrNull { it.address.address.size == 4 }?.address?.hostAddress ?: "")
        } catch (e: Exception) { logE("red", e) }
        try {
            val st = StatFs(Environment.getDataDirectory().path)
            s.put("storage_free_mb", st.availableBytes / 1_048_576)
            s.put("storage_total_mb", st.totalBytes / 1_048_576)
        } catch (e: Exception) { logE("almacenamiento", e) }
        val lockState = ctx.getSystemService(ActivityManager::class.java)?.lockTaskModeState
            ?: ActivityManager.LOCK_TASK_MODE_NONE
        s.put("kiosk_active", lockState != ActivityManager.LOCK_TASK_MODE_NONE)
        s.put("kiosk_suspended", prefs.kioskSuspended())
        s.put("device_owner", Dpm.isOwner(ctx))
        s.put("uptime_min", SystemClock.elapsedRealtime() / 60_000)
        s.put("model", "${Build.MANUFACTURER} ${Build.MODEL}")
        s.put("security_patch", Build.VERSION.SECURITY_PATCH)
        if (prefs.lastError.isNotEmpty()) s.put("last_error", prefs.lastError)
        val apps = JSONArray()
        try {
            for (pi in ctx.packageManager.getInstalledPackages(0)) {
                val ai = pi.applicationInfo ?: continue
                if (ai.flags and ApplicationInfo.FLAG_SYSTEM != 0 && ai.flags and ApplicationInfo.FLAG_UPDATED_SYSTEM_APP == 0) continue
                apps.put(JSONObject()
                    .put("package", pi.packageName)
                    .put("version_name", pi.versionName ?: "")
                    .put("version_code", if (Build.VERSION.SDK_INT >= 28) pi.longVersionCode else @Suppress("DEPRECATION") pi.versionCode.toLong()))
            }
        } catch (e: Exception) { logE("apps", e) }
        s.put("apps", apps)
        return s
    }
}
