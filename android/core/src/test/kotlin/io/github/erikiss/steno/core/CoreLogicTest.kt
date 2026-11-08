package io.github.erikiss.steno.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.time.LocalDate
import kotlin.random.Random

/** Same cases as tests/test_steno_core.py, so the Kotlin port behaves like the tested Python original. */
class CoreLogicTest {
    // ------------------------------------------------------------------ image math
    @Test
    fun resizeMatchesVisionDocsExamples() {
        assertEquals(924 to 1307, ImageMath.resizedSize(1075, 1520))
        assertEquals(1456 to 819, ImageMath.resizedSize(1920, 1080))
        assertEquals(1920 to 1080, ImageMath.resizedSize(1920, 1080, ImageMath.HIGH_RES_MAX_EDGE, ImageMath.HIGH_RES_MAX_TOKENS))
        assertEquals(1000 to 1000, ImageMath.resizedSize(1000, 1000))
        assertEquals(1296, ImageMath.countImageTokens(1000, 1000))
    }

    // ------------------------------------------------------------------ item stream
    private val doc = """{"items": [
        {"box": [1, 2, 30, 40], "text": "Haus {mit} \"Klammern\" ]", "alternatives": ["Hans"], "kind": "error",
         "issue": "Verstärkung fehlt", "intended": "Haus", "fix": "a verstärken", "spot": [5, 6], "confidence": 0.4},
        {"box": [40, 2, 90, 40], "text": "und", "alternatives": [], "kind": "word", "issue": "", "intended": "",
         "fix": "", "spot": [], "confidence": 0.9}
    ], "answer": "Im ersten Wort fehlt die Verstärkung."}"""

    private fun itemsFrom(chunks: List<String>): List<String> {
        val s = ItemStream()
        val got = chunks.flatMap { s.feed(it) }.map { it.path("text").asText() }
        assertEquals("Im ersten Wort fehlt die Verstärkung.", s.final()!!.path("answer").asText())
        return got
    }

    @Test
    fun itemStreamYieldsEachItemOnceInAnyChunking() {
        val expected = listOf("Haus {mit} \"Klammern\" ]", "und")
        assertEquals(expected, itemsFrom(doc.map { it.toString() }))
        assertEquals(expected, itemsFrom(listOf(doc)))
        val rng = Random(7)
        val random = ArrayList<String>()
        var i = 0
        while (i < doc.length) { val n = rng.nextInt(1, 10); random.add(doc.substring(i, minOf(doc.length, i + n))); i += n }
        assertEquals(expected, itemsFrom(random))
    }

    @Test
    fun itemStreamIgnoresOtherArraysAndPartialItems() {
        val s = ItemStream()
        assertTrue(s.feed("""{"other": [{"a": 1}], "items": [{"text": "x", "box": [1,2""").isEmpty())
        val done = s.feed(""",3,4]}, {"te""")
        assertEquals(1, done.size)
        assertEquals("x", done[0].path("text").asText())
        assertNull(s.final())
    }

    // ------------------------------------------------------------------ marks
    @Test
    fun markFromJsonCleansModelOutput() {
        fun parse(json: String, w: Int = 100, h: Int = 60) = Mark.fromJson(Json.mapper.readTree(json), w, h)
        val m = parse("""{"box": [50, 80, 10, -5], "text": " Haus ", "alternatives": ["Haus", "Hans", " "], "kind": "error", "confidence": 3, "spot": [500, 10]}""")!!
        assertEquals(Box(10f, 0f, 50f, 60f), m.box)
        assertEquals(listOf("Hans"), m.alternatives)
        assertEquals(1f, m.confidence)
        assertEquals(100f to 10f, m.spot)
        assertNull(parse("""{"box": [5, 5, 5, 9], "text": "x", "kind": "word"}""")!!.box)
        assertNull(parse("""{"box": [], "text": "", "kind": "weird"}"""))
        assertEquals(MarkKind.NOTE, parse("""{"text": "Aufgabe: Fehler finden", "kind": "weird"}""")!!.kind)
        assertEquals("", parse("""{"text": null, "issue": "fehlt", "kind": "error"}""")!!.text)
    }

    // ------------------------------------------------------------------ change gate
    private fun frame(vararg blocks: Pair<Int, Int>): Signature {
        val px = FloatArray(90 * 160) { 255f }
        for ((r, c) in blocks) for (y in r until r + 10) for (x in c until c + 10) px[y * 160 + x] = 0f
        return Signature(160, 90, px)
    }

