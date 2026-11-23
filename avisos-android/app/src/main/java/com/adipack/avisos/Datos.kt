package com.adipack.avisos

import android.content.Context
import android.content.RestrictionsManager
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

const val TAG = "AdipackAvisos"
const val APP_VERSION = BuildConfig.VERSION_NAME

fun logI(msg: String) = Log.i(TAG, msg)
fun logE(msg: String, e: Throwable? = null) = Log.e(TAG, msg, e)

// ====================================================================== configuración
/** Datos para conectarse al MDM. Llegan solos desde el MDM (configuración gestionada) o se escriben a mano. */
data class AvisosConfig(
    val host: String,
    val port: Int,
    val user: String,
    val pass: String,
    val deviceId: String,
    val deviceName: String,
    val managed: Boolean,
) {
    val complete get() = host.isNotBlank() && port in 1..65535 && user.isNotBlank() && pass.isNotBlank() && deviceId.isNotBlank()
    val avisosTopic get() = "adipack/avisos/$deviceId"
    val ackTopic get() = "adipack/ack/$deviceId"
    val estadoTopic get() = "adipack/estado/$deviceId"
    val name get() = deviceName.ifBlank { "Tablet $deviceId" }
}

object ConfigStore {
    private const val PREFS = "config_manual"

    fun load(ctx: Context): AvisosConfig {
        val b = try {
            ctx.getSystemService(RestrictionsManager::class.java)?.applicationRestrictions
        } catch (e: Exception) { null }
        if (b != null && !b.getString("broker_host").isNullOrBlank()) {
            @Suppress("DEPRECATION") val rawPort = b.get("broker_port")
            return AvisosConfig(
                host = b.getString("broker_host").orEmpty().trim(),
                port = portOf(rawPort),
                user = b.getString("usuario").orEmpty().trim(),
                pass = b.getString("clave").orEmpty(),
                deviceId = b.getString("dispositivo_id").orEmpty().trim(),
                deviceName = b.getString("dispositivo_nombre").orEmpty().trim(),
                managed = true,
            )
        }
        val p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        return AvisosConfig(
            p.getString("host", "").orEmpty(), p.getInt("port", 1883), p.getString("user", "").orEmpty(),
            p.getString("pass", "").orEmpty(), p.getString("id", "").orEmpty(), p.getString("name", "").orEmpty(), false,
        )
    }

    fun saveManual(ctx: Context, c: AvisosConfig) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString("host", c.host.trim()).putInt("port", c.port).putString("user", c.user.trim())
            .putString("pass", c.pass).putString("id", c.deviceId.trim()).putString("name", c.deviceName.trim())
            .apply()
    }

    fun portOf(v: Any?): Int = when (v) {
        is Int -> v
        is Long -> v.toInt()
        is String -> v.trim().toIntOrNull() ?: 1883
        else -> 1883
    }
}

// ====================================================================== avisos guardados
data class Aviso(
    val id: Long,
    val titulo: String,
    val mensaje: String,
    val prioridad: String,
    val de: String,
    val enviadoMs: Long,
    val venceMs: Long,
    var recibidoMs: Long,
    var leidoMs: Long = 0,
    var retirado: Boolean = false,
    var ackRecibido: Boolean = false,
    var ackLeido: Boolean = false,
) {
    val urgente get() = prioridad == "urgente"
    val alta get() = prioridad == "alta"
    fun vigente(now: Long = System.currentTimeMillis()) = !retirado && venceMs > now
    fun porLeer(now: Long = System.currentTimeMillis()) = vigente(now) && leidoMs == 0L

    fun toJson(): JSONObject = JSONObject()
        .put("id", id).put("titulo", titulo).put("mensaje", mensaje).put("prioridad", prioridad).put("de", de)
        .put("enviado_ms", enviadoMs).put("vence_ms", venceMs).put("recibido_ms", recibidoMs).put("leido_ms", leidoMs)
        .put("retirado", retirado).put("ack_recibido", ackRecibido).put("ack_leido", ackLeido)

    companion object {
        /** Desde el JSON del servidor ({"tipo":"aviso", "id":…}) o desde el archivo local. */
        fun fromJson(o: JSONObject, now: Long = System.currentTimeMillis()): Aviso {
            val p = o.optString("prioridad", "normal")
            return Aviso(
                id = o.getLong("id"),
                titulo = o.optString("titulo", "Aviso"),
                mensaje = o.optString("mensaje", ""),
                prioridad = if (p == "alta" || p == "urgente") p else "normal",
                de = o.optString("de", ""),
                enviadoMs = o.optLong("enviado_ms", now),
                venceMs = o.optLong("vence_ms", now + 8 * 3600_000L),
                recibidoMs = o.optLong("recibido_ms", now),
                leidoMs = o.optLong("leido_ms", 0),
                retirado = o.optBoolean("retirado", false),
                ackRecibido = o.optBoolean("ack_recibido", false),
                ackLeido = o.optBoolean("ack_leido", false),
            )
        }
    }
}

