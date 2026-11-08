package io.github.erikiss.steno.core

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** A captured picture; [image] is platform-specific (an Android Bitmap in the app). */
class Frame<T>(val id: Long, val image: T)

sealed interface EngineEvent {
    val frameId: Long
    val stage: Stage

    data class Started(override val frameId: Long, override val stage: Stage, val model: String, val imageWidth: Int, val imageHeight: Int) : EngineEvent
    data class MarkAdded(override val frameId: Long, override val stage: Stage, val mark: Mark) : EngineEvent
    data class Done(override val frameId: Long, override val stage: Stage, val result: StenoResult) : EngineEvent
    data class Failed(override val frameId: Long, override val stage: Stage, val message: String, val budget: Boolean) : EngineEvent
    data class Cancelled(override val frameId: Long, override val stage: Stage) : EngineEvent
}

/**
 * Schedules analyses (port of `factcheck/steno/engine.py`): at most [maxInflightFast] fast calls, where a newer
 * waiting frame replaces an older one; a new deep request cancels the running one; fresh content can cancel a deep
 * look at the old picture. All state lives on [scope]'s (single-threaded) dispatcher, and every event is delivered
 * there in order, so the app can touch its views directly in [onEvent].
 */
class LiveEngine<T>(
    private val scope: CoroutineScope,
    private val backend: VisionBackend,
    private val stages: Map<Stage, StageConfig>,
    private val prepare: (T) -> PreparedImage,
    private val hint: () -> String,
    private val onEvent: (EngineEvent) -> Unit,
    private val maxInflightFast: Int = 2,
    private val prepareDispatcher: CoroutineDispatcher = Dispatchers.Default,
) {
    private val running = mapOf(Stage.FAST to LinkedHashMap<Job, Long>(), Stage.DEEP to LinkedHashMap<Job, Long>())
    private var pendingFast: Frame<T>? = null

    fun hasStage(stage: Stage) = stage in stages

    /** Queue an analysis (any thread). `cancelDeep = true` when the picture changed. */
    fun submit(frame: Frame<T>, stage: Stage, cancelDeep: Boolean = false) {
        scope.launch { submitNow(frame, stage, cancelDeep) }
    }

    fun cancel(stage: Stage) {
        scope.launch { cancelNow(stage) }
    }

    private fun emit(event: EngineEvent) {
        scope.launch { onEvent(event) }                         // one queue: marks always arrive before their Done
    }

    private fun submitNow(frame: Frame<T>, stage: Stage, cancelDeep: Boolean) {
        if (stage !in stages) {
            emit(EngineEvent.Failed(frame.id, stage, "Stufe '${stage.id}' ist nicht eingerichtet", budget = false))
            return
        }
        if (stage == Stage.DEEP) {
            cancelNow(Stage.DEEP)                               // only the newest picture deserves the slow look
            start(frame, Stage.DEEP)
            return
        }
        if (cancelDeep) cancelNow(Stage.DEEP)
        if (running.getValue(Stage.FAST).size < maxInflightFast) start(frame, Stage.FAST) else pendingFast = frame
    }

    private fun cancelNow(stage: Stage) {
        running.getValue(stage).keys.toList().forEach { it.cancel() }
        if (stage == Stage.FAST) pendingFast = null
    }

    private fun start(frame: Frame<T>, stage: Stage) {
        val job = scope.launch { run(frame, stage) }
        running.getValue(stage)[job] = frame.id
    }

    private suspend fun run(frame: Frame<T>, stage: Stage) {
        val cfg = stages.getValue(stage)
        try {
            val prepared = withContext(prepareDispatcher) { prepare(frame.image) }
            emit(EngineEvent.Started(frame.id, stage, cfg.model, prepared.width, prepared.height))
            val result = backend.analyze(prepared, cfg, frame.id, hint()) { mark ->
                emit(EngineEvent.MarkAdded(frame.id, stage, mark))
            }
            emit(EngineEvent.Done(frame.id, stage, result))
        } catch (e: CancellationException) {
            emit(EngineEvent.Cancelled(frame.id, stage))
            throw e
        } catch (e: BudgetExceeded) {
            emit(EngineEvent.Failed(frame.id, stage, e.message ?: "Budget erreicht", budget = true))
        } catch (e: Exception) {                                // never let one frame kill the live loop
            emit(EngineEvent.Failed(frame.id, stage, "${e::class.simpleName}: ${e.message}".take(240), budget = false))
        } finally {
            running.getValue(stage).remove(currentCoroutineContext().job)
            if (stage == Stage.FAST) {
                val next = pendingFast
                if (next != null && running.getValue(Stage.FAST).size < maxInflightFast) {
                    pendingFast = null
                    start(next, Stage.FAST)
                }
            }
        }
    }
}
