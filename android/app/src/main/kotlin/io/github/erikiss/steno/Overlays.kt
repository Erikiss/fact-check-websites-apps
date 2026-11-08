package io.github.erikiss.steno

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.PointF
import android.graphics.RectF
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.text.TextUtils
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import io.github.erikiss.steno.core.Layout
import io.github.erikiss.steno.core.Mark
import io.github.erikiss.steno.core.MarkKind
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import android.graphics.Rect as ScreenRect
import io.github.erikiss.steno.core.Rect as LRect

private fun View.dp(v: Float) = v * resources.displayMetrics.density

/**
 * Full-screen, non-touchable overlay that draws the marks over the watched region (same look as the desktop
 * overlay): boxes, labels with reading / error / fix / alternatives, a circle at the exact faulty spot, and a banner
 * with the answer. Coordinates: region in screen pixels, marks in model-image pixels.
 */
class MarksOverlayView(context: Context) : View(context) {
    var region: ScreenRect? = null
        set(value) { field = value; invalidate() }
    var status: String = ""
        set(value) { if (field != value) { field = value; invalidate() } }
    var stale: Boolean = false
        set(value) { if (field != value) { field = value; invalidate() } }

    private val marks = ArrayList<Mark>()
    private var imageW = 1
    private var imageH = 1
    private var answer = ""
    private var headline = ""
    private val loc = IntArray(2)

    private val stroke = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val mainText = Paint(Paint.ANTI_ALIAS_FLAG).apply { textSize = dp(13.5f); typeface = Typeface.DEFAULT_BOLD }
    private val smallText = Paint(Paint.ANTI_ALIAS_FLAG).apply { textSize = dp(12f); color = Color.rgb(236, 236, 236) }
    private val bannerText = Paint(Paint.ANTI_ALIAS_FLAG).apply { textSize = dp(15f); typeface = Typeface.DEFAULT_BOLD; color = Color.WHITE }

    fun showMarks(list: List<Mark>, width: Int, height: Int, answer: String, headline: String) {
        marks.clear()
        marks.addAll(list)
        imageW = max(1, width)
        imageH = max(1, height)
        this.answer = answer
        this.headline = headline
        stale = false
        invalidate()
    }

    fun addMark(mark: Mark) {
        marks.add(mark)
        invalidate()
    }

    fun clear() {
        marks.clear()
        answer = ""
        headline = ""
        stale = false
        invalidate()
    }

    private fun lineHeight(p: Paint) = p.fontMetrics.let { it.descent - it.ascent }

