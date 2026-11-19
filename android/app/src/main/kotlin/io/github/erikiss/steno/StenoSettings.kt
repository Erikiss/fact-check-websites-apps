package io.github.erikiss.steno

import android.content.Context
import android.graphics.Rect
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import io.github.erikiss.steno.core.ImageMath
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import kotlin.properties.ReadWriteProperty
import kotlin.reflect.KProperty

/** App settings (SharedPreferences); the API key is stored encrypted with a key that never leaves the Keystore. */
class StenoSettings(context: Context) {
    companion object {
        const val DEFAULT_FAST_MODEL = "claude-sonnet-5-5"
        const val DEFAULT_DEEP_MODEL = "claude-opus-5-5"
        const val DEFAULT_FAST_EFFORT = "low"
        const val DEFAULT_FAST_THINKING = "adaptive"
        const val DEFAULT_DEEP_EFFORT = "high"
        const val DEFAULT_MAX_IMAGE_EDGE = ImageMath.HIGH_RES_MAX_EDGE
        const val DEFAULT_MAX_IMAGE_TOKENS = ImageMath.HIGH_RES_MAX_TOKENS
        const val DEFAULT_MIN_IMAGE_EDGE = 896
        private const val CURRENT_PREFS_VERSION = 2
        val VALID_EFFORTS = setOf("low", "medium", "high", "xhigh", "max")
    }

    private val prefs = context.getSharedPreferences("steno", Context.MODE_PRIVATE)

    init {
        migrate()
    }

    private fun migrate() {
        if (prefs.getInt("prefs_version", 1) >= CURRENT_PREFS_VERSION) return
        val edit = prefs.edit()
        // Upgrade untouched v1 defaults so existing installs get the accuracy improvements automatically.
        if (prefs.getString("fast_model", null) == "claude-haiku-5-5") {
            edit.putString("fast_model", DEFAULT_FAST_MODEL)
        }
        if (prefs.getString("deep_effort", null) == "low") {
            edit.putString("deep_effort", DEFAULT_DEEP_EFFORT)
        }
        if (prefs.contains("max_image_tokens") && prefs.getInt("max_image_tokens", 0) == ImageMath.STANDARD_MAX_TOKENS) {
            edit.putInt("max_image_tokens", DEFAULT_MAX_IMAGE_TOKENS)
            edit.putInt("max_image_edge", DEFAULT_MAX_IMAGE_EDGE)
        }
        edit.putInt("prefs_version", CURRENT_PREFS_VERSION).apply()
    }

    var apiKey: String
        get() = prefs.getString("api_key", null)?.let { KeyCrypto.decrypt(it) } ?: ""
        set(value) = prefs.edit().putString("api_key", if (value.isBlank()) null else KeyCrypto.encrypt(value.trim())).apply()

    val hasApiKey: Boolean get() = apiKey.isNotBlank()

    var hint by string("hint", "")
    var extraKnowledge by string("extra_knowledge", "")
    var fastModel by string("fast_model", DEFAULT_FAST_MODEL)
    var deepModel by string("deep_model", DEFAULT_DEEP_MODEL)     // empty = no deep stage
    var fastEffort by string("fast_effort", DEFAULT_FAST_EFFORT)
    var fastThinking by string("fast_thinking", DEFAULT_FAST_THINKING)
    var deepEffort by string("deep_effort", DEFAULT_DEEP_EFFORT)
    var deepAuto by boolean("deep_auto", true)
    var fake by boolean("fake", false)
    var dailyBudget by double("daily_budget", 20.0)
    var totalBudget by double("total_budget", 100.0)
    var maxImageEdge by int("max_image_edge", DEFAULT_MAX_IMAGE_EDGE)
    var maxImageTokens by int("max_image_tokens", DEFAULT_MAX_IMAGE_TOKENS)

    /** The watched region in screen pixels, remembered per screen size (i.e. per orientation). */
    fun loadRegion(screenW: Int, screenH: Int): Rect? {
        val p = prefs.getString("region_${screenW}x$screenH", null)?.split(",")?.mapNotNull { it.toIntOrNull() } ?: return null
        if (p.size != 4) return null
        val r = Rect(p[0], p[1], p[2], p[3])
        return if (r.width() >= 48 && r.height() >= 48 && r.right <= screenW && r.bottom <= screenH) r else null
    }

    fun saveRegion(r: Rect, screenW: Int, screenH: Int) {
        prefs.edit().putString("region_${screenW}x$screenH", "${r.left},${r.top},${r.right},${r.bottom}").apply()
    }

    private fun string(key: String, default: String) = object : ReadWriteProperty<Any?, String> {
        override fun getValue(thisRef: Any?, property: KProperty<*>) = prefs.getString(key, default) ?: default
        override fun setValue(thisRef: Any?, property: KProperty<*>, value: String) = prefs.edit().putString(key, value).apply()
    }

    private fun boolean(key: String, default: Boolean) = object : ReadWriteProperty<Any?, Boolean> {
        override fun getValue(thisRef: Any?, property: KProperty<*>) = prefs.getBoolean(key, default)
        override fun setValue(thisRef: Any?, property: KProperty<*>, value: Boolean) = prefs.edit().putBoolean(key, value).apply()
    }

    private fun double(key: String, default: Double) = object : ReadWriteProperty<Any?, Double> {
        override fun getValue(thisRef: Any?, property: KProperty<*>) = prefs.getString(key, null)?.toDoubleOrNull() ?: default
        override fun setValue(thisRef: Any?, property: KProperty<*>, value: Double) = prefs.edit().putString(key, value.toString()).apply()
    }

    private fun int(key: String, default: Int) = object : ReadWriteProperty<Any?, Int> {
        override fun getValue(thisRef: Any?, property: KProperty<*>) = prefs.getInt(key, default)
        override fun setValue(thisRef: Any?, property: KProperty<*>, value: Int) = prefs.edit().putInt(key, value).apply()
    }
}

/** AES-GCM with a key generated inside the Android Keystore (not exportable). */
object KeyCrypto {
    private const val ALIAS = "steno_api_key"
    private const val TRANSFORMATION = "AES/GCM/NoPadding"

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build(),
        )
        return generator.generateKey()
    }

    fun encrypt(plain: String): String {
        val cipher = Cipher.getInstance(TRANSFORMATION).apply { init(Cipher.ENCRYPT_MODE, key()) }
        return Base64.encodeToString(cipher.iv + cipher.doFinal(plain.toByteArray()), Base64.NO_WRAP)
    }

    fun decrypt(stored: String): String? = runCatching {
        val bytes = Base64.decode(stored, Base64.NO_WRAP)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, bytes, 0, 12))
        String(cipher.doFinal(bytes, 12, bytes.size - 12))
    }.getOrNull()
}
