package io.github.erikiss.steno.core

import java.io.File
import java.time.LocalDate
import java.time.LocalDateTime

class BudgetExceeded(message: String) : Exception(message)

/** USD per million tokens (same table as `pipeline/budget.py`; cache writes 1.25x / 2x input). */
data class Price(val input: Double, val output: Double, val cacheWrite5m: Double, val cacheWrite1h: Double, val cacheRead: Double)

data class UsageNumbers(
    val input: Long = 0,
    val output: Long = 0,
    val cacheRead: Long = 0,
    val cacheWrite5m: Long = 0,
    val cacheWrite1h: Long = 0,
)

object Pricing {
    val PER_MTOK: Map<String, Price> = mapOf(
        "claude-haiku-5-5" to Price(0.10, 0.50, 0.125, 0.20, 0.01),
        "claude-sonnet-5-5" to Price(2.00, 10.00, 2.50, 4.00, 0.20),
        "claude-opus-5-5" to Price(4.00, 20.00, 5.00, 8.00, 0.20),
    )

    /** Unknown models are priced like the most expensive known one (conservative). */
    fun price(model: String): Price = PER_MTOK[model] ?: PER_MTOK.values.maxBy { it.output }

    fun estimate(model: String, u: UsageNumbers): Double {
        val p = price(model)
        return (u.input * p.input + u.output * p.output + u.cacheWrite5m * p.cacheWrite5m +
            u.cacheWrite1h * p.cacheWrite1h + u.cacheRead * p.cacheRead) / 1_000_000.0
    }

    /** Upper bound for one call: every input token as a cache write, the full max_tokens as output. */
    fun worstCase(model: String, inputTokens: Int, maxTokens: Long): Double {
        val p = price(model)
        return (inputTokens * maxOf(p.input, p.cacheWrite5m) + maxTokens * p.output) / 1_000_000.0
    }
}

/**
 * Append-only JSONL spend ledger in app storage with a total cap and a daily cap (the deep stage may use only
 * [deepShare] of the daily cap, so the cheap fast stage keeps working). Mirrors the desktop ledger's behaviour.
 */
class SpendLedger(
    private val file: File,
    @Volatile var totalCapUsd: Double,
    @Volatile var dailyCapUsd: Double,
    @Volatile var deepShare: Double = 0.8,
    private val today: () -> LocalDate = { LocalDate.now() },
) {
    private data class Row(val ts: String, val purpose: String, val cost: Double)

    private val rows = ArrayList<Row>()
    private var reserved = 0.0

    init {
        if (file.exists()) {
            file.forEachLine { line ->
                if (line.isNotBlank()) runCatching { Json.mapper.readTree(line) }.getOrNull()?.let { n ->
                    rows.add(Row(n.path("ts").asText(""), n.path("purpose").asText(""), n.path("cost_usd").asDouble(0.0)))
                }
            }
        }
    }

    @Synchronized
    fun spentTotal(): Double = rows.sumOf { it.cost }

    @Synchronized
    fun spentToday(): Double {
        val day = today().toString()
        return rows.filter { it.ts.startsWith(day) }.sumOf { it.cost }
    }

    /** Claim room for one call; throws [BudgetExceeded] before anything is spent. */
    @Synchronized
    fun reserve(worstCase: Double, stage: Stage) {
        val total = spentTotal()
        if (total + reserved + worstCase > totalCapUsd) {
            throw BudgetExceeded("Gesamtbudget (${"$%.2f".format(totalCapUsd)}) erreicht – bisher ${"$%.4f".format(total)}")
        }
        val cap = dailyCapUsd * (if (stage == Stage.DEEP) deepShare else 1.0)
        val day = spentToday()
        if (day + reserved + worstCase > cap) {
            val which = if (stage == Stage.DEEP) "Deep-Anteil des Tagesbudgets" else "Tagesbudget"
            throw BudgetExceeded("$which (${"$%.2f".format(cap)}) erreicht – heute ${"$%.4f".format(day)}")
        }
        reserved += worstCase
    }

    @Synchronized
    fun release(worstCase: Double) {
        reserved = maxOf(0.0, reserved - worstCase)
    }

    /** Append one row and return its cost. */
    @Synchronized
    fun record(stage: Stage, model: String, usage: UsageNumbers, extra: Map<String, Any?> = emptyMap()): Double {
        val cost = Pricing.estimate(model, usage)
        val ts = LocalDateTime.now().withNano(0).toString()
        val row = linkedMapOf<String, Any?>(
            "ts" to ts, "purpose" to "steno-${stage.id}", "model" to model, "cost_usd" to cost,
            "input_tokens" to usage.input, "output_tokens" to usage.output, "cache_read_tokens" to usage.cacheRead,
            "cache_write_tokens" to usage.cacheWrite5m + usage.cacheWrite1h,
        )
        row.putAll(extra)
        file.parentFile?.mkdirs()
        file.appendText(Json.mapper.writeValueAsString(row) + "\n")
        rows.add(Row(ts, "steno-${stage.id}", cost))
        return cost
    }
}