    override fun onDraw(canvas: Canvas) {
        val r = region ?: return
        getLocationOnScreen(loc)
        val left = (r.left - loc[0]).toFloat()
        val top = (r.top - loc[1]).toFloat()
        val sx = r.width().toFloat() / imageW
        val sy = r.height().toFloat() / imageH

        stroke.color = Color.argb(170, 66, 133, 244)
        stroke.strokeWidth = dp(1.5f)
        stroke.pathEffect = DashPathEffect(floatArrayOf(dp(8f), dp(6f)), 0f)
        canvas.drawRect(left - 2, top - 2, left + r.width() + 2, top + r.height() + 2, stroke)
        stroke.pathEffect = null

        val drawn = marks.filter { it.box != null }
        val anchors = drawn.map { m ->
            val b = m.box!!
            LRect(left + b.x1 * sx, top + b.y1 * sy, (b.x2 - b.x1) * sx, (b.y2 - b.y1) * sy)
        }
        val texts = drawn.map { Layout.labelLines(it) }
        val gap = dp(2f)
        val sizes = texts.map { lines ->
            val w = lines.mapIndexed { i, t -> (if (i == 0) mainText else smallText).measureText(t) }.maxOrNull() ?: 0f
            val h = lines.indices.sumOf { i -> (lineHeight(if (i == 0) mainText else smallText) + gap).toDouble() }.toFloat()
            w + dp(12f) to h + dp(6f)
        }
        val positions = Layout.placeLabels(anchors, sizes, LRect(0f, 0f, width.toFloat(), height.toFloat()))
        val alpha = if (stale) 100 else 255
        for (i in drawn.indices) {
            val m = drawn[i]
            val a = anchors[i]
            val colour = Layout.color(m.kind)
            stroke.color = colour
            stroke.alpha = alpha
            stroke.strokeWidth = dp(if (m.kind == MarkKind.ERROR) 3f else 2f)
            stroke.pathEffect = if (m.confidence < 0.5f) DashPathEffect(floatArrayOf(dp(6f), dp(4f)), 0f) else null
            canvas.drawRoundRect(RectF(a.x, a.y, a.right, a.bottom), dp(4f), dp(4f), stroke)
            stroke.pathEffect = null
            m.spot?.let { (x, y) ->
                val cx = left + x * sx
                val cy = top + y * sy
                stroke.strokeWidth = dp(2.5f)
                canvas.drawCircle(cx, cy, dp(10f), stroke)
            }
            val (lx, ly) = positions[i]
            val (lw, lh) = sizes[i]
            fill.color = Color.argb(if (stale) 110 else 215, 18, 18, 18)
            canvas.drawRoundRect(RectF(lx, ly, lx + lw, ly + lh), dp(5f), dp(5f), fill)
            stroke.strokeWidth = dp(1.2f)
            canvas.drawRoundRect(RectF(lx, ly, lx + lw, ly + lh), dp(5f), dp(5f), stroke)
            var y = ly + dp(3f)
            for ((j, line) in texts[i].withIndex()) {
                val p = if (j == 0) mainText else smallText
                if (j == 0) p.color = lighter(colour)
                p.alpha = alpha
                y += -p.fontMetrics.ascent
                canvas.drawText(line, lx + dp(6f), y, p)
                y += p.fontMetrics.descent + gap
            }
        }
        drawBanner(canvas, left, top, r.width().toFloat())
        drawStatus(canvas, left + r.width(), top + r.height())
    }

    private fun lighter(c: Int): Int {
        fun ch(v: Int) = min(255, v + (255 - v) / 3)
        return Color.rgb(ch(Color.red(c)), ch(Color.green(c)), ch(Color.blue(c)))
    }

    private fun drawBanner(canvas: Canvas, left: Float, top: Float, regionWidth: Float) {
        if (answer.isEmpty() && headline.isEmpty()) return
        val h = lineHeight(smallText) + lineHeight(bannerText) + dp(12f)
        val bannerTop = if (top - h - dp(6f) >= 0) top - h - dp(6f) else top + dp(4f)
        val w = max(regionWidth, dp(360f)).coerceAtMost(width - dp(4f))
        val bx = left.coerceAtMost(width - w - dp(2f)).coerceAtLeast(dp(2f))
        fill.color = Color.argb(215, 18, 18, 18)
        canvas.drawRoundRect(RectF(bx, bannerTop, bx + w, bannerTop + h), dp(6f), dp(6f), fill)
        smallText.color = Color.rgb(170, 190, 255)
        canvas.drawText(headline, bx + dp(8f), bannerTop + dp(4f) - smallText.fontMetrics.ascent, smallText)
        smallText.color = Color.rgb(236, 236, 236)
        val text = TextUtils.ellipsize(answer, android.text.TextPaint(bannerText), w - dp(16f), TextUtils.TruncateAt.END).toString()
        canvas.drawText(text, bx + dp(8f), bannerTop + dp(6f) + lineHeight(smallText) - bannerText.fontMetrics.ascent, bannerText)
    }

    private fun drawStatus(canvas: Canvas, right: Float, bottom: Float) {
        if (status.isEmpty()) return
        val w = smallText.measureText(status) + dp(12f)
        val h = lineHeight(smallText) + dp(6f)
        var top = bottom + dp(4f)
        if (top + h > height) top = bottom - h - dp(2f)
        fill.color = Color.argb(190, 20, 20, 20)
        canvas.drawRoundRect(RectF(right - w, top, right, top + h), dp(5f), dp(5f), fill)
        canvas.drawText(status, right - w + dp(6f), top + dp(3f) - smallText.fontMetrics.ascent, smallText)
    }
}

