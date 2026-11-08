package io.github.erikiss.steno

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Rect
import android.media.Image
import io.github.erikiss.steno.core.Box
import io.github.erikiss.steno.core.ImageMath
import io.github.erikiss.steno.core.InkBoxes
import io.github.erikiss.steno.core.PreparedImage
import io.github.erikiss.steno.core.Signature
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import kotlin.math.max

/** Pixel work on screen captures (RGBA_8888 images from the ImageReader). */
object Images {
    /** True if [r] lies inside the captured frame. */
    fun fits(image: Image, r: Rect) = r.left >= 0 && r.top >= 0 && r.right <= image.width && r.bottom <= image.height

    /**
     * Change-detection thumbnail of the region, block-averaged straight from the capture buffer (no bitmap per tick).
     * Every pixel is averaged, so a new 1-px pen stroke is never skipped.
     */
    fun signature(image: Image, r: Rect, targetWidth: Int = 160): Signature {
        val plane = image.planes[0]
        val buf = plane.buffer
        val rowStride = plane.rowStride
        val px = plane.pixelStride
        val f = max(1, r.width() / targetWidth)
        val w = max(1, r.width() / f)
        val h = max(1, r.height() / f)
        val out = FloatArray(w * h)
        val norm = 3f * f * f
        for (by in 0 until h) {
            for (bx in 0 until w) {
                var sum = 0
                for (dy in 0 until f) {
                    var off = (r.top + by * f + dy) * rowStride + (r.left + bx * f) * px
                    for (dx in 0 until f) {
                        sum += (buf.get(off).toInt() and 0xFF) + (buf.get(off + 1).toInt() and 0xFF) + (buf.get(off + 2).toInt() and 0xFF)
                        off += px
                    }
                }
                out[by * w + bx] = sum / norm
            }
        }
        return Signature(w, h, out)
    }

    /** The region as a bitmap, copying only its rows out of the capture buffer. */
    fun crop(image: Image, r: Rect): Bitmap {
        val plane = image.planes[0]
        val src = plane.buffer
        val rowStride = plane.rowStride
        val px = plane.pixelStride
        val rowBytes = r.width() * 4
        val dst = ByteBuffer.allocateDirect(rowBytes * r.height())
        for (y in 0 until r.height()) {
            val start = (r.top + y) * rowStride + r.left * px
            if (px == 4) {
                val row = src.duplicate()
                row.limit(start + rowBytes)
                row.position(start)
                dst.put(row)
            } else {                                         // unusual layout: copy pixel by pixel
                for (x in 0 until r.width()) for (c in 0 until 4) dst.put(src.get(start + x * px + c))
            }
        }
        dst.rewind()
        return Bitmap.createBitmap(r.width(), r.height(), Bitmap.Config.ARGB_8888).apply { copyPixelsFromBuffer(dst) }
    }

    /** Scale to the API's image limits (so model pixel boxes map 1:1) and encode as PNG (keeps thin strokes). */
    fun prepare(bitmap: Bitmap, maxEdge: Int, maxTokens: Int): PreparedImage {
        val (w, h) = ImageMath.resizedSize(bitmap.width, bitmap.height, maxEdge, maxTokens)
        val sent = if (w == bitmap.width && h == bitmap.height) bitmap else Bitmap.createScaledBitmap(bitmap, w, h, true)
        val out = ByteArrayOutputStream()
        sent.compress(Bitmap.CompressFormat.PNG, 100, out)
        return PreparedImage(out.toByteArray(), w, h, bitmap.width, bitmap.height)
    }

    /** Ink boxes for the fake mode, found in the PNG the model would have seen. */
    fun fakeBoxes(image: PreparedImage): List<Box> {
        val bmp = BitmapFactory.decodeByteArray(image.png, 0, image.png.size) ?: return emptyList()
        val pixels = IntArray(bmp.width * bmp.height)
        bmp.getPixels(pixels, 0, bmp.width, 0, 0, bmp.width, bmp.height)
        val gray = FloatArray(pixels.size) { i ->
            val c = pixels[i]
            (((c shr 16) and 0xFF) + ((c shr 8) and 0xFF) + (c and 0xFF)) / 3f
        }
        return InkBoxes.find(gray, bmp.width, bmp.height)
    }
}
