package com.adipack.mdm.agent

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.app.admin.DevicePolicyManager
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import org.json.JSONObject
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Servicio en primer plano: enrola, hace check-in periódico, aplica la política y ejecuta comandos.
 * Un solo hilo de trabajo => nunca hay dos sincronizaciones simultáneas.
 */
class SyncService : Service() {

    private val wakeups = LinkedBlockingQueue<Boolean>()
    @Volatile private var running = false
    private var worker: Thread? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startAsForeground()
        selfGrantPermissions()
        running = true
        worker = Thread({ loop() }, "mdm-sync").apply { isDaemon = true; start() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.getBooleanExtra(EXTRA_NOW, false) == true) wakeups.offer(true)
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        worker?.interrupt()
        super.onDestroy()
    }

    private fun loop() {
        val prefs = Prefs(this)
        while (running) {
            try {
                syncOnce(prefs)
                prefs.lastError = ""
            } catch (e: HttpError) {
                prefs.lastError = e.message ?: "HTTP"
                logE("Sync HTTP", e)
                if (e.code == 401) logE("Token rechazado: equipo retirado o re-enrolado. Re-aprovisionar.")
            } catch (e: Exception) {
                prefs.lastError = "${e.javaClass.simpleName}: ${e.message}"
                logE("Sync falló", e)
            }
            // Fin de suspensión del kiosco: volver a bloquear.
            if (prefs.kioskSuspendedUntil in 1..System.currentTimeMillis()) {
                prefs.kioskSuspendedUntil = 0
                KioskActivity.launch(this)
            }
            val interval = prefs.policy().optInt("sync_interval_sec", 60).coerceIn(15, 3600)
            val wait = if (prefs.kioskSuspendedUntil > 0)
                minOf(interval * 1000L, prefs.kioskSuspendedUntil - System.currentTimeMillis() + 500).coerceAtLeast(1000)
            else interval * 1000L
            try { wakeups.poll(wait, TimeUnit.MILLISECONDS); wakeups.clear() } catch (_: InterruptedException) { return }
        }
    }

    private fun syncOnce(prefs: Prefs) {
        if (prefs.serverUrl.isEmpty()) return
        if (prefs.deviceToken.isEmpty()) {
            if (prefs.enrollmentToken.isEmpty()) return
            enroll(prefs)
        }
        val api = Api(prefs.serverUrl, prefs.deviceToken)
        val body = JSONObject()
            .put("agent_version", AGENT_VERSION)
            .put("os_version", "Android ${Build.VERSION.RELEASE} (SDK ${Build.VERSION.SDK_INT})")
            .put("applied_policy_version", prefs.appliedVersion)
            .put("status", StatusCollector.collect(this, prefs))
        val resp = api.post("/api/v1/checkin", body)
        resp.optString("name").takeIf { it.isNotEmpty() }?.let { prefs.deviceName = it }

        val policy = resp.optJSONObject("policy")
        if (policy != null) {
            prefs.policyJson = policy.toString()
            val errors = PolicyApplier.apply(this, policy, api)
            if (errors.isEmpty()) prefs.appliedVersion = policy.optInt("version")
            else prefs.lastError = "Política: " + errors.joinToString("; ")
        } else {
            // Reintenta apps obligatorias pendientes (p.ej. falló una descarga).
            PolicyApplier.ensureRequiredApps(this, prefs.policy(), api)
            // Permisos y servicios permanentes (p. ej. reencender ADIPACK Avisos si Android lo detuvo).
            try { PolicyApplier.afterApps(this, prefs.policy()) } catch (e: Exception) { logE("afterApps", e) }
        }

        val cmds = resp.optJSONArray("commands")
        if (cmds != null) for (i in 0 until cmds.length()) {
            val c = cmds.getJSONObject(i)
            val r = CommandRunner.run(this, c, api, prefs)
            try {
                api.post("/api/v1/commands/${c.getInt("id")}/result",
                    JSONObject().put("success", r.success).put("output", r.output.take(19_000)))
            } catch (e: Exception) { logE("No se pudo reportar el comando ${c.optInt("id")}", e) }
            r.after?.invoke()  // reboot / wipe se ejecutan DESPUÉS de reportar
        }
    }

    private fun enroll(prefs: Prefs) {
        @Suppress("HardwareIds")
        val androidId = Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID) ?: "unknown"
        val serial = try { if (Build.VERSION.SDK_INT >= 26) Build.getSerial() else "" } catch (e: SecurityException) { "" }
        val body = JSONObject()
            .put("enrollment_token", prefs.enrollmentToken)
            .put("platform", "android")
            // El serial sobrevive a un restablecimiento de fábrica (ANDROID_ID no): así la tablet conserva nombre y ubicación.
            .put("hardware_id", if (serial.isNotBlank() && serial != Build.UNKNOWN) "android-sn-$serial" else "android-$androidId")
            .put("manufacturer", Build.MANUFACTURER.take(64))
            .put("model", Build.MODEL.take(64))
            .put("serial", serial.take(64))
            .put("os_version", "Android ${Build.VERSION.RELEASE}")
            .put("agent_version", AGENT_VERSION)
        val r = Api(prefs.serverUrl).post("/api/v1/enroll", body)
        prefs.deviceToken = r.getString("device_token")
        prefs.deviceName = r.optString("name")
        prefs.appliedVersion = 0
        logI("Enrolado como ${prefs.deviceName}")
    }

    private fun selfGrantPermissions() {
        if (!Dpm.isOwner(this)) return
        val dpm = Dpm.dpm(this)
        val perms = mutableListOf(Manifest.permission.READ_PHONE_STATE, Manifest.permission.ACCESS_FINE_LOCATION)
        if (Build.VERSION.SDK_INT >= 33) perms += Manifest.permission.POST_NOTIFICATIONS
        for (p in perms) try {
            dpm.setPermissionGrantState(Dpm.admin(this), packageName, p, DevicePolicyManager.PERMISSION_GRANT_STATE_GRANTED)
        } catch (e: Exception) { logE("No se pudo conceder $p", e) }
    }

    private fun startAsForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        nm?.createNotificationChannel(NotificationChannel(CHANNEL, "Agente MDM", NotificationManager.IMPORTANCE_MIN))
        val n = Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notif)
            .setContentTitle("ADIPACK MDM activo")
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 34) startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        else startForeground(1, n)
    }

    companion object {
        private const val CHANNEL = "mdm"
        private const val EXTRA_NOW = "now"
        fun start(ctx: Context, now: Boolean = false) {
            val i = Intent(ctx, SyncService::class.java).putExtra(EXTRA_NOW, now)
            try { ctx.startForegroundService(i) } catch (e: Exception) { logE("No se pudo iniciar SyncService", e) }
        }
    }
}
