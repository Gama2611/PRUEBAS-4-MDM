package com.adipack.avisos

import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.IBinder
import org.json.JSONObject
import java.io.IOException
import kotlin.random.Random

/**
 * Servicio en primer plano que mantiene la conexión MQTT con el MDM.
 *
 * - Reconecta solo (espera 2 s, 4 s, 8 s… hasta 60 s) y al instante cuando vuelve la red.
 * - Sesión persistente: lo enviado mientras la tablet estaba desconectada llega al reconectar.
 * - Cada aviso se guarda, se muestra y se confirma («recibido»); al tocar «Entendido» se confirma «leído».
 * - Las confirmaciones que no pudieron enviarse se reintentan en la siguiente conexión.
 */
class AvisosService : Service(), MqttClient.Listener {

    companion object {
        const val ACTION_LEIDO = "com.adipack.avisos.action.LEIDO"
        const val EXTRA_ID = "aviso_id"
        /** Aviso interno a las pantallas de la app para que se actualicen. */
        const val BROADCAST_CAMBIO = "com.adipack.avisos.action.CAMBIO"

        @Volatile var estado: String = "Iniciando…"
            private set
        @Volatile var conectado = false
            private set

        fun start(ctx: Context, action: String? = null, id: Long = 0) {
            val i = Intent(ctx, AvisosService::class.java)
            if (action != null) i.action = action
            if (id > 0) i.putExtra(EXTRA_ID, id)
            try { ctx.startForegroundService(i) } catch (e: Exception) { logE("No se pudo iniciar el servicio", e) }
        }

        fun avisarCambio(ctx: Context) = ctx.sendBroadcast(Intent(BROADCAST_CAMBIO).setPackage(ctx.packageName))
    }

    private val lock = Object()
    private var kicked = false
    @Volatile private var running = false
    @Volatile private var client: MqttClient? = null
    @Volatile private var cfg: AvisosConfig? = null
    private var worker: Thread? = null
    private lateinit var sonido: Sonido
    private var netCallback: ConnectivityManager.NetworkCallback? = null

