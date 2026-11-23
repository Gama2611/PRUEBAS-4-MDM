package com.adipack.mdm.agent

import android.app.Activity
import android.app.ActivityManager
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

/**
 * Lanzador (HOME) del kiosco. Entra en lock task y abre la app MES/CMMS.
 * Si el operario cierra la app, Android vuelve aquí y la reabre (con protección contra bucles de crash).
 * Salida de administrador: tocar 7 veces el logo en < 3 s y escribir el PIN de la política.
 */
class KioskActivity : Activity() {

    private val handler = Handler(Looper.getMainLooper())
    private lateinit var status: TextView
    private lateinit var adminPanel: LinearLayout
    private var taps = 0
    private var firstTap = 0L
    private val launches = ArrayDeque<Long>()

    private val ROSA = Color.parseColor("#D81B72")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        buildUi()
        SyncService.start(this)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    private fun refresh() {
        handler.removeCallbacksAndMessages(null)
        val prefs = Prefs(this)
        val p = prefs.policy()
        val kioskOn = p.optBoolean("kiosk_enabled") && !prefs.kioskSuspended() && Dpm.isOwner(this)
        if (intent.getBooleanExtra(EXTRA_UNLOCK, false) || !kioskOn) {
            intent.removeExtra(EXTRA_UNLOCK)
            try { if (inLockTask()) stopLockTask() } catch (e: Exception) { logE("stopLockTask", e) }
            showAdmin(prefs)
            return
        }
        adminPanel.visibility = View.GONE
        try { if (!inLockTask()) startLockTask() } catch (e: Exception) { logE("startLockTask", e) }
        val pkg = p.optString("kiosk_package")
        val launch = packageManager.getLaunchIntentForPackage(pkg)
        if (launch == null) {
            status.text = "Instalando $pkg…\n${prefs.deviceName}"
            handler.postDelayed({ refresh() }, 10_000)
            return
        }
        // Protección: más de 5 aperturas en 60 s => la app está fallando; esperar 30 s.
        val now = System.currentTimeMillis()
        while (launches.isNotEmpty() && now - launches.first() > 60_000) launches.removeFirst()
        if (launches.size >= 5) {
            status.text = "La aplicación se cerró varias veces.\nReintentando en 30 s…"
            handler.postDelayed({ launches.clear(); refresh() }, 30_000)
            return
        }
        launches.addLast(now)
        status.text = "Abriendo aplicación…\n${prefs.deviceName}"
        // 4 s de espera: tiempo para que el administrador toque 7 veces el logo antes de que se reabra el MES.
        handler.postDelayed({
            try {
                startActivity(launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED))
            } catch (e: Exception) { status.text = "No se pudo abrir $pkg: ${e.message}" }
        }, RELAUNCH_DELAY_MS)
    }

    override fun onPause() {
        super.onPause()
        handler.removeCallbacksAndMessages(null)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() { /* el botón Atrás no sale del lanzador */ }

    private fun inLockTask() =
        (getSystemService(ActivityManager::class.java)?.lockTaskModeState
            ?: ActivityManager.LOCK_TASK_MODE_NONE) != ActivityManager.LOCK_TASK_MODE_NONE

    // ------------------------------------------------------------------ UI
    private fun buildUi() {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.WHITE)
            setPadding(48, 48, 48, 48)
        }
        val logo = TextView(this).apply {
            text = "ADIPACK"
            textSize = 42f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(ROSA)
            gravity = Gravity.CENTER
            setOnClickListener { onLogoTap() }
        }
        status = TextView(this).apply { textSize = 18f; gravity = Gravity.CENTER; setTextColor(Color.DKGRAY); setPadding(0, 24, 0, 24) }
        adminPanel = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; visibility = View.GONE }
        fun btn(label: String, action: () -> Unit) = Button(this).apply {
            text = label; setBackgroundColor(ROSA); setTextColor(Color.WHITE)
            setOnClickListener { action() }
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply { topMargin = 16 }
        }
        adminPanel.addView(btn("Abrir Ajustes") { startActivity(Intent(Settings.ACTION_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) })
        adminPanel.addView(btn("Abrir app MES/CMMS") {
            packageManager.getLaunchIntentForPackage(Prefs(this).policy().optString("kiosk_package"))?.let { startActivity(it) }
        })
        adminPanel.addView(btn("Sincronizar ahora") { SyncService.start(this, now = true) })
        adminPanel.addView(btn("Volver al modo kiosco") {
            val prefs = Prefs(this)
            prefs.kioskSuspendedUntil = 0
            try { PolicyApplier.applyKiosk(this, prefs.policy()) } catch (e: Exception) { logE("kiosk", e) }
            refresh()
        })
        root.addView(logo); root.addView(status); root.addView(adminPanel)
        setContentView(root)
    }

    private fun showAdmin(prefs: Prefs) {
        val p = prefs.policy()
        val until = prefs.kioskSuspendedUntil
        status.text = buildString {
            append("Equipo: ${prefs.deviceName.ifEmpty { "(sin enrolar)" }}\n")
            append("Servidor: ${prefs.serverUrl.ifEmpty { "-" }}\n")
            append("Device Owner: ${if (Dpm.isOwner(this@KioskActivity)) "sí" else "NO"}\n")
            if (until > System.currentTimeMillis()) append("Kiosco suspendido ${(until - System.currentTimeMillis()) / 60_000 + 1} min más\n")
            else if (!p.optBoolean("kiosk_enabled")) append("Kiosco desactivado por política\n")
            if (prefs.lastError.isNotEmpty()) append("Último error: ${prefs.lastError}\n")
        }
        adminPanel.visibility = View.VISIBLE
    }

    private fun onLogoTap() {
        // Cada toque pospone la reapertura del MES para que dé tiempo a completar los 7 toques.
        if (adminPanel.visibility != View.VISIBLE) {
            handler.removeCallbacksAndMessages(null)
            handler.postDelayed({ refresh() }, 8_000)
        }
        val now = System.currentTimeMillis()
        if (now - firstTap > 3000) { firstTap = now; taps = 0 }
        if (++taps < 7) return
        taps = 0
        val prefs = Prefs(this)
        val p = prefs.policy()
        val hash = p.optString("kiosk_exit_pin_hash")
        if (hash.isEmpty()) { status.text = "Salida local deshabilitada (sin PIN). Use la consola MDM."; return }
        handler.removeCallbacksAndMessages(null)  // no reabrir el MES mientras se escribe el PIN
        val input = EditText(this).apply { inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD }
        AlertDialog.Builder(this).setTitle("PIN de administrador").setView(input)
            .setPositiveButton("Entrar") { _, _ -> checkPin(input.text.toString(), prefs) }
            .setNegativeButton("Cancelar") { _, _ -> refresh() }
            .setOnCancelListener { refresh() }
            .show()
    }

    private fun checkPin(pin: String, prefs: Prefs) {
        val p = prefs.policy()
        if (sha256Hex(p.optString("kiosk_exit_pin_salt") + ":" + pin) != p.optString("kiosk_exit_pin_hash")) {
            status.text = "PIN incorrecto"
            handler.postDelayed({ refresh() }, 2_000)
            return
        }
        prefs.kioskSuspendedUntil = System.currentTimeMillis() + 10 * 60_000L
        try { PolicyApplier.applyKiosk(this, p) } catch (e: Exception) { logE("kiosk", e) }
        refresh()
    }

    companion object {
        private const val EXTRA_UNLOCK = "unlock"
        private const val RELAUNCH_DELAY_MS = 4_000L

        fun launch(ctx: Context, unlock: Boolean = false) {
            if (unlock) {
                val state = ctx.getSystemService(ActivityManager::class.java)?.lockTaskModeState
                    ?: ActivityManager.LOCK_TASK_MODE_NONE
                if (state == ActivityManager.LOCK_TASK_MODE_NONE && !Prefs(ctx).kioskSuspended()) return
            }
            val i = Intent(ctx, KioskActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
                .putExtra(EXTRA_UNLOCK, unlock)
            try { ctx.startActivity(i) } catch (e: Exception) { logE("No se pudo abrir KioskActivity", e) }
        }
    }
}

class MessageActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setTitle("Mensaje de Mejora Continua")
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(40, 30, 40, 30) }
        root.addView(TextView(this).apply { text = intent.getStringExtra("text").orEmpty(); textSize = 20f })
        root.addView(Button(this).apply { text = "Entendido"; setOnClickListener { finish() } })
        setContentView(root)
    }
}
