package com.adipack.avisos

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.window.OnBackInvokedCallback
import android.window.OnBackInvokedDispatcher
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

private val ROSA = Color.parseColor("#D81B72")
private val FMT = SimpleDateFormat("dd/MM HH:mm", Locale.forLanguageTag("es-VE"))

private fun Context.dp(v: Int) = (v * resources.displayMetrics.density).toInt()

private fun registrarCambios(ctx: Context, r: BroadcastReceiver) {
    val f = IntentFilter(AvisosService.BROADCAST_CAMBIO)
    if (Build.VERSION.SDK_INT >= 33) ctx.registerReceiver(r, f, Context.RECEIVER_NOT_EXPORTED)
    else ctx.registerReceiver(r, f)
}

// ====================================================================== alerta a pantalla completa
/**
 * Muestra los avisos sin leer, uno por uno, encima de cualquier app (también del kiosco).
 * «ENTENDIDO» lo marca como leído y avisa al MDM. Un aviso urgente no se puede cerrar con «Atrás».
 */
class AlertActivity : Activity() {
    companion object {
        fun intent(ctx: Context, id: Long): Intent = Intent(ctx, AlertActivity::class.java)
            .putExtra(AvisosService.EXTRA_ID, id)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
    }

    private var actual: Aviso? = null
    private lateinit var raiz: LinearLayout
    private lateinit var encabezado: TextView
    private lateinit var titulo: TextView
    private lateinit var mensaje: TextView
    private lateinit var meta: TextView
    private lateinit var contador: TextView
    private lateinit var boton: Button
    private var backCallback: Any? = null

