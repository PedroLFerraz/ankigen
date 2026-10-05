package com.pedrolopes.ankigen

import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract

/**
 * The folder picked in Settings for study guides, reached through the
 * Storage Access Framework: any folder on the phone, or one a cloud app
 * offers (Drive, Dropbox), with no storage permission.
 */
class GuideFolder(private val context: Context, private val tree: Uri) {
    private val treeId = DocumentsContract.getTreeDocumentId(tree)

    /** "primary:Documents/AnkiGen" reads as "Documents/AnkiGen". */
    val label: String = treeId.substringAfter(':').ifBlank { treeId }

    fun has(name: String): Boolean {
        val children = DocumentsContract.buildChildDocumentsUriUsingTree(tree, treeId)
        context.contentResolver.query(
            children, arrayOf(DocumentsContract.Document.COLUMN_DISPLAY_NAME), null, null, null,
        )?.use { rows ->
            while (rows.moveToNext()) if (rows.getString(0) == name) return true
        }
        return false
    }

    fun write(name: String, bytes: ByteArray) {
        val parent = DocumentsContract.buildDocumentUriUsingTree(tree, treeId)
        val doc = DocumentsContract.createDocument(context.contentResolver, parent, "application/pdf", name)
            ?: error("Could not create $name in $label.")
        context.contentResolver.openOutputStream(doc)?.use { it.write(bytes) }
            ?: error("Could not write $name in $label.")
    }

    companion object {
        /** A guide's file name, by pipeline and curriculum day: "Data Platform 2026-10-13.pdf". */
        fun fileName(pipeline: String, day: String) = "$pipeline $day.pdf"
    }
}
