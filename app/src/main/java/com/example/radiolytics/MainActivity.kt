package com.example.radiolytics

import android.Manifest
import android.app.AlertDialog
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.Button
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.viewModels
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.google.firebase.firestore.FirebaseFirestore
import com.google.firebase.firestore.ListenerRegistration
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {
    private val viewModel: MainViewModel by viewModels()
    private lateinit var recordButton: Button
    private lateinit var statusText: TextView
    private lateinit var resultHistoryText: TextView
    private lateinit var resultScrollView: ScrollView
    private var resultListener: ListenerRegistration? = null
    private val resultHistory = StringBuilder()
    private var lastResultId: String? = null

    companion object {
        private const val PERMISSION_REQUEST_CODE = 123
        private val REQUIRED_PERMISSIONS = arrayOf(Manifest.permission.RECORD_AUDIO)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        recordButton = findViewById(R.id.recordButton)
        statusText = findViewById(R.id.statusText)
        resultHistoryText = findViewById(R.id.resultHistoryText)
        resultScrollView = findViewById(R.id.resultScrollView)

        setupUI()
        observeViewModel()
        checkPermissions()
    }

    private fun setupUI() {
        recordButton.setOnClickListener {
            when (viewModel.recordingState.value) {
                is MainViewModel.RecordingState.Idle -> {
                    viewModel.startRecording()
                }
                is MainViewModel.RecordingState.Recording -> {
                    viewModel.stopRecording()
                }
                else -> {
                    // Do nothing if in error state
                }
            }
        }
    }

    private fun observeViewModel() {
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                viewModel.recordingState.observe(this@MainActivity) { state ->
                    when (state) {
                        is MainViewModel.RecordingState.Idle -> {
                            recordButton.text = "Start Recording"
                            statusText.text = "Ready to record"
                        }
                        is MainViewModel.RecordingState.Recording -> {
                            recordButton.text = "Stop Recording"
                            statusText.text = "Recording..."
                        }
                        is MainViewModel.RecordingState.Error -> {
                            recordButton.text = "Start Recording"
                            statusText.text = "Error: ${state.message}"
                            Toast.makeText(this@MainActivity, state.message, Toast.LENGTH_LONG).show()
                        }
                    }
                }
            }
        }

        // Observe match results
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                viewModel.observeResults { result ->
                    appendResultToHistory(result)
                }
            }
        }

        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                viewModel.uploadState.observe(this@MainActivity) { state ->
                    when (state) {
                        is MainViewModel.UploadState.Idle -> {
                            // Initial state, do nothing
                        }
                        is MainViewModel.UploadState.Uploading -> {
                            statusText.text = "Uploading fingerprint..."
                        }
                        is MainViewModel.UploadState.Success -> {
                            statusText.text = "Fingerprint uploaded successfully"
                            Toast.makeText(this@MainActivity, "Upload successful!", Toast.LENGTH_SHORT).show()
                            listenForResult(state.fingerprint)
                        }
                        is MainViewModel.UploadState.Error -> {
                            statusText.text = "Upload failed: ${state.message}"
                            Toast.makeText(this@MainActivity, state.message, Toast.LENGTH_LONG).show()
                        }
                    }
                }
            }
        }
    }

    private fun listenForResult(fingerprint: String) {
        // Remove previous listener if any
        resultListener?.remove()
        // The document ID is the timestamp used in the backend upload
        val lastUploadTimestamp = viewModel.lastUploadTimestamp
        if (lastUploadTimestamp == null) return
        
        // Convert timestamp to string to match backend format
        val docRef = FirebaseFirestore.getInstance()
            .collection("results")
            .document(lastUploadTimestamp.toString())
            
        resultListener = docRef.addSnapshotListener { snapshot, error ->
            if (error != null) {
                appendResultToHistory("Error: ${error.message}")
                return@addSnapshotListener
            }
            if (snapshot != null && snapshot.exists()) {
                val station = snapshot.getString("station") ?: "Unknown"
                val confidence = snapshot.getDouble("confidence") ?: 0.0
                val matchedAt = snapshot.getLong("matched_at") ?: 0L
                val distance = snapshot.getLong("distance") ?: -1L
                val matchTimestamp = snapshot.getLong("match_timestamp") ?: 0L
                
                // Format the time for better readability
                val dateFormat = java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.getDefault())
                val matchedTimeStr = dateFormat.format(java.util.Date(matchedAt))
                val matchTimeStr = dateFormat.format(java.util.Date(matchTimestamp))
                
                val resultText = "Matched: $station (${(confidence * 100).toInt()}%) | Distance: $distance | Matched at: $matchedTimeStr | Stream time: $matchTimeStr"
                
                if (lastResultId != snapshot.id) {
                    showResultDialog(resultText)
                    appendResultToHistory(resultText)
                    lastResultId = snapshot.id
                }
            }
        }
    }

    private fun showResultDialog(result: String) {
        AlertDialog.Builder(this)
            .setTitle("Match Result")
            .setMessage(result)
            .setPositiveButton("OK", null)
            .show()
    }

    private fun appendResultToHistory(result: String) {
        resultHistory.append(result).append("\n")
        resultHistoryText.text = resultHistory.toString()
        resultScrollView.post {
            resultScrollView.fullScroll(ScrollView.FOCUS_DOWN)
        }
    }

    private fun checkPermissions() {
        if (!hasPermissions()) {
            ActivityCompat.requestPermissions(this, REQUIRED_PERMISSIONS, PERMISSION_REQUEST_CODE)
        }
    }

    private fun hasPermissions(): Boolean {
        return REQUIRED_PERMISSIONS.all {
            ContextCompat.checkSelfPermission(this, it) == PackageManager.PERMISSION_GRANTED
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == PERMISSION_REQUEST_CODE) {
            if (grantResults.all { it == PackageManager.PERMISSION_GRANTED }) {
                // Permissions granted, we can proceed
            } else {
                Toast.makeText(this, "Recording permission is required", Toast.LENGTH_LONG).show()
                finish()
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        resultListener?.remove()
    }
}