package com.pedrolopes.ankigen

import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.temporal.ChronoUnit

/**
 * A five-field cron, read the way the tick reads pipeline.yaml's (cronsim):
 * in the pipeline's own time zone, and with day of month and day of week
 * both set, either one matching is enough.
 *
 * ponytail: numbers, *, lists, ranges and steps only; names like MON or JAN
 * are refused here though cronsim takes them.
 */
class Cron(expr: String) {
    private val fields: List<Set<Int>>
    private val anyDay: Boolean
    private val anyWeekday: Boolean

    init {
        val parts = expr.trim().split(Regex("\\s+"))
        require(parts.size == 5) { "A cron has five fields: minute hour day month weekday." }
        val ranges = listOf(0..59, 0..23, 1..31, 1..12, 0..7)
        fields = parts.zip(ranges, ::parse)
        anyDay = parts[2] == "*"
        anyWeekday = parts[4] == "*"
    }

    private fun parse(field: String, range: IntRange): Set<Int> = field.split(",").flatMap { part ->
        val pieces = part.split("/")
        require(pieces.size <= 2) { "\"$part\" is not a cron field." }
        val step = pieces.getOrNull(1)?.toIntOrNull() ?: if (pieces.size == 2) 0 else 1
        val base = pieces[0]
        val values = when {
            base == "*" -> range
            "-" in base -> base.split("-").let { (a, b) -> a.toInt()..b.toInt() }
            pieces.size == 2 -> base.toInt()..range.last
            else -> base.toInt()..base.toInt()
        }
        require(step > 0 && values.first in range && values.last in range && !values.isEmpty()) {
            "\"$part\" is out of range ${range.first}-${range.last}."
        }
        (values step step).toList()
    }.map { if (range.last == 7 && it == 7) 0 else it }.toSet()   // Sunday is 0 or 7

    private fun matches(t: ZonedDateTime): Boolean {
        if (t.minute !in fields[0] || t.hour !in fields[1] || t.monthValue !in fields[3]) return false
        val day = t.dayOfMonth in fields[2]
        val weekday = t.dayOfWeek.value % 7 in fields[4]
        return when {
            anyDay && anyWeekday -> true
            anyDay -> weekday
            anyWeekday -> day
            else -> day || weekday
        }
    }

    /** The first time after [from] that the cron fires, read in [zone]. */
    fun next(from: ZonedDateTime, zone: ZoneId): ZonedDateTime? {
        var t = from.withZoneSameInstant(zone).truncatedTo(ChronoUnit.MINUTES).plusMinutes(1)
        // ponytail: a minute-by-minute walk, at most a year of minutes; fast
        // enough for the few crons on screen.
        repeat(366 * 24 * 60) {
            if (matches(t)) return t
            t = t.plusMinutes(1)
        }
        return null
    }

    companion object {
        /** Why [expr] is not a cron, or null when it is one. */
        fun problem(expr: String): String? = try {
            Cron(expr); null
        } catch (e: IllegalArgumentException) {
            e.message ?: "Not a cron."
        }
    }
}
