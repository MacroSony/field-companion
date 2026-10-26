package com.macrosony.fieldprobe

import android.content.ContentValues
import android.content.Context
import android.provider.MediaStore
import java.io.File

/** Copies finished files into the public Download/FieldProbe folder (no storage permission needed). */
object Exporter {
    const val FOLDER = "Download/FieldProbe"

    fun export(ctx: Context, src: File, mime: String): String {
        val resolver = ctx.contentResolver
        val values = ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, src.name)
            put(MediaStore.MediaColumns.MIME_TYPE, mime)
            put(MediaStore.MediaColumns.RELATIVE_PATH, FOLDER)
            put(MediaStore.MediaColumns.IS_PENDING, 1)
        }
        val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: throw IllegalStateException("MediaStore insert failed for ${src.name}")
        resolver.openOutputStream(uri)!!.use { out -> src.inputStream().use { it.copyTo(out, 1 shl 16) } }
        values.clear()
        values.put(MediaStore.MediaColumns.IS_PENDING, 0)
        resolver.update(uri, values, null, null)
        return "$FOLDER/${src.name}"
    }

    fun exportText(ctx: Context, name: String, text: String): String {
        val tmp = File(ctx.cacheDir, name)
        tmp.writeText(text)
        return try {
            export(ctx, tmp, "application/json")
        } finally {
            tmp.delete()
        }
    }
}
