package io.github.erikiss.steno.core

import com.anthropic.client.AnthropicClient
import com.anthropic.client.okhttp.AnthropicOkHttpClient
import com.anthropic.core.JsonValue
import com.anthropic.errors.AnthropicException
import com.anthropic.errors.AnthropicIoException
import com.anthropic.errors.AnthropicServiceException
import com.anthropic.errors.BadRequestException
import com.anthropic.errors.NotFoundException
import com.anthropic.errors.PermissionDeniedException
import com.anthropic.errors.RateLimitException
import com.anthropic.errors.UnauthorizedException
import com.anthropic.helpers.MessageAccumulator
import com.anthropic.models.messages.Base64ImageSource
import com.anthropic.models.messages.CacheControlEphemeral
import com.anthropic.models.messages.ContentBlockParam
import com.anthropic.models.messages.ImageBlockParam
import com.anthropic.models.messages.JsonOutputFormat
import com.anthropic.models.messages.Message
import com.anthropic.models.messages.MessageCreateParams
import com.anthropic.models.messages.OutputConfig
import com.anthropic.models.messages.TextBlockParam
import com.anthropic.models.messages.ThinkingConfigAdaptive
import com.anthropic.models.messages.ThinkingConfigBetweenTools
import com.anthropic.models.messages.ThinkingConfigDisabled
import com.anthropic.models.messages.Usage
import com.fasterxml.jackson.databind.JsonNode
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runInterruptible
import kotlinx.coroutines.withContext
import java.time.Duration
import java.util.Base64
import java.util.concurrent.ConcurrentHashMap

data class StageConfig(
    val stage: Stage,
    val model: String,
    val effort: String = "low",
    val thinking: String = "off",          // "off" | "adaptive"
    val maxTokens: Long = 2048,
    val fallbacks: Boolean = true,
    val maxRetries: Int = 0,               // a retried live frame is stale anyway: the next frame is the retry
    val timeoutS: Long = 30,
)

/**
 * Per-model request rules (claude-api skill, checked 2026-11-08): Haiku 5.5 takes thinking "disabled" at effort
 * <= high; Sonnet 5.5 turns thinking off with "between_tools"; Opus 5.5 cannot turn it off (effort is the lever);
 * server-side refusal fallback ("fallbacks": "default") exists for Opus/Sonnet 5.x, not for Haiku 5.5.
 */
object ModelRules {
    enum class Thinking { OMIT, ADAPTIVE, DISABLED, BETWEEN_TOOLS }

    const val FALLBACK_BETA = "server-side-fallback-2026-07-01"
    private val alwaysThinks = listOf("claude-opus-5-5", "claude-fable", "claude-mythos")
    private val betweenTools = listOf("claude-sonnet-5-5")
    private val noEffort = listOf("claude-haiku-4-5", "claude-sonnet-4-5", "claude-3")
    private val fallbackModels = listOf("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5")

    fun thinking(model: String, mode: String, effort: String): Thinking = when {
        alwaysThinks.any { model.startsWith(it) } -> if (mode == "adaptive") Thinking.ADAPTIVE else Thinking.OMIT
        mode == "adaptive" || effort == "xhigh" || effort == "max" -> Thinking.ADAPTIVE   // "off" is a 400 there
        betweenTools.any { model.startsWith(it) } -> Thinking.BETWEEN_TOOLS              // no tools = no thinking
        else -> Thinking.DISABLED
    }

    fun supportsEffort(model: String) = noEffort.none { model.startsWith(it) }
    fun supportsFallbacks(model: String) = fallbackModels.any { model.startsWith(it) }
}

interface VisionBackend {
    /** Analyse one image; [onMark] is called (on any thread) as soon as each mark is complete. */
    suspend fun analyze(image: PreparedImage, stage: StageConfig, frameId: Long, hint: String, onMark: (Mark) -> Unit): StenoResult
}

/** Short German message for the status line (most specific SDK error first). */
fun describeError(e: Throwable): String = when (e) {
    is UnauthorizedException -> "API-Schlüssel ungültig (in den Einstellungen prüfen)"
    is PermissionDeniedException -> "Kein Zugriff auf dieses Modell mit diesem Schlüssel"
    is NotFoundException -> "Modell nicht gefunden (Modellname prüfen)"
    is RateLimitException -> "Rate-Limit erreicht – der nächste Frame versucht es erneut"
    is BadRequestException -> "Anfrage abgelehnt: ${e.message}".take(240)
    is AnthropicServiceException -> "API-Fehler ${e.statusCode()}: ${e.message}".take(240)
    is AnthropicIoException -> "Keine Verbindung zur API"
    else -> "${e::class.simpleName}: ${e.message}".take(240)
}