/** Full-screen picker: drag a rectangle around the shorthand (screen pixels); "Abbrechen" top right. */
@SuppressLint("ViewConstructor")
class RegionPickerView(
    context: Context,
    private val onPicked: (ScreenRect) -> Unit,
    private val onCancel: () -> Unit,
) : View(context) {
    private var start: PointF? = null
    private var end: PointF? = null
    private val loc = IntArray(2)
    private val dim = Paint().apply { color = Color.argb(120, 0, 0, 0) }
    private val frame = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; color = Color.rgb(66, 133, 244); strokeWidth = dp(2.5f) }
    private val text = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; textSize = dp(18f); textAlign = Paint.Align.CENTER; typeface = Typeface.DEFAULT_BOLD }
    private val cancelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.rgb(217, 48, 37) }
    private val cancelBox = RectF()

    private fun selection(): RectF? {
        val s = start ?: return null
        val e = end ?: return null
        getLocationOnScreen(loc)
        return RectF(min(s.x, e.x) - loc[0], min(s.y, e.y) - loc[1], max(s.x, e.x) - loc[0], max(s.y, e.y) - loc[1])
    }

    override fun onDraw(canvas: Canvas) {
        val w = width.toFloat()
        val h = height.toFloat()
        val sel = selection()
        if (sel == null) {
            canvas.drawRect(0f, 0f, w, h, dim)
        } else {                                              // dim everything around the selection
            canvas.drawRect(0f, 0f, w, sel.top, dim)
            canvas.drawRect(0f, sel.bottom, w, h, dim)
            canvas.drawRect(0f, sel.top, sel.left, sel.bottom, dim)
            canvas.drawRect(sel.right, sel.top, w, sel.bottom, dim)
            canvas.drawRect(sel, frame)
        }
        canvas.drawText("Bereich mit der Steno-Schrift aufziehen (nur Tafel/Folie, nicht die Kamera)", w / 2, dp(72f), text)
        val label = "✕ Abbrechen"
        val tw = text.measureText(label)
        cancelBox.set(w - tw - dp(40f), dp(20f), w - dp(16f), dp(64f))
        canvas.drawRoundRect(cancelBox, dp(8f), dp(8f), cancelPaint)
        canvas.drawText(label, cancelBox.centerX(), cancelBox.centerY() - (text.fontMetrics.ascent + text.fontMetrics.descent) / 2, text)
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                if (cancelBox.contains(event.x, event.y)) {
                    onCancel()
                    return true
                }
                start = PointF(event.rawX, event.rawY)
                end = start
            }
            MotionEvent.ACTION_MOVE -> end = PointF(event.rawX, event.rawY)
            MotionEvent.ACTION_UP -> {
                val s = start
                end = PointF(event.rawX, event.rawY)
                val e = end!!
                start = null
                if (s != null && abs(e.x - s.x) >= dp(48f) && abs(e.y - s.y) >= dp(48f)) {
                    onPicked(ScreenRect(min(s.x, e.x).toInt(), min(s.y, e.y).toInt(), max(s.x, e.x).toInt(), max(s.y, e.y).toInt()))
                }
            }
            MotionEvent.ACTION_CANCEL -> start = null
        }
        invalidate()
        return true
    }
}

/** Floating control bar (drag at "⠿"): pause/live, analyse now, deep check, region, overlay, details, app, stop. */
@SuppressLint("ViewConstructor", "SetTextI18n")
class ControlsView(context: Context, private val actions: Actions, deepAvailable: Boolean) : LinearLayout(context) {
    interface Actions {
        fun togglePause()
        fun analyseNow()
        fun deepNow()
        fun pickRegion()
        fun toggleOverlay(): Boolean
        fun openApp()
        fun stopAll()
        fun moveControlsBy(dx: Int, dy: Int)
    }

