package com.pedrolopes.ankigen

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import java.time.ZoneId
import java.time.ZonedDateTime

class CronTest {
    private val utc = ZoneId.of("UTC")
    private fun at(iso: String) = ZonedDateTime.parse(iso)
    private fun assertAt(iso: String, actual: ZonedDateTime?) = assertEquals(at(iso).toInstant(), actual!!.toInstant())

    @Test
    fun dataPlatformRunsAt0517InSaoPaulo() {
        val next = Cron("17 5 * * *").next(at("2026-10-05T07:00:00Z"), ZoneId.of("America/Sao_Paulo"))
        assertEquals(at("2026-10-05T08:17:00Z").toInstant(), next!!.toInstant())
        // Once it has fired, the next one is tomorrow.
        val after = Cron("17 5 * * *").next(at("2026-10-05T08:17:00Z"), ZoneId.of("America/Sao_Paulo"))
        assertEquals(at("2026-10-06T08:17:00Z").toInstant(), after!!.toInstant())
    }

    @Test
    fun weekdaysStepsAndSundayAsSeven() {
        // 2026-10-03 is a Saturday: weekdays-only waits for Monday.
        assertAt("2026-10-05T09:00Z", Cron("0 9-17/4 * * 1-5").next(at("2026-10-03T12:00Z"), utc))
        assertAt("2026-10-04T06:30Z", Cron("30 6 * * 7").next(at("2026-10-03T12:00Z"), utc))
    }

    @Test
    fun dayAndWeekdayEitherMatch() {
        // The 15th, or any Monday: Monday 2026-10-05 comes first.
        assertAt("2026-10-05T00:00Z", Cron("0 0 15 * 1").next(at("2026-10-03T12:00Z"), utc))
    }

    @Test
    fun badCronsSayWhy() {
        assertNotNull(Cron.problem("nope"))
        assertNotNull(Cron.problem("61 5 * * *"))
        assertNotNull(Cron.problem("*/0 * * * *"))
        assertNotNull(Cron.problem("1 2 3 4"))
        assertNull(Cron.problem("*/15 * * * *"))
    }
}
