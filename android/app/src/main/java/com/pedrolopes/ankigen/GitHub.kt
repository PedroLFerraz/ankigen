package com.pedrolopes.ankigen

import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * The repository is the backend: pipelines/ on the default branch says what
 * runs, the ankigen-status branch says what ran, and Actions runs it. Every
 * call blocks, so callers run it off the main thread.
 *
 * A public repository reads without a token (60 requests an hour); saving a
 * pipeline and "Run now" need a fine-grained token with Contents and Actions
 * set to read and write.
 */
class GitHub(private val repo: String, private val token: String) {

    class Failure(val code: Int, message: String) : Exception(message)

    private fun call(
        method: String,
        path: String,
        body: JSONObject? = null,
        accept: String = "application/vnd.github+json",
    ): String {
        val conn = URL("https://api.github.com/repos/$repo$path").openConnection() as HttpURLConnection
        try {
            conn.requestMethod = method
            conn.connectTimeout = 15_000
            conn.readTimeout = 30_000
            conn.setRequestProperty("Accept", accept)
            conn.setRequestProperty("X-GitHub-Api-Version", "2022-11-28")
            if (token.isNotBlank()) conn.setRequestProperty("Authorization", "Bearer ${token.trim()}")
            if (body != null) {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(body.toString().toByteArray()) }
            }
            val code = conn.responseCode
            val text = (if (code < 400) conn.inputStream else conn.errorStream)
                ?.bufferedReader()?.use { it.readText() }.orEmpty()
            if (code >= 400) {
                val message = runCatching { JSONObject(text).getString("message") }.getOrDefault(text.take(200))
                throw Failure(code, explain(code, message))
            }
            return text
        } finally {
            conn.disconnect()
        }
    }

    private fun explain(code: Int, message: String) = when {
        code == 401 -> "GitHub refused the token: $message"
        code == 403 && "rate limit" in message.lowercase() ->
            "GitHub's hourly limit for reading without a token is used up. Add a token in Settings."
        code == 403 || (code == 404 && token.isBlank()) ->
            "$message. Saving and running need a token with Contents and Actions: read and write."
        else -> "GitHub: $message ($code)"
    }

    private fun ref(branch: String?) = branch?.let { "?ref=$it" }.orEmpty()

    fun defaultBranch(): String = JSONObject(call("GET", "")).getString("default_branch")

    /** A file's text, or null when it is not there. */
    fun text(path: String, branch: String? = null): String? = try {
        call("GET", "/contents/$path${ref(branch)}", accept = "application/vnd.github.raw+json")
    } catch (e: Failure) {
        if (e.code == 404) null else throw e
    }

    /** A file's text and the blob sha that saving it again needs. */
    fun file(path: String, branch: String? = null): Pair<String, String> {
        val json = JSONObject(call("GET", "/contents/$path${ref(branch)}"))
        val text = String(Base64.decode(json.getString("content"), Base64.DEFAULT))
        return text to json.getString("sha")
    }

    /** The names in a folder, folders only or files only; empty when it is not there. */
    fun list(path: String, branch: String? = null, dirs: Boolean): List<String> = try {
        val items = JSONArray(call("GET", "/contents/$path${ref(branch)}"))
        (0 until items.length()).map { items.getJSONObject(it) }
            .filter { (it.getString("type") == "dir") == dirs }
            .map { it.getString("name") }
    } catch (e: Failure) {
        if (e.code == 404) emptyList() else throw e
    }

    fun save(path: String, text: String, sha: String, message: String, branch: String) {
        call("PUT", "/contents/$path", JSONObject()
            .put("message", message)
            .put("content", Base64.encodeToString(text.toByteArray(), Base64.NO_WRAP))
            .put("sha", sha)
            .put("branch", branch))
    }

    fun dispatch(workflow: String, branch: String, inputs: Map<String, String>) {
        call("POST", "/actions/workflows/$workflow/dispatches", JSONObject()
            .put("ref", branch)
            .put("inputs", JSONObject(inputs)))
    }

    /** The workflow's latest runs, newest first. */
    fun runs(workflow: String, count: Int = 10): List<JSONObject> = try {
        val runs = JSONObject(call("GET", "/actions/workflows/$workflow/runs?per_page=$count"))
            .getJSONArray("workflow_runs")
        (0 until runs.length()).map { runs.getJSONObject(it) }
    } catch (e: Failure) {
        if (e.code == 404) emptyList() else throw e     // not on the default branch yet
    }
}