    private fun run(gate: ChangeGate, frames: List<Signature>, dt: Double = 0.25) = frames.mapIndexed { i, f -> gate.update(f, i * dt) }

    @Test
    fun signatureDownscalesAndDetectsChanges() {
        val big = Signature.downscale(FloatArray(900 * 1600) { 255f }, 1600, 900, 160)
        assertEquals(160, big.width)
        assertEquals(90, big.height)
        assertEquals(100.0 / (90 * 160), changedFraction(frame(), frame(10 to 10)), 1e-9)
    }

    @Test
    fun gateFastAfterSettleThenOneDeepThenQuiet() {
        val out = run(ChangeGate(GateConfig(settleS = 0.45, deepAfterS = 2.5)), List(16) { frame(10 to 10) })
        assertEquals(listOf(Trigger.NONE, Trigger.NONE, Trigger.FAST), out.take(3))
        assertEquals(1, out.count { it == Trigger.FAST })
        assertEquals(1, out.count { it == Trigger.DEEP })
        assertEquals(10, out.indexOf(Trigger.DEEP))
    }

    @Test
    fun gateStaysLiveWhileDrawingAndBacksOff() {
        val drawing = List(80) { i -> frame((i % 8) * 10 to (i * 13) % 150) }
        val out = run(ChangeGate(GateConfig(maxWaitS = 2.0, minIntervalS = 0.8, deepAfterS = 0.0)), drawing)
        val times = out.withIndex().filter { it.value == Trigger.FAST }.map { it.index * 0.25 }
        assertEquals(2.0, times[0], 1e-9)
        val gaps = times.zipWithNext { a, b -> b - a }
        assertTrue(gaps[0] in 4.0..4.5 && gaps[1] in 8.0..8.5)
        assertFalse(Trigger.DEEP in out)
    }

    @Test
    fun gateRespectsMinIntervalIgnoresSmallChangesAndBlankBoards() {
        val gate = ChangeGate(GateConfig(settleS = 0.0, minIntervalS = 1.0, deepAfterS = 0.0))
        assertEquals(Trigger.FAST, gate.update(frame(0 to 0), 0.0))
        assertEquals(Trigger.NONE, gate.update(frame(0 to 0, 50 to 50), 0.5))
        assertEquals(Trigger.FAST, gate.update(frame(0 to 0, 50 to 50), 1.0))
        val tiny = frame(0 to 0, 50 to 50).also { it.pixels[100] = 0f }
        assertEquals(Trigger.NONE, gate.update(tiny, 3.0))

        val blank = ChangeGate(GateConfig(settleS = 0.0, minIntervalS = 0.0, deepAfterS = 0.5))
        assertTrue(run(blank, List(8) { frame() }).all { it == Trigger.NONE })
        assertEquals(Trigger.NONE, blank.update(frame(30 to 30), 3.0))
        assertEquals(Trigger.FAST, blank.update(frame(30 to 30), 3.25))
    }

    @Test
    fun gateRebaseAcceptsOverlayRepaint() {
        val gate = ChangeGate(GateConfig(settleS = 0.0, minIntervalS = 0.0, deepAfterS = 0.0))
        assertEquals(Trigger.FAST, gate.update(frame(0 to 0), 0.0))
        gate.rebaseNext()
        val withOverlay = frame(0 to 0, 40 to 40, 60 to 100)
        assertEquals(Trigger.NONE, gate.update(withOverlay, 0.5))
        assertEquals(Trigger.NONE, gate.update(withOverlay, 1.0))
    }

    // ------------------------------------------------------------------ layout
    @Test
    fun labelsAvoidOverlapsAndStayInside() {
        val anchors = listOf(Rect(100f, 100f, 60f, 40f), Rect(170f, 100f, 60f, 40f), Rect(240f, 100f, 60f, 40f))
        val sizes = List(3) { 120f to 30f }
        val bounds = Rect(0f, 0f, 600f, 400f)
        val rects = Layout.placeLabels(anchors, sizes, bounds).zip(sizes) { (x, y), (w, h) -> Rect(x, y, w, h) }
        assertTrue(rects.all { it.inside(bounds) })
        for ((i, r) in rects.withIndex()) {
            assertTrue(rects.withIndex().none { (j, o) -> j != i && r.intersects(o) })
            assertTrue(anchors.none { r.intersects(it) })
        }
    }

