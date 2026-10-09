package com.pedrolopes.ankigen

import org.json.JSONArray
import org.json.JSONObject
import org.snakeyaml.engine.v2.api.Load
import org.snakeyaml.engine.v2.api.LoadSettings
import java.time.LocalDate
import java.time.ZoneId
import java.time.temporal.ChronoUnit

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
    /** The pipeline's name when it ran. */
    val name = json.str("name")
    val conclusion = json.str("conclusion") ?: "unknown"
    val ok get() = conclusion == "success"
    val day = json.str("curriculum_date")
    val trigger = json.str("trigger") ?: "manual"
    val attempt = json.optInt("attempt", 1)
    val finishedAt = json.str("finished_at")
    val url = json.str("run_url")
    val runId = json.str("run_id")
    val artifact = json.str("artifact")
    val error = json.str("error")
    val llmCalls = json.optInt("llm_calls")
    private val cards = json.obj("cards")
    val kept = cards?.optInt("kept") ?: 0
    val dropped = cards?.optInt("dropped") ?: 0
    val byDeck = cards?.objects("by_deck").orEmpty()
    val guide = json.obj("guide")
    val hasPdf get() = guide?.optBoolean("pdf") == true && runId != null && artifact != null
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
    val start = json.str("start")?.let(LocalDate::parse)
    val lastDay = json.str("last_day")?.let(LocalDate::parse)
    val quota = json.optInt("daily_quota")
    val topics = json.objects("topics").map(::Topic)
}

/**
 * The curriculum is a queue, not a calendar: each run writes the day after
 * the last one written, so a run by hand moves the whole plan forward and
 * its dates stop being the days they are written on. Days are numbered from
 * the first deck's start instead.
 */
class Curriculum(json: JSONObject) {
    val nextDay = json.str("next_day")?.let(LocalDate::parse)
    val decks = json.objects("decks").map(::DeckPlan)
    private val first = decks.mapNotNull { it.start }.minOrNull()
    val totalDays = decks.mapNotNull { it.lastDay }.maxOrNull()?.let(::number)

    /** Day 1 is the first deck's start; null without dates, or for a day before the plan began. */
    fun number(day: LocalDate?): Int? =
        if (day == null || first == null) null
        else (ChronoUnit.DAYS.between(first, day).toInt() + 1).takeIf { it >= 1 }

    fun number(day: String?): Int? = number(day?.let { runCatching { LocalDate.parse(it) }.getOrNull() })

    /** How many runs from now [day] is written: 0 for the next run. */
    fun runsUntil(day: LocalDate): Long? = nextDay?.let { ChronoUnit.DAYS.between(it, day) }

    fun topicsOn(day: LocalDate?): List<Topic> =
        decks.flatMap { d -> d.topics.filter { it.day == day?.toString() } }
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

/** One request to Claude and its reply: a commit edit-plan.yml made on a draft. */
class Round(val asked: String, val reply: String) {
    companion object {
        /** "plan: …\n\nAsked:\n<request>\n\nClaude:\n<reply>", or null for any other commit. */
        fun parse(message: String): Round? {
            val body = message.substringAfter("\n\nAsked:\n", "").ifEmpty { return null }
            return Round(body.substringBefore("\n\nClaude:\n").trim(), body.substringAfter("\n\nClaude:\n", "").trim())
        }
    }
}

/**
 * A plan change Claude is drafting on plan/<pipeline>/<when>, which [base]
 * gets when it is applied. [run] is the latest edit-plan run for it.
 */
class Draft(
    val pipeline: String,
    val branch: String,
    val base: String,
    val isNew: Boolean,
    val rounds: List<Round>,
    /** File name to unified diff. */
    val files: List<Pair<String, String>>,
    val run: JSONObject?,
    private val lastCommitAt: String?,
) {
    val working get() = run != null && run.optString("status") != "completed"

    /** The latest run failed before it could record a round. */
    val failed get() = run != null && !working && run.optString("conclusion") != "success" &&
        (lastCommitAt == null || run.optString("updated_at") > lastCommitAt)

    companion object {
        fun branchFor(pipeline: String, now: java.time.LocalDateTime = java.time.LocalDateTime.now()) =
            "plan/$pipeline/" + now.format(java.time.format.DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss"))

        fun pipelineOf(branch: String) = branch.removePrefix("plan/").substringBefore('/')

        /** A pipeline id from its name: "Inglês B1" is "ingles-b1". */
        fun idFor(name: String) = java.text.Normalizer.normalize(name.lowercase(), java.text.Normalizer.Form.NFD)
            .replace(Regex("\\p{M}"), "").replace(Regex("[^a-z0-9]+"), "-").trim('-').take(40).trimEnd('-')
    }
}

/** A pipeline as the home screen lists it. */
class Pipeline(val id: String, val spec: Spec?, val specError: String?, val latest: Run?)