    private val restrictionsReceiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) { logI("El MDM cambió la configuración"); kick() }
    }

    // ------------------------------------------------------------------ ciclo de vida
    override fun onCreate() {
        super.onCreate()
        Notifier.createChannels(this)
        sonido = Sonido(this)
        enPrimerPlano()
        running = true
        try {
            val cb = object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) { kick() }
            }
            getSystemService(ConnectivityManager::class.java)?.registerDefaultNetworkCallback(cb)
            netCallback = cb
        } catch (e: Exception) { logE("Sin aviso de cambios de red", e) }
        val f = IntentFilter(Intent.ACTION_APPLICATION_RESTRICTIONS_CHANGED)
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(restrictionsReceiver, f, Context.RECEIVER_NOT_EXPORTED)
        else registerReceiver(restrictionsReceiver, f)
        worker = Thread({ bucle() }, "avisos-conexion").apply { isDaemon = true; start() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        enPrimerPlano()  // obligatorio en cada startForegroundService, aunque ya esté en primer plano
        if (intent?.action == ACTION_LEIDO) {
            val id = intent.getLongExtra(EXTRA_ID, 0)
            if (id > 0) marcarLeido(id)
        }
        kick()
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        kick()
        try { netCallback?.let { getSystemService(ConnectivityManager::class.java)?.unregisterNetworkCallback(it) } } catch (_: Exception) { }
        try { unregisterReceiver(restrictionsReceiver) } catch (_: Exception) { }
        val c = client
        val conf = cfg
        if (c != null && conf != null) Thread { despedirse(c, conf) }.start()
        sonido.stop()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun enPrimerPlano() {
        val n = Notifier.servicio(this, estado)
        if (Build.VERSION.SDK_INT >= 34) startForeground(Notifier.ID_SERVICIO, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        else startForeground(Notifier.ID_SERVICIO, n)
    }

    private fun setEstado(texto: String, ok: Boolean = conectado) {
        if (texto == estado && ok == conectado) return
        estado = texto
        conectado = ok
        logI("Estado: $texto")
        try {
            getSystemService(android.app.NotificationManager::class.java)?.notify(Notifier.ID_SERVICIO, Notifier.servicio(this, texto))
        } catch (_: SecurityException) { }
        avisarCambio(this)
    }

    // ------------------------------------------------------------------ conexión
    private fun kick() = synchronized(lock) { kicked = true; lock.notifyAll() }

    private fun esperar(ms: Long) = synchronized(lock) {
        if (!kicked) lock.wait(ms)
        kicked = false
    }

    private fun bucle() {
        var espera = 2_000L
        while (running) {
            val c = ConfigStore.load(this)
            cfg = c
            if (!c.complete) {
                setEstado("Sin configurar: instala la app desde el MDM o configúrala a mano", false)
                esperar(60_000)
                continue
            }
            try {
                setEstado("Conectando con ${c.host}:${c.port}…", false)
                val will = MqttClient.Will(c.estadoTopic, estadoJson("desconectado"))
                val cl = MqttClient(c.host, c.port, c.user, c.user, c.pass, 30, false, will, this)
                cl.connect()
                client = cl
                if (cl.subscribe(c.avisosTopic, 1) == 0x80)
                    throw IOException("El MDM no permite recibir avisos en esta tablet")
                cl.publish(c.estadoTopic, estadoJson("en_linea"), 1, retain = true, waitMs = 5_000)
                setEstado("Conectada · ${c.name}", true)
                espera = 2_000L
                while (running && cl.connected) {
                    enviarConfirmaciones(cl, c)
                    esperar(30_000)
                    if (ConfigStore.load(this) != c) { logI("Configuración nueva: reconectando"); break }
                }
                if (cl.connected) despedirse(cl, c)
            } catch (e: MqttClient.MqttException) {
                setEstado("Rechazada por el servidor: ${e.message}", false)
                espera = maxOf(espera, 30_000L)
            } catch (e: Exception) {
                setEstado("Sin conexión con ${c.host}:${c.port} (${e.message ?: e.javaClass.simpleName}). Reintentando…", false)
            }
            client = null
            if (!running) break
            if (conectado) setEstado("Reconectando…", false)
            esperar(espera + Random.nextLong(0, 1_000))
            espera = minOf(espera * 2, 60_000L)
        }
    }

    private fun despedirse(cl: MqttClient, c: AvisosConfig) {
        try { cl.publish(c.estadoTopic, estadoJson("desconectado"), 1, retain = true, waitMs = 2_000) } catch (_: Exception) { }
        cl.disconnect()
    }

    private fun estadoJson(estado: String) = JSONObject()
        .put("estado", estado).put("app", APP_VERSION).put("ms", System.currentTimeMillis())
        .toString().toByteArray(Charsets.UTF_8)

    private fun enviarConfirmaciones(cl: MqttClient, c: AvisosConfig) {
        for ((id, est) in Store.pendingAcks(this)) {
            if (!cl.connected) return
            val body = JSONObject().put("id", id).put("estado", est).put("ms", System.currentTimeMillis())
            try {
                if (cl.publish(c.ackTopic, body.toString().toByteArray(Charsets.UTF_8), 1, waitMs = 5_000))
                    Store.ackSent(this, id, est)
            } catch (e: Exception) {
                logE("No se pudo confirmar el aviso $id", e)
                return
            }
        }
    }

    // ------------------------------------------------------------------ mensajes (hilo lector)
    override fun onMessage(topic: String, payload: ByteArray, qos: Int, retained: Boolean) {
        val o = try { JSONObject(String(payload, Charsets.UTF_8)) } catch (e: Exception) {
            logE("Mensaje ilegible en $topic", e); return
        }
        try {
            when (o.optString("tipo", "aviso")) {
                "aviso" -> recibirAviso(Aviso.fromJson(o))
                "retirar" -> retirarAviso(o.getLong("id"))
                else -> logI("Mensaje de tipo desconocido: ${o.optString("tipo")}")
            }
        } catch (e: Exception) {
            logE("Error procesando un aviso", e)
        }
    }

    override fun onConnectionLost(cause: Throwable?) {
        logI("Conexión perdida: ${cause?.message}")
        kick()
    }

    private fun recibirAviso(a: Aviso) {
        val nuevo = Store.add(this, a)
        if (nuevo && a.vigente()) {
            Notifier.mostrar(this, a)
            sonido.play(a)
        }
        avisarCambio(this)
        kick()  // enviar la confirmación «recibido» (o «leído» si ya lo estaba) desde el hilo de conexión
    }

    private fun retirarAviso(id: Long) {
        Store.retire(this, id)
        Notifier.cancelar(this, id)
        sonido.stop(id)
        avisarCambio(this)
    }

    private fun marcarLeido(id: Long) {
        Store.markRead(this, id)
        Notifier.cancelar(this, id)
        sonido.stop(id)
        avisarCambio(this)
        kick()
    }
}
