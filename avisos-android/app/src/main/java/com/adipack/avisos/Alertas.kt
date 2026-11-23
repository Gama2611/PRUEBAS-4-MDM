package com.adipack.avisos

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.provider.Settings

/** Notificaciones y apertura de la pantalla de alerta. */
object Notifier {
    const val CH_SERVICIO = "servicio"
    const val CH_AVISOS = "avisos_v1"
    const val CH_URGENTES = "urgentes_v1"
    const val ID_SERVICIO = 1
    private const val TAG_AVISO = "aviso"
    const val AGENT_PACKAGE = "com.adipack.mdm.agent"
    const val AGENT_SHOW_ALERT = "com.adipack.mdm.agent.action.SHOW_ALERT"

    fun createChannels(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        nm.createNotificationChannel(NotificationChannel(CH_SERVICIO, "Conexión con el MDM", NotificationManager.IMPORTANCE_MIN).apply {
            description = "Aviso permanente mientras la app escucha los mensajes"
            setShowBadge(false)
        })
        // El sonido lo reproduce la app (también funciona con el kiosco, que silencia las notificaciones).
        for ((id, name) in listOf(CH_AVISOS to "Avisos", CH_URGENTES to "Avisos urgentes")) {
            nm.createNotificationChannel(NotificationChannel(id, name, NotificationManager.IMPORTANCE_HIGH).apply {
                setSound(null, null)
                enableVibration(false)
                lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            })
        }
    }

    fun servicio(ctx: Context, texto: String): Notification {
        val open = PendingIntent.getActivity(ctx, 0, Intent(ctx, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return Notification.Builder(ctx, CH_SERVICIO)
            .setSmallIcon(R.drawable.ic_aviso)
            .setContentTitle("ADIPACK Avisos")
            .setContentText(texto)
            .setOngoing(true)
            .setContentIntent(open)
            .build()
    }

    fun mostrar(ctx: Context, a: Aviso) {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        val req = (a.id % Int.MAX_VALUE).toInt()
        val open = PendingIntent.getActivity(ctx, req, AlertActivity.intent(ctx, a.id),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val leido = PendingIntent.getForegroundService(ctx, req,
            Intent(ctx, AvisosService::class.java).setAction(AvisosService.ACTION_LEIDO).putExtra(AvisosService.EXTRA_ID, a.id),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = Notification.Builder(ctx, if (a.urgente) CH_URGENTES else CH_AVISOS)
            .setSmallIcon(R.drawable.ic_aviso)
            .setContentTitle(a.titulo)
            .setContentText(a.mensaje)
            .setStyle(Notification.BigTextStyle().bigText(a.mensaje))
            .setCategory(if (a.urgente || a.alta) Notification.CATEGORY_ALARM else Notification.CATEGORY_MESSAGE)
            .setContentIntent(open)
            .setFullScreenIntent(open, true)  // con la pantalla apagada, la enciende y abre la alerta
            .addAction(Notification.Action.Builder(null, "Entendido", leido).build())
            .setOngoing(a.urgente)
            .setAutoCancel(false)
            .setWhen(a.enviadoMs)
            .setShowWhen(true)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .setColor(colorDe(a))
            .build()
        try { nm.notify(TAG_AVISO, req, n) } catch (e: SecurityException) { logE("Sin permiso de notificaciones", e) }
        abrirPantalla(ctx, a.id)
    }

    /**
     * Abre la alerta encima de cualquier app (también en modo kiosco). Android no deja que una app en
     * segundo plano abra pantallas, pero el agente MDM es Device Owner y sí puede: se lo pedimos a él.
     * Si el agente no está, queda la notificación con «pantalla completa».
     */
    fun abrirPantalla(ctx: Context, id: Long) {
        try {
            ctx.sendBroadcast(Intent(AGENT_SHOW_ALERT).setPackage(AGENT_PACKAGE)
                .putExtra("component", "${ctx.packageName}/${AlertActivity::class.java.name}")
                .putExtra(AvisosService.EXTRA_ID, id))
        } catch (e: Exception) { logE("No se pudo pedir la alerta al agente", e) }
        try { ctx.startActivity(AlertActivity.intent(ctx, id)) } catch (_: Exception) { }
    }

    fun cancelar(ctx: Context, id: Long) {
        ctx.getSystemService(NotificationManager::class.java)?.cancel(TAG_AVISO, (id % Int.MAX_VALUE).toInt())
    }

    fun colorDe(a: Aviso): Int = when {
        a.urgente -> 0xFFB71C1C.toInt()
        a.alta -> 0xFFE65100.toInt()
        else -> 0xFFD81B72.toInt()
    }
}

/** Sonido y vibración. Normal: un tono. Alta: alarma 10 s. Urgente: alarma sin parar hasta «Entendido». */
class Sonido(private val ctx: Context) {
    private var player: MediaPlayer? = null
    private var sonandoId = 0L
    private val handler = Handler(Looper.getMainLooper())
    private val parar = Runnable { stop() }

    @Synchronized fun play(a: Aviso) {
        stop()
        val usage = if (a.urgente || a.alta) AudioAttributes.USAGE_ALARM else AudioAttributes.USAGE_NOTIFICATION
        val uri: Uri = (if (a.urgente || a.alta) RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM) else null)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
            ?: Settings.System.DEFAULT_NOTIFICATION_URI
        try {
            player = MediaPlayer().apply {
                setAudioAttributes(AudioAttributes.Builder().setUsage(usage)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
                setDataSource(ctx, uri)
                isLooping = a.urgente || a.alta
                setOnCompletionListener { stop() }
                prepare()
                start()
            }
            sonandoId = a.id
            if (a.alta) handler.postDelayed(parar, 10_000)
        } catch (e: Exception) {
            logE("No se pudo reproducir el sonido", e)
            stop()
        }
        vibrar(a)
    }

    /** Detiene el sonido si es el de ese aviso (o cualquiera con id = 0). */
    @Synchronized fun stop(id: Long = 0) {
        if (id != 0L && id != sonandoId) return
        handler.removeCallbacks(parar)
        try { player?.stop() } catch (_: Exception) { }
        try { player?.release() } catch (_: Exception) { }
        player = null
        sonandoId = 0
        vibrator()?.cancel()
    }

    private fun vibrar(a: Aviso) {
        val v = vibrator() ?: return
        if (!v.hasVibrator()) return
        val pattern = longArrayOf(0, 700, 400, 700, 400)
        try {
            v.vibrate(VibrationEffect.createWaveform(pattern, if (a.urgente) 0 else -1))
        } catch (e: Exception) { logE("Vibración no disponible", e) }
    }

    private fun vibrator(): Vibrator? =
        if (Build.VERSION.SDK_INT >= 31) ctx.getSystemService(VibratorManager::class.java)?.defaultVibrator
        else @Suppress("DEPRECATION") ctx.getSystemService(Vibrator::class.java)
}
