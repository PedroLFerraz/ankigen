package com.pedrolopes.ankigen

import android.app.Application
import android.content.Context
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.graphics.toArgb
import androidx.core.content.edit
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val paper = Paper.toArgb()
        enableEdgeToEdge(SystemBarStyle.light(paper, paper), SystemBarStyle.light(paper, paper))
        setContent { AnkiGenTheme { AppScreen() } }
    }
}

sealed interface Screen {
    data object Home : Screen
    data object Settings : Screen
    data class Pipeline(val id: String) : Screen
    data class RunFile(val id: String, val file: String) : Screen
}

/** Everything one pipeline's screen shows. */
class Detail(
    val id: String,
    val spec: Spec?,
    val specError: String?,
    val sha: String?,
    val latest: Run?,
    val latestCards: List<Card>,
    val curriculum: Curriculum?,
    val runFiles: List<String>,
    val active: List<JSONObject>,
)

const val RUN_WORKFLOW = "run-pipeline.yml"

class AppModel(app: Application) : AndroidViewModel(app) {
    // ponytail: the token sits in private app storage, unencrypted, with
    // backup off; move it to the Keystore if the app ever holds more.
    private val prefs = app.getSharedPreferences("ankigen", Context.MODE_PRIVATE)

    var repo by mutableStateOf(prefs.getString("repo", null) ?: "PedroLFerraz/ankigen")
        private set
    var token by mutableStateOf(prefs.getString("token", null).orEmpty())
        private set
    var statusBranch by mutableStateOf(prefs.getString("status_branch", null) ?: "ankigen-status")
        private set
    /** Where pipelines/ is read and saved; blank is the repository's default branch. */
    var codeBranch by mutableStateOf(prefs.getString("code_branch", null).orEmpty())
        private set

    val stack = mutableStateListOf<Screen>(Screen.Home)
    val screen get() = stack.last()

    var busy by mutableStateOf(false)
        private set
    var message by mutableStateOf<String?>(null)
    var pipelines by mutableStateOf<List<Pipeline>?>(null)
        private set
    var detail by mutableStateOf<Detail?>(null)
        private set
    var runView by mutableStateOf<Pair<Run?, List<Card>>?>(null)
        private set

    private var defaultBranch: String? = null
    private val gh get() = GitHub(repo, token)

    init {
        refreshHome()
    }

    fun open(screen: Screen) {
        stack.add(screen)
        when (screen) {
            is Screen.Pipeline -> loadPipeline(screen.id)
            is Screen.RunFile -> loadRunFile(screen.id, screen.file)
            else -> Unit
        }
    }

    fun back(): Boolean {
        if (stack.size <= 1) return false
        stack.removeAt(stack.lastIndex)
        return true
    }

    fun saveSettings(repo: String, token: String, statusBranch: String, codeBranch: String) {
        this.repo = repo.trim().removePrefix("https://github.com/").trim('/')
        this.token = token.trim()
        this.statusBranch = statusBranch.trim().ifBlank { "ankigen-status" }
        this.codeBranch = codeBranch.trim()
        prefs.edit {
            putString("repo", this@AppModel.repo)
            putString("token", this@AppModel.token)
            putString("status_branch", this@AppModel.statusBranch)
            putString("code_branch", this@AppModel.codeBranch)
        }
        defaultBranch = null
        back()
        refreshHome()
    }

    /** Runs [work] off the main thread, with the spinner on and any failure shown. */
    private fun task(work: suspend () -> Unit) {
        viewModelScope.launch {
            busy = true
            try {
                work()
            } catch (e: Exception) {
                message = e.message ?: e.toString()
            } finally {
                busy = false
            }
        }
    }

    private suspend fun branch(): String =
        codeBranch.ifBlank { null } ?: defaultBranch ?: io { gh.defaultBranch() }.also { defaultBranch = it }

    private suspend fun <T> io(block: () -> T): T = withContext(Dispatchers.IO) { block() }

    private fun latestOf(id: String): Run? =
        gh.text("$id/latest.json", statusBranch)?.let { Run(JSONObject(it)) }

    private fun specOf(id: String, branch: String): Pair<Spec?, String?> = try {
        Spec.parse(gh.text("pipelines/$id/pipeline.yaml", branch).orEmpty()) to null
    } catch (e: GitHub.Failure) {
        throw e
    } catch (e: Exception) {
        null to "pipeline.yaml does not read: ${e.message}"
    }

    fun refreshHome() = task {
        val branch = branch()
        pipelines = io {
            gh.list("pipelines", branch, dirs = true).map { id ->
                val (spec, error) = specOf(id, branch)
                Pipeline(id, spec, error, latestOf(id))
            }
        }
    }

    fun loadPipeline(id: String) = task {
        if (detail?.id != id) detail = null
        val branch = branch()
        detail = withContext(Dispatchers.IO) {
            run {
                val spec = async {
                    try {
                        val (text, sha) = gh.file("pipelines/$id/pipeline.yaml", branch)
                        Triple(runCatching { Spec.parse(text) }.getOrNull(), sha, text)
                    } catch (e: GitHub.Failure) {
                        if (e.code == 404) Triple(null, null, null) else throw e
                    }
                }
                val latest = async { latestOf(id) }
                val curriculum = async { gh.text("$id/curriculum.json", statusBranch)?.let { Curriculum(JSONObject(it)) } }
                val runFiles = async { gh.list("$id/runs", statusBranch, dirs = false).sortedDescending() }
                val active = async {
                    gh.runs(RUN_WORKFLOW).filter {
                        it.optString("status") != "completed" &&
                            it.optString("display_title").startsWith("$id ·")
                    }
                }
                val run = latest.await()
                val cards = run?.day?.let { cardsOf(id, it) }.orEmpty()
                val (parsed, sha, text) = spec.await()
                Detail(
                    id = id,
                    spec = parsed,
                    specError = if (parsed == null && text != null) "pipeline.yaml does not read." else null,
                    sha = sha,
                    latest = run,
                    latestCards = cards,
                    curriculum = curriculum.await(),
                    runFiles = runFiles.await(),
                    active = active.await(),
                )
            }
        }
    }

    private fun cardsOf(id: String, day: String): List<Card> =
        gh.text("$id/cards/$day.json", statusBranch)
            ?.let { JSONObject(it).objects("cards").map(::Card) }
            .orEmpty()

    fun loadRunFile(id: String, file: String) = task {
        runView = null
        runView = io {
            val run = gh.text("$id/runs/$file", statusBranch)?.let { Run(JSONObject(it)) }
            run to run?.day?.let { cardsOf(id, it) }.orEmpty()
        }
    }

    fun saveSpec(id: String, spec: Spec) = task {
        val problems = spec.problems()
        if (problems.isNotEmpty()) {
            message = problems.joinToString(" ")
            return@task
        }
        val sha = detail?.sha ?: error("Reload the pipeline before saving it.")
        val branch = branch()
        io { gh.save("pipelines/$id/pipeline.yaml", spec.toYaml(), sha, "app: $id's settings", branch) }
        message = "Saved. The next tick goes by the new settings."
        loadPipeline(id)
        refreshHome()
    }

    fun runNow(id: String) = task {
        val branch = branch()
        io { gh.dispatch(RUN_WORKFLOW, branch, mapOf("pipeline" to id)) }
        message = "Started. Its outcome shows here when it finishes."
        // GitHub takes a moment to list a dispatched run.
        delay(4_000)
        loadPipeline(id)
    }
}
