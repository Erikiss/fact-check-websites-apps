package io.github.erikiss.steno

import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.content.res.Configuration
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.graphics.Rect
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import android.util.DisplayMetrics
import android.view.Display
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.Toast
import io.github.erikiss.steno.core.AnthropicVision
import io.github.erikiss.steno.core.ChangeGate
import io.github.erikiss.steno.core.EngineEvent
import io.github.erikiss.steno.core.FakeVision
import io.github.erikiss.steno.core.Frame
import io.github.erikiss.steno.core.LiveEngine
import io.github.erikiss.steno.core.Mark
import io.github.erikiss.steno.core.MarkKind
import io.github.erikiss.steno.core.Prompts
import io.github.erikiss.steno.core.Signature
import io.github.erikiss.steno.core.SpendLedger
import io.github.erikiss.steno.core.Stage
import io.github.erikiss.steno.core.StageConfig
import io.github.erikiss.steno.core.StenoResult
import io.github.erikiss.steno.core.Trigger
import io.github.erikiss.steno.core.VisionBackend
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import java.io.File
import java.util.Locale

/**
 * The live loop on the tablet: a foreground service holding the screen capture (MediaProjection -> VirtualDisplay
 * -> ImageReader), the change gate, the analysis engine and the overlay windows.
 *
 * Capture thread: every tick, a thumbnail of the region (no bitmap) for the change gate. Main thread: gate,
 * engine events, windows. Android cannot keep our overlay out of a full-screen capture, so for every frame that
 * goes to the model the overlay is hidden for a moment ("clean grab"), and the gate is re-based after each repaint
 * so our own drawing never counts as new content (the desktop tool's non-Windows mode).
 */
class CaptureService : Service(), ControlsView.Actions {
    companion object {
        private const val ACTION_START = "io.github.erikiss.steno.START"
        private const val ACTION_STOP = "io.github.erikiss.steno.STOP"
        private const val EXTRA_CODE = "code"
        private const val EXTRA_DATA = "data"
        private const val CHANNEL = "steno"
        private const val NOTIFICATION_ID = 1
        private const val TICK_MS = 250L                 // 4 captures per second, only for change detection
        private const val CLEAN_GRAB_DELAY_MS = 160L     // time for the compositor to drop our hidden overlay
        private const val MAX_FAST_FAILURES = 3

        @Volatile
        var isRunning = false
            private set

        fun start(context: Context, resultCode: Int, data: Intent) {
            context.startForegroundService(
                Intent(context, CaptureService::class.java).setAction(ACTION_START)
                    .putExtra(EXTRA_CODE, resultCode).putExtra(EXTRA_DATA, data),
            )
        }

        fun stop(context: Context) {
            context.startService(Intent(context, CaptureService::class.java).setAction(ACTION_STOP))
        }
    }

