package com.pedrolopes.ankigen

import android.app.Application
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
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
import java.util.concurrent.ConcurrentHashMap

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val paper = Paper.toArgb()
        enableEdgeToEdge(SystemBarStyle.light(paper, paper), SystemBarStyle.light(paper, paper))
        GuideJob.schedule(this)
        setContent { AnkiGenTheme { AppScreen() } }
    }
}

sealed interface Screen {
    data object Home : Screen
    data object Settings : Screen
    data class Pipeline(val id: String) : Screen
    data class RunFile(val id: String, val file: String) : Screen
    data object NewPipeline : Screen
    data class Draft(val id: String, val branch: String) : Screen
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
    /** Each run's file on the status branch, newest first, and the run when it reads. */
    val runs: List<Pair<String, Run?>>,
    val active: List<JSONObject>,
    /** The plan change being drafted for this pipeline, if any. */
    val draftBranch: String?,
)

const val RUN_WORKFLOW = "run-pipeline.yml"
const val PLAN_WORKFLOW = "edit-plan.yml"

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
    /** Where study guides are saved: a folder picked in Settings. */
    var guideFolder by mutableStateOf(prefs.getString("guide_folder", null)?.let(Uri::parse))
        private set

    val stack = mutableStateListOf<Screen>(Screen.Home)
    val screen get() = stack.last()

    var busy by mutableStateOf(false)
        private set
    var message by mutableStateOf<String?>(null)
    var pipelines by mutableStateOf<List<Pipeline>?>(null)
        private set
    /** Every plan/ branch: changes being drafted, and pipelines not made yet. */
    var drafts by mutableStateOf<List<String>>(emptyList())
        private set
    var draft by mutableStateOf<Draft?>(null)
        private set
    /** When a request last went to Claude: its run takes a moment to show up. */
    var askedAt = 0L
        private set
    var detail by mutableStateOf<Detail?>(null)
        private set
    var runView by mutableStateOf<Pair<Run?, List<Card>>?>(null)
        private set

    private var defaultBranch: String? = null
    private val gh get() = GitHub(repo, token)
    // A run's record never changes once written.
    private val runCache = ConcurrentHashMap<String, Run>()

    init {
        refreshHome()
    }

    fun open(screen: Screen) {
        stack.add(screen)
        when (screen) {
            is Screen.Pipeline -> loadPipeline(screen.id)
            is Screen.RunFile -> loadRunFile(screen.id, screen.file)
            is Screen.Draft -> loadDraft(screen.id, screen.branch)
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

    fun chooseGuideFolder(uri: Uri) {
        // Kept across restarts; without this the grant ends with the process.
        getApplication<Application>().contentResolver.takePersistableUriPermission(
            uri, Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
        )
        guideFolder = uri
        prefs.edit { putString("guide_folder", uri.toString()) }
        task { syncGuides() }
    }

    fun folder(): GuideFolder? = guideFolder?.let { GuideFolder(getApplication(), it) }

    /** Saves the guides the folder is missing; quiet when there are none. */
    private suspend fun syncGuides() {
        val saved = io { Guides.sync(getApplication()) }
        if (saved.isNotEmpty()) {
            message = "Saved ${saved.singleOrNull() ?: "${saved.size} study guides"} to ${folder()?.label}."
        }
    }

    /** Opens a run's study guide, saving it to the folder first when it is not there. */
    fun openGuide(pipeline: String, run: Run) = task {
        val app = getApplication<Application>()
        val folder = folder()
        when {
            folder == null -> message = "Choose a folder for study guides in Settings first."
            token.isBlank() -> message = "Opening a study guide needs the GitHub token in Settings."
            else -> {
                val uri = io { Guides.fetch(app, pipeline, run) }
                if (uri == null) {
                    message = "That run's files are gone: GitHub keeps them for 90 days."
                    return@task
                }
                val view = Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/pdf")
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
                try {
                    app.startActivity(view)
                } catch (e: ActivityNotFoundException) {
                    message = "Saved to ${folder.label}, but no app on this phone opens PDFs."
                }
            }
        }
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
        drafts = io { gh.branches("plan/") }
        syncGuides()
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
                val runs = async {
                    gh.list("$id/runs", statusBranch, dirs = false).sortedDescending()
                        .map { file -> async { file to runOf(id, file) } }
                        .map { it.await() }
                }
                val draftBranch = async { gh.branches("plan/$id/").maxOrNull() }
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
                    runs = runs.await(),
                    active = active.await(),
                    draftBranch = draftBranch.await(),
                )
            }
        }
        syncGuides()
    }

    private fun runOf(id: String, file: String): Run? = runCache["$id/$file"]
        ?: gh.text("$id/runs/$file", statusBranch)?.let { Run(JSONObject(it)) }?.also { runCache["$id/$file"] = it }

    private fun cardsOf(id: String, day: String): List<Card> =
        gh.text("$id/cards/$day.json", statusBranch)
            ?.let { JSONObject(it).objects("cards").map(::Card) }
            .orEmpty()

    fun loadRunFile(id: String, file: String) = task {
        runView = null
        runView = io {
            val run = runOf(id, file)
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

    // ------------------------------------------------------------ plan drafts

    fun loadDraft(id: String, branch: String) = task {
        if (draft?.branch != branch) draft = null
        val base = branch()
        draft = io { draftOf(id, branch, base) }
    }

    private fun draftOf(id: String, branch: String, base: String): Draft {
        val compare = try {
            gh.compare(base, branch)
        } catch (e: GitHub.Failure) {
            if (e.code == 404) null else throw e       // the run has not pushed the branch yet
        }
        val commits = compare?.objects("commits").orEmpty()
        return Draft(
            pipeline = id,
            branch = branch,
            base = base,
            isNew = pipelines?.none { it.id == id } ?: false,
            rounds = commits.mapNotNull { Round.parse(it.getJSONObject("commit").getString("message")) },
            files = compare?.objects("files").orEmpty().map { it.getString("filename") to it.optString("patch") },
            run = gh.runs(PLAN_WORKFLOW, 20).firstOrNull { it.optString("display_title").endsWith(" · $branch") },
            lastCommitAt = commits.lastOrNull()?.getJSONObject("commit")?.getJSONObject("committer")?.optString("date"),
        )
    }

    /**
     * Sends [request] to Claude: a new draft when [branch] is null, another
     * round on it otherwise. [isNew] creates the pipeline instead.
     */
    fun ask(id: String, request: String, branch: String?, isNew: Boolean = false) = task {
        val base = branch()
        val target = branch ?: Draft.branchFor(id)
        io {
            gh.dispatch(PLAN_WORKFLOW, base, mapOf(
                "pipeline" to id, "request" to request.trim(), "branch" to target, "new" to isNew.toString(),
            ))
        }
        askedAt = System.currentTimeMillis()
        val next = Screen.Draft(id, target)
        if (screen == Screen.NewPipeline) stack.removeAt(stack.lastIndex)
        if (screen != next) {
            draft = null
            stack.add(next)
        }
        message = "Asked. Claude takes a minute or two; this page follows along."
        // GitHub takes a moment to list a dispatched run.
        delay(4_000)
        draft = io { draftOf(id, target, base) }
    }

    fun apply(d: Draft) = task {
        val asked = d.rounds.joinToString("\n") { "- " + it.asked.lineSequence().first().take(100) }
        io {
            gh.merge(d.base, d.branch, "plan: ${if (d.isNew) "add" else "change"} ${d.pipeline}, from the app\n\n$asked")
            gh.deleteBranch(d.branch)
        }
        message = if (d.isNew) "Applied: ${d.pipeline} runs on its schedule from now on."
        else "Applied. The curriculum shows the change in a minute or so."
        leaveDraft()
    }

    fun discard(d: Draft) = task {
        io { gh.deleteBranch(d.branch) }
        message = "Discarded."
        leaveDraft()
    }

    private fun leaveDraft() {
        draft = null
        back()
        when (val s = screen) {
            is Screen.Pipeline -> loadPipeline(s.id)
            else -> refreshHome()
        }
    }
}
