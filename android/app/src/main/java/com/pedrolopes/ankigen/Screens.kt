package com.pedrolopes.ankigen

import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Snackbar
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.delay
import java.time.LocalDate
import java.time.ZoneId
import java.time.temporal.ChronoUnit
import java.time.ZonedDateTime

private val Gutter = 16.dp

@Composable
fun AppScreen(model: AppModel = viewModel()) {
    BackHandler(enabled = model.stack.size > 1) { model.back() }
    val snackbar = remember { SnackbarHostState() }
    LaunchedEffect(model.message) {
        model.message?.let {
            snackbar.showSnackbar(it)
            model.message = null
        }
    }

    Column(Modifier.fillMaxSize().safeDrawingPadding()) {
        TopBar(model)
        Rule()
        Column(Modifier.weight(1f)) {
            when (val screen = model.screen) {
                Screen.Home -> HomeScreen(model)
                Screen.Settings -> SettingsScreen(model)
                is Screen.Pipeline -> PipelineScreen(model, screen.id)
                is Screen.RunFile -> RunFileScreen(model, screen.id, screen.file)
                Screen.NewPipeline -> NewPipelineScreen(model)
                is Screen.Draft -> DraftScreen(model, screen.id, screen.branch)
            }
        }
        SnackbarHost(snackbar) { data ->
            Snackbar(containerColor = Ink, contentColor = Paper) { Text(data.visuals.message) }
        }
    }
}

@Composable
private fun TopBar(model: AppModel) {
    Row(
        Modifier.fillMaxWidth().height(48.dp).padding(horizontal = Gutter),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        if (model.stack.size > 1) {
            Text("‹ Back", Modifier.clickable { model.back() }, Cyan, style = MaterialTheme.typography.labelLarge)
        } else {
            Kicker("AnkiGen", color = Ink)
        }
        Spacer(Modifier.weight(1f))
        if (model.busy) Spinner()
        val reload: (() -> Unit)? = when (val s = model.screen) {
            Screen.Home -> model::refreshHome
            is Screen.Pipeline -> ({ model.loadPipeline(s.id) })
            is Screen.RunFile -> ({ model.loadRunFile(s.id, s.file) })
            is Screen.Draft -> ({ model.loadDraft(s.id, s.branch) })
            Screen.Settings, Screen.NewPipeline -> null
        }
        if (reload != null && !model.busy) {
            Text("Reload", Modifier.clickable(onClick = reload), Cyan, style = MaterialTheme.typography.labelLarge)
        }
        if (model.screen == Screen.Home) {
            Text(
                "Settings", Modifier.clickable { model.open(Screen.Settings) }, Cyan,
                style = MaterialTheme.typography.labelLarge,
            )
        }
    }
}

private fun nextRun(spec: Spec): String = runCatching {
    Cron(spec.cron).next(ZonedDateTime.now(), ZoneId.of(spec.timezone))?.let(::local)
}.getOrNull() ?: "never"

private fun LazyListScope.title(text: String, kicker: String? = null) {
    item {
        Column(Modifier.padding(top = 20.dp, bottom = 8.dp)) {
            kicker?.let { Kicker(it) }
            Text(text, style = MaterialTheme.typography.headlineSmall)
        }
    }
}

private fun LazyListScope.note(text: String) {
    item { Text(text, style = MaterialTheme.typography.bodyMedium, color = ink(0.62f)) }
}

// ------------------------------------------------------------ home

