package com.macrosony.fieldprobe

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.pm.PackageManager
import android.os.Bundle
import android.webkit.GeolocationPermissions
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient

/**
 * Hosts the existing web UI as a display client of the same session. Audio stays native;
 * the page is opened with display=1 so it never asks for the microphone.
 */
class WebUiActivity : Activity() {
    companion object { const val EXTRA_URL = "url" }

    private lateinit var web: WebView
    private var pendingRequest: PermissionRequest? = null

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.settings.mediaPlaybackRequiresUserGesture = false
        web.settings.setGeolocationEnabled(true)
        web.webViewClient = WebViewClient()
        web.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest) {
                // Only the camera is granted (Look mode); audio capture belongs to the native service.
                val video = request.resources.filter { it == PermissionRequest.RESOURCE_VIDEO_CAPTURE }
                if (video.isEmpty()) { request.deny(); return }
                if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
                    request.grant(video.toTypedArray())
                } else {
                    pendingRequest = request
                    requestPermissions(arrayOf(Manifest.permission.CAMERA), 2)
                }
            }

            override fun onGeolocationPermissionsShowPrompt(origin: String, callback: GeolocationPermissions.Callback) {
                callback.invoke(origin, true, false)
            }
        }
        // WebView ignores its own padding for page layout, so inset a container instead.
        val frame = android.widget.FrameLayout(this).apply {
            addView(web)
            setOnApplyWindowInsetsListener { v, insets ->
                val bars = insets.getInsets(android.view.WindowInsets.Type.systemBars())
                v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
                insets
            }
        }
        onBackInvokedDispatcher.registerOnBackInvokedCallback(
            android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT
        ) { if (web.canGoBack()) web.goBack() else finish() }
        setContentView(frame)
        val url = intent.getStringExtra(EXTRA_URL) ?: return finish()
        ProbeState.log("web UI -> $url")
        web.loadUrl(url)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        val req = pendingRequest ?: return
        pendingRequest = null
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            req.grant(arrayOf(PermissionRequest.RESOURCE_VIDEO_CAPTURE))
        } else {
            req.deny()
        }
    }


    override fun onDestroy() {
        web.destroy()
        super.onDestroy()
    }
}
