package io.github.erikiss.steno.core

/**
 * Builds the same prompts as `factcheck/steno/prompts.py` from the same files (`system_prompt.md`, `dek.md`), which
 * the app bundles as assets: the static system prompt is cached by the API, everything per-frame goes into the
 * short user text.
 */
object Prompts {
    const val MAX_CONTEXT_CHARS = 160
    private val COMMENT = Regex("<!--.*?-->", RegexOption.DOT_MATCHES_ALL)
    private val WHITESPACE = Regex("\\s+")

    /** Knowledge text without the editor notes (HTML comments). */
    fun cleanKnowledge(markdown: String): String = markdown.replace(COMMENT, "").trim()

    fun buildSystemPrompt(template: String, knowledge: String): String =
        template.trim().replace("{knowledge}", cleanKnowledge(knowledge))

    fun buildUserText(width: Int, height: Int, hint: String = "", context: String = ""): String {
        val sb = StringBuilder("Bildgröße: ${width}×${height} Pixel.")
        if (hint.isNotBlank()) sb.append("\nHinweis des Nutzers: ").append(hint.trim())
        val cleaned = context.trim().replace(WHITESPACE, " ")
        if (cleaned.isNotEmpty()) {
            val ctx = if (cleaned.length > MAX_CONTEXT_CHARS) {
                cleaned.substring(0, MAX_CONTEXT_CHARS - 1).trimEnd() + "…"
            } else {
                cleaned
            }
            sb.append("\nVorheriger Bildausschnitt (nur Themenkontext, nicht blind übernehmen): ").append(ctx)
        }
        return sb.toString()
    }
}