    private lateinit var settings: StenoSettings
    private lateinit var ui: Context
    private lateinit var wm: WindowManager
    private val main = Handler(Looper.getMainLooper())
    private val captureThread = HandlerThread("steno-capture")
    private lateinit var capture: Handler
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)   // FIFO: marks before their Done

    private var projection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    @Volatile private var reader: ImageReader? = null
    private var held: Image? = null                     // capture thread only: newest frame, kept for clean grabs
    @Volatile private var region: Rect? = null
    @Volatile private var capturing = false
    private var screenW = 0
    private var screenH = 0
    private var densityDpi = 0

    private var overlay: MarksOverlayView? = null
    private var controls: ControlsView? = null
    private var controlsParams: WindowManager.LayoutParams? = null
    private var picker: RegionPickerView? = null

    private var engine: LiveEngine<Bitmap>? = null
    private var ledger: SpendLedger? = null
    private val gate = ChangeGate()
    private var live = true
    private var grabbingClean = false
    private var lastSig: Signature? = null
    private var nextFrameId = 1L
    private val imageSizes = HashMap<Long, Pair<Int, Int>>()
    private var shownFrame = 0L
    private var shownStage: Stage? = null
    private var deepProgress = 0
    private var fastFailures = 0
    private var lastInfo = ""
    private val latest = HashMap<Stage, StenoResult>()

    private val projectionCallback = object : MediaProjection.Callback() {
        override fun onStop() {
            main.post {
                AppLog.i("Bildschirmaufnahme wurde vom System beendet")
                stopSelf()
            }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        AppLog.init(this)
        settings = StenoSettings(this)
        ui = if (Build.VERSION.SDK_INT >= 30) {
            val display = getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY)
            createDisplayContext(display).createWindowContext(WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY, null)
        } else {
            this
        }
        wm = ui.getSystemService(WindowManager::class.java)
        captureThread.start()
        capture = Handler(captureThread.looper)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> stopSelf()
            ACTION_START -> if (projection == null) {
                startInForeground()                          // must precede getMediaProjection (Android 14)
                val code = intent.getIntExtra(EXTRA_CODE, Activity.RESULT_CANCELED)
                val data: Intent? = if (Build.VERSION.SDK_INT >= 33) {
                    intent.getParcelableExtra(EXTRA_DATA, Intent::class.java)
                } else {
                    @Suppress("DEPRECATION")
                    intent.getParcelableExtra(EXTRA_DATA)
                }
                try {
                    begin(code, data ?: error("keine Aufnahme-Erlaubnis"))
                } catch (e: Exception) {
                    AppLog.e("Start fehlgeschlagen", e)
                    Toast.makeText(this, "Steno-Live: Start fehlgeschlagen (${e.message})", Toast.LENGTH_LONG).show()
                    stopSelf()
                }
            }
        }
        return START_NOT_STICKY
    }

    private fun startInForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Steno-Live", NotificationManager.IMPORTANCE_LOW))
        val stop = PendingIntent.getService(this, 1, Intent(this, CaptureService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE)
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val notification = Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Steno-Live läuft")
            .setContentText("Der gewählte Bildschirmbereich wird beobachtet")
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "Stopp", stop).build())
            .build()
        startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
    }

    private fun begin(code: Int, data: Intent) {
        val mpm = getSystemService(MediaProjectionManager::class.java)
        val p = mpm.getMediaProjection(code, data) ?: error("MediaProjection nicht verfügbar")
        p.registerCallback(projectionCallback, main)        // must precede createVirtualDisplay (Android 14)
        projection = p
        measureScreen()
        val r = newReader()
        reader = r
        virtualDisplay = p.createVirtualDisplay("steno-live", screenW, screenH, densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, r.surface, null, capture)
        setupEngine()
        showOverlay()
        showControls()
        region = settings.loadRegion(screenW, screenH)
        overlay?.region = region
        if (region == null) showPicker()
        isRunning = true
        updateCapturing()
        capture.post(tick)
        setStatus(null)
        AppLog.i("Gestartet: Bildschirm ${screenW}x$screenH, Modelle ${settings.fastModel} / ${settings.deepModel.ifBlank { "–" }}" +
            if (settings.fake) " (Fake-Modus)" else "")
    }

    private fun measureScreen() {
        if (Build.VERSION.SDK_INT >= 30) {
            val b = wm.maximumWindowMetrics.bounds
            screenW = b.width()
            screenH = b.height()
        } else {
            val dm = DisplayMetrics()
            @Suppress("DEPRECATION")
            wm.defaultDisplay.getRealMetrics(dm)
            screenW = dm.widthPixels
            screenH = dm.heightPixels
        }
        densityDpi = resources.displayMetrics.densityDpi
    }

    private fun newReader(): ImageReader = ImageReader.newInstance(screenW, screenH, PixelFormat.RGBA_8888, 3)

    private fun setupEngine() {
        val s = settings
        val stages = buildMap {
            put(Stage.FAST, StageConfig(Stage.FAST, s.fastModel.trim(), s.fastEffort, "off", 2048, true, maxRetries = 0, timeoutS = 30))
            if (s.deepModel.isNotBlank()) {
                put(Stage.DEEP, StageConfig(Stage.DEEP, s.deepModel.trim(), s.deepEffort, "adaptive", 8000, true, maxRetries = 1, timeoutS = 120))
            }
        }
        gate.cfg.deepAfterS = if (s.deepAuto && Stage.DEEP in stages) 2.5 else 0.0
        val backend: VisionBackend = if (s.fake) {
            ledger = null
            FakeVision(150) { Images.fakeBoxes(it) }
        } else {
            val l = SpendLedger(File(filesDir, "spend_ledger.jsonl"), s.totalBudget, s.dailyBudget)
            ledger = l
            val template = assets.open("system_prompt.md").reader().use { it.readText() }
            var knowledge = assets.open("dek.md").reader().use { it.readText() }
            if (s.extraKnowledge.isNotBlank()) knowledge += "\n\n## Material aus meinem Kurs\n" + s.extraKnowledge.trim()
            val schema = assets.open("analysis_schema.json").reader().use { it.readText() }
            AnthropicVision(s.apiKey, Prompts.buildSystemPrompt(template, knowledge), schema, l, log = { AppLog.w(it) })
        }
        engine = LiveEngine(scope, backend, stages, prepare = { Images.prepare(it, s.maxImageEdge, s.maxImageTokens) },
            hint = { settings.hint }, onEvent = ::onEvent)
    }

    // ------------------------------------------------------------------ windows
    private fun windowParams(touchable: Boolean, fullScreen: Boolean): WindowManager.LayoutParams {
        var flags = WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
            WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS
        if (!touchable) flags = flags or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
        val size = if (fullScreen) WindowManager.LayoutParams.MATCH_PARENT else WindowManager.LayoutParams.WRAP_CONTENT
        return WindowManager.LayoutParams(size, size, WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY, flags, PixelFormat.TRANSLUCENT).apply {
            gravity = Gravity.TOP or Gravity.START
            layoutInDisplayCutoutMode = if (Build.VERSION.SDK_INT >= 30) {
                WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS
            } else {
                WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
            }
            if (Build.VERSION.SDK_INT >= 30) fitInsetsTypes = 0
        }
    }

    private fun showOverlay() {
        val view = MarksOverlayView(ui)
        // Android 12+ only lets touches pass through another app's overlay if it is at most 80 % opaque.
        wm.addView(view, windowParams(touchable = false, fullScreen = true).apply { alpha = 0.8f; title = "Steno-Overlay" })
        overlay = view
    }

    private fun showControls() {
        val view = ControlsView(ui, this, deepAvailable = engine?.hasStage(Stage.DEEP) == true)
        val params = windowParams(touchable = true, fullScreen = false).apply {
            x = (screenW * 0.25).toInt()
            y = (24 * resources.displayMetrics.density).toInt()
            title = "Steno-Steuerung"
        }
        wm.addView(view, params)
        controls = view
        controlsParams = params
    }

    private fun showPicker() {
        if (picker != null) return
        capturing = false
        overlay?.visibility = View.INVISIBLE
        val view = RegionPickerView(ui, onPicked = { r ->
            removePicker()
            region = r
            settings.saveRegion(r, screenW, screenH)
            overlay?.region = r
            overlay?.clear()
            gate.reset()
            AppLog.i("Bereich gewählt: $r")
            updateCapturing()
        }, onCancel = {
            removePicker()
            updateCapturing()
        })
        wm.addView(view, windowParams(touchable = true, fullScreen = true).apply { title = "Steno-Bereich" })
        picker = view
    }

    private fun removePicker() {
        picker?.let { runCatching { wm.removeView(it) } }
        picker = null
        overlay?.visibility = View.VISIBLE
    }

    private fun updateCapturing() {
        capturing = live && !grabbingClean && region != null && picker == null
    }

    // ------------------------------------------------------------------ capture thread
    /** Newest frame (keeps it open so a clean grab still has pixels when the screen did not change). */
    private fun newestImage(): Pair<Image?, Boolean> {
        val img = runCatching { reader?.acquireLatestImage() }.getOrNull()
        if (img != null) {
            held?.close()
            held = img
        }
        return held to (img != null)
    }

    private val tick = object : Runnable {
        override fun run() {
            try {
                if (capturing) {
                    val r = region
                    val (img, fresh) = newestImage()
                    if (r != null && img != null) {
                        val sig = if (fresh && Images.fits(img, r)) Images.signature(img, r) else null
                        main.post { onTick(sig) }
                    }
                }
            } catch (e: Exception) {
                AppLog.e("Aufnahmefehler", e)
            }
            capture.postDelayed(this, TICK_MS)
        }
    }

    private fun grabRegion(): Bitmap? {
        val r = region ?: return null
        val (img, _) = newestImage()
        return if (img != null && Images.fits(img, r)) Images.crop(img, r) else null
    }

    // ------------------------------------------------------------------ main thread: gate and engine
    private fun now() = SystemClock.elapsedRealtime() / 1000.0

    private fun onTick(sig: Signature?) {
        if (!live || grabbingClean || region == null || picker != null) return
        val s = sig ?: lastSig ?: return                     // no new frame = the screen did not change
        lastSig = s
        when (gate.update(s, now())) {
            Trigger.FAST -> analyse(Stage.FAST, cancelDeep = true)
            Trigger.DEEP -> analyse(Stage.DEEP, cancelDeep = false)
            Trigger.NONE -> Unit
        }
    }

    private fun analyse(stage: Stage, cancelDeep: Boolean) {
        val e = engine ?: return
        if (!e.hasStage(stage) || region == null || grabbingClean) return
        val frameId = nextFrameId++
        if (cancelDeep) overlay?.stale = true                // the picture changed: old marks are outdated
        grabbingClean = true
        updateCapturing()
        val hidden = listOfNotNull(overlay, controls?.takeIf { coversRegion(it) }).filter { it.visibility == View.VISIBLE }
        hidden.forEach { it.visibility = View.INVISIBLE }
        capture.postDelayed({
            val bitmap = runCatching { grabRegion() }.onFailure { AppLog.e("Bild konnte nicht gelesen werden", it) }.getOrNull()
            main.post {
                hidden.forEach { it.visibility = View.VISIBLE }
                grabbingClean = false
                updateCapturing()
                gate.rebaseNext()
                if (bitmap != null) e.submit(Frame(frameId, bitmap), stage, cancelDeep) else AppLog.w("Kein Bild für die Analyse")
            }
        }, CLEAN_GRAB_DELAY_MS)
    }

    private fun coversRegion(v: View): Boolean {
        val r = region ?: return false
        val loc = IntArray(2)
        v.getLocationOnScreen(loc)
        return Rect.intersects(r, Rect(loc[0], loc[1], loc[0] + v.width, loc[1] + v.height))
    }

    private fun onEvent(ev: EngineEvent) {
        when (ev) {
            is EngineEvent.Started -> {
                imageSizes[ev.frameId] = ev.imageWidth to ev.imageHeight
                imageSizes.keys.removeAll { it < ev.frameId - 40 }
                setStatus(if (ev.stage == Stage.FAST) "⚡ schnell …" else "🔍 genau …")
            }
            is EngineEvent.MarkAdded -> if (ev.stage == Stage.FAST) {
                if (ev.frameId > shownFrame) {                // first mark of a newer picture replaces the overlay
                    show(ev.frameId, Stage.FAST, listOf(ev.mark), imageSizes[ev.frameId] ?: (1 to 1), "", "⚡ schnell …")
                } else if (ev.frameId == shownFrame && shownStage == Stage.FAST) {
                    overlay?.addMark(ev.mark)
                    gate.rebaseNext()
                }
            } else {
                deepProgress++
                setStatus("🔍 genau: $deepProgress Zeichen gelesen …")
            }
            is EngineEvent.Done -> onDone(ev.result)
            is EngineEvent.Failed -> {
                AppLog.w("${ev.stage.id}: ${ev.message}")
                if (ev.budget) {
                    if (ev.stage == Stage.DEEP) gate.cfg.deepAfterS = 0.0 else setLive(false)
                }
                setStatus("⚠ ${ev.message}")
            }
            is EngineEvent.Cancelled -> if (ev.stage == Stage.DEEP) deepProgress = 0
        }
    }

    private fun onDone(r: StenoResult) {
        if (r.stage == Stage.DEEP) deepProgress = 0
        latest[r.stage] = r
        controls?.setDetails(detailsText())
        val failed = r.error != null && r.marks.isEmpty()
        if (r.error != null) AppLog.w("${r.stage.id}: ${r.error}")
        if (r.stage == Stage.FAST) {
            fastFailures = if (failed) fastFailures + 1 else 0
            if (fastFailures >= MAX_FAST_FAILURES && live) {
                setLive(false)
                setStatus("⚠ ${r.error} – pausiert (▶ Live zum Fortsetzen)")
                return
            }
        }
        if (!failed && r.frameId >= shownFrame) {
            show(r.frameId, r.stage, r.marks.toList(), r.imageWidth to r.imageHeight, r.answer, headline(r))
        }
        lastInfo = "${label(r.stage)} ${String.format(Locale.GERMANY, "%.1f", r.durationS)} s"
        setStatus(r.error?.let { "⚠ $it" })
    }

    private fun show(frameId: Long, stage: Stage, marks: List<Mark>, size: Pair<Int, Int>, answer: String, headline: String) {
        shownFrame = frameId
        shownStage = stage
        overlay?.showMarks(marks, size.first, size.second, answer, headline)
        gate.rebaseNext()                                    // our own drawing is not new content
    }

    private fun label(stage: Stage) = if (stage == Stage.FAST) "⚡ schnell" else "🔍 genau"

    private fun headline(r: StenoResult) =
        "${label(r.stage)} · ${r.servedBy.ifEmpty { r.model }} · ${String.format(Locale.GERMANY, "%.1f", r.durationS)} s"

    private fun detailsText(): String = latest.values.sortedByDescending { it.frameId }.joinToString("\n\n") { r ->
        buildString {
            append(headline(r)).append('\n')
            r.error?.let { append("⚠ ").append(it).append('\n') }
            if (r.answer.isNotEmpty()) append(r.answer).append('\n')
            val reading = r.reading()
            if (reading.isNotEmpty()) append("Übertragung: ").append(reading).append('\n')
            for (m in r.marks) {
                when (m.kind) {
                    MarkKind.ERROR -> append("✗ ${m.text}${if (m.intended.isNotEmpty()) " → ${m.intended}" else ""}: ${m.issue}" +
                        (if (m.fix.isNotEmpty()) " (richtig: ${m.fix})" else "") + "\n")
                    MarkKind.NOTE -> append("• ${m.text.ifEmpty { m.issue }}\n")
                    MarkKind.WORD -> if (m.alternatives.isNotEmpty()) append("${m.text}: oder ${m.alternatives.joinToString(" / ")}\n")
                }
            }
        }.trim()
    }

    private fun setStatus(message: String?) {
        val state = message ?: if (live) "live" else "pausiert"
        val spent = ledger?.let { String.format(Locale.GERMANY, "heute %.3f $", it.spentToday()) } ?: "Fake-Modus (0 $)"
        controls?.setStatus(listOf(state, lastInfo, spent).filter { it.isNotEmpty() }.joinToString(" · "))
        overlay?.status = if (live) "" else "⏸ pausiert"
    }

    private fun setLive(value: Boolean) {
        live = value
        controls?.setLive(value)
        if (value) {
            fastFailures = 0
            gate.reset()
        }
        updateCapturing()
        setStatus(null)
    }

    // ------------------------------------------------------------------ control bar actions
    override fun togglePause() = setLive(!live)

    override fun analyseNow() {
        if (region == null) return showPicker()
        lastSig?.let { gate.markFast(it, now()) }
        analyse(Stage.FAST, cancelDeep = false)
    }

    override fun deepNow() {
        if (region == null) return showPicker()
        lastSig?.let { gate.markDeep(it) }
        analyse(Stage.DEEP, cancelDeep = false)
    }

    override fun pickRegion() = showPicker()

    override fun toggleOverlay(): Boolean {
        val v = overlay ?: return false
        v.visibility = if (v.visibility == View.VISIBLE) View.GONE else View.VISIBLE
        return v.visibility == View.VISIBLE
    }

    override fun openApp() {
        startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    override fun stopAll() = stopSelf()

    override fun moveControlsBy(dx: Int, dy: Int) {
        val v = controls ?: return
        val p = controlsParams ?: return
        p.x = (p.x + dx).coerceIn(0, maxOf(0, screenW - v.width))
        p.y = (p.y + dy).coerceIn(0, maxOf(0, screenH - v.height))
        runCatching { wm.updateViewLayout(v, p) }
    }

    // ------------------------------------------------------------------ rotation and shutdown
    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        if (projection == null) return
        val oldW = screenW
        val oldH = screenH
        measureScreen()
        if (screenW == oldW && screenH == oldH) return
        val w = screenW
        val h = screenH
        capture.post {
            held?.close()
            held = null
            val old = reader
            val fresh = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 3)
            virtualDisplay?.resize(w, h, densityDpi)
            virtualDisplay?.surface = fresh.surface
            reader = fresh
            old?.close()
        }
        region = settings.loadRegion(w, h)
        lastSig = null
        gate.reset()
        overlay?.region = region
        overlay?.clear()
        AppLog.i("Bildschirm gedreht: ${w}x$h")
        if (region == null) showPicker()
    }

    override fun onDestroy() {
        isRunning = false
        capturing = false
        capture.removeCallbacksAndMessages(null)
        scope.cancel()
        listOfNotNull(overlay, controls, picker).forEach { v -> runCatching { wm.removeView(v) } }
        overlay = null
        controls = null
        picker = null
        virtualDisplay?.release()
        projection?.unregisterCallback(projectionCallback)
        projection?.stop()
        projection = null
        capture.post {
            held?.close()
            held = null
            reader?.close()
        }
        captureThread.quitSafely()
        AppLog.i("Gestoppt")
        super.onDestroy()
    }
}
