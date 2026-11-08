package io.github.erikiss.steno.core

import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.databind.ObjectMapper

/** Shared Jackson mapper (Jackson already ships with the Anthropic SDK). */
object Json {
    val mapper: ObjectMapper = ObjectMapper()
}

/**
 * Pull complete elements of the top-level `"items"` array out of a JSON document while it is still streaming in
 * (port of `factcheck/steno/jsonstream.py`). [feed] scans only new characters, tracks strings/escapes and nesting,
 * and returns each item the moment its closing brace arrives, so the overlay can draw the first word early.
 */
class ItemStream(private val arrayKey: String = "items") {
    private val buf = StringBuilder()
    private var scanned = 0
    private val stack = ArrayList<Char>()
    private var inString = false
    private var escape = false
    private var expectKey = false
    private var keyStart = -1
    private var lastKey: String? = null
    private var arrayDepth = NONE                 // stack size just inside the target array; NONE / CLOSED
    private var itemStart = -1

    val text: String get() = buf.toString()

    fun feed(chunk: String): List<JsonNode> {
        buf.append(chunk)
        val out = ArrayList<JsonNode>()
        for (i in scanned until buf.length) {
            val ch = buf[i]
            if (inString) {
                when {
                    escape -> escape = false
                    ch == '\\' -> escape = true
                    ch == '"' -> {
                        inString = false
                        if (keyStart >= 0) {
                            if (stack.size == 1) {
                                lastKey = runCatching { Json.mapper.readTree(buf.substring(keyStart, i + 1)).asText() }
                                    .getOrNull()
                            }
                            keyStart = -1
                        }
                    }
                }
                continue
            }
            when (ch) {
                '"' -> {
                    inString = true
                    if (stack.isNotEmpty() && stack.last() == '{' && expectKey) keyStart = i
                }
                '{' -> {
                    if (arrayDepth >= 0 && stack.size == arrayDepth && itemStart < 0) itemStart = i
                    stack.add('{')
                    expectKey = true
                }
                '[' -> {
                    val isTarget = arrayDepth == NONE && stack.size == 1 && stack[0] == '{' && lastKey == arrayKey
                    stack.add('[')
                    if (isTarget) arrayDepth = stack.size
                    expectKey = false
                }
                '}', ']' -> {
                    if (stack.isNotEmpty()) stack.removeAt(stack.size - 1)
                    if (ch == '}' && itemStart >= 0 && stack.size == arrayDepth) {
                        val node = runCatching { Json.mapper.readTree(buf.substring(itemStart, i + 1)) }.getOrNull()
                        if (node != null && node.isObject) out.add(node)
                        itemStart = -1
                    }
                    if (ch == ']' && arrayDepth >= 0 && stack.size == arrayDepth - 1) arrayDepth = CLOSED
                    expectKey = false
                }
                ',' -> expectKey = stack.isNotEmpty() && stack.last() == '{'
                ':' -> expectKey = false
            }
        }
        scanned = buf.length
        return out
    }

    /** The whole document, or null if it is not (yet) a valid JSON object. */
    fun final(): JsonNode? =
        runCatching { Json.mapper.readTree(buf.toString()) }.getOrNull()?.takeIf { it.isObject }

    private companion object {
        const val NONE = -2
        const val CLOSED = -1
    }
}