private fun usageNumbers(u: Usage?): UsageNumbers {
    if (u == null) return UsageNumbers()
    val breakdown = u.cacheCreation().orElse(null)
    return UsageNumbers(
        input = u.inputTokens(),
        output = u.outputTokens(),
        cacheRead = u.cacheReadInputTokens().orElse(0L),
        cacheWrite5m = breakdown?.ephemeral5mInputTokens() ?: u.cacheCreationInputTokens().orElse(0L),
        cacheWrite1h = breakdown?.ephemeral1hInputTokens() ?: 0L,
    )
}

/**
 * One streamed Messages API call per analysed frame, with the official SDK (anthropic-java): cached system
 * prompt, structured JSON output, per-model thinking/effort, marks handed out as each JSON item completes,
 * every call recorded in the [SpendLedger].
 */
class AnthropicVision(
    private val apiKey: String,
    private val systemPrompt: String,
    schemaJson: String,
    private val ledger: SpendLedger,
    private val baseUrl: String? = null,
    private val log: (String) -> Unit = {},
) : VisionBackend {
    private val schema: Map<String, JsonValue> = Json.mapper.readTree(schemaJson).properties()
        .associate { (k, v) -> k to JsonValue.from(Json.mapper.treeToValue(v, Any::class.java)) }
    private val systemTokens = systemPrompt.length / 3 + 50          // generous, for worst-case reservations
    private val clients = ConcurrentHashMap<Pair<Int, Long>, AnthropicClient>()

    @Volatile
    var fallbacksOk = true                                         // off for the session if the API rejects the beta
        private set

    private fun client(stage: StageConfig): AnthropicClient = clients.getOrPut(stage.maxRetries to stage.timeoutS) {
        AnthropicOkHttpClient.builder()
            .apiKey(apiKey)
            .maxRetries(stage.maxRetries)
            .timeout(Duration.ofSeconds(stage.timeoutS))
            .apply { if (baseUrl != null) baseUrl(baseUrl) }
            .build()
    }

    fun usesFallbacks(stage: StageConfig) = stage.fallbacks && fallbacksOk && ModelRules.supportsFallbacks(stage.model)

    fun buildParams(image: PreparedImage, stage: StageConfig, hint: String): MessageCreateParams {
        val output = OutputConfig.builder()
            .format(JsonOutputFormat.builder().schema(JsonOutputFormat.Schema.builder().putAllAdditionalProperties(schema).build()).build())
            .apply { if (ModelRules.supportsEffort(stage.model)) effort(OutputConfig.Effort.of(stage.effort)) }
            .build()
        val b = MessageCreateParams.builder()
            .model(stage.model)
            .maxTokens(stage.maxTokens)
            .systemOfTextBlockParams(listOf(
                TextBlockParam.builder().text(systemPrompt).cacheControl(CacheControlEphemeral.builder().build()).build()))
            .addUserMessageOfContentBlockParams(listOf(
                ContentBlockParam.ofImage(ImageBlockParam.builder().source(
                    Base64ImageSource.builder()
                        .data(Base64.getEncoder().encodeToString(image.png))
                        .mediaType(Base64ImageSource.MediaType.IMAGE_PNG)
                        .build()).build()),
                ContentBlockParam.ofText(Prompts.buildUserText(image.width, image.height, hint)),
            ))
            .outputConfig(output)
        when (ModelRules.thinking(stage.model, stage.thinking, stage.effort)) {
            ModelRules.Thinking.OMIT -> Unit
            ModelRules.Thinking.ADAPTIVE -> b.thinking(ThinkingConfigAdaptive.builder().build())
            ModelRules.Thinking.DISABLED -> b.thinking(ThinkingConfigDisabled.builder().build())
            ModelRules.Thinking.BETWEEN_TOOLS -> b.thinking(ThinkingConfigBetweenTools.builder().build())
        }
        if (usesFallbacks(stage)) {
            b.putAdditionalHeader("anthropic-beta", ModelRules.FALLBACK_BETA)
            b.putAdditionalBodyProperty("fallbacks", JsonValue.from("default"))
        }
        return b.build()
    }

    override suspend fun analyze(image: PreparedImage, stage: StageConfig, frameId: Long, hint: String, onMark: (Mark) -> Unit): StenoResult {
        val worst = Pricing.worstCase(stage.model, systemTokens + image.tokens + 200, stage.maxTokens)
        ledger.reserve(worst, stage.stage)                         // throws BudgetExceeded before spending anything
        val result = StenoResult(frameId, stage.stage, stage.model, image.width, image.height, image.tokens)
        val parser = ItemStream()
        val started = StartUsage()
        val t0 = System.nanoTime()
        fun seconds() = (System.nanoTime() - t0) / 1e9
        var final: Message? = null
        try {
            var params = buildParams(image, stage, hint)
            while (true) {
                try {
                    final = stream(params, stage, image, parser, result, started, onMark, ::seconds)
                    break
                } catch (e: BadRequestException) {
                    if (usesFallbacks(stage) && e.message.orEmpty().contains("fallback", ignoreCase = true) && parser.text.isEmpty()) {
                        log("Refusal-Fallback vom Server abgelehnt – ohne weiter: ${e.message}")
                        fallbacksOk = false
                        params = buildParams(image, stage, hint)
                        continue
                    }
                    throw e
                }
            }
        } catch (e: CancellationException) {                       // newer picture or user stop: charge what was used
            result.cancelled = true
            result.durationS = seconds()
            recordPartial(stage, image, parser, started.usage, result)
            throw e
        } catch (e: AnthropicException) {
            result.error = describeError(e)
            log("${stage.stage.id}: ${result.error}")
        } finally {
            ledger.release(worst)
        }
        result.durationS = seconds()
        val message = final
        if (message != null) {
            finish(result, message, parser, image)
            result.costUsd = ledger.record(stage.stage, stage.model, usageNumbers(message.usage()),
                mapOf("frame_id" to frameId, "served_by" to result.servedBy, "ttft_s" to result.ttftS,
                    "duration_s" to result.durationS, "stop_reason" to result.stopReason))
        } else if (parser.text.isNotEmpty()) {
            recordPartial(stage, image, parser, started.usage, result)
        }
        return result
    }

    /** Holder for the usage reported in message_start (needed to charge a cancelled call). */
    private class StartUsage {
        @Volatile var usage: Usage? = null
    }

    private suspend fun stream(
        params: MessageCreateParams, stage: StageConfig, image: PreparedImage, parser: ItemStream,
        result: StenoResult, started: StartUsage, onMark: (Mark) -> Unit, seconds: () -> Double,
    ): Message = coroutineScope {
        val accumulator = MessageAccumulator.create()
        val response = runInterruptible(Dispatchers.IO) { client(stage).messages().createStreaming(params) }
        // Closing the response from here aborts the blocking read below as soon as we are cancelled.
        val watcher = launch(start = CoroutineStart.UNDISPATCHED) {
            try { awaitCancellation() } finally { response.close() }
        }
        try {
            runInterruptible(Dispatchers.IO) {
                response.stream().forEach { event ->
                    accumulator.accumulate(event)
                    event.messageStart().ifPresent { started.usage = it.message().usage() }
                    event.contentBlockDelta().flatMap { it.delta().text() }.ifPresent { delta ->
                        if (result.ttftS == null) result.ttftS = seconds()
                        for (node in parser.feed(delta.text())) {
                            val mark = Mark.fromJson(node, image.width, image.height) ?: continue
                            if (result.firstMarkS == null) result.firstMarkS = seconds()
                            result.marks.add(mark)
                            onMark(mark)
                        }
                    }
                }
            }
            accumulator.message()
        } finally {
            watcher.cancel()
            response.close()
        }
    }

    private fun finish(result: StenoResult, message: Message, parser: ItemStream, image: PreparedImage) {
        result.stopReason = message.stopReason().map { it.asString() }.orElse("")
        result.servedBy = message.model().asString()
        if (result.stopReason == "refusal") {
            val category = message.stopDetails().flatMap { it.category() }.map { it.asString() }.orElse("ohne Kategorie")
            result.error = "Modell hat die Analyse abgelehnt ($category)"
            result.marks.clear()                                 // partial output before a refusal is not trustworthy
            return
        }
        val data: JsonNode? = parser.final() ?: message.content().asReversed().firstNotNullOfOrNull { block ->
            block.text().map { runCatching { Json.mapper.readTree(it.text()) }.getOrNull() }.orElse(null)?.takeIf { it.isObject }
        }
        if (data != null) {
            result.answer = data.path("answer").asText("").trim()
            if (result.marks.isEmpty()) {                        // nothing parsed while streaming: use the final document
                data.path("items").forEach { node -> Mark.fromJson(node, image.width, image.height)?.let(result.marks::add) }
            }
        }
        if (result.stopReason == "max_tokens") result.error = "Antwort abgeschnitten (max. Tokens erhöhen)"
    }

    private fun recordPartial(stage: StageConfig, image: PreparedImage, parser: ItemStream, startUsage: Usage?, result: StenoResult) {
        val base = usageNumbers(startUsage)
        val usage = base.copy(
            input = if (startUsage == null) (systemTokens + image.tokens).toLong() else base.input,   // assume no cache hit
            output = maxOf(base.output, (parser.text.length / 3).toLong()),
        )
        result.costUsd = ledger.record(stage.stage, stage.model, usage,
            mapOf("frame_id" to result.frameId, "partial" to true, "cancelled" to result.cancelled))
    }
}

