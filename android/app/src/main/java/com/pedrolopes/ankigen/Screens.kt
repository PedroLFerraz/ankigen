package com.pedrolopes.ankigen

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.clickable
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
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import java.time.ZoneId
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
                is Screen.RunFile -> RunFileScreen(model, screen.file)
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
            Screen.Settings -> null
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
            pipelines.isEmpty() -> note("No pipelines/ folder on the default branch yet.")
        }
        items(pipelines.orEmpty(), key = { it.id }) { p ->
            PipelineRow(p) { model.open(Screen.Pipeline(p.id)) }
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
            0 -> latest(d)
            1 -> curriculum(d.curriculum)
            2 -> runs(d.runFiles) { model.open(Screen.RunFile(id, it)) }
            else -> item {
                if (d.spec != null) SpecEditor(d.spec) { model.saveSpec(id, it) }
                else Text("pipeline.yaml has to read before it can be edited here.")
            }
        }
    }
}

private fun LazyListScope.latest(d: Detail) {
    val run = d.latest
    if (run == null) {
        note("No run recorded yet. Runs are recorded on the status branch from the first tick after the pipelines change is merged.")
        return
    }
    item { RunSummary(run) }
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
private fun RunSummary(run: Run) {
    val uri = LocalUriHandler.current
    Slip(rule = if (run.ok) Cyan else Magenta) {
        Kicker(
            "${run.conclusion} · ${if (run.trigger == "schedule") "scheduled" else "by hand"}" +
                if (run.attempt > 1) " · attempt ${run.attempt}" else "",
            color = if (run.ok) Cyan else Magenta,
        )
        Text(run.day?.let { "Curriculum day $it" } ?: "No curriculum day", style = MaterialTheme.typography.titleMedium)
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

private fun LazyListScope.curriculum(c: Curriculum?) {
    if (c == null) {
        note("The curriculum shows after the first recorded run.")
        return
    }
    c.nextDay?.let { item { Fact("Next day", it, Cyan) } }
    c.decks.forEach { deck ->
        item(key = "deck-${deck.deck}") {
            val done = deck.topics.count { it.status == "done" }
            Column(Modifier.padding(top = 14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(deck.deck.substringAfter("::"), style = MaterialTheme.typography.titleMedium)
                Kicker("$done of ${deck.topics.size} topics · ${deck.quota} cards a day")
                LinearProgressIndicator(
                    progress = { if (deck.topics.isEmpty()) 0f else done.toFloat() / deck.topics.size },
                    modifier = Modifier.fillMaxWidth(), color = Cyan, trackColor = Divider,
                    drawStopIndicator = {},
                )
            }
        }
        deck.topics.groupBy { it.day }.forEach { (day, topics) ->
            item(key = "${deck.deck}-$day") {
                Row(Modifier.fillMaxWidth().padding(top = 4.dp)) {
                    Kicker(day ?: "rotating", Modifier.width(84.dp).padding(top = 3.dp))
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

private fun LazyListScope.runs(files: List<String>, onOpen: (String) -> Unit) {
    if (files.isEmpty()) {
        note("No runs recorded yet.")
        return
    }
    items(files) { file ->
        // <curriculum day>-<run id>.json
        val name = file.removeSuffix(".json")
        Row(
            Modifier.fillMaxWidth().clickable { onOpen(file) }.padding(vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(name.take(10), style = MaterialTheme.typography.titleSmall)
            Spacer(Modifier.weight(1f))
            Kicker("run ${name.drop(11)} ›")
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
private fun RunFileScreen(model: AppModel, file: String) {
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
        item { RunSummary(run) }
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
    Column(
        Modifier.fillMaxSize().padding(horizontal = Gutter, vertical = 20.dp),
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
                Text("Reading a public repository needs none. Saving a pipeline and running one need a fine-grained token for this repository with Contents and Actions set to read and write.")
            },
        )
        OutlinedTextField(branch, { branch = it }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("Status branch") })
        OutlinedTextField(code, { code = it }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("Code branch") },
            supportingText = { Text("Where pipelines/ is read and saved. Blank: the default branch.") })
        PrimaryButton("Save", { model.saveSettings(repo, token, branch, code) }, Modifier.fillMaxWidth())
    }
}
