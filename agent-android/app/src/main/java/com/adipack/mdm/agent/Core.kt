package com.adipack.mdm.agent

import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

const val TAG = "AdipackMDM"
const val AGENT_VERSION = "1.0.0"

/** Estado persistente del agente. Contexto "device protected" para funcionar antes del primer desbloqueo. */
class Prefs(ctx: Context) {
    private val sp: SharedPreferences =
        ctx.createDeviceProtectedStorageContext().getSharedPreferences("mdm", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = sp.getString("server_url", "") ?: ""
        set(v) = sp.edit().putString("server_url", v.trimEnd('/')).apply()
    var enrollmentToken: String
        get() = sp.getString("enrollment_token", "") ?: ""
        set(v) = sp.edit().putString("enrollment_token", v).apply()
    var deviceToken: String
        get() = sp.getString("device_token", "") ?: ""
        set(v) = sp.edit().putString("device_token", v).commit().let { }
    var deviceName: String
        get() = sp.getString("device_name", "") ?: ""
        set(v) = sp.edit().putString("device_name", v).apply()
    var policyJson: String
        get() = sp.getString("policy", "{}") ?: "{}"
        set(v) = sp.edit().putString("policy", v).commit().let { }
    var appliedVersion: Int
        get() = sp.getInt("applied_version", 0)
        set(v) = sp.edit().putInt("applied_version", v).apply()
    /** Epoch ms hasta el cual el kiosco está suspendido (comando exit_kiosk o PIN). */
    var kioskSuspendedUntil: Long
        get() = sp.getLong("kiosk_suspended_until", 0L)
        set(v) = sp.edit().putLong("kiosk_suspended_until", v).commit().let { }
    var lastError: String
        get() = sp.getString("last_error", "") ?: ""
        set(v) = sp.edit().putString("last_error", v.take(500)).apply()

    fun policy(): JSONObject = try { JSONObject(policyJson) } catch (e: Exception) { JSONObject() }
    fun kioskSuspended(): Boolean = System.currentTimeMillis() < kioskSuspendedUntil
    val isEnrolled get() = deviceToken.isNotEmpty() && serverUrl.isNotEmpty()
}

object Dpm {
    fun dpm(ctx: Context) = ctx.getSystemService(DevicePolicyManager::class.java)!!
    fun admin(ctx: Context) = ComponentName(ctx, AdminReceiver::class.java)
    fun isOwner(ctx: Context) = dpm(ctx).isDeviceOwnerApp(ctx.packageName)
}

class HttpError(val code: Int, msg: String) : IOException("HTTP $code: $msg")

/** Cliente HTTP mínimo (sin librerías externas). */
class Api(private val base: String, private val token: String = "") {

    fun post(path: String, body: JSONObject): JSONObject {
        val conn = open(base + path)
        conn.requestMethod = "POST"
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
        conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
        return read(conn)
    }

    /** Descarga a [dest] verificando SHA-256; borra el archivo si no coincide. */
    fun download(url: String, dest: File, expectedSha256: String) {
        val conn = open(url)
        conn.readTimeout = 120_000
        val code = conn.responseCode
        if (code !in 200..299) throw HttpError(code, "descarga fallida")
        val md = MessageDigest.getInstance("SHA-256")
        dest.parentFile?.mkdirs()
        conn.inputStream.use { input ->
            dest.outputStream().use { out ->
                val buf = ByteArray(64 * 1024)
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    md.update(buf, 0, n)
                    out.write(buf, 0, n)
                }
            }
        }
        val got = md.digest().joinToString("") { "%02x".format(it) }
        if (!got.equals(expectedSha256, ignoreCase = true)) {
            dest.delete()
            throw IOException("SHA-256 no coincide (esperado $expectedSha256, obtenido $got)")
        }
    }

    private fun open(url: String): HttpURLConnection {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 15_000
        conn.readTimeout = 30_000
        conn.setRequestProperty("Accept", "application/json")
        conn.setRequestProperty("User-Agent", "AdipackMDM/$AGENT_VERSION")
        if (token.isNotEmpty()) conn.setRequestProperty("Authorization", "Bearer $token")
        return conn
    }

    private fun read(conn: HttpURLConnection): JSONObject {
        val code = conn.responseCode
        val stream = if (code in 200..299) conn.inputStream else conn.errorStream
        val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
        conn.disconnect()
        if (code !in 200..299) {
            val detail = try { JSONObject(text).optString("detail", text) } catch (e: Exception) { text }
            throw HttpError(code, detail.take(300))
        }
        return if (text.isBlank()) JSONObject() else JSONObject(text)
    }
}

fun sha256Hex(s: String): String =
    MessageDigest.getInstance("SHA-256").digest(s.toByteArray()).joinToString("") { "%02x".format(it) }

fun logE(msg: String, e: Throwable? = null) = Log.e(TAG, msg, e)
fun logI(msg: String) = Log.i(TAG, msg)
