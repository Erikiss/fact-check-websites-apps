package io.github.erikiss.steno.core

/**
 * Builds the same prompts as `factcheck/steno/prompts.py` from the same files (`system_prompt.md`, `dek.md`), which
 * the app bundles as assets: the static system prompt is cached by the API, everything per-frame goes into the
 * short user text.
 */
object Prompts {
    private val COMMENT = Regex("<!--.*?-->", RegexOption.DOT_MATCHES_ALL)

    /** Knowledge text without the editor notes (HTML comments). */
    fun cleanKnowledge(markdown: String): String = markdown.replace(COMMENT, "").trim()

    fun buildSystemPrompt(template: String, knowledge: String): String =
        template.trim().replace("{knowledge}", cleanKnowledge(knowledge))

    fun buildUserText(width: Int, height: Int, hint: String = ""): String {
        val base = "Bildgröße: ${width}×${height} Pixel."
        return if (hint.isBlank()) base else "$base\nHinweis des Nutzers: ${hint.trim()}"
    }
}