@Composable
private fun HomeScreen(model: AppModel) {
    val pipelines = model.pipelines
    LazyColumn(
        contentPadding = PaddingValues(horizontal = Gutter, vertical = 4.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        title("Pipelines", kicker = model.repo)
        when {
            pipelines == null -> if (!model.busy) note("Could not read the repository. Check Settings, then Reload.")
            pipelines.isEmpty() -> note(
                "No pipelines/ folder on ${model.codeBranch.ifBlank { "the default branch" }}. " +
                    "If Settings names a code branch, check it still exists.",
            )
        }
        items(pipelines.orEmpty(), key = { it.id }) { p ->
            PipelineRow(p) { model.open(Screen.Pipeline(p.id)) }
        }
        if (pipelines != null) {
            val made = pipelines.map { it.id }.toSet()
            items(model.drafts.filter { Draft.pipelineOf(it) !in made }, key = { it }) { branch ->
                val id = Draft.pipelineOf(branch)
                Slip(rule = ink(0.3f), modifier = Modifier.clickable { model.open(Screen.Draft(id, branch)) }) {
                    Kicker("$id · draft", color = Cyan)
                    Text("New pipeline, not applied yet ›", style = MaterialTheme.typography.titleMedium)
                }
            }
            item {
                SecondaryButton("New pipeline", { model.open(Screen.NewPipeline) }, Modifier.fillMaxWidth(), Cyan)
            }
        }
    }
}

@Composable
private fun PipelineRow(p: Pipeline, onClick: () -> Unit) {
    val run = p.latest
    Slip(
        rule = when {
            run == null -> Color.Transparent
            run.ok -> Cyan
            else -> Magenta
        },
        modifier = Modifier.clickable(onClick = onClick),
    ) {
        Kicker(
            when {
                p.spec == null -> "${p.id} · unreadable"
                !p.spec.enabled -> "${p.id} · off"
                else -> "${p.id} · next ${nextRun(p.spec)}"
            },
            color = if (p.spec?.enabled == true) Cyan else ink(0.5f),
        )
        Text(p.spec?.name ?: p.id, style = MaterialTheme.typography.titleMedium)
        Text(
            when {
                p.specError != null -> p.specError
                run == null -> "No run recorded yet."
                run.ok -> "Last run ${local(run.finishedAt)} · ${run.kept} cards kept"
                else -> "Last run ${local(run.finishedAt)} failed: ${run.error ?: run.conclusion}"
            },
            style = MaterialTheme.typography.bodyMedium,
            color = if (run != null && !run.ok) Magenta else ink(0.62f),
        )
    }
}

// ------------------------------------------------------------ one pipeline

@Composable
private fun PipelineScreen(model: AppModel, id: String) {
    val d = model.detail?.takeIf { it.id == id }
    var tab by rememberSaveable(id) { mutableIntStateOf(0) }
    var showWritten by rememberSaveable(id) { mutableStateOf(false) }
    val uri = LocalUriHandler.current

    LazyColumn(
        contentPadding = PaddingValues(horizontal = Gutter, vertical = 4.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        title(d?.spec?.name ?: id, kicker = id)
        if (d == null) {
            if (!model.busy) note("Could not load this pipeline. Reload to try again.")
            return@LazyColumn
        }
        item {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                d.spec?.let { spec ->
                    Fact("Schedule", if (spec.enabled) "${spec.cron} · ${spec.timezone}" else "off")
                    if (spec.enabled) Fact("Next run", nextRun(spec))
                    d.curriculum?.let { c -> where(c)?.let { Fact("Curriculum", it, Cyan) } }
                    Fact("Makes", listOfNotNull(
                        "cards",
                        "study guide".takeIf { spec.guidePdf },
                        "pushed to AnkiWeb".takeIf { spec.push },
                    ).joinToString(", "))
                }
                d.specError?.let { Text(it, color = Magenta, style = MaterialTheme.typography.bodyMedium) }
                d.active.forEach { run ->
                    Text(
                        "Running now · started ${local(run.optString("run_started_at"))} ›",
                        Modifier.clickable { uri.openUri(run.getString("html_url")) },
                        Cyan, style = MaterialTheme.typography.bodyMedium,
                    )
                }
                Spacer(Modifier.height(4.dp))
                PrimaryButton(
                    "Run the next day now", { model.runNow(id) },
                    Modifier.fillMaxWidth(), enabled = !model.busy && d.active.isEmpty(),
                )
            }
        }
        item { Tabs(listOf("Latest", "Curriculum", "Runs", "Setup"), tab, { tab = it }) }
        when (tab) {
            0 -> latest(d) { model.openGuide(id, it) }
            1 -> {
                item { PlanBox(model, id, d.draftBranch) }
                curriculum(d.curriculum, d.spec, showWritten) { showWritten = it }
            }
            2 -> runs(d.runs, d.curriculum, { model.open(Screen.RunFile(id, it)) }) { model.openGuide(id, it) }
            else -> item {
                if (d.spec != null) SpecEditor(d.spec) { model.saveSpec(id, it) }
                else Text("pipeline.yaml has to read before it can be edited here.")
            }
        }
    }
}

private fun LazyListScope.latest(d: Detail, onOpenGuide: (Run) -> Unit) {
    val run = d.latest
    if (run == null) {
        note("No run recorded yet. Runs are recorded on the status branch from the first tick after the pipelines change is merged.")
        return
    }
    item { RunSummary(run, d.curriculum) { onOpenGuide(run) } }
    cards(run, d.latestCards)
}

private fun LazyListScope.cards(run: Run, cards: List<Card>) {
    if (cards.isEmpty()) {
        if (run.kept > 0) note("This run's cards were not recorded; they are in its artifact on GitHub.")
        return
    }
    item { Kicker("${cards.size} cards", Modifier.padding(top = 8.dp)) }
    items(cards) { CardSlip(it) }
}

@Composable
private fun RunSummary(run: Run, curriculum: Curriculum?, onOpenGuide: () -> Unit) {
    val uri = LocalUriHandler.current
    Slip(rule = if (run.ok) Cyan else Magenta) {
        Kicker(
            "${run.conclusion} · ${if (run.trigger == "schedule") "scheduled" else "by hand"}" +
                if (run.attempt > 1) " · attempt ${run.attempt}" else "",
            color = if (run.ok) Cyan else Magenta,
        )
        Text(
            when (val n = curriculum?.number(run.day)) {
                null -> run.day?.let { "Curriculum day $it" } ?: "No curriculum day"
                else -> "Day $n · ${run.day}"
            },
            style = MaterialTheme.typography.titleMedium,
        )
        Fact("Finished", local(run.finishedAt))
        Fact("Cards", "${run.kept} kept, ${run.dropped} dropped")
        run.byDeck.forEach { deck ->
            Fact("", "${deck.optString("deck").substringAfterLast("::")}: ${deck.optInt("kept")} of ${deck.optInt("generated")}")
        }
        run.guide?.let { g ->
            val disputed = g.optInt("disputed")
            Fact("Guide", "${g.optInt("chapters")} chapters, ${g.optInt("sections")} sections" +
                (if (disputed > 0) ", $disputed disputed" else "") +
                (if (g.optBoolean("pdf")) " · PDF" else ""))
        }
        Fact("Model calls", run.llmCalls.toString())
        if (run.stages.isNotEmpty()) {
            Fact("Stages", run.stages.joinToString(" · ") { s ->
                s.optString("stage") + if (s.optString("status") == "success") "" else " (${s.optString("status")})"
            })
        }
        run.error?.let { Text(it, color = Magenta, style = MaterialTheme.typography.bodyMedium) }
        if (run.hasPdf) {
            SecondaryButton("Open the study guide PDF", onOpenGuide, Modifier.fillMaxWidth().padding(top = 4.dp))
        }
        run.url?.let { url ->
            Text("Open the run on GitHub ›", Modifier.clickable { uri.openUri(url) }, Cyan,
                style = MaterialTheme.typography.labelLarge)
        }
    }
}

@Composable
private fun CardSlip(card: Card) {
    Slip(rule = if (card.unverified) ink(0.3f) else Cyan.copy(alpha = 0.5f)) {
        Kicker(listOfNotNull(
            card.deck.substringAfterLast("::"),
            card.type,
            "unverified".takeIf { card.unverified },
            card.picture?.let { "picture $it" },
        ).joinToString(" · "), color = Cyan700)
        Text(codeSpans(card.front), style = MaterialTheme.typography.titleSmall)
        Text(codeSpans(card.back), style = MaterialTheme.typography.bodyMedium, color = ink(0.7f))
        card.guideRef?.let { Kicker("Guide $it") }
    }
}

/** "next run writes day 9 of 46 · 8 days ahead of the plan", or null without dates. */
private fun where(c: Curriculum): String? {
    val next = c.nextDay ?: return null
    val n = c.number(next) ?: return null
    val total = c.totalDays
    if (total != null && n > total) return "every day is written"
    val ahead = ChronoUnit.DAYS.between(LocalDate.now(), next)
    return "next run writes day $n of $total" + when {
        ahead == 1L -> " · 1 day ahead of the plan"
        ahead > 1 -> " · $ahead days ahead of the plan"
        ahead == -1L -> " · 1 day behind the plan"
        ahead < -1 -> " · ${-ahead} days behind the plan"
        else -> ""
    }
}

/** When the next [count] runs happen, if the schedule writes one day each. */
private fun runTimes(spec: Spec?, count: Int): List<ZonedDateTime> {
    if (spec == null || !spec.enabled || count <= 0) return emptyList()
    val cron = runCatching { Cron(spec.cron) }.getOrNull() ?: return emptyList()
    val zone = ZoneId.of(spec.timezone)
    val times = mutableListOf<ZonedDateTime>()
    var t = ZonedDateTime.now()
    repeat(count) {
        t = cron.next(t, zone) ?: return times
        times += t
    }
    return times
}

private fun LazyListScope.curriculum(
    c: Curriculum?,
    spec: Spec?,
    showWritten: Boolean,
    onShowWritten: (Boolean) -> Unit,
) {
    if (c == null) {
        note("The curriculum shows after the first recorded run.")
        return
    }
    val next = c.nextDay
    val lastDay = c.decks.mapNotNull { it.lastDay }.maxOrNull()
    val runs = if (lastDay != null) (c.runsUntil(lastDay) ?: 0).toInt() + 1 else 0
    val times = runTimes(spec, runs)
    fun whenWritten(day: LocalDate): String? {
        val k = c.runsUntil(day)?.toInt() ?: return null
        val time = times.getOrNull(k) ?: return if (spec?.enabled == false) "schedule off" else null
        return (if (k == 0) "next run, " else "") + localDay(time)
    }

    item {
        Slip(rule = Cyan) {
            Kicker("Where it is", color = Cyan)
            Text(
                where(c)?.replaceFirstChar(Char::uppercase) ?: "No dated days.",
                style = MaterialTheme.typography.titleSmall,
            )
            if (next != null) {
                whenWritten(next)?.let { Text("Written $it:", style = MaterialTheme.typography.bodyMedium) }
                c.topicsOn(next).forEach {
                    Text("· ${it.topic}", style = MaterialTheme.typography.bodyMedium, color = ink(0.7f))
                }
            }
            Text(
                "Each run writes the next day whenever it runs, so a run by hand pulls the plan forward. " +
                    "Days are numbered; the plan's dates only say where it started.",
                style = MaterialTheme.typography.bodySmall, color = ink(0.55f),
            )
        }
    }
    item {
        Text(
            if (showWritten) "Hide written days" else "Show written days",
            Modifier.clickable { onShowWritten(!showWritten) }.padding(vertical = 4.dp),
            Cyan, style = MaterialTheme.typography.labelLarge,
        )
    }
    c.decks.forEach { deck ->
        val done = deck.topics.count { it.status == "done" }
        item(key = "deck-${deck.deck}") {
            Column(Modifier.padding(top = 14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(deck.deck.substringAfter("::"), style = MaterialTheme.typography.titleMedium)
                Kicker("$done of ${deck.topics.size} topics written · ${deck.quota} cards a day")
                LinearProgressIndicator(
                    progress = { if (deck.topics.isEmpty()) 0f else done.toFloat() / deck.topics.size },
                    modifier = Modifier.fillMaxWidth(), color = Cyan, trackColor = Divider,
                    drawStopIndicator = {},
                )
            }
        }
        deck.topics.groupBy { it.day }.forEach { (day, topics) ->
            val written = topics.all { it.status == "done" }
            if (written && !showWritten) return@forEach
            item(key = "${deck.deck}-$day") {
                val date = day?.let(LocalDate::parse)
                Row(Modifier.fillMaxWidth().padding(top = 4.dp)) {
                    Column(Modifier.width(96.dp).padding(top = 2.dp)) {
                        Kicker(
                            c.number(date)?.let { "Day $it" } ?: "rotating",
                            color = if (topics.any { it.status == "next" }) Cyan else ink(0.5f),
                        )
                        Text(
                            when {
                                date == null -> ""
                                written -> "written"
                                else -> whenWritten(date).orEmpty()
                            },
                            style = MaterialTheme.typography.bodySmall, color = ink(0.5f),
                        )
                    }
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                        topics.forEach { t ->
                            Text(
                                (if (t.status == "done") "✓ " else "") + t.topic,
                                style = MaterialTheme.typography.bodyMedium,
                                color = when (t.status) {
                                    "done" -> ink(0.4f)
                                    "next" -> Cyan
                                    else -> Ink
                                },
                            )
                        }
                    }
                }
            }
        }
    }
}

private fun LazyListScope.runs(
    runs: List<Pair<String, Run?>>,
    curriculum: Curriculum?,
    onOpen: (String) -> Unit,
    onOpenGuide: (Run) -> Unit,
) {
    if (runs.isEmpty()) {
        note("No runs recorded yet.")
        return
    }
    note("Every run, newest first. Guides are kept by GitHub for 90 days and saved to your folder as they come.")
    items(runs, key = { it.first }) { (file, run) ->
        // <curriculum day>-<run id>.json
        val day = file.take(10)
        Row(
            Modifier.fillMaxWidth().clickable { onOpen(file) }.padding(vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(
                    curriculum?.number(day)?.let { "Day $it · $day" } ?: day,
                    style = MaterialTheme.typography.titleSmall,
                    color = if (run != null && !run.ok) Magenta else Ink,
                )
                Text(
                    when {
                        run == null -> "run ${file.removeSuffix(".json").drop(11)}"
                        !run.ok -> "failed: ${run.error ?: run.conclusion}"
                        else -> "${run.kept} cards" + if (run.hasPdf) " · study guide" else ""
                    },
                    style = MaterialTheme.typography.bodySmall, color = ink(0.55f), maxLines = 1,
                )
            }
            if (run != null && run.hasPdf) {
                Text("Open PDF", Modifier.clickable { onOpenGuide(run) }.padding(8.dp), Cyan,
                    style = MaterialTheme.typography.labelLarge)
            }
            Kicker("›")
        }
        Rule()
    }
}

@Composable
private fun SpecEditor(spec: Spec, onSave: (Spec) -> Unit) {
    var name by remember(spec) { mutableStateOf(spec.name) }
    var enabled by remember(spec) { mutableStateOf(spec.enabled) }
    var cron by remember(spec) { mutableStateOf(spec.cron) }
    var zone by remember(spec) { mutableStateOf(spec.timezone) }
    var guide by remember(spec) { mutableStateOf(spec.guidePdf) }
    var push by remember(spec) { mutableStateOf(spec.push) }
    var calls by remember(spec) { mutableStateOf(spec.maxCalls.toString()) }
    val edited = spec.copy(
        name = name, enabled = enabled, cron = cron, timezone = zone.trim(),
        guidePdf = guide, push = push, maxCalls = calls.toIntOrNull() ?: -1,
    )

    Column(Modifier.padding(top = 8.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        OutlinedTextField(name, { name = it }, Modifier.fillMaxWidth(), label = { Text("Name") }, singleLine = true)
        Toggle("Runs on its schedule", enabled) { enabled = it }
        OutlinedTextField(
            cron, { cron = it }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("Cron: minute hour day month weekday") },
            isError = Cron.problem(cron) != null,
            supportingText = {
                Text(Cron.problem(cron) ?: "Next: ${nextRun(edited)}. Avoid minute 0; GitHub drops runs on the hour.")
            },
        )
        OutlinedTextField(
            zone, { zone = it }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("Time zone") },
            isError = zone.trim() !in ZoneId.getAvailableZoneIds(),
            supportingText = { Text("An IANA zone, e.g. America/Sao_Paulo") },
        )
        Toggle("Write the study guide PDF", guide) { guide = it }
        Toggle("Push the cards to AnkiWeb", push) { push = it }
        OutlinedTextField(
            calls, { calls = it.filter(Char::isDigit) }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("Model calls per run, 0 for no cap") },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
        )
        Text(
            "Models stay as pipeline.yaml has them. Saving commits pipeline.yaml to the default branch.",
            style = MaterialTheme.typography.bodySmall, color = ink(0.62f),
        )
        PrimaryButton("Save", { onSave(edited) }, Modifier.fillMaxWidth(),
            enabled = edited != spec && edited.problems().isEmpty())
    }
}