    private val cambios = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) = mostrar(actual?.id ?: 0)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 27) { setShowWhenLocked(true); setTurnScreenOn(true) }
        else @Suppress("DEPRECATION") window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        construir()
        if (Build.VERSION.SDK_INT >= 33) {
            val cb = OnBackInvokedCallback { atras() }
            onBackInvokedDispatcher.registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT, cb)
            backCallback = cb
        }
        mostrar(intent.getLongExtra(AvisosService.EXTRA_ID, 0))
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        mostrar(intent.getLongExtra(AvisosService.EXTRA_ID, 0))
    }

    override fun onResume() {
        super.onResume()
        registrarCambios(this, cambios)
        mostrar(actual?.id ?: 0)
    }

    override fun onPause() {
        super.onPause()
        try { unregisterReceiver(cambios) } catch (_: Exception) { }
    }

    override fun onDestroy() {
        if (Build.VERSION.SDK_INT >= 33) (backCallback as? OnBackInvokedCallback)?.let {
            onBackInvokedDispatcher.unregisterOnBackInvokedCallback(it)
        }
        super.onDestroy()
    }

    @Deprecated("Android 12 o anterior")
    @Suppress("DEPRECATION")
    override fun onBackPressed() = atras()

    private fun atras() {
        // Un aviso urgente sólo se cierra con «ENTENDIDO». Los demás quedan sin leer (siguen en la notificación).
        if (Store.unread(this).any { it.urgente }) return
        finish()
    }

    private fun construir() {
        val pad = dp(28)
        raiz = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        encabezado = TextView(this).apply { textSize = 22f; setTextColor(Color.WHITE); typeface = Typeface.DEFAULT_BOLD; letterSpacing = 0.08f }
        contador = TextView(this).apply { textSize = 16f; setTextColor(0xDDFFFFFF.toInt()) }
        titulo = TextView(this).apply { textSize = 36f; setTextColor(Color.WHITE); typeface = Typeface.DEFAULT_BOLD; setPadding(0, dp(18), 0, dp(12)) }
        mensaje = TextView(this).apply { textSize = 26f; setTextColor(Color.WHITE); setLineSpacing(0f, 1.15f) }
        meta = TextView(this).apply { textSize = 16f; setTextColor(0xDDFFFFFF.toInt()); setPadding(0, dp(16), 0, 0) }
        val scroll = ScrollView(this).apply {
            addView(LinearLayout(this@AlertActivity).apply {
                orientation = LinearLayout.VERTICAL
                addView(titulo); addView(mensaje); addView(meta)
            })
        }
        boton = Button(this).apply {
            text = "ENTENDIDO"
            textSize = 26f
            typeface = Typeface.DEFAULT_BOLD
            isAllCaps = true
            minHeight = dp(84)
            background = GradientDrawable().apply { setColor(Color.WHITE); cornerRadius = dp(16).toFloat() }
            setOnClickListener { entendido() }
        }
        raiz.addView(encabezado, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        raiz.addView(contador, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        raiz.addView(scroll, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        raiz.addView(boton, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT).apply { topMargin = dp(20) })
        setContentView(raiz)
    }

    private fun mostrar(preferido: Long) {
        val pendientes = Store.unread(this)
        if (pendientes.isEmpty()) { finish(); return }
        val a = pendientes.firstOrNull { it.id == preferido && it.urgente }
            ?: pendientes.firstOrNull { it.urgente }  // un urgente siempre va primero
            ?: pendientes.firstOrNull { it.id == preferido }
            ?: pendientes.first()
        actual = a
        val color = Notifier.colorDe(a)
        raiz.setBackgroundColor(color)
        boton.setTextColor(color)
        encabezado.text = when {
            a.urgente -> "⚠  AVISO URGENTE"
            a.alta -> "AVISO IMPORTANTE"
            else -> "AVISO"
        }
        contador.text = if (pendientes.size > 1) "${pendientes.indexOf(a) + 1} de ${pendientes.size} sin leer" else ""
        titulo.text = a.titulo
        mensaje.text = a.mensaje
        mensaje.visibility = if (a.mensaje.isBlank()) View.GONE else View.VISIBLE
        meta.text = "Enviado ${FMT.format(Date(a.enviadoMs))}" + (if (a.de.isNotBlank()) " por ${a.de}" else "")
    }

    private fun entendido() {
        val a = actual ?: return finish()
        Store.markRead(this, a.id)                       // la pantalla avanza al siguiente al instante
        AvisosService.start(this, AvisosService.ACTION_LEIDO, a.id)  // y el servicio confirma al MDM
        mostrar(0)
    }
}

// ====================================================================== pantalla principal
/** Estado de la conexión, historial de avisos y configuración. */
class MainActivity : Activity() {
    private lateinit var estado: TextView
    private lateinit var equipo: TextView
    private lateinit var lista: LinearLayout
    private lateinit var verPendientes: Button
    private val handler = Handler(Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() { refrescarEstado(); handler.postDelayed(this, 2_000) }
    }
    private val cambios = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) = refrescar()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = dp(20)
        val raiz = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad); setBackgroundColor(Color.WHITE) }
        raiz.addView(TextView(this).apply { text = "ADIPACK Avisos"; textSize = 26f; setTextColor(ROSA); typeface = Typeface.DEFAULT_BOLD })
        equipo = TextView(this).apply { textSize = 15f; setTextColor(Color.DKGRAY) }
        estado = TextView(this).apply { textSize = 16f; setPadding(0, dp(8), 0, dp(8)) }
        raiz.addView(equipo); raiz.addView(estado)
        val botones = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        verPendientes = Button(this).apply { setOnClickListener {
            Store.unread(this@MainActivity).firstOrNull()?.let { startActivity(AlertActivity.intent(this@MainActivity, it.id)) }
        } }
        botones.addView(verPendientes)
        botones.addView(Button(this).apply { text = "Configuración"; setOnClickListener { configuracion() } })
        raiz.addView(botones)
        raiz.addView(TextView(this).apply { text = "Historial"; textSize = 18f; typeface = Typeface.DEFAULT_BOLD; setPadding(0, dp(16), 0, dp(6)) })
        lista = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        raiz.addView(ScrollView(this).apply { addView(lista) }, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        setContentView(raiz)

        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        AvisosService.start(this)
    }

    override fun onResume() {
        super.onResume()
        registrarCambios(this, cambios)
        refrescar()
        handler.post(tick)
    }

    override fun onPause() {
        super.onPause()
        handler.removeCallbacks(tick)
        try { unregisterReceiver(cambios) } catch (_: Exception) { }
    }

    private fun refrescarEstado() {
        val c = ConfigStore.load(this)
        equipo.text = if (c.complete) "${c.name} · servidor ${c.host}:${c.port}" + (if (c.managed) " · configurada por el MDM" else "")
                      else "Sin configurar"
        estado.text = (if (AvisosService.conectado) "●  " else "○  ") + AvisosService.estado
        estado.setTextColor(if (AvisosService.conectado) Color.parseColor("#1B7F3B") else Color.parseColor("#B71C1C"))
    }

    private fun refrescar() {
        refrescarEstado()
        val pend = Store.unread(this).size
        verPendientes.text = if (pend > 0) "Ver $pend sin leer" else "Sin avisos pendientes"
        verPendientes.isEnabled = pend > 0
        lista.removeAllViews()
        val todos = Store.all(this)
        if (todos.isEmpty()) {
            lista.addView(TextView(this).apply { text = "Todavía no ha llegado ningún aviso."; setTextColor(Color.GRAY) })
            return
        }
        val now = System.currentTimeMillis()
        for (a in todos.take(100)) {
            val st = when {
                a.retirado -> "Retirado"
                a.leidoMs > 0 -> "Leído ${FMT.format(Date(a.leidoMs))}"
                a.venceMs <= now -> "Vencido"
                else -> "SIN LEER"
            }
            lista.addView(LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(12), dp(10), dp(12), dp(10))
                background = GradientDrawable().apply { setColor(0xFFF7F2F5.toInt()); cornerRadius = dp(10).toFloat() }
                addView(TextView(context).apply { text = a.titulo; textSize = 17f; typeface = Typeface.DEFAULT_BOLD; setTextColor(Notifier.colorDe(a)) })
                if (a.mensaje.isNotBlank()) addView(TextView(context).apply { text = a.mensaje; textSize = 15f; setTextColor(Color.DKGRAY) })
                addView(TextView(context).apply { text = "${FMT.format(Date(a.enviadoMs))} · ${a.prioridad} · $st"; textSize = 13f; setTextColor(Color.GRAY) })
                if (a.porLeer(now)) setOnClickListener { startActivity(AlertActivity.intent(this@MainActivity, a.id)) }
            }, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT).apply { bottomMargin = dp(8) })
        }
    }

    private fun configuracion() {
        val c = ConfigStore.load(this)
        if (c.managed) {
            AlertDialog.Builder(this)
                .setTitle("Configurada por ADIPACK MDM")
                .setMessage("Servidor: ${c.host}:${c.port}\nTablet: ${c.name} (n.º ${c.deviceId})\nUsuario: ${c.user}\n\n" +
                    "Estos datos los entrega el MDM y no se pueden cambiar aquí.")
                .setPositiveButton("Cerrar", null).show()
            return
        }
        val form = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(20), dp(8), dp(20), 0) }
        fun campo(hint: String, valor: String, tipo: Int = InputType.TYPE_CLASS_TEXT) =
            EditText(this).apply { this.hint = hint; setText(valor); inputType = tipo; isSingleLine = true }.also { form.addView(it) }
        val host = campo("IP del servidor MDM (ej. 192.168.1.50)", c.host)
        val port = campo("Puerto MQTT", c.port.toString(), InputType.TYPE_CLASS_NUMBER)
        val user = campo("Usuario (ej. tablet-3)", c.user)
        val pass = campo("Clave", c.pass, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD)
        val id = campo("N.º de la tablet en el MDM", c.deviceId, InputType.TYPE_CLASS_NUMBER)
        val name = campo("Nombre (ej. TAB-03)", c.deviceName)
        AlertDialog.Builder(this)
            .setTitle("Configuración manual")
            .setView(ScrollView(this).apply { addView(form) })
            .setPositiveButton("Guardar") { _, _ ->
                ConfigStore.saveManual(this, AvisosConfig(host.text.toString(), port.text.toString().toIntOrNull() ?: 1883,
                    user.text.toString(), pass.text.toString(), id.text.toString(), name.text.toString(), false))
                AvisosService.start(this)
                refrescar()
            }
            .setNegativeButton("Cancelar", null).show()
    }
}

// ====================================================================== arranque
/** Inicia el servicio al encender la tablet y al actualizarse la app. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED -> AvisosService.start(ctx)
        }
    }
}
