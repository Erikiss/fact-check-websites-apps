package io.github.erikiss.steno.core

import com.fasterxml.jackson.databind.JsonNode
import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.net.InetSocketAddress
import java.nio.file.Files
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * The real SDK client against a local fake Messages API that streams genuine SSE events: checks the request the
 * SDK sends (cache control, image block, schema, effort, thinking, fallback opt-in) and how streamed text becomes
 * marks, costs and errors. No network, no spend.
 */
class VisionTest {
    private lateinit var server: HttpServer
    private val requests = CopyOnWriteArrayList<Pair<Map<String, List<String>>, JsonNode>>()
    private val responders = ConcurrentLinkedQueue<(HttpExchange) -> Unit>()
    private lateinit var ledger: SpendLedger
    private lateinit var ledgerFile: File

    private val doc = """{"items": [
        {"box": [10, 10, 60, 50], "strokes": "Kürzel und", "text": "und", "alternatives": [], "kind": "word", "issue": "", "intended": "",
         "fix": "", "spot": [], "confidence": 0.9},
        {"box": [80, 10, 150, 60], "strokes": "h + au-tief + s", "text": "Hans", "alternatives": ["Hanse"], "kind": "error", "issue": "Verstärkung fehlt",
         "intended": "Haus", "fix": "au: tief + verstärkt", "spot": [100, 30], "confidence": 0.5}
    ], "answer": "Hans statt Haus: Verstärkung fehlt."}"""

    private val fast = StageConfig(Stage.FAST, "claude-haiku-5-5", "low", "off", 2048)
    private val deep = StageConfig(Stage.DEEP, "claude-opus-5-5", "low", "adaptive", 8000, maxRetries = 0)
    private val image = PreparedImage(byteArrayOf(1, 2, 3), 200, 80, 400, 160)

