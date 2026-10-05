package com.pedrolopes.ankigen

import org.json.JSONArray
import org.json.JSONObject
import org.snakeyaml.engine.v2.api.Load
import org.snakeyaml.engine.v2.api.LoadSettings
import java.time.ZoneId

/**
 * pipelines/<id>/pipeline.yaml. Its fields are fixed (the schema forbids any
 * others), so it is written from a template rather than a YAML library; the
 * models are carried through as read, since the app does not edit them.
 */
data class Spec(
    val name: String,
    val enabled: Boolean,
    val cron: String,
    val timezone: String,
    val guidePdf: Boolean,
    val push: Boolean,
    val maxCalls: Int,
    val models: Map<String, Any?>,
) {
    /** What the tick would refuse, checked before committing. */
    fun problems(): List<String> = listOfNotNull(
        "A pipeline needs a name.".takeIf { name.isBlank() },
        Cron.problem(cron),
        "Unknown time zone \"$timezone\".".takeIf { timezone !in ZoneId.getAvailableZoneIds() },
        "The call budget is 0 or more.".takeIf { maxCalls < 0 },
    )

    fun toYaml(): String = buildString {
        appendLine("# How this pipeline runs. Its curriculum is in profile.yaml beside it.")
        appendLine("# Checked against pipelines/schema.json; saved by the AnkiGen app.")
        appendLine("name: ${quote(name)}")
        appendLine("enabled: $enabled")
        appendLine("schedule:")
        appendLine("  cron: ${quote(cron.trim().split(Regex("\\s+")).joinToString(" "))}")
        appendLine("  timezone: ${quote(timezone)}")
        appendLine("outputs:")
        appendLine("  guide_pdf: $guidePdf")
        appendLine("  push_to_ankiweb: $push")
        if (models.isNotEmpty()) {
            appendLine("models:")
            models.forEach { (key, value) ->
                val text = when (value) {
                    is List<*> -> value.joinToString(", ", "[", "]") { quote(it.toString()) }
                    else -> quote(value?.toString().orEmpty())
                }
                appendLine("  $key: $text")
            }
        }
        appendLine("budget:")
        appendLine("  # 0: no cap.")
        appendLine("  max_llm_calls_per_run: $maxCalls")
    }

    companion object {
        fun parse(yaml: String): Spec {
            val root = Load(LoadSettings.builder().build()).loadFromString(yaml) as? Map<*, *>
                ?: error("pipeline.yaml is not a mapping")
            fun section(key: String) = root[key] as? Map<*, *> ?: emptyMap<Any, Any>()
            val schedule = section("schedule")
            val outputs = section("outputs")
            return Spec(
                name = root["name"]?.toString().orEmpty(),
                enabled = root["enabled"] as? Boolean ?: true,
                cron = schedule["cron"]?.toString().orEmpty(),
                timezone = schedule["timezone"]?.toString() ?: "UTC",
                guidePdf = outputs["guide_pdf"] as? Boolean ?: true,
                push = outputs["push_to_ankiweb"] as? Boolean ?: true,
                maxCalls = (section("budget")["max_llm_calls_per_run"] as? Number)?.toInt() ?: 0,
                models = section("models").entries.associate { it.key.toString() to it.value },
            )
        }

        // A YAML double-quoted scalar, which reads the same in YAML 1.1 (PyYAML) and 1.2.
        private fun quote(s: String) = "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\""
    }
}

/** org.json reads a JSON null as the string "null"; these do not. */
fun JSONObject.str(key: String): String? = if (isNull(key)) null else optString(key)
fun JSONObject.obj(key: String): JSONObject? = if (isNull(key)) null else optJSONObject(key)
fun JSONObject.objects(key: String): List<JSONObject> =
    (optJSONArray(key) ?: JSONArray()).let { a -> (0 until a.length()).map { a.getJSONObject(it) } }

/** One run, as `ankigen pipelines record` writes it to the status branch. */
class Run(val json: JSONObject) {
    val conclusion = json.str("conclusion") ?: "unknown"
    val ok get() = conclusion == "success"
    val day = json.str("curriculum_date")
    val trigger = json.str("trigger") ?: "manual"
    val attempt = json.optInt("attempt", 1)
    val finishedAt = json.str("finished_at")
    val url = json.str("run_url")
    val error = json.str("error")
    val llmCalls = json.optInt("llm_calls")
    private val cards = json.obj("cards")
    val kept = cards?.optInt("kept") ?: 0
    val dropped = cards?.optInt("dropped") ?: 0
    val byDeck = cards?.objects("by_deck").orEmpty()
    val guide = json.obj("guide")
    val stages = json.objects("stages")
}

class Topic(json: JSONObject) {
    val topic = json.optString("topic")
    val kind = json.str("kind")
    val day = json.str("day")
    val status = json.optString("status")      // done, next, upcoming or rotating
}

class DeckPlan(json: JSONObject) {
    val deck = json.optString("deck")
    val quota = json.optInt("daily_quota")
    val topics = json.objects("topics").map(::Topic)
}

class Curriculum(json: JSONObject) {
    val nextDay = json.str("next_day")
    val decks = json.objects("decks").map(::DeckPlan)
}

class Card(json: JSONObject) {
    val deck = json.optString("deck")
    val type = json.optString("card_type")
    val front = json.optString("front")
    val back = json.optString("back")
    val topic = json.str("topic")
    val guideRef = json.str("guide_ref")
    val picture = json.str("picture")
    val unverified = json.optBoolean("unverified")
}

/** A pipeline as the home screen lists it. */
class Pipeline(val id: String, val spec: Spec?, val specError: String?, val latest: Run?)