    @Test
    fun labelLinesMatchTheDesktopTool() {
        val err = Mark(MarkKind.ERROR, Box(1f, 2f, 3f, 4f), "Hans", listOf("Hanse"), "Verbindung zu weit", "Haus", "eng anschließen", confidence = 0.4f)
        assertEquals(listOf("✗ Hans → Haus · 40%", "Verbindung zu weit", "richtig: eng anschließen", "oder: Hanse"), Layout.labelLines(err))
        assertEquals(listOf("und"), Layout.labelLines(Mark(MarkKind.WORD, text = "und", confidence = 0.9f)))
    }

    // ------------------------------------------------------------------ prompts (shared files)
    private fun resource(name: String) = javaClass.classLoader.getResource(name)!!.readText()

    @Test
    fun systemPromptIsBuiltFromTheSharedFiles() {
        val prompt = Prompts.buildSystemPrompt(resource("system_prompt.md"), resource("dek.md"))
        assertTrue(prompt.startsWith("Du bist ein Live-Assistent für deutsche Kurzschrift"))
        assertTrue("# DEK-Wissen" in prompt && "Verstärkung" in prompt && "[x1, y1, x2, y2]" in prompt)
        assertFalse("{knowledge}" in prompt || "<!--" in prompt || "STENO_KNOWLEDGE_FILE" in prompt)
        assertEquals(prompt, Prompts.buildSystemPrompt(resource("system_prompt.md"), resource("dek.md")))
        assertEquals("Bildgröße: 640×480 Pixel.", Prompts.buildUserText(640, 480))
        assertEquals("Bildgröße: 640×480 Pixel.\nHinweis des Nutzers: Finde den Fehler", Prompts.buildUserText(640, 480, " Finde den Fehler "))
        val schema = Json.mapper.readTree(resource("analysis_schema.json"))
        assertEquals("object", schema.path("type").asText())
        assertFalse(listOf("minimum", "maximum", "minItems", "maxItems").any { it in schema.toString() })
    }

    // ------------------------------------------------------------------ ledger
    @Test
    fun ledgerEnforcesCapsAndPersists() {
        val file = File(Files.createTempDirectory("ledger").toFile(), "spend.jsonl")
        val ledger = SpendLedger(file, totalCapUsd = 100.0, dailyCapUsd = 1.0, deepShare = 0.5, today = { LocalDate.now() })
        val cost = ledger.record(Stage.FAST, "claude-haiku-5-5", UsageNumbers(input = 1_000_000, output = 1_000_000))
        assertEquals(0.60, cost, 1e-9)                                  // $0.10 + $0.50 per MTok
        try {
            ledger.reserve(0.05, Stage.DEEP)                            // 0.60 + 0.05 > 1.0 * 0.5
            error("deep share must stop the deep stage")
        } catch (e: BudgetExceeded) {
            assertTrue(e.message!!.startsWith("Deep-Anteil"))
        }
        ledger.reserve(0.3, Stage.FAST)                                 // 0.60 + 0.3 <= 1.0
        try {
            ledger.reserve(0.2, Stage.FAST)                             // the in-flight reservation counts too
            error("daily cap must hold")
        } catch (e: BudgetExceeded) {
            assertTrue(e.message!!.startsWith("Tagesbudget"))
        }
        ledger.release(0.3)
        val reloaded = SpendLedger(file, 0.5, 10.0)
        assertEquals(0.60, reloaded.spentToday(), 1e-9)
        try {
            reloaded.reserve(0.01, Stage.FAST)                          // total cap 0.5 already exceeded
            error("total cap must hold")
        } catch (e: BudgetExceeded) {
            assertTrue(e.message!!.startsWith("Gesamtbudget"))
        }
        assertTrue(file.readText().contains("\"purpose\":\"steno-fast\""))
    }

    @Test
    fun inkBoxesFindSeparateWords() {
        val w = 240
        val h = 90
        val gray = FloatArray(w * h) { 255f }
        for (y in 20..70) { for (x in 30..35) gray[y * w + x] = 0f; for (x in 150..156) gray[y * w + x] = 0f }
        val boxes = InkBoxes.find(gray, w, h)
        assertEquals(2, boxes.size)
        assertEquals(Box(30f, 20f, 36f, 71f), boxes[0])
    }
}
