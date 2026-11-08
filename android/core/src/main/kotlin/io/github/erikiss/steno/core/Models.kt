package io.github.erikiss.steno.core

import com.fasterxml.jackson.databind.JsonNode

enum class MarkKind(val id: String) {
    WORD("word"), ERROR("error"), NOTE("note");

    companion object {
        fun of(id: String?): MarkKind = entries.firstOrNull { it.id == id } ?: NOTE
    }
}

enum class Stage(val id: String) { FAST("fast"), DEEP("deep") }

/** A rectangle in model-image pixels (x1 < x2, y1 < y2). */
data class Box(val x1: Float, val y1: Float, val x2: Float, val y2: Float)

/** One thing to draw: a read word, a suspected error, or a note (same fields as `models.Mark` in Python). */
data class Mark(
    val kind: MarkKind,
    val box: Box? = null,
    val text: String = "",
    val alternatives: List<String> = emptyList(),
    val issue: String = "",
    val intended: String = "",
    val fix: String = "",
    val spot: Pair<Float, Float>? = null,
    val confidence: Float = 0.5f,
) {
    companion object {
        private fun str(node: JsonNode, key: String): String =
            node.get(key)?.takeIf { it.isTextual || it.isNumber }?.asText()?.trim() ?: ""

        private fun numbers(node: JsonNode, key: String, n: Int): List<Float>? {
            val arr = node.get(key) ?: return null
            if (!arr.isArray || arr.size() != n || !arr.all { it.isNumber }) return null
            return arr.map { it.asDouble().toFloat() }
        }

        /** Validate one streamed item: clamp boxes into the image, drop degenerate ones, normalise fields. */
        fun fromJson(node: JsonNode, width: Int, height: Int): Mark? {
            val w = width.toFloat()
            val h = height.toFloat()
            val kind = MarkKind.of(node.get("kind")?.asText())
            val box = numbers(node, "box", 4)?.let { b ->
                val xs = listOf(b[0].coerceIn(0f, w), b[2].coerceIn(0f, w)).sorted()
                val ys = listOf(b[1].coerceIn(0f, h), b[3].coerceIn(0f, h)).sorted()
                if (xs[1] - xs[0] >= 2f && ys[1] - ys[0] >= 2f) Box(xs[0], ys[0], xs[1], ys[1]) else null
            }
            val spot = numbers(node, "spot", 2)?.let { s -> s[0].coerceIn(0f, w) to s[1].coerceIn(0f, h) }
            val text = str(node, "text")
            val issue = str(node, "issue")
            if (box == null && text.isEmpty() && issue.isEmpty()) return null
            val alts = (node.get("alternatives")?.takeIf { it.isArray }?.map { it.asText().trim() } ?: emptyList())
                .filter { it.isNotEmpty() && it != text }
                .take(3)
            val conf = node.get("confidence")?.takeIf { it.isNumber }?.asDouble()?.toFloat()?.coerceIn(0f, 1f) ?: 0.5f
            return Mark(kind, box, text, alts, issue, str(node, "intended"), str(node, "fix"), spot, conf)
        }
    }
}

/** The exact image the model sees (PNG), plus the size of the capture it came from. */
class PreparedImage(
    val png: ByteArray,
    val width: Int,
    val height: Int,
    val sourceWidth: Int,
    val sourceHeight: Int,
) {
    val tokens: Int get() = ImageMath.countImageTokens(width, height)
}

/** One finished (or failed) analysis of one frame; filled in while the answer streams. */
class StenoResult(
    val frameId: Long,
    val stage: Stage,
    val model: String,
    val imageWidth: Int,
    val imageHeight: Int,
    val imageTokens: Int,
) {
    val marks: MutableList<Mark> = ArrayList()
    var answer: String = ""
    var ttftS: Double? = null
    var firstMarkS: Double? = null
    var durationS: Double = 0.0
    var costUsd: Double = 0.0
    var stopReason: String = ""
    var servedBy: String = ""
    var error: String? = null
    var cancelled: Boolean = false
    val createdAtMillis: Long = System.currentTimeMillis()

    val errors: List<Mark> get() = marks.filter { it.kind == MarkKind.ERROR }

    /** The read words in order, as one line of longhand. */
    fun reading(): String = marks.filter { it.kind != MarkKind.NOTE && it.text.isNotEmpty() }.joinToString(" ") { it.text }
}
