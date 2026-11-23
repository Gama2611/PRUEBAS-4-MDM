package com.adipack.avisos

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Cliente MQTT 3.1.1 mínimo y sin dependencias (sólo java.net), hecho para avisos en red local.
 *
 * Soporta: CONNECT con usuario/clave y Last Will, SUBSCRIBE, PUBLISH QoS 0/1 (y recepción QoS 2),
 * PUBACK, keep-alive con PINGREQ y detección de conexión caída. No reconecta solo: eso lo decide
 * [AvisosService] (con espera progresiva y aviso del sistema cuando vuelve la red).
 *
 * No usa clases de Android: se prueba en la PC contra el broker real del MDM.
 */
class MqttClient(
    private val host: String,
    private val port: Int,
    val clientId: String,
    private val username: String,
    private val password: String,
    private val keepAliveSec: Int = 30,
    private val cleanSession: Boolean = false,
    private val will: Will? = null,
    private val listener: Listener,
) {
    class Will(val topic: String, val payload: ByteArray, val qos: Int = 1, val retain: Boolean = true)

    interface Listener {
        /** Llega en el hilo lector: debe ser rápido (guardar y delegar). */
        fun onMessage(topic: String, payload: ByteArray, qos: Int, retained: Boolean)

        /** La conexión se perdió (no se llama si la cerró [disconnect]). */
        fun onConnectionLost(cause: Throwable?)
    }

    class MqttException(val code: Int, message: String) : IOException(message)

    @Volatile private var socket: Socket? = null
    @Volatile private var output: OutputStream? = null
    private val writeLock = Any()
    private val closed = AtomicBoolean(false)
    private val pendingAcks = ConcurrentHashMap<Int, CountDownLatch>()
    private val subackCodes = ConcurrentHashMap<Int, Int>()
    private val qos2Received = ConcurrentHashMap.newKeySet<Int>()
    private var nextId = 1
    @Volatile var connected = false
        private set
    @Volatile var sessionPresent = false
        private set
    @Volatile private var lastRx = 0L

    // ------------------------------------------------------------------ conexión
    /** Conecta y espera CONNACK. Lanza [MqttException] (código de rechazo) o IOException. */
    fun connect(timeoutMs: Int = 10_000) {
        val s = Socket()
        socket = s
        try {
            s.tcpNoDelay = true
            s.keepAlive = true
            s.connect(InetSocketAddress(host, port), timeoutMs)
            s.soTimeout = timeoutMs
            val out = BufferedOutputStream(s.getOutputStream())
            val inp = BufferedInputStream(s.getInputStream())
            output = out
            writePacket(CONNECT, 0, connectBody())
            val (type, _, body) = readPacket(inp)
            if (type != CONNACK || body.size != 2) throw IOException("Respuesta inesperada del servidor ($type)")
            val rc = body[1].toInt() and 0xFF
            if (rc != 0) throw MqttException(rc, connackText(rc))
            sessionPresent = (body[0].toInt() and 0x01) == 1
            // Lecturas: si en 1,5×keep-alive (+margen) no llega nada (ni PINGRESP), la conexión está muerta.
            s.soTimeout = if (keepAliveSec > 0) keepAliveSec * 1500 + 5000 else 0
            lastRx = System.currentTimeMillis()
            connected = true
            Thread({ readLoop(inp) }, "mqtt-lector").apply { isDaemon = true }.start()
            if (keepAliveSec > 0) Thread({ pingLoop() }, "mqtt-ping").apply { isDaemon = true }.start()
        } catch (e: Exception) {
            closed.set(true)
            closeSocket()
            throw e
        }
    }

    /** Suscribe y espera SUBACK. Devuelve el QoS concedido (0x80 = rechazado por el servidor). */
    fun subscribe(topic: String, qos: Int = 1, timeoutMs: Long = 10_000): Int {
        val id = nextPacketId()
        val latch = CountDownLatch(1)
        pendingAcks[id] = latch
        val body = ByteArrayOutputStream()
        body.write(u16(id)); body.write(str(topic)); body.write(qos)
        writePacket(SUBSCRIBE, 0x02, body.toByteArray())
        if (!latch.await(timeoutMs, TimeUnit.MILLISECONDS)) {
            pendingAcks.remove(id)
            throw IOException("El servidor no confirmó la suscripción")
        }
        return subackCodes.remove(id) ?: throw IOException("Conexión perdida al suscribirse")
    }

    /** Publica. Con QoS 1 y [waitMs] > 0 espera el PUBACK (devuelve false si no llegó a tiempo). */
    fun publish(topic: String, payload: ByteArray, qos: Int = 1, retain: Boolean = false, waitMs: Long = 0): Boolean {
        require(qos == 0 || qos == 1) { "Este cliente publica con QoS 0 o 1" }
        val body = ByteArrayOutputStream()
        body.write(str(topic))
        var latch: CountDownLatch? = null
        var id = 0
        if (qos == 1) {
            id = nextPacketId()
            if (waitMs > 0) { latch = CountDownLatch(1); pendingAcks[id] = latch }
            body.write(u16(id))
        }
        body.write(payload)
        writePacket(PUBLISH, (qos shl 1) or (if (retain) 1 else 0), body.toByteArray())
        if (latch == null) return true
        val ok = latch.await(waitMs, TimeUnit.MILLISECONDS)
        pendingAcks.remove(id)
        return ok && connected  // si se cayó la conexión, el latch se liberó sin PUBACK
    }

    /** Cierre ordenado (sin Last Will). */
    fun disconnect() {
        if (closed.getAndSet(true)) return
        connected = false
        try { writePacket(DISCONNECT, 0, ByteArray(0)) } catch (_: Exception) { }
        closeSocket()
        releaseWaiters()
    }

    // ------------------------------------------------------------------ hilos
    private fun readLoop(inp: InputStream) {
        try {
            while (!closed.get()) {
                val (type, flags, body) = readPacket(inp)
                lastRx = System.currentTimeMillis()
                when (type) {
                    PUBLISH -> onPublish(flags, body)
                    PUBACK, UNSUBACK -> pendingAcks.remove(u16At(body, 0))?.countDown()
                    SUBACK -> {
                        val id = u16At(body, 0)
                        subackCodes[id] = if (body.size > 2) body[2].toInt() and 0xFF else 0x80
                        pendingAcks.remove(id)?.countDown()
                    }
                    PUBREL -> {
                        val id = u16At(body, 0)
                        qos2Received.remove(id)
                        writePacket(PUBCOMP, 0, u16(id))
                    }
                    PINGRESP, PUBREC, PUBCOMP -> {}
                    else -> throw IOException("Paquete MQTT inesperado: $type")
                }
            }
        } catch (e: Exception) {
            lost(e)
        }
    }

    private fun onPublish(flags: Int, body: ByteArray) {
        val qos = (flags shr 1) and 0x03
        val retained = (flags and 0x01) == 1
        val tlen = u16At(body, 0)
        if (2 + tlen > body.size) throw IOException("PUBLISH truncado")
        val topic = String(body, 2, tlen, Charsets.UTF_8)
        var pos = 2 + tlen
        var id = 0
        if (qos > 0) { id = u16At(body, pos); pos += 2 }
        val payload = body.copyOfRange(pos, body.size)
        when (qos) {
            0 -> listener.onMessage(topic, payload, 0, retained)
            1 -> {
                // Primero se entrega a la app (que lo guarda) y luego se confirma al servidor.
                listener.onMessage(topic, payload, 1, retained)
                writePacket(PUBACK, 0, u16(id))
            }
            2 -> {
                if (qos2Received.add(id)) listener.onMessage(topic, payload, 2, retained)
                writePacket(PUBREC, 0, u16(id))
            }
            else -> throw IOException("QoS inválido")
        }
    }

    private fun pingLoop() {
        val period = keepAliveSec * 1000L
        try {
            while (!closed.get()) {
                Thread.sleep(period)
                if (closed.get()) break
                if (System.currentTimeMillis() - lastRx > period * 2 + 5000) throw IOException("Sin respuesta del servidor")
                writePacket(PINGREQ, 0, ByteArray(0))
            }
        } catch (_: InterruptedException) {
        } catch (e: Exception) {
            lost(e)
        }
    }

    private fun lost(e: Throwable?) {
        if (closed.getAndSet(true)) return
        connected = false
        closeSocket()
        releaseWaiters()
        listener.onConnectionLost(e)
    }

    private fun releaseWaiters() {
        for (l in pendingAcks.values) l.countDown()
        pendingAcks.clear()
    }

    private fun closeSocket() {
        try { socket?.close() } catch (_: Exception) { }
    }

    // ------------------------------------------------------------------ protocolo
    private fun connectBody(): ByteArray {
        val b = ByteArrayOutputStream()
        b.write(str("MQTT")); b.write(4)
        var f = 0
        if (cleanSession) f = f or 0x02
        if (will != null) f = f or 0x04 or ((will.qos and 0x03) shl 3) or (if (will.retain) 0x20 else 0)
        if (password.isNotEmpty()) f = f or 0x40
        if (username.isNotEmpty()) f = f or 0x80
        b.write(f)
        b.write(u16(keepAliveSec))
        b.write(str(clientId))
        if (will != null) { b.write(str(will.topic)); b.write(u16(will.payload.size)); b.write(will.payload) }
        if (username.isNotEmpty()) b.write(str(username))
        if (password.isNotEmpty()) { val p = password.toByteArray(Charsets.UTF_8); b.write(u16(p.size)); b.write(p) }
        return b.toByteArray()
    }

    private fun writePacket(type: Int, flags: Int, body: ByteArray) {
        val out = output ?: throw IOException("Sin conexión")
        val header = ByteArrayOutputStream()
        header.write((type shl 4) or flags)
        var n = body.size
        do {
            var d = n % 128
            n /= 128
            if (n > 0) d = d or 0x80
            header.write(d)
        } while (n > 0)
        synchronized(writeLock) {
            out.write(header.toByteArray())
            out.write(body)
            out.flush()
        }
    }

    private fun readPacket(inp: InputStream): Triple<Int, Int, ByteArray> {
        val h = inp.read()
        if (h < 0) throw EOFException("El servidor cerró la conexión")
        var mult = 1
        var len = 0
        var i = 0
        while (true) {
            val d = inp.read()
            if (d < 0) throw EOFException("Conexión cerrada")
            len += (d and 0x7F) * mult
            if (d and 0x80 == 0) break
            mult *= 128
            if (++i >= 4) throw IOException("Longitud MQTT inválida")
        }
        if (len > MAX_PACKET) throw IOException("Paquete demasiado grande ($len bytes)")
        val body = ByteArray(len)
        var off = 0
        while (off < len) {
            val r = inp.read(body, off, len - off)
            if (r < 0) throw EOFException("Conexión cerrada")
            off += r
        }
        return Triple(h shr 4, h and 0x0F, body)
    }

    @Synchronized private fun nextPacketId(): Int {
        val id = nextId
        nextId = if (nextId >= 65535) 1 else nextId + 1
        return id
    }

    companion object {
        const val CONNECT = 1; const val CONNACK = 2; const val PUBLISH = 3; const val PUBACK = 4
        const val PUBREC = 5; const val PUBREL = 6; const val PUBCOMP = 7; const val SUBSCRIBE = 8
        const val SUBACK = 9; const val UNSUBACK = 11; const val PINGREQ = 12; const val PINGRESP = 13
        const val DISCONNECT = 14
        const val MAX_PACKET = 512 * 1024

        fun u16(v: Int) = byteArrayOf(((v shr 8) and 0xFF).toByte(), (v and 0xFF).toByte())
        fun u16At(b: ByteArray, i: Int): Int {
            if (i + 2 > b.size) throw IOException("Paquete MQTT truncado")
            return ((b[i].toInt() and 0xFF) shl 8) or (b[i + 1].toInt() and 0xFF)
        }
        fun str(s: String): ByteArray {
            val b = s.toByteArray(Charsets.UTF_8)
            return u16(b.size) + b
        }

        fun connackText(rc: Int) = when (rc) {
            1 -> "El servidor no acepta esta versión de MQTT"
            2 -> "Identificador de cliente rechazado"
            3 -> "Servicio MQTT no disponible"
            4 -> "Usuario o clave incorrectos"
            5 -> "No autorizado (la tablet fue retirada del MDM o no tiene permiso)"
            else -> "Conexión rechazada (código $rc)"
        }
    }
}
