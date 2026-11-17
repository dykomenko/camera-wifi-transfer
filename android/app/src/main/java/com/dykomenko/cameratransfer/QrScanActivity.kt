package com.dykomenko.cameratransfer

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.util.Size
import android.view.MotionEvent
import android.view.View
import android.widget.Button
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.NotFoundException
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Portrait QR scanner for the code on the camera's "Send to Smartphone"
 * screen.  Tapping the preview focuses and meters exposure on that spot,
 * which helps with a bright, glossy camera screen.
 */
class QrScanActivity : ComponentActivity() {
    companion object {
        const val EXTRA_TEXT = "qr_text"
        private const val CAMERA_PERMISSION = 7
    }

    private val analysisExecutor = Executors.newSingleThreadExecutor()
    private val done = AtomicBoolean(false)
    private lateinit var previewView: PreviewView
    private lateinit var focusRing: View
    private var camera: Camera? = null
    private val reader = MultiFormatReader().apply {
        setHints(mapOf(
            DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE),
            DecodeHintType.TRY_HARDER to true,
        ))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_qr_scan)
        previewView = findViewById(R.id.preview)
        focusRing = findViewById(R.id.focus_ring)
        findViewById<TextView>(R.id.prompt).text = getString(R.string.qr_prompt)
        findViewById<Button>(R.id.close).setOnClickListener { finish() }
        previewView.setOnTouchListener { view, event ->
            if (event.action == MotionEvent.ACTION_UP) focusAt(event.x, event.y)
            if (event.action == MotionEvent.ACTION_UP) view.performClick()
            true
        }
        if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) startCamera()
        else requestPermissions(arrayOf(Manifest.permission.CAMERA), CAMERA_PERMISSION)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == CAMERA_PERMISSION && grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            startCamera()
        } else {
            finish()
        }
    }

    override fun onDestroy() {
        analysisExecutor.shutdown()
        super.onDestroy()
    }

    private fun startCamera() {
        val providerFuture = ProcessCameraProvider.getInstance(this)
        providerFuture.addListener({
            val provider = providerFuture.get()
            val preview = Preview.Builder().build().also { it.setSurfaceProvider(previewView.surfaceProvider) }
            // Enough pixels for a small code on the camera's screen.
            val resolution = ResolutionSelector.Builder().setResolutionStrategy(
                ResolutionStrategy(Size(1280, 720), ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER),
            ).build()
            val analysis = ImageAnalysis.Builder()
                .setResolutionSelector(resolution)
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build()
                .also { it.setAnalyzer(analysisExecutor, ::analyze) }
            provider.unbindAll()
            camera = provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis)
        }, mainExecutor)
    }

    private fun focusAt(x: Float, y: Float) {
        val control = camera?.cameraControl ?: return
        val point = previewView.meteringPointFactory.createPoint(x, y)
        val action = FocusMeteringAction.Builder(
            point,
            FocusMeteringAction.FLAG_AF or FocusMeteringAction.FLAG_AE or FocusMeteringAction.FLAG_AWB,
        ).setAutoCancelDuration(5, TimeUnit.SECONDS).build()
        control.startFocusAndMetering(action)
        focusRing.x = x - focusRing.width / 2f
        focusRing.y = y - focusRing.height / 2f
        focusRing.animate().cancel()
        focusRing.alpha = 1f
        focusRing.visibility = View.VISIBLE
        focusRing.animate().alpha(0f).setStartDelay(700).setDuration(400).start()
    }

    private fun analyze(image: ImageProxy) {
        try {
            if (done.get()) return
            // Copy the Y plane row by row: rows may be padded and the last
            // one may stop short of the stride.
            val plane = image.planes[0]
            val buffer = plane.buffer
            val width = image.width
            val height = image.height
            val data = ByteArray(width * height)
            for (row in 0 until height) {
                buffer.position(row * plane.rowStride)
                buffer.get(data, row * width, width)
            }
            val source = PlanarYUVLuminanceSource(data, width, height, 0, 0, width, height, false)
            val text = try {
                reader.decodeWithState(BinaryBitmap(HybridBinarizer(source))).text
            } catch (_: NotFoundException) {
                null
            } finally {
                reader.reset()
            }
            if (text != null && done.compareAndSet(false, true)) {
                runOnUiThread {
                    setResult(RESULT_OK, Intent().putExtra(EXTRA_TEXT, text))
                    finish()
                }
            }
        } finally {
            image.close()
        }
    }
}
