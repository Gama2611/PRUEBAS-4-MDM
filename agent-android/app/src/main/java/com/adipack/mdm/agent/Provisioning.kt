package com.adipack.mdm.agent

import android.app.Activity
import android.app.admin.DeviceAdminReceiver
import android.app.admin.DevicePolicyManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.PersistableBundle
import android.widget.TextView

/** Guarda server_url y enrollment_token que vienen en el QR (ADMIN_EXTRAS_BUNDLE). */
fun saveAdminExtras(ctx: Context, extras: PersistableBundle?) {
    if (extras == null) return
    val p = Prefs(ctx)
    extras.getString("server_url")?.takeIf { it.isNotBlank() }?.let { p.serverUrl = it }
    extras.getString("enrollment_token")?.takeIf { it.isNotBlank() }?.let { p.enrollmentToken = it }
}

@Suppress("DEPRECATION")
private fun Intent.adminExtras(): PersistableBundle? =
    if (Build.VERSION.SDK_INT >= 33)
        getParcelableExtra(DevicePolicyManager.EXTRA_PROVISIONING_ADMIN_EXTRAS_BUNDLE, PersistableBundle::class.java)
    else getParcelableExtra(DevicePolicyManager.EXTRA_PROVISIONING_ADMIN_EXTRAS_BUNDLE)

class AdminReceiver : DeviceAdminReceiver() {
    override fun onProfileProvisioningComplete(context: Context, intent: Intent) {
        saveAdminExtras(context, intent.adminExtras())
        logI("Aprovisionamiento completo; Device Owner=${Dpm.isOwner(context)}")
        SyncService.start(context, now = true)
    }

    override fun onEnabled(context: Context, intent: Intent) {
        logI("Administrador habilitado")
    }
}

/**
 * Android 12+: el asistente pregunta qué modo queremos -> totalmente gestionado.
 * Debe responder SIEMPRE y rápido: si esta pantalla falla o devuelve un modo no permitido,
 * Android muestra «No se puede configurar el dispositivo. Comunícate con el administrador de TI».
 */
class GetProvisioningModeActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        try {
            saveAdminExtras(this, intent.adminExtras())
        } catch (e: Exception) {
            logE("No se pudieron leer los datos del QR", e)
        }
        val allowed = if (Build.VERSION.SDK_INT >= 31)
            intent.getIntegerArrayListExtra(DevicePolicyManager.EXTRA_PROVISIONING_ALLOWED_PROVISIONING_MODES)
        else null
        val mode = DevicePolicyManager.PROVISIONING_MODE_FULLY_MANAGED_DEVICE
        logI("GET_PROVISIONING_MODE: permitidos=$allowed -> totalmente gestionado")
        val result = Intent().putExtra(DevicePolicyManager.EXTRA_PROVISIONING_MODE, mode)
        // Devolvemos también los datos del QR para que lleguen a ADMIN_POLICY_COMPLIANCE.
        intent.adminExtras()?.let { result.putExtra(DevicePolicyManager.EXTRA_PROVISIONING_ADMIN_EXTRAS_BUNDLE, it) }
        setResult(if (allowed == null || allowed.isEmpty() || mode in allowed) RESULT_OK else RESULT_CANCELED, result)
        finish()
    }
}

/** Android 10+: último paso del asistente; aquí ya somos Device Owner. Nunca debe fallar. */
class PolicyComplianceActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        try {
            saveAdminExtras(this, intent.adminExtras())
            logI("ADMIN_POLICY_COMPLIANCE: Device Owner=${Dpm.isOwner(this)}")
        } catch (e: Exception) {
            logE("ADMIN_POLICY_COMPLIANCE: error leyendo datos", e)
        }
        setResult(RESULT_OK)
        finish()
        // El servicio arranca después de responder al asistente (si fallara, no tumba la activación).
        val app = applicationContext
        android.os.Handler(mainLooper).postDelayed({ SyncService.start(app, now = true) }, 1500)
    }
}

/**
 * Alternativa por USB (Xiaomi que rechace el QR):
 *   adb shell dpm set-device-owner com.adipack.mdm.agent/.AdminReceiver
 *   adb shell am start -n com.adipack.mdm.agent/.SetupActivity --es server_url URL --es enrollment_token TOKEN
 * Sólo acepta datos si el agente es Device Owner y todavía no está enrolado.
 */
class SetupActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val p = Prefs(this)
        val msg = when {
            !Dpm.isOwner(this) -> "ERROR: el agente no es Device Owner"
            p.isEnrolled -> "Ya enrolado como ${p.deviceName}; no se cambian los datos"
            else -> {
                val url = intent.getStringExtra("server_url").orEmpty()
                val tok = intent.getStringExtra("enrollment_token").orEmpty()
                if (!url.startsWith("http") || tok.length < 8) "ERROR: faltan server_url / enrollment_token"
                else {
                    p.serverUrl = url; p.enrollmentToken = tok
                    SyncService.start(this, now = true)
                    "Configurado. Enrolando contra $url…"
                }
            }
        }
        logI("SetupActivity: $msg")
        setContentView(TextView(this).apply { text = msg; textSize = 20f; setPadding(48, 48, 48, 48) })
        window.decorView.postDelayed({ finish() }, 2500)
    }
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (Prefs(context).serverUrl.isNotEmpty()) SyncService.start(context, now = true)
    }
}