/** Historial local en un archivo JSON (escritura atómica). Seguro entre hilos. */
object Store {
    private const val FILE = "avisos.json"
    private const val MAX = 300
    private var cache: MutableList<Aviso>? = null

    @Synchronized private fun list(ctx: Context): MutableList<Aviso> {
        cache?.let { return it }
        val out = mutableListOf<Aviso>()
        val f = File(ctx.filesDir, FILE)
        if (f.exists()) {
            try {
                val arr = JSONArray(f.readText())
                for (i in 0 until arr.length()) out += Aviso.fromJson(arr.getJSONObject(i))
            } catch (e: Exception) {
                logE("Historial dañado: se empieza de nuevo", e)
            }
        }
        cache = out
        return out
    }

    @Synchronized private fun save(ctx: Context) {
        val l = list(ctx)
        if (l.size > MAX) {
            // Primero se descartan los ya leídos/vencidos más antiguos; nunca uno pendiente de leer.
            val removable = l.filter { !it.porLeer() }.sortedBy { it.recibidoMs }
            l.removeAll(removable.take(l.size - MAX).toSet())
        }
        val arr = JSONArray()
        for (a in l) arr.put(a.toJson())
        val tmp = File(ctx.filesDir, "$FILE.tmp")
        tmp.writeText(arr.toString())
        if (!tmp.renameTo(File(ctx.filesDir, FILE))) {
            File(ctx.filesDir, FILE).writeText(arr.toString())
            tmp.delete()
        }
    }

    /** Guarda un aviso nuevo. Devuelve false si ya existía (el servidor lo reenvió). */
    @Synchronized fun add(ctx: Context, a: Aviso): Boolean {
        val l = list(ctx)
        if (l.any { it.id == a.id }) return false
        l += a
        save(ctx)
        return true
    }

    @Synchronized fun get(ctx: Context, id: Long): Aviso? = list(ctx).firstOrNull { it.id == id }

    @Synchronized fun markRead(ctx: Context, id: Long): Aviso? {
        val a = get(ctx, id) ?: return null
        if (a.leidoMs == 0L) { a.leidoMs = System.currentTimeMillis(); save(ctx) }
        return a
    }

    @Synchronized fun retire(ctx: Context, id: Long): Aviso? {
        val a = get(ctx, id) ?: return null
        if (!a.retirado) { a.retirado = true; save(ctx) }
        return a
    }

    @Synchronized fun all(ctx: Context): List<Aviso> = list(ctx).sortedByDescending { it.recibidoMs }

    /** Sin leer y vigentes: urgentes primero, luego altas, luego por antigüedad. */
    @Synchronized fun unread(ctx: Context): List<Aviso> {
        val now = System.currentTimeMillis()
        return list(ctx).filter { it.porLeer(now) }
            .sortedWith(compareBy<Aviso>({ if (it.urgente) 0 else if (it.alta) 1 else 2 }, { it.recibidoMs }))
    }

    /** Confirmaciones que faltan por enviar al servidor: (id, "recibido"|"leido"). */
    @Synchronized fun pendingAcks(ctx: Context): List<Pair<Long, String>> {
        val out = mutableListOf<Pair<Long, String>>()
        for (a in list(ctx)) {
            if (a.leidoMs > 0 && !a.ackLeido) out += a.id to "leido"
            else if (!a.ackRecibido) out += a.id to "recibido"
        }
        return out
    }

    @Synchronized fun ackSent(ctx: Context, id: Long, estado: String) {
        val a = get(ctx, id) ?: return
        a.ackRecibido = true
        if (estado == "leido") a.ackLeido = true
        save(ctx)
    }
}
