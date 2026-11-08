package io.github.erikiss.steno.core

import kotlin.math.max
import kotlin.math.round

/**
 * The resize rule the Messages API applies to images (reference implementation from the vision-coordinates docs,
 * same as `factcheck/steno/imaging.py`). Pre-resizing with it means the API never resizes again, so the pixel
 * boxes the model returns map 1:1 onto the image we sent.
 */
object ImageMath {
    const val PATCH = 28
    const val STANDARD_MAX_EDGE = 1568
    const val STANDARD_MAX_TOKENS = 1568
    const val HIGH_RES_MAX_EDGE = 2576
    const val HIGH_RES_MAX_TOKENS = 4784

    private fun ceilDiv(a: Int, b: Int) = (a + b - 1) / b

    /** Visual tokens: one per 28x28 patch. */
    fun countImageTokens(width: Int, height: Int): Int = ceilDiv(width, PATCH) * ceilDiv(height, PATCH)

    /** Largest aspect-preserving size that fits both limits; images that fit are returned unchanged. */
    fun resizedSize(
        width: Int,
        height: Int,
        maxEdge: Int = STANDARD_MAX_EDGE,
        maxTokens: Int = STANDARD_MAX_TOKENS,
    ): Pair<Int, Int> {
        fun fits(w: Int, h: Int) = ceilDiv(w, PATCH) * PATCH <= maxEdge &&
            ceilDiv(h, PATCH) * PATCH <= maxEdge && countImageTokens(w, h) <= maxTokens

        if (fits(width, height)) return width to height
        if (height > width) {                                  // solve on the long edge, then swap back
            val (rh, rw) = resizedSize(height, width, maxEdge, maxTokens)
            return rw to rh
        }
        val aspect = width.toDouble() / height
        var lo = 1                                             // lo always fits, hi never does
        var hi = width
        while (lo + 1 < hi) {
            val mid = (lo + hi) / 2
            if (fits(mid, max(round(mid / aspect).toInt(), 1))) lo = mid else hi = mid
        }
        return lo to max(round(lo / aspect).toInt(), 1)     // round = half to even, like Python's round()
    }
}