    private val pauseButton: TextView
    private val overlayButton: TextView
    private val statusView: TextView
    private val detailsView: TextView
    private val detailsScroll: ScrollView

    init {
        orientation = VERTICAL
        background = GradientDrawable().apply { setColor(Color.argb(235, 28, 28, 30)); cornerRadius = dp(12f) }
        val pad = dp(8f).toInt()
        setPadding(pad, pad / 2, pad, pad / 2)
        val row = LinearLayout(context).apply { orientation = HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        val grip = TextView(context).apply {
            text = "⠿ Steno"
            setTextColor(Color.rgb(138, 180, 248))
            typeface = Typeface.DEFAULT_BOLD
            setPadding(dp(4f).toInt(), dp(6f).toInt(), dp(8f).toInt(), dp(6f).toInt())
        }
        attachDrag(grip)
        row.addView(grip)
        pauseButton = button(row, "⏸ Pause") { actions.togglePause() }
        button(row, "⚡ Jetzt") { actions.analyseNow() }
        button(row, "🔍 Genau") { actions.deepNow() }.isEnabled = deepAvailable
        button(row, "⬚ Bereich") { actions.pickRegion() }
        overlayButton = button(row, "👁 An") { setOverlayShown(actions.toggleOverlay()) }
        button(row, "☰") { toggleDetails() }
        button(row, "⚙") { actions.openApp() }
        button(row, "✕") { actions.stopAll() }
        addView(row)
        statusView = TextView(context).apply {
            setTextColor(Color.rgb(189, 193, 198))
            textSize = 12f
            maxLines = 2
            ellipsize = TextUtils.TruncateAt.END
            text = "bereit"
        }
        addView(statusView)
        detailsView = TextView(context).apply {
            setTextColor(Color.rgb(236, 236, 236))
            textSize = 13f
            setTextIsSelectable(false)
        }
        detailsScroll = ScrollView(context).apply {
            addView(detailsView)
            visibility = GONE
        }
        addView(detailsScroll, LayoutParams(dp(520f).toInt(), dp(260f).toInt()))
    }

    private fun button(row: LinearLayout, label: String, onClick: () -> Unit): TextView {
        val b = TextView(context).apply {
            text = label
            setTextColor(Color.rgb(241, 243, 244))
            textSize = 14f
            val h = dp(10f).toInt()
            val v = dp(7f).toInt()
            setPadding(h, v, h, v)
            background = GradientDrawable().apply { setColor(Color.rgb(60, 64, 67)); cornerRadius = dp(7f) }
            setOnClickListener { onClick() }
        }
        row.addView(b, LayoutParams(LayoutParams.WRAP_CONTENT, LayoutParams.WRAP_CONTENT).apply { marginStart = dp(4f).toInt() })
        return b
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun attachDrag(handle: View) {
        var lastX = 0f
        var lastY = 0f
        handle.setOnTouchListener { _, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> { lastX = e.rawX; lastY = e.rawY; true }
                MotionEvent.ACTION_MOVE -> {
                    actions.moveControlsBy((e.rawX - lastX).toInt(), (e.rawY - lastY).toInt())
                    lastX = e.rawX
                    lastY = e.rawY
                    true
                }
                else -> false
            }
        }
    }

    fun setLive(live: Boolean) {
        pauseButton.text = if (live) "⏸ Pause" else "▶ Live"
    }

    fun setOverlayShown(shown: Boolean) {
        overlayButton.text = if (shown) "👁 An" else "👁 Aus"
    }

    fun setStatus(text: String) {
        statusView.text = text
    }

    fun setDetails(text: CharSequence) {
        detailsView.text = text
    }

    private fun toggleDetails() {
        detailsScroll.visibility = if (detailsScroll.visibility == VISIBLE) GONE else VISIBLE
    }
}