@Composable
private fun Toggle(label: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(label, Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
        Switch(checked, onChange, colors = SwitchDefaults.colors(checkedTrackColor = Cyan))
    }
}

// ------------------------------------------------------------ one past run

@Composable
private fun RunFileScreen(model: AppModel, id: String, file: String) {
    val view = model.runView
    LazyColumn(
        contentPadding = PaddingValues(horizontal = Gutter, vertical = 4.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        title(file.removeSuffix(".json").take(10), kicker = "run")
        val run = view?.first
        if (run == null) {
            if (!model.busy) note("Could not load this run.")
            return@LazyColumn
        }
        val known = model.detail?.takeIf { it.id == id }
        item { RunSummary(run, known?.curriculum) { model.openGuide(id, run) } }
        cards(run, view.second)
    }
}

// ------------------------------------------------------------ settings

@Composable
private fun SettingsScreen(model: AppModel) {
    var repo by remember { mutableStateOf(model.repo) }
    var token by remember { mutableStateOf(model.token) }
    var branch by remember { mutableStateOf(model.statusBranch) }
    var code by remember { mutableStateOf(model.codeBranch) }
    // Scrolls: on a short phone the study guides folder is below the fold.
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = Gutter, vertical = 20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Settings", style = MaterialTheme.typography.headlineSmall)
        OutlinedTextField(repo, { repo = it }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("Repository, owner/name") })
        OutlinedTextField(
            token, { token = it }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("GitHub token") },
            visualTransformation = PasswordVisualTransformation(),
            supportingText = {
                Text("Reading a public repository needs none. Saving, running, changing a plan and study guides need a fine-grained token for this repository with Contents and Actions set to read and write.")
            },
        )
        OutlinedTextField(branch, { branch = it }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("Status branch") })
        OutlinedTextField(code, { code = it }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("Code branch") },
            supportingText = { Text("Where pipelines/ is read and saved. Blank: the default branch.") })
        PrimaryButton("Save", { model.saveSettings(repo, token, branch, code) }, Modifier.fillMaxWidth())

        Rule(Modifier.padding(top = 12.dp))
        val pick = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
            uri?.let(model::chooseGuideFolder)
        }
        Text("Study guides", style = MaterialTheme.typography.titleMedium)
        Text(
            model.folder()?.let {
                "Saved to ${it.label}, in a folder per pipeline: every guide, a few times a day in the background and whenever the app reloads. Open any day's from the Runs tab."
            } ?: "Choose a folder, on the phone or in a cloud app like Drive, and every run's PDF is saved there, in a folder per pipeline, in the background too. Needs the token.",
            style = MaterialTheme.typography.bodyMedium, color = ink(0.62f),
        )
        SecondaryButton(
            if (model.guideFolder == null) "Choose a folder" else "Change the folder",
            { pick.launch(null) }, Modifier.fillMaxWidth(),
        )
    }
}

