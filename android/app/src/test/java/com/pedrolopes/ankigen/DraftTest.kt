package com.pedrolopes.ankigen

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.time.LocalDateTime

class DraftTest {
    @Test
    fun aRoundReadsBackFromTheCommitEditPlanMakes() {
        // What .github/workflows/edit-plan.yml commits for each round.
        val message = "plan: Move Docker before Git\n\nAsked:\nMove Docker before Git.\nKeep the dates.\n\n" +
            "Claude:\nMoved 06 Docker to start on 2026-10-10.\n\nGit now starts on 2026-10-17."
        val round = Round.parse(message)!!
        assertEquals("Move Docker before Git.\nKeep the dates.", round.asked)
        assertEquals("Moved 06 Docker to start on 2026-10-10.\n\nGit now starts on 2026-10-17.", round.reply)
    }

    @Test
    fun otherCommitsAreNotRounds() {
        assertNull(Round.parse("fix: something else"))
    }

    @Test
    fun aNameBecomesAPipelineId() {
        // The same rule as ID in src/ankigen/pipelines.py.
        assertEquals("ingles-b1", Draft.idFor("Inglês B1"))
        assertEquals("data-platform", Draft.idFor("  Data Platform! "))
        assertEquals("", Draft.idFor("!!"))
        assertEquals(40, Draft.idFor("a".repeat(60)).length)
    }

    @Test
    fun aDraftBranchNamesItsPipeline() {
        val branch = Draft.branchFor("data-platform", LocalDateTime.of(2026, 10, 7, 8, 15, 3))
        assertEquals("plan/data-platform/20261007-081503", branch)
        assertEquals("data-platform", Draft.pipelineOf(branch))
    }
}