    @Before
    fun setUp() {
        server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/") { ex ->
            val body = ex.requestBody.readBytes().decodeToString()
            requests.add(ex.requestHeaders.mapKeys { it.key.lowercase() } to Json.mapper.readTree(body))
            (responders.poll() ?: { e -> e.sendResponseHeaders(500, -1); e.close() })(ex)
        }
        server.executor = Executors.newCachedThreadPool()
        server.start()
        ledgerFile = File(Files.createTempDirectory("vision").toFile(), "spend.jsonl")
        ledger = SpendLedger(ledgerFile, totalCapUsd = 100.0, dailyCapUsd = 20.0)
    }

    @After
    fun tearDown() = server.stop(0)

    private fun vision() = AnthropicVision("sk-ant-test", "SYSTEM PROMPT", javaClass.classLoader.getResource("analysis_schema.json")!!.readText(),
        ledger, baseUrl = "http://127.0.0.1:${server.address.port}")

    private fun sse(text: String, stopReason: String = "end_turn", model: String = "claude-haiku-5-5", hold: CountDownLatch? = null): (HttpExchange) -> Unit = { ex ->
        ex.responseHeaders.add("Content-Type", "text/event-stream")
        ex.sendResponseHeaders(200, 0)
        runCatching {
            ex.responseBody.use { out ->
                fun send(event: String, data: String) { out.write("event: $event\ndata: $data\n\n".toByteArray()); out.flush() }
                send("message_start", """{"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","model":"$model","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":900,"output_tokens":1,"cache_read_input_tokens":2400,"cache_creation_input_tokens":0}}}""")
                send("content_block_start", """{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}""")
                text.chunked(7).forEachIndexed { i, c ->
                    send("content_block_delta", """{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":${Json.mapper.writeValueAsString(c)}}}""")
                    if (i == 0) hold?.await(10, TimeUnit.SECONDS)
                }
                send("content_block_stop", """{"type":"content_block_stop","index":0}""")
                send("message_delta", """{"type":"message_delta","delta":{"stop_reason":"$stopReason","stop_sequence":null},"usage":{"output_tokens":210}}""")
                send("message_stop", """{"type":"message_stop"}""")
            }
        }
    }

    private fun error(status: Int, type: String, message: String): (HttpExchange) -> Unit = { ex ->
        val body = """{"type":"error","error":{"type":"$type","message":"$message"}}""".toByteArray()
        ex.responseHeaders.add("Content-Type", "application/json")
        ex.sendResponseHeaders(status, body.size.toLong())
        ex.responseBody.use { it.write(body) }
    }

    @Test
    fun requestShapeForHaikuAndOpus() {
        val v = vision()
        responders.add(sse(doc))
        responders.add(sse(doc, model = "claude-opus-5-5"))
        runBlocking {
            v.analyze(image, fast, 1, "Finde den Fehler") {}
            v.analyze(image, deep, 2, "", "Vorheriger Satz") {}
        }
        val (fastHeaders, f) = requests[0]
        assertEquals("claude-haiku-5-5", f.path("model").asText())
        assertEquals("ephemeral", f.path("system")[0].path("cache_control").path("type").asText())
        assertEquals("SYSTEM PROMPT", f.path("system")[0].path("text").asText())
        val content = f.path("messages")[0].path("content")
        assertEquals("image", content[0].path("type").asText())
        assertEquals("image/png", content[0].path("source").path("media_type").asText())
        assertEquals("AQID", content[0].path("source").path("data").asText())
        assertEquals("Bildgröße: 200×80 Pixel.\nHinweis des Nutzers: Finde den Fehler", content[1].path("text").asText())
        assertEquals("json_schema", f.path("output_config").path("format").path("type").asText())
        assertEquals("object", f.path("output_config").path("format").path("schema").path("type").asText())
        assertEquals("low", f.path("output_config").path("effort").asText())
        assertEquals("disabled", f.path("thinking").path("type").asText())
        assertTrue(f.path("fallbacks").isMissingNode)
        assertFalse(fastHeaders["anthropic-beta"].orEmpty().any { "server-side-fallback" in it })

        val (deepHeaders, d) = requests[1]
        assertEquals("adaptive", d.path("thinking").path("type").asText())
        assertEquals("default", d.path("fallbacks").asText())
        assertTrue(deepHeaders["anthropic-beta"].orEmpty().any { ModelRules.FALLBACK_BETA in it })
        assertTrue("Vorheriger Satz" in d.path("messages")[0].path("content")[1].path("text").asText())
    }

    @Test
    fun streamsMarksInOrderAndRecordsSpend() {
        responders.add(sse(doc))
        val seen = ArrayList<Mark>()
        val result = runBlocking { vision().analyze(image, fast, 7, "") { seen.add(it) } }
        assertEquals(listOf("und", "Hans"), seen.map { it.text })
        assertEquals(listOf("Kürzel und", "h + au-tief + s"), seen.map { it.strokes })
        assertEquals(seen, result.marks)
        assertEquals("Hans statt Haus: Verstärkung fehlt.", result.answer)
        assertNull(result.error)
        assertEquals("end_turn", result.stopReason)
        assertEquals("claude-haiku-5-5", result.servedBy)
        assertNotNull(result.firstMarkS)
        assertEquals("Haus", result.errors.single().intended)
        // 900 input + 210 output + 2400 cache reads at Haiku 5.5 prices
        assertEquals((900 * 0.10 + 210 * 0.50 + 2400 * 0.01) / 1e6, result.costUsd, 1e-12)
        assertTrue(ledgerFile.readText().contains("\"frame_id\":7"))
    }

    @Test
    fun refusalDropsPartialMarks() {
        responders.add(sse(doc, stopReason = "refusal"))
        val result = runBlocking { vision().analyze(image, fast, 1, "") {} }
        assertTrue(result.marks.isEmpty())
        assertTrue(result.error!!.contains("abgelehnt"))
    }

    @Test
    fun maxTokensKeepsCompleteItems() {
        responders.add(sse(doc.substring(0, doc.indexOf("\"answer\"")), stopReason = "max_tokens"))
        val result = runBlocking { vision().analyze(image, fast, 1, "") {} }
        assertEquals(2, result.marks.size)
        assertTrue(result.error!!.contains("abgeschnitten"))
    }

    @Test
    fun invalidKeyIsReportedWithoutSpend() {
        responders.add(error(401, "authentication_error", "invalid x-api-key"))
        val result = runBlocking { vision().analyze(image, fast, 1, "") {} }
        assertEquals("API-Schlüssel ungültig (in den Einstellungen prüfen)", result.error)
        assertFalse(ledgerFile.exists() && ledgerFile.readText().isNotBlank())
    }

    @Test
    fun rejectedFallbackBetaIsDroppedForTheSession() {
        responders.add(error(400, "invalid_request_error", "fallbacks: unsupported beta"))
        responders.add(sse(doc, model = "claude-opus-5-5"))
        val v = vision()
        val result = runBlocking { v.analyze(image, deep, 3, "") {} }
        assertNull(result.error)
        assertEquals(2, result.marks.size)
        assertEquals("default", requests[0].second.path("fallbacks").asText())
        assertTrue(requests[1].second.path("fallbacks").isMissingNode)
        assertFalse(v.fallbacksOk)
    }

    @Test
    fun cancellingMidStreamChargesPartialUsage() {
        val hold = CountDownLatch(1)
        responders.add(sse(doc, hold = hold))
        val v = vision()
        val firstChunk = CountDownLatch(1)
        runBlocking {
            val job = async { v.analyze(image, deep, 9, "") {} }
            // wait until the request reached the server and the first chunk was sent
            withTimeout(10_000) { while (requests.isEmpty()) kotlinx.coroutines.delay(10) }
            kotlinx.coroutines.delay(300)
            job.cancel()
            try {
                job.await()
                error("must be cancelled")
            } catch (e: CancellationException) {
                firstChunk.countDown()
            }
        }
        hold.countDown()
        val row = Json.mapper.readTree(ledgerFile.readLines().single())
        assertEquals("steno-deep", row.path("purpose").asText())
        assertTrue(row.path("partial").asBoolean() && row.path("cancelled").asBoolean())
        assertEquals(900, row.path("input_tokens").asInt())                  // from message_start
    }

    @Test
    fun budgetStopsBeforeCalling() {
        ledger.dailyCapUsd = 0.00001
        try {
            runBlocking { vision().analyze(image, fast, 1, "") {} }
            error("must throw")
        } catch (e: BudgetExceeded) {
            assertTrue(requests.isEmpty())
        }
    }

    private fun geminiVision(apiKey: String = "AIzaSyTest") = GeminiVision(
        apiKey,
        "SYSTEM PROMPT",
        javaClass.classLoader.getResource("analysis_schema.json")!!.readText(),
        ledger,
        baseUrl = "http://127.0.0.1:${server.address.port}",
    )

    private fun geminiSse(itemsJson: String, answer: String, finish: String = "STOP"): (HttpExchange) -> Unit = { ex ->
        ex.responseHeaders.add("Content-Type", "text/event-stream")
        ex.sendResponseHeaders(200, 0)
        runCatching {
            ex.responseBody.use { out ->
                fun sendPart(text: String, thought: Boolean = false, finishReason: String? = null, usage: Boolean = false) {
                    val root = Json.mapper.createObjectNode()
                    val cand = root.putArray("candidates").addObject()
                    val part = cand.putObject("content").putArray("parts").addObject()
                    if (thought) part.put("thought", true)
                    part.put("text", text)
                    if (finishReason != null) cand.put("finishReason", finishReason)
                    if (usage) {
                        val u = root.putObject("usageMetadata")
                        u.put("promptTokenCount", 850)
                        u.put("candidatesTokenCount", 90)
                    }
                    val line = "data: " + Json.mapper.writeValueAsString(root) + "\n\n"
                    out.write(line.toByteArray())
                    out.flush()
                }
                sendPart("Denken...", thought = true)
                sendPart("""{"items": $itemsJson""")
                sendPart(""", "answer": "$answer"}""", finishReason = finish, usage = true)
            }
        }
    }

    @Test
    fun geminiModelDetectionAndNormalization() {
        assertTrue(isGeminiModel("gemini-2.5-flash"))
        assertTrue(isGeminiModel("models/gemini-2.5-pro"))
        assertTrue(isGeminiModel("google/gemini-2.0-flash"))
        assertTrue(isGeminiModel("neon"))
        assertTrue(isGeminiModel("argon"))
        assertTrue(isGeminiModel("carbon"))
        assertTrue(isGeminiModel("hydrogen"))
        assertTrue(isGeminiModel("gemini:custom-model"))
        assertFalse(isGeminiModel("claude-sonnet-5-5"))
        assertFalse(isGeminiModel("claude-opus-5-5"))
        assertEquals("models/gemini-2.5-flash", normalizeGeminiModel("gemini-2.5-flash"))
        assertEquals("models/my-model", normalizeGeminiModel("gemini:my-model"))
        assertEquals("models/gemini-4-neon", normalizeGeminiModel("neon"))
        assertEquals("models/gemini-4-argon", normalizeGeminiModel("argon"))
        assertEquals("models/gemini-4-carbon", normalizeGeminiModel("carbon"))
        assertEquals("models/gemini-4-hydrogen", normalizeGeminiModel("hydrogen"))
        assertEquals("models/gemini-2.5-flash", normalizeGeminiModel("flash"))
        assertEquals("models/gemini-2.5-pro", normalizeGeminiModel("pro"))
    }

    @Test
    fun geminiRequestShape() {
        val gv = geminiVision()
        responders.add(geminiSse("[]", "Kein Text"))
        val fastGemini = StageConfig(Stage.FAST, "gemini-2.5-flash", "low", "off", 4096)
        runBlocking {
            gv.analyze(image, fastGemini, 1, "Finde den Fehler", "Vorheriger Satz") {}
        }
        val (headers, req) = requests[0]
        assertEquals("AIzaSyTest", headers["x-goog-api-key"]?.single())
        assertEquals("SYSTEM PROMPT", req.path("system_instruction").path("parts")[0].path("text").asText())
        val parts = req.path("contents")[0].path("parts")
        assertEquals("image/png", parts[0].path("inline_data").path("mime_type").asText())
        assertTrue(parts[1].path("text").asText().contains("Finde den Fehler"))
        assertTrue(parts[1].path("text").asText().contains("Vorheriger Satz"))
        val schema = req.path("generationConfig").path("responseSchema")
        assertTrue(schema.path("additionalProperties").isMissingNode)
        assertEquals(4096, req.path("generationConfig").path("maxOutputTokens").asInt())
        assertEquals(0, req.path("generationConfig").path("thinkingConfig").path("thinkingBudget").asInt())

        val reqNeon = Json.mapper.readTree(gv.buildRequestBody(image, StageConfig(Stage.DEEP, "neon", "high", "adaptive", 16000), ""))
        assertEquals("high", reqNeon.path("generationConfig").path("thinkingConfig").path("thinkingLevel").asText())
    }

    @Test
    fun geminiStreamsMarksAndRecordsSpend() {
        val gv = geminiVision()
        val items = """[{"box":[10,10,60,50],"strokes":"Kürzel und","text":"und","alternatives":[],"kind":"word","confidence":0.95}]"""
        responders.add(geminiSse(items, "Alles richtig"))
        val fastGemini = StageConfig(Stage.FAST, "gemini-2.5-flash", "low", "off", 4096)
        val seen = ArrayList<Mark>()
        val result = runBlocking {
            gv.analyze(image, fastGemini, 12, "") { seen.add(it) }
        }
        assertEquals(listOf("und"), seen.map { it.text })
        assertEquals("Alles richtig", result.answer)
        assertNull(result.error)
        assertEquals("STOP", result.stopReason)
        assertTrue(result.costUsd > 0.0)
        val rows = ledgerFile.readLines().map { Json.mapper.readTree(it) }
        assertEquals(1, rows.size)
        assertEquals("steno-fast", rows[0].path("purpose").asText())
        assertEquals("gemini-2.5-flash", rows[0].path("model").asText())
        assertEquals(850, rows[0].path("input_tokens").asInt())
        assertEquals(90, rows[0].path("output_tokens").asInt())
    }

    @Test
    fun geminiErrorHandling() {
        val gv = geminiVision()
        responders.add { ex ->
            val body = """{"error":{"message":"API key not valid"}}""".toByteArray()
            ex.sendResponseHeaders(401, body.size.toLong())
            ex.responseBody.use { it.write(body) }
        }
        val fastGemini = StageConfig(Stage.FAST, "gemini-2.5-flash", "low", "off", 4096)
        val res = runBlocking { gv.analyze(image, fastGemini, 1, "") {} }
        assertTrue(res.error!!.contains("ungültig"))
    }

    @Test
    fun multiVisionRoutesCorrectly() {
        val calls = ArrayList<String>()
        val mockClaude = object : VisionBackend {
            override suspend fun analyze(image: PreparedImage, stage: StageConfig, frameId: Long, hint: String, context: String, onMark: (Mark) -> Unit): StenoResult {
                calls.add(stage.model)
                return StenoResult(frameId, stage.stage, stage.model, image.width, image.height, image.tokens)
            }
        }
        val mockGemini = object : VisionBackend {
            override suspend fun analyze(image: PreparedImage, stage: StageConfig, frameId: Long, hint: String, context: String, onMark: (Mark) -> Unit): StenoResult {
                calls.add(stage.model)
                return StenoResult(frameId, stage.stage, stage.model, image.width, image.height, image.tokens)
            }
        }
        val multi = MultiVision(mockClaude, mockGemini)
        runBlocking {
            multi.analyze(image, StageConfig(Stage.FAST, "claude-sonnet-5-5"), 1, "") {}
            multi.analyze(image, StageConfig(Stage.FAST, "gemini-2.5-flash"), 2, "") {}
            multi.analyze(image, StageConfig(Stage.DEEP, "neon"), 3, "") {}
        }
        assertEquals(listOf("claude-sonnet-5-5", "gemini-2.5-flash", "neon"), calls)
    }
}