// ------------------------------------------------------------ changing the plan

@Composable
private fun PlanBox(model: AppModel, id: String, draftBranch: String?) {
    Slip(rule = Cyan) {
        Kicker("Change the plan", color = Cyan)
        if (draftBranch != null) {
            Text(
                "A change is being drafted. Review it, ask Claude for more, then apply or discard it.",
                style = MaterialTheme.typography.bodyMedium,
            )
            SecondaryButton("Open the draft ›", { model.open(Screen.Draft(id, draftBranch)) }, Modifier.fillMaxWidth(), Cyan)
        } else {
            var request by rememberSaveable(id) { mutableStateOf("") }
            OutlinedTextField(
                request, { request = it }, Modifier.fillMaxWidth(), minLines = 2,
                label = { Text("What to change") },
                placeholder = { Text("e.g. Move Docker before Git, and add a deck on dbt after Spark.") },
            )
            Text(
                "Claude edits the plan on a draft. Nothing changes until you apply it.",
                style = MaterialTheme.typography.bodySmall, color = ink(0.55f),
            )
            PrimaryButton(
                "Ask Claude", { model.ask(id, request, null); request = "" }, Modifier.fillMaxWidth(),
                enabled = request.isNotBlank() && !model.busy,
            )
        }
    }
}

@Composable
private fun DraftScreen(model: AppModel, id: String, branch: String) {
    val d = model.draft?.takeIf { it.branch == branch }
    val uri = LocalUriHandler.current
    var request by rememberSaveable(branch) { mutableStateOf("") }
    // Follows Claude's run: a look every 15 seconds while it works, and for
    // a while after asking, before GitHub lists the run.
    LaunchedEffect(branch) {
        while (true) {
            delay(15_000)
            val following = model.draft?.working == true || System.currentTimeMillis() - model.askedAt < 180_000
            if (following && !model.busy) model.loadDraft(id, branch)
        }
    }

    LazyColumn(
        contentPadding = PaddingValues(horizontal = Gutter, vertical = 4.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        title(if (d?.isNew == true) "New pipeline" else "Plan change", kicker = "$id · draft")
        if (d == null) {
            if (!model.busy) note("Could not load this draft. Reload to try again.")
            return@LazyColumn
        }
        items(d.rounds) { r ->
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Slip(rule = ink(0.3f)) {
                    Kicker("You asked")
                    Text(r.asked, style = MaterialTheme.typography.bodyMedium)
                }
                Slip(rule = Cyan) {
                    Kicker("Claude", color = Cyan)
                    Text(r.reply, style = MaterialTheme.typography.bodyMedium)
                }
            }
        }
        item {
            val runUrl = d.run?.optString("html_url")
            when {
                d.working -> Row(
                    Modifier.clickable { runUrl?.let(uri::openUri) },
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Spinner()
                    Text("Claude is working on it ›", color = Cyan, style = MaterialTheme.typography.bodyMedium)
                }
                d.failed -> Text(
                    "The last request failed on GitHub ›", Modifier.clickable { runUrl?.let(uri::openUri) },
                    Magenta, style = MaterialTheme.typography.bodyMedium,
                )
                d.rounds.isEmpty() -> Text(
                    "Waiting for Claude to start…", style = MaterialTheme.typography.bodyMedium, color = ink(0.62f),
                )
            }
        }
        if (d.files.isNotEmpty()) {
            item { Kicker("What changed", Modifier.padding(top = 8.dp)) }
            items(d.files) { (name, patch) -> DiffSlip(name, patch) }
        }
        item {
            Column(Modifier.padding(top = 8.dp, bottom = 16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedTextField(
                    request, { request = it }, Modifier.fillMaxWidth(), minLines = 2,
                    label = { Text("Ask for more changes") },
                )
                PrimaryButton(
                    "Ask Claude", { model.ask(id, request, branch, d.isNew); request = "" }, Modifier.fillMaxWidth(),
                    enabled = request.isNotBlank() && !d.working && !model.busy,
                )
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    PrimaryButton(
                        if (d.isNew) "Apply: create it" else "Apply", { model.apply(d) }, Modifier.weight(1f),
                        enabled = d.files.isNotEmpty() && !d.working && !model.busy,
                    )
                    SecondaryButton("Discard", { model.discard(d) }, Modifier.weight(1f), Magenta)
                }
                Text(
                    "Apply merges the draft into ${d.base}, and the next run goes by it.",
                    style = MaterialTheme.typography.bodySmall, color = ink(0.55f),
                )
            }
        }
    }
}

