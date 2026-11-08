package io.github.erikiss.steno.core

import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/** A small grayscale thumbnail (0..255) of one capture tick, used only for change detection. */
class Signature(val width: Int, val height: Int, val pixels: FloatArray) {
    init {
        require(pixels.size == width * height) { "pixels must be width*height" }
    }

    /** Population standard deviation (numpy's default), used to recognise a blank picture. */
    fun std(): Double {
        if (pixels.isEmpty()) return 0.0
        var sum = 0.0
        for (p in pixels) sum += p
        val mean = sum / pixels.size
        var sq = 0.0
        for (p in pixels) sq += (p - mean) * (p - mean)
        return sqrt(sq / pixels.size)
    }

    companion object {
        /** Block-average a grayscale image down to about [targetWidth] columns. */
        fun downscale(gray: FloatArray, width: Int, height: Int, targetWidth: Int = 160): Signature {
            val f = max(1, width / max(1, targetWidth))
            val w = width / f
            val h = height / f
            if (f == 1 || w == 0 || h == 0) return Signature(width, height, gray.copyOf())
            val out = FloatArray(w * h)
            val area = (f * f).toFloat()
            for (y in 0 until h) {
                for (x in 0 until w) {
                    var s = 0f
                    for (dy in 0 until f) {
                        val row = (y * f + dy) * width + x * f
                        for (dx in 0 until f) s += gray[row + dx]
                    }
                    out[y * w + x] = s / area
                }
            }
            return Signature(w, h, out)
        }
    }
}

/** Share of pixels whose brightness differs by more than [pixelThreshold]; ignores video-compression shimmer. */
fun changedFraction(a: Signature, b: Signature, pixelThreshold: Float = 24f): Double {
    if (a.width != b.width || a.height != b.height) return 1.0
    var n = 0
    for (i in a.pixels.indices) if (abs(a.pixels[i] - b.pixels[i]) > pixelThreshold) n++
    return n.toDouble() / a.pixels.size
}

enum class Trigger { NONE, FAST, DEEP }

data class GateConfig(
    var settleS: Double = 0.45,          // still this long after a change -> fast analysis
    var maxWaitS: Double = 2.0,          // changing continuously -> fast analysis at least this often
    var minIntervalS: Double = 0.8,      // never start fast analyses closer together
    var changeThreshold: Double = 0.004, // share of pixels that must differ from the last analysed picture
    var motionThreshold: Double = 0.002, // share of pixels differing between ticks = "still moving"
    var deepAfterS: Double = 2.5,        // still this long -> one deep analysis (<= 0: never)
    var pixelThreshold: Float = 24f,
    var maxBackoffSteps: Int = 3,        // constant motion: 2 s, 4 s, 8 s, 16 s
    var blankStd: Double = 1.5,          // a picture this uniform is never sent
)

/**
 * Decides when a frame is worth sending (port of `factcheck/steno/frames.py`, same rules and defaults):
 * FAST when the picture changed and settled (or kept changing for maxWait), DEEP once per still picture,
 * never for a blank picture. Time is passed in (seconds), so tests need no clock.
 */
class ChangeGate(val cfg: GateConfig = GateConfig()) {
    private var prev: Signature? = null
    private var ref: Signature? = null
    private var deepRef: Signature? = null
    private var stillSince: Double? = null
    private var dirtySince: Double? = null
    private var lastFast = Double.NEGATIVE_INFINITY
    private var overdueStreak = 0
    private var rebaseNext = false

    fun reset() {
        prev = null; ref = null; deepRef = null; stillSince = null; dirtySince = null
        lastFast = Double.NEGATIVE_INFINITY; overdueStreak = 0; rebaseNext = false
    }

    private fun diff(a: Signature, b: Signature) = changedFraction(a, b, cfg.pixelThreshold)

    fun update(sig: Signature, now: Double): Trigger {
        if (rebaseNext) {                                   // our own overlay just changed the picture
            rebaseNext = false
            val r = ref
            val d = deepRef
            if (r != null && d != null && diff(r, d) <= cfg.changeThreshold) deepRef = sig
            prev = sig
            ref = sig
            return Trigger.NONE
        }
        val p = prev
        val moving = p != null && diff(sig, p) > cfg.motionThreshold
        prev = sig
        if (moving) stillSince = null else if (stillSince == null) stillSince = now

        if (sig.std() < cfg.blankStd) {                     // nothing written: nothing to analyse
            ref = sig
            dirtySince = null
            return Trigger.NONE
        }

        val r = ref
        val dirty = r == null || diff(sig, r) > cfg.changeThreshold
        if (!dirty) dirtySince = null else if (dirtySince == null) dirtySince = now

        if (dirty && now - lastFast >= cfg.minIntervalS) {
            val still = stillSince
            val settled = still != null && now - still >= cfg.settleS
            val maxWait = cfg.maxWaitS * (1 shl min(overdueStreak, cfg.maxBackoffSteps))
            val ds = dirtySince
            val overdue = ds != null && now - ds >= maxWait
            if (settled || overdue) {
                overdueStreak = if (settled) 0 else overdueStreak + 1
                markFast(sig, now)
                return Trigger.FAST
            }
        }

        val still = stillSince
        val d = deepRef
        if (!dirty && cfg.deepAfterS > 0 && still != null && now - still >= cfg.deepAfterS &&
            (d == null || diff(sig, d) > cfg.changeThreshold)
        ) {
            markDeep(sig)
            return Trigger.DEEP
        }
        return Trigger.NONE
    }

    /** Record that [sig] was (or is being) analysed by the fast stage. */
    fun markFast(sig: Signature, now: Double) {
        ref = sig
        dirtySince = null
        lastFast = now
    }

    /** Record that [sig] was (or is being) analysed by the deep stage. */
    fun markDeep(sig: Signature) {
        deepRef = sig
    }

    /** Accept the next tick as the new reference without triggering (our overlay is in the capture). */
    fun rebaseNext() {
        rebaseNext = true
    }
}
