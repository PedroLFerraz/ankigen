package com.pedrolopes.ankigen

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class SpecTest {
    // Unit tests run in android/app.
    private val dataPlatform = File("../../pipelines/data-platform/pipeline.yaml").readText()

    @Test
    fun theAppWritesBackWhatItRead() {
        val spec = Spec.parse(dataPlatform)
        assertEquals("Data Platform", spec.name)
        assertEquals("17 5 * * *", spec.cron)
        assertEquals("America/Sao_Paulo", spec.timezone)
        assertTrue(spec.problems().isEmpty())
        assertEquals(spec, Spec.parse(spec.toYaml()))
        // A quote in the name survives too.
        val odd = spec.copy(name = "Data \"Platform\" \\ two", enabled = false, maxCalls = 40)
        assertEquals(odd, Spec.parse(odd.toYaml()))
    }

    @Test
    fun badSettingsAreCaughtBeforeSaving() {
        val spec = Spec.parse(dataPlatform)
        assertEquals(3, spec.copy(cron = "every day", timezone = "Mars/Olympus", maxCalls = -1).problems().size)
    }
}
