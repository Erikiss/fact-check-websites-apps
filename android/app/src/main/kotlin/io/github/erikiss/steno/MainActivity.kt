package io.github.erikiss.steno

import android.Manifest
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.media.projection.MediaProjectionConfig
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.view.View
import android.view.WindowInsets
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import io.github.erikiss.steno.core.ImageMath
import io.github.erikiss.steno.core.SpendLedger
import java.io.File
import java.util.Locale

/** Settings, permissions and start/stop. The live work happens in [CaptureService] while WebEx is in front. */
class MainActivity : Activity() {
    private companion object {
        const val REQ_CAPTURE = 1
        const val REQ_NOTIFICATIONS = 2
    }

    private lateinit var settings: StenoSettings
    private lateinit var apiKey: EditText
    private lateinit var hint: EditText
    private lateinit var knowledge: EditText
    private lateinit var fastModel: EditText
    private lateinit var deepModel: EditText
    private lateinit var deepAuto: Switch
    private lateinit var fake: Switch
    private lateinit var dailyBudget: EditText
    private lateinit var totalBudget: EditText
    private lateinit var imageTokens: EditText
    private lateinit var state: TextView
    private lateinit var log: TextView
    private var askedNotifications = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AppLog.init(this)
        settings = StenoSettings(this)
        setContentView(buildUi())
        load()
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun buildUi(): View {
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(16), dp(24), dp(24))
        }
        fun title(text: String, size: Float = 20f) = TextView(this).apply {
            this.text = text
            textSize = size
            typeface = Typeface.DEFAULT_BOLD
            setPadding(0, dp(18), 0, dp(4))
        }.also { col.addView(it) }
        fun note(text: String) = TextView(this).apply { this.text = text; setTextColor(Color.DKGRAY) }.also { col.addView(it) }
        fun field(label: String, type: Int = InputType.TYPE_CLASS_TEXT, lines: Int = 1): EditText {
            col.addView(TextView(this).apply { text = label; setPadding(0, dp(10), 0, 0) })
            return EditText(this).apply {
                inputType = type
                if (lines > 1) { minLines = lines; maxLines = lines * 2 } else isSingleLine = true
            }.also { col.addView(it) }
        }
        fun switch(label: String) = Switch(this).apply { text = label; setPadding(0, dp(10), 0, dp(4)) }.also { col.addView(it) }
        fun button(label: String, onClick: () -> Unit) = Button(this).apply { text = label; setOnClickListener { onClick() } }.also { col.addView(it) }

        title("Steno-Live", 26f)
        note("Liest die Kurzschrift im gewählten Bildschirmbereich (z. B. WebEx-Tafel) live und markiert Fehler direkt " +
            "darüber. Grün = gelesen, Rot = vermuteter Fehler, oben die Lösung in einem Satz.")
        state = TextView(this).apply { setPadding(0, dp(12), 0, 0); typeface = Typeface.DEFAULT_BOLD }
        col.addView(state)
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        row.addView(Button(this).apply { text = "▶ Starten"; setOnClickListener { onStartClicked() } })
        row.addView(Button(this).apply { text = "■ Stoppen"; setOnClickListener { CaptureService.stop(this@MainActivity); state.postDelayed({ refresh() }, 500) } })
        col.addView(row)
        button("„Über anderen Apps einblenden“ erlauben") {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
        }

        title("Zugang")
        apiKey = field("Claude-API-Schlüssel (leer lassen = gespeicherten behalten)",
            InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD)
        note("Wird verschlüsselt auf dem Tablet gespeichert und nur an api.anthropic.com gesendet.")

        title("Analyse")
        hint = field("Hinweis ans Modell (z. B. „Finde den Fehler“, „Eilschrift“)")
        knowledge = field("Eigenes Kursmaterial (z. B. eure Kürzelliste) – wird dem DEK-Wissen angehängt",
            InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE, lines = 3)
        fastModel = field("Schnelles Modell")
        deepModel = field("Genaues Modell (leer = keine genaue Prüfung)")
        deepAuto = switch("Genaue Prüfung automatisch, sobald das Bild still steht")
        imageTokens = field("Bilddetail: max. Bild-Tokens (1568 = Standard, bis 4784 = feiner, langsamer)", InputType.TYPE_CLASS_NUMBER)
        fake = switch("Fake-Modus: ohne API, 0 $ (nur zum Ausprobieren der Oberfläche)")

        title("Budget (geschätzte API-Kosten)")
        dailyBudget = field("Tageslimit in $", InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL)
        totalBudget = field("Gesamtlimit in $", InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL)
        button("Einstellungen speichern") { save(); toast("Gespeichert – gilt beim nächsten Start") }

        title("Protokoll")
        log = TextView(this).apply { typeface = Typeface.MONOSPACE; textSize = 11f; setTextIsSelectable(true) }
        col.addView(log)
        val logRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        logRow.addView(Button(this).apply { text = "Aktualisieren"; setOnClickListener { refresh() } })
        logRow.addView(Button(this).apply {
            text = "Kopieren"
            setOnClickListener {
                getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("Steno-Live-Protokoll", AppLog.text()))
                toast("Protokoll kopiert")
            }
        })
        col.addView(logRow)

        val scroll = ScrollView(this).apply { addView(col) }
        scroll.setOnApplyWindowInsetsListener { v, insets ->      // edge-to-edge on Android 15
            if (Build.VERSION.SDK_INT >= 30) {
                val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.ime())
                v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            } else {
                @Suppress("DEPRECATION")
                v.setPadding(insets.systemWindowInsetLeft, insets.systemWindowInsetTop, insets.systemWindowInsetRight, insets.systemWindowInsetBottom)
            }
            insets
        }
        return scroll
    }

    private fun load() {
        apiKey.hint = if (settings.hasApiKey) "gespeichert ✓" else "sk-ant-…"
        hint.setText(settings.hint)
        knowledge.setText(settings.extraKnowledge)
        fastModel.setText(settings.fastModel)
        deepModel.setText(settings.deepModel)
        deepAuto.isChecked = settings.deepAuto
        fake.isChecked = settings.fake
        imageTokens.setText(settings.maxImageTokens.toString())
        dailyBudget.setText(settings.dailyBudget.toString())
        totalBudget.setText(settings.totalBudget.toString())
    }

    private fun number(e: EditText, default: Double) = e.text.toString().replace(',', '.').trim().toDoubleOrNull() ?: default

    private fun save() {
        if (apiKey.text.isNotBlank()) {
            settings.apiKey = apiKey.text.toString()
            apiKey.setText("")
            apiKey.hint = "gespeichert ✓"
        }
        settings.hint = hint.text.toString().trim()
        settings.extraKnowledge = knowledge.text.toString().trim()
        settings.fastModel = fastModel.text.toString().trim().ifEmpty { "claude-haiku-5-5" }
        settings.deepModel = deepModel.text.toString().trim()
        settings.deepAuto = deepAuto.isChecked
        settings.fake = fake.isChecked
        val tokens = number(imageTokens, 1568.0).toInt().coerceIn(256, ImageMath.HIGH_RES_MAX_TOKENS)
        settings.maxImageTokens = tokens
        settings.maxImageEdge = if (tokens > ImageMath.STANDARD_MAX_TOKENS) ImageMath.HIGH_RES_MAX_EDGE else ImageMath.STANDARD_MAX_EDGE
        settings.dailyBudget = number(dailyBudget, 20.0).coerceAtLeast(0.0)
        settings.totalBudget = number(totalBudget, 100.0).coerceAtLeast(0.0)
    }

    private fun refresh() {
        val overlayOk = Settings.canDrawOverlays(this)
        val ledgerFile = File(filesDir, "spend_ledger.jsonl")
        val spend = if (ledgerFile.exists()) {
            val l = SpendLedger(ledgerFile, settings.totalBudget, settings.dailyBudget)
            String.format(Locale.GERMANY, "Kosten heute %.3f $ von %.2f $, gesamt %.3f $ von %.2f $",
                l.spentToday(), settings.dailyBudget, l.spentTotal(), settings.totalBudget)
        } else {
            "Noch keine Kosten"
        }
        state.text = buildString {
            append(if (CaptureService.isRunning) "● Läuft – Steuerung schwebt über WebEx" else "○ Gestoppt").append('\n')
            append(if (overlayOk) "Über anderen Apps einblenden: erlaubt ✓" else "Über anderen Apps einblenden: fehlt – bitte erlauben").append('\n')
            append(if (settings.hasApiKey || settings.fake) "API-Schlüssel: ✓" else "API-Schlüssel: fehlt").append('\n')
            append(spend)
        }
        log.text = AppLog.text().ifEmpty { "(noch leer)" }
    }

    private fun onStartClicked() {
        save()
        if (!Settings.canDrawOverlays(this)) {
            toast("Bitte zuerst „Über anderen Apps einblenden“ für Steno-Live erlauben")
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
            return
        }
        if (!settings.fake && !settings.hasApiKey) {
            toast("API-Schlüssel fehlt (oder Fake-Modus einschalten)")
            return
        }
        if (Build.VERSION.SDK_INT >= 33 && !askedNotifications &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            askedNotifications = true
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIFICATIONS)
            return
        }
        if (CaptureService.isRunning) {
            toast("Läuft bereits")
            return
        }
        val mpm = getSystemService(MediaProjectionManager::class.java)
        // Android 14+: whole screen only, so the region coordinates match what is captured.
        val intent = if (Build.VERSION.SDK_INT >= 34) {
            mpm.createScreenCaptureIntent(MediaProjectionConfig.createConfigForDefaultDisplay())
        } else {
            mpm.createScreenCaptureIntent()
        }
        @Suppress("DEPRECATION")
        startActivityForResult(intent, REQ_CAPTURE)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_NOTIFICATIONS) onStartClicked()
    }

    @Deprecated("Platform Activity API")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_CAPTURE) return
        if (resultCode == RESULT_OK && data != null) {
            CaptureService.start(this, resultCode, data)
            toast("Gestartet – wechsle jetzt zu WebEx")
            moveTaskToBack(true)
        } else {
            toast("Ohne Bildschirmaufnahme geht es nicht")
        }
    }

    private fun toast(text: String) = Toast.makeText(this, text, Toast.LENGTH_LONG).show()
}