/** Rough word boxes from a grayscale image: rows with ink form lines, column gaps separate words (fake mode). */
object InkBoxes {
    private fun runs(mask: BooleanArray, maxGap: Int): List<IntRange> {
        val out = ArrayList<IntRange>()
        var start = -1
        var prev = -1
        for (i in mask.indices) {
            if (!mask[i]) continue
            if (start < 0) start = i else if (i - prev > maxGap + 1) { out.add(start..prev); start = i }
            prev = i
        }
        if (start >= 0) out.add(start..prev)
        return out
    }

    fun find(gray: FloatArray, width: Int, height: Int, minSize: Int = 4): List<Box> {
        val sorted = gray.copyOf().also { it.sort() }
        val background = if (sorted.isEmpty()) 255f else sorted[sorted.size / 2]
        val ink = BooleanArray(gray.size) { kotlin.math.abs(gray[it] - background) > 60f }
        val rowHas = BooleanArray(height) { y -> (0 until width).any { x -> ink[y * width + x] } }
        val boxes = ArrayList<Box>()
        for (rows in runs(rowHas, maxOf(2, height / 60))) {
            val colHas = BooleanArray(width) { x -> rows.any { y -> ink[y * width + x] } }
            for (cols in runs(colHas, maxOf(6, (rows.last - rows.first + 1) / 2))) {
                if (cols.last - cols.first + 1 < minSize) continue
                val inkRows = rows.filter { y -> cols.any { x -> ink[y * width + x] } }
                if (inkRows.isEmpty()) continue
                boxes.add(Box(cols.first.toFloat(), inkRows.first().toFloat(), (cols.last + 1).toFloat(), (inkRows.last() + 1).toFloat()))
            }
        }
        return boxes
    }
}