/** A file's diff: added lines cyan, removed magenta, the rest grey. */
@Composable
private fun DiffSlip(name: String, patch: String) {
    Slip(rule = ink(0.3f)) {
        Kicker(name.removePrefix("pipelines/"))
        if (patch.isBlank()) {
            Text("Too large to show here.", style = MaterialTheme.typography.bodySmall, color = ink(0.55f))
            return@Slip
        }
        Text(
            buildAnnotatedString {
                patch.lines().forEach { line ->
                    val color = when {
                        line.startsWith("@@") -> ink(0.35f)
                        line.startsWith("+") -> Cyan700
                        line.startsWith("-") -> Magenta
                        else -> ink(0.55f)
                    }
                    withStyle(SpanStyle(color = color)) { append(if (line.startsWith("@@")) "⋯" else line) }
                    append('\n')
                }
            },
            style = MaterialTheme.typography.bodySmall.copy(fontFamily = Mono),
        )
    }
}

// ------------------------------------------------------------ a new pipeline

@Composable
private fun NewPipelineScreen(model: AppModel) {
    var name by rememberSaveable { mutableStateOf("") }
    var about by rememberSaveable { mutableStateOf("") }
    var quota by rememberSaveable { mutableStateOf("20") }
    var cron by rememberSaveable { mutableStateOf("17 6 * * *") }
    var zone by rememberSaveable { mutableStateOf(ZoneId.systemDefault().id) }
    val id = Draft.idFor(name)
    val taken = model.pipelines.orEmpty().any { it.id == id } || model.drafts.any { Draft.pipelineOf(it) == id }
    val nameProblem = when {
        name.isBlank() -> null
        id.isEmpty() -> "The name needs a letter or a digit."
        taken -> "There is already a pipeline or a draft called $id."
        else -> null
    }
    val cards = quota.toIntOrNull()
    val ok = name.isNotBlank() && nameProblem == null && about.isNotBlank() && cards != null && cards in 1..100 &&
        Cron.problem(cron) == null && zone.trim() in ZoneId.getAvailableZoneIds()

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = Gutter, vertical = 20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("New pipeline", style = MaterialTheme.typography.headlineSmall)
        Text(
            "Say what to learn. Claude plans the decks and topics from the ground up, on a draft you can " +
                "review and change before anything runs.",
            style = MaterialTheme.typography.bodyMedium, color = ink(0.62f),
        )
        OutlinedTextField(
            name, { name = it }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("Name") }, isError = nameProblem != null,
            supportingText = { Text(nameProblem ?: if (id.isEmpty()) "e.g. Spanish B1" else "Its folder: pipelines/$id") },
        )
        OutlinedTextField(
            about, { about = it }, Modifier.fillMaxWidth(), minLines = 4,
            label = { Text("What to learn") },
            supportingText = { Text("The subject, where you are now, and where you want to get to.") },
        )
        OutlinedTextField(
            quota, { quota = it.filter(Char::isDigit) }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("Cards a day") }, isError = cards == null || cards !in 1..100,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
        )
        OutlinedTextField(
            cron, { cron = it }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("Cron: minute hour day month weekday") }, isError = Cron.problem(cron) != null,
            supportingText = { Text(Cron.problem(cron) ?: "Avoid minute 0; GitHub drops runs on the hour.") },
        )
        OutlinedTextField(
            zone, { zone = it }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("Time zone") }, isError = zone.trim() !in ZoneId.getAvailableZoneIds(),
        )
        PrimaryButton(
            "Ask Claude to plan it",
            {
                model.ask(
                    id,
                    "Name: ${name.trim()}\nSchedule: cron \"${cron.trim()}\", time zone ${zone.trim()}\n" +
                        "Cards a day: $cards\nWhat to learn:\n${about.trim()}",
                    branch = null, isNew = true,
                )
            },
            Modifier.fillMaxWidth(), enabled = ok && !model.busy,
        )
    }
}
