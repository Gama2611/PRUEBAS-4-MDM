package com.adipack.mdm.agent

import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent

/**
 * Abre la pantalla de alerta de otra app (ADIPACK Avisos) encima de todo, también en modo kiosco.
 *
 * Android no deja que una app en segundo plano abra pantallas, pero el Device Owner está exento de esa
 * restricción. Por seguridad sólo se abren las pantallas que la política del MDM autoriza en
 * "alert_activities" (paquete/clase), y sólo con un dato: el número del aviso.
 */
class AlertRelayReceiver : BroadcastReceiver() {
    companion object {
        const val ACTION = "com.adipack.mdm.agent.action.SHOW_ALERT"
    }

    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != ACTION) return
        val comp = intent.getStringExtra("component") ?: return
        val allowed = Prefs(ctx).policy().optJSONArray("alert_activities") ?: return
        if ((0 until allowed.length()).none { allowed.optString(it) == comp }) {
            logE("Alerta rechazada: $comp no está autorizada por la política")
            return
        }
        val cn = ComponentName.unflattenFromString(comp) ?: return
        val open = Intent().setComponent(cn)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        val id = intent.getLongExtra("aviso_id", 0)
        if (id > 0) open.putExtra("aviso_id", id)
        try {
            ctx.startActivity(open)
        } catch (e: Exception) {
            logE("No se pudo abrir la alerta $comp", e)
        }
    }
}
