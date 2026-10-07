package com.pedrolopes.ankigen

import android.app.job.JobInfo
import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.app.job.JobService
import android.content.ComponentName
import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import androidx.core.content.edit
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * The folder picked in Settings for study guides, reached through the
 * Storage Access Framework: any folder on the phone, or one a cloud app
 * offers (Drive, Dropbox), with no storage permission.
 */
class GuideFolder(private val context: Context, private val tree: Uri) {
    private val treeId = DocumentsContract.getTreeDocumentId(tree)

    /** "primary:Documents/AnkiGen" reads as "Documents/AnkiGen". */
    val label: String = treeId.substringAfter(':').ifBlank { treeId }

    /** The files in the folder, by name. */
    fun files(): Map<String, Uri> {
        val children = DocumentsContract.buildChildDocumentsUriUsingTree(tree, treeId)
        val found = mutableMapOf<String, Uri>()
        context.contentResolver.query(
            children,
            arrayOf(DocumentsContract.Document.COLUMN_DISPLAY_NAME, DocumentsContract.Document.COLUMN_DOCUMENT_ID),
            null, null, null,
        )?.use { rows ->
            while (rows.moveToNext()) {
                found[rows.getString(0)] = DocumentsContract.buildDocumentUriUsingTree(tree, rows.getString(1))
            }
        }
        return found
    }

    fun write(name: String, bytes: ByteArray): Uri {
        val parent = DocumentsContract.buildDocumentUriUsingTree(tree, treeId)
        val doc = DocumentsContract.createDocument(context.contentResolver, parent, "application/pdf", name)
            ?: error("Could not create $name in $label.")
        context.contentResolver.openOutputStream(doc)?.use { it.write(bytes) }
            ?: error("Could not write $name in $label.")
        return doc
    }

    companion object {
        /** A guide's file name, by pipeline and curriculum day: "Data Platform 2026-10-13.pdf". */
        fun fileName(pipeline: String, day: String) = "$pipeline $day.pdf"
    }
}

/**
 * Keeps the guide folder holding every study guide, from the app and from
 * [GuideJob] in the background. Reads the settings AppModel saves.
 */
object Guides {
    private const val SEEN = "guides_seen"

    fun prefs(context: Context) = context.getSharedPreferences("ankigen", Context.MODE_PRIVATE)

    fun folder(context: Context): GuideFolder? =
        prefs(context).getString("guide_folder", null)?.let { GuideFolder(context, Uri.parse(it)) }

    fun github(context: Context): GitHub = prefs(context).let {
        GitHub(it.getString("repo", null) ?: "PedroLFerraz/ankigen", it.getString("token", null).orEmpty())
    }

    fun statusBranch(context: Context): String =
        prefs(context).getString("status_branch", null) ?: "ankigen-status"

    /**
     * Saves every guide the folder is missing, for every pipeline with runs,
     * and returns the names saved. Each run is looked at once: a guide
     * deleted from the folder stays deleted, though its run can still open it.
     * Synchronized, so the app and the job never write one file twice.
     */
    @Synchronized
    fun sync(context: Context): List<String> {
        val folder = folder(context) ?: return emptyList()
        val prefs = prefs(context)
        // A run's files need the token even on a public repository.
        if (prefs.getString("token", null).isNullOrBlank()) return emptyList()
        val gh = github(context)
        val branch = statusBranch(context)
        val seen = prefs.getStringSet(SEEN, null).orEmpty().toMutableSet()
        val saved = mutableListOf<String>()
        try {
            val present = folder.files().keys
            for (id in gh.list("", branch, dirs = true)) {
                for (file in gh.list("$id/runs", branch, dirs = false)) {
                    val key = "$id/$file"
                    if (key in seen) continue
                    val run = gh.text("$id/runs/$file", branch)?.let { Run(JSONObject(it)) }
                    val day = run?.day
                    if (run != null && run.hasPdf && day != null) {
                        val name = GuideFolder.fileName(run.name ?: id, day)
                        if (name !in present) {
                            // Null when GitHub no longer has the run's files.
                            gh.artifactFile(run.runId!!, run.artifact!!, ".pdf")?.let {
                                folder.write(name, it)
                                saved += name
                            }
                        }
                    }
                    seen += key
                }
            }
        } finally {
            prefs.edit { putStringSet(SEEN, seen) }
        }
        return saved
    }

    /** The run's guide in the folder, downloaded first when it is not there. Null when GitHub no longer has it. */
    @Synchronized
    fun fetch(context: Context, pipeline: String, run: Run): Uri? {
        val folder = folder(context) ?: return null
        val name = GuideFolder.fileName(run.name ?: pipeline, run.day ?: return null)
        folder.files()[name]?.let { return it }
        val pdf = github(context).artifactFile(run.runId ?: return null, run.artifact ?: return null, ".pdf")
        return pdf?.let { folder.write(name, it) }
    }
}

/** Saves new study guides a few times a day, with the app closed. */
class GuideJob : JobService() {
    override fun onStartJob(params: JobParameters): Boolean {
        thread {
            // A failure (no network, a token without access) waits for the next period.
            runCatching { Guides.sync(applicationContext) }
            jobFinished(params, false)
        }
        return true
    }

    override fun onStopJob(params: JobParameters) = true

    companion object {
        private const val ID = 1

        /** Once: scheduling again would restart the period. */
        fun schedule(context: Context) {
            val jobs = context.getSystemService(JobScheduler::class.java)
            if (jobs.getPendingJob(ID) != null) return
            jobs.schedule(
                JobInfo.Builder(ID, ComponentName(context, GuideJob::class.java))
                    .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                    .setPeriodic(TimeUnit.HOURS.toMillis(6))
                    .setPersisted(true)
                    .build(),
            )
        }
    }
}
