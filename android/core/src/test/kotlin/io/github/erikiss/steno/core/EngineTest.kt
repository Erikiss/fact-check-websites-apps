package io.github.erikiss.steno.core

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** LiveEngine scheduling with virtual time: event order, newest-wins, deep cancellation, budget errors. */
class EngineTest {
    private val stages = mapOf(Stage.FAST to StageConfig(Stage.FAST, "claude-haiku-5-5"), Stage.DEEP to StageConfig(Stage.DEEP, "claude-opus-5-5"))

    private class Scripted(val fastMs: Long = 100, val budgetError: Boolean = false) : VisionBackend {
        val calls = ArrayList<Pair<Stage, Long>>()
        override suspend fun analyze(image: PreparedImage, stage: StageConfig, frameId: Long, hint: String, onMark: (Mark) -> Unit): StenoResult {
            calls.add(stage.stage to frameId)
            if (budgetError) throw BudgetExceeded("Tagesbudget erreicht")
            if (stage.stage == Stage.DEEP) awaitCancellation()
            delay(fastMs)
            onMark(Mark(MarkKind.WORD, Box(1f, 1f, 9f, 9f), "f$frameId"))
            onMark(Mark(MarkKind.WORD, Box(11f, 1f, 19f, 9f), "g$frameId"))
            return StenoResult(frameId, stage.stage, stage.model, image.width, image.height, image.tokens).also {
                it.marks.add(Mark(MarkKind.WORD, text = "f$frameId"))
            }
        }
    }

    private fun TestScope.engine(backend: VisionBackend, events: MutableList<EngineEvent>, maxInflight: Int = 2): Pair<LiveEngine<String>, CoroutineScope> {
        val dispatcher = StandardTestDispatcher(testScheduler)
        val scope = CoroutineScope(dispatcher + SupervisorJob())
        val engine = LiveEngine<String>(scope, backend, stages, prepare = { PreparedImage(ByteArray(0), 120, 40, 240, 80) },
            hint = { "Hinweis" }, onEvent = { events.add(it) }, maxInflightFast = maxInflight, prepareDispatcher = dispatcher)
        return engine to scope
    }

    @Test
    fun eventsArriveInOrderMarksBeforeDone() = runTest {
        val events = ArrayList<EngineEvent>()
        val (engine, scope) = engine(Scripted(), events)
        engine.submit(Frame(1, "pic"), Stage.FAST)
        advanceUntilIdle()
        assertEquals(listOf("Started", "MarkAdded", "MarkAdded", "Done"), events.map { it::class.simpleName })
        val started = events[0] as EngineEvent.Started
        assertEquals(120 to 40, started.imageWidth to started.imageHeight)
        scope.cancel()
    }

    @Test
    fun newContentCancelsRunningDeepLook() = runTest {
        val events = ArrayList<EngineEvent>()
        val (engine, scope) = engine(Scripted(), events)
        engine.submit(Frame(1, "pic"), Stage.DEEP)
        advanceUntilIdle()
        engine.submit(Frame(2, "pic"), Stage.FAST, cancelDeep = true)
        advanceUntilIdle()
        val cancelled = events.filterIsInstance<EngineEvent.Cancelled>().single()
        assertEquals(Stage.DEEP to 1L, cancelled.stage to cancelled.frameId)
        assertEquals(2L, events.filterIsInstance<EngineEvent.Done>().single().frameId)
        scope.cancel()
    }

    @Test
    fun waitingFastFrameIsReplacedByNewerOne() = runTest {
        val events = ArrayList<EngineEvent>()
        val backend = Scripted(fastMs = 300)
        val (engine, scope) = engine(backend, events, maxInflight = 1)
        for (id in 1L..3L) engine.submit(Frame(id, "pic"), Stage.FAST)
        advanceUntilIdle()
        assertEquals(listOf(1L, 3L), events.filterIsInstance<EngineEvent.Done>().map { it.frameId })
        assertEquals(listOf(Stage.FAST to 1L, Stage.FAST to 3L), backend.calls)
        scope.cancel()
    }

    @Test
    fun budgetErrorsAreFlaggedAndUnknownStagesReported() = runTest {
        val events = ArrayList<EngineEvent>()
        val (engine, scope) = engine(Scripted(budgetError = true), events)
        engine.submit(Frame(1, "pic"), Stage.FAST)
        advanceUntilIdle()
        val failed = events.filterIsInstance<EngineEvent.Failed>().single()
        assertTrue(failed.budget && "Tagesbudget" in failed.message)
        scope.cancel()

        val events2 = ArrayList<EngineEvent>()
        val dispatcher = StandardTestDispatcher(testScheduler)
        val scope2 = CoroutineScope(dispatcher + SupervisorJob())
        val fastOnly = LiveEngine<String>(scope2, Scripted(), mapOf(Stage.FAST to stages.getValue(Stage.FAST)),
            prepare = { PreparedImage(ByteArray(0), 1, 1, 1, 1) }, hint = { "" }, onEvent = { events2.add(it) }, prepareDispatcher = dispatcher)
        fastOnly.submit(Frame(1, "pic"), Stage.DEEP)
        advanceUntilIdle()
        assertTrue((events2.single() as EngineEvent.Failed).message.contains("nicht eingerichtet"))
        scope2.cancel()
    }
}
