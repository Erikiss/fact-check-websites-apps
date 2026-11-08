package io.github.erikiss.steno.core

import kotlin.math.max
import kotlin.math.min
import kotlin.math.round

data class Rect(val x: Float, val y: Float, val w: Float, val h: Float) {
    val right: Float get() = x + w
    val bottom: Float get() = y + h

    fun intersects(o: Rect) = x < o.right && o.x < right && y < o.bottom && o.y < bottom
    fun inside(b: Rect) = x >= b.x && y >= b.y && right <= b.right && bottom <= b.bottom
}

/** Label text, colours and placement shared by the overlay (same rules as `factcheck/steno/layout.py`). */
object Layout {
    const val COLOR_WORD = 0xFF1E8E3E.toInt()    // green
    const val COLOR_ERROR = 0xFFD93025.toInt()   // red
    const val COLOR_NOTE = 0xFFF29900.toInt()    // amber
    private const val MAX_LINE = 52

    fun color(kind: MarkKind): Int = when (kind) {
        MarkKind.WORD -> COLOR_WORD
        MarkKind.ERROR -> COLOR_ERROR
        MarkKind.NOTE -> COLOR_NOTE
    }

    private fun clamp(v: Float, lo: Float, hi: Float) = if (hi >= lo) max(lo, min(v, hi)) else lo

    /**
     * Top-left corner for each label: the first candidate position (above, below, right, left, further out) that
     * stays clear of earlier labels and of every anchor box; otherwise the first candidate.
     */
    fun placeLabels(anchors: List<Rect>, sizes: List<Pair<Float, Float>>, bounds: Rect, gap: Float = 3f): List<Pair<Float, Float>> {
        require(anchors.size == sizes.size)
        val placed = ArrayList<Rect>()
        val out = ArrayList<Pair<Float, Float>>()
        for ((anchor, size) in anchors.zip(sizes)) {
            val (w, h) = size
            val candidates = listOf(
                anchor.x to anchor.y - h - gap,
                anchor.x to anchor.bottom + gap,
                anchor.right + gap to anchor.y,
                anchor.x - w - gap to anchor.y,
                anchor.x to anchor.y - 2 * (h + gap),
                anchor.x to anchor.bottom + h + 2 * gap,
            )
            fun at(c: Pair<Float, Float>) = Rect(clamp(c.first, bounds.x, bounds.right - w), clamp(c.second, bounds.y, bounds.bottom - h), w, h)
            val chosen = candidates.map(::at).firstOrNull { r -> placed.none { r.intersects(it) } && anchors.none { r.intersects(it) } }
                ?: at(candidates[0])
            placed.add(chosen)
            out.add(chosen.x to chosen.y)
        }
        return out
    }

    private fun short(text: String): String {
        val t = text.split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ")
        return if (t.length <= MAX_LINE) t else t.take(MAX_LINE - 1) + "…"
    }

    /** Lines drawn next to a mark, most important first. */
    fun labelLines(m: Mark): List<String> {
        val conf = if (m.kind == MarkKind.ERROR || m.confidence < 0.75f) " · ${round(m.confidence * 100.0).toInt()}%" else ""
        val alts = if (m.alternatives.isNotEmpty()) "oder: ${m.alternatives.joinToString(" / ")}" else ""
        val lines = when (m.kind) {
            MarkKind.ERROR -> listOf(
                "✗ ${m.text.ifEmpty { "?" }}" + (if (m.intended.isNotEmpty()) " → ${m.intended}" else "") + conf,
                m.issue,
                if (m.fix.isNotEmpty()) "richtig: ${m.fix}" else "",
                alts,
            )
            MarkKind.NOTE -> listOf(m.text.ifEmpty { m.issue } + conf, if (m.text.isNotEmpty()) m.issue else "")
            MarkKind.WORD -> listOf(m.text.ifEmpty { "?" } + conf, alts)
        }
        return lines.filter { it.isNotBlank() }.map(::short)
    }
}