/** Deterministic $0 stand-in (fake mode): labels ink blobs, streaming them with small delays. */
class FakeVision(private val delayMs: Long = 150, private val boxFinder: (PreparedImage) -> List<Box>) : VisionBackend {
    override suspend fun analyze(image: PreparedImage, stage: StageConfig, frameId: Long, hint: String, onMark: (Mark) -> Unit): StenoResult {
        val t0 = System.nanoTime()
        val result = StenoResult(frameId, stage.stage, "fake:${stage.model}", image.width, image.height, image.tokens)
        result.servedBy = "fake"
        val boxes = withContext(Dispatchers.Default) { boxFinder(image) }.take(12)   // keep pixel work off the UI thread
        val bad = if (boxes.isEmpty()) -1 else boxes.size / 2
        for ((i, b) in boxes.withIndex()) {
            delay(delayMs * (if (stage.stage == Stage.DEEP) 2 else 1))
            val mark = if (i == bad) {
                Mark(MarkKind.ERROR, b, "Wort ${i + 1}", listOf("Wort ${i + 1}b"), "FAKE: Verbindung zu rund statt spitz?",
                    "Wort ${i + 1} (richtig)", "FAKE: spitz anschließen", (b.x1 + b.x2) / 2 to (b.y1 + b.y2) / 2, 0.45f)
            } else {
                Mark(MarkKind.WORD, b, "Wort ${i + 1}", if (i % 3 == 1) listOf("Wort ${i + 1}?") else emptyList(), confidence = 0.8f)
            }
            if (result.ttftS == null) {
                result.ttftS = (System.nanoTime() - t0) / 1e9
                result.firstMarkS = result.ttftS
            }
            result.marks.add(mark)
            onMark(mark)
        }
        result.answer = if (boxes.isEmpty()) "FAKE-Modus: keine Schrift gefunden." else "FAKE-Modus (keine echte Analyse): verdächtig ist Wort ${bad + 1}."
        result.stopReason = "end_turn"
        result.durationS = (System.nanoTime() - t0) / 1e9
        return result
    }
}
